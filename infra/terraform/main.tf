data "aws_caller_identity" "current" {}

locals {
  account_id    = data.aws_caller_identity.current.account_id
  function_name = "showcase-status-api"
  envs          = { staging = "staging", prod = "main" } # environment => branch
  oidc_host     = "token.actions.githubusercontent.com"
}

# ---- Data -------------------------------------------------------------------

# Provisioned, not on-demand: on-demand gets no always-free offset.
resource "aws_dynamodb_table" "showcase" {
  name           = var.table_name
  billing_mode   = "PROVISIONED"
  read_capacity  = var.table_read_capacity
  write_capacity = var.table_write_capacity
  hash_key       = "pk"
  range_key      = "sk"

  deletion_protection_enabled = true

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

# ---- Status API Lambda ------------------------------------------------------

data "archive_file" "status_api" {
  type        = "zip"
  output_path = "${path.module}/.build/status_api.zip"

  source {
    content  = file("${path.module}/../../status_api/handler.py")
    filename = "handler.py"
  }
}

resource "aws_cloudwatch_log_group" "status_api" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 7
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "status_api" {
  name               = "showcase-status-api"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "status_api" {
  statement {
    sid       = "Table"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.showcase.arn]
  }

  statement {
    sid       = "HmacKeys"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter/showcase/*"]
  }

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.status_api.arn}:*"]
  }
}

resource "aws_iam_role_policy" "status_api" {
  name   = "status-api"
  role   = aws_iam_role.status_api.id
  policy = data.aws_iam_policy_document.status_api.json
}

resource "aws_lambda_function" "status_api" {
  function_name    = local.function_name
  role             = aws_iam_role.status_api.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.status_api.output_path
  source_code_hash = data.archive_file.status_api.output_base64sha256
  memory_size      = 128
  timeout          = 5

  reserved_concurrent_executions = var.lambda_reserved_concurrency

  environment {
    variables = {
      TABLE_NAME           = aws_dynamodb_table.showcase.name
      SSM_PREFIX           = "/showcase"
      ALLOWED_ORIGINS      = join(",", var.allowed_origins)
      PREVIEW_ORIGIN_REGEX = var.preview_origin_regex
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.status_api.name
  }

  depends_on = [aws_iam_role_policy.status_api]
}

# Public URL, no API Gateway. The routes do their own auth; CORS is handled in the
# function so it can match Vercel preview origins by pattern.
resource "aws_lambda_function_url" "status_api" {
  function_name      = aws_lambda_function.status_api.function_name
  authorization_type = "NONE"
}

# Public Function URLs need both statements: InvokeFunctionUrl, and InvokeFunction
# limited to calls that arrive through the URL.
resource "aws_lambda_permission" "url_public" {
  statement_id           = "FunctionUrlPublic"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.status_api.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "url_invoke" {
  statement_id             = "FunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.status_api.function_name
  principal                = "*"
  invoked_via_function_url = true
}

# ---- GitHub Actions OIDC ----------------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://${local.oidc_host}"
  client_id_list = ["sts.amazonaws.com"]
}

# Trust policy for one exact GitHub OIDC subject, e.g. repo:o/r:ref:refs/heads/main.
data "aws_iam_policy_document" "github_trust" {
  # Jobs that use a GitHub environment get an environment subject instead of a ref;
  # the `aws` environment only allows the main branch (set in repo settings).
  for_each = merge(
    { for env, branch in local.envs : "build_${env}" => "repo:${var.github_repo}:ref:refs/heads/${branch}" },
    {
      terraform_apply = "repo:${var.github_repo}:environment:aws"
      terraform_plan  = "repo:${var.github_repo}:pull_request"
    },
  )

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = [each.value]
    }
  }
}

# Build writers: one per environment, each able to PutItem into its own BUILD#<env>
# partition only. (IAM can restrict the partition key, not the sort key, which is why
# BUILD records have their own partition.)
resource "aws_iam_role" "gha_build_writer" {
  for_each             = local.envs
  name                 = "showcase-gha-build-writer-${each.key}"
  assume_role_policy   = data.aws_iam_policy_document.github_trust["build_${each.key}"].json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "gha_build_writer" {
  for_each = local.envs

  statement {
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.showcase.arn]
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "dynamodb:LeadingKeys"
      values   = ["BUILD#${each.key}"]
    }
  }
}

resource "aws_iam_role_policy" "gha_build_writer" {
  for_each = local.envs
  name     = "put-build-record"
  role     = aws_iam_role.gha_build_writer[each.key].id
  policy   = data.aws_iam_policy_document.gha_build_writer[each.key].json
}

# Terraform plan on pull requests: read-only, and explicitly unable to read the HMAC keys.
resource "aws_iam_role" "gha_terraform_plan" {
  name               = "showcase-gha-terraform-plan"
  assume_role_policy = data.aws_iam_policy_document.github_trust["terraform_plan"].json
}

resource "aws_iam_role_policy_attachment" "gha_terraform_plan_readonly" {
  role       = aws_iam_role.gha_terraform_plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "gha_terraform_plan_deny" {
  statement {
    effect    = "Deny"
    actions   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "kms:Decrypt"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "gha_terraform_plan_deny" {
  name   = "deny-secrets"
  role   = aws_iam_role.gha_terraform_plan.id
  policy = data.aws_iam_policy_document.gha_terraform_plan_deny.json
}

# Terraform apply from main, behind manual approval on the `aws` environment. This is the
# one broad role: it manages IAM for this project, so treat its approval as an admin action.
resource "aws_iam_role" "gha_terraform" {
  name               = "showcase-gha-terraform"
  assume_role_policy = data.aws_iam_policy_document.github_trust["terraform_apply"].json
}

data "aws_iam_policy_document" "gha_terraform" {
  statement {
    sid       = "State"
    actions   = ["s3:ListBucket", "s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${var.state_bucket}", "arn:aws:s3:::${var.state_bucket}/rudyrod-platform/*"]
  }

  statement {
    sid     = "ProjectResources"
    actions = ["dynamodb:*", "lambda:*", "logs:*", "budgets:*"]
    resources = [
      aws_dynamodb_table.showcase.arn,
      aws_lambda_function.status_api.arn,
      aws_cloudwatch_log_group.status_api.arn,
      "${aws_cloudwatch_log_group.status_api.arn}:*",
      "arn:aws:budgets::${local.account_id}:budget/showcase-*",
    ]
  }

  statement {
    sid     = "ProjectIam"
    actions = ["iam:*"]
    resources = [
      "arn:aws:iam::${local.account_id}:role/showcase-*",
      aws_iam_openid_connect_provider.github.arn,
    ]
  }

  statement {
    sid       = "Describe"
    actions   = ["logs:DescribeLogGroups", "iam:GetPolicy", "iam:GetPolicyVersion", "lambda:GetAccountSettings"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "gha_terraform" {
  name   = "manage-showcase"
  role   = aws_iam_role.gha_terraform.id
  policy = data.aws_iam_policy_document.gha_terraform.json
}

# ---- Cost guardrail ---------------------------------------------------------

resource "aws_budgets_budget" "monthly" {
  name         = "showcase-monthly"
  budget_type  = "COST"
  limit_amount = var.monthly_budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = [
      { threshold = 50, type = "ACTUAL" },
      { threshold = 100, type = "ACTUAL" },
      { threshold = 100, type = "FORECASTED" },
    ]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value.threshold
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value.type
      subscriber_email_addresses = [var.budget_email]
    }
  }
}
