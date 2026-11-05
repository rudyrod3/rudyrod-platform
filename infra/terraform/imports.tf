# Adopt resources that already exist in the account instead of failing on "already exists".
# Uncomment the ones that apply before the first `terraform plan`, then delete them after
# the first apply (once a resource is in state, its import block does nothing).

# Created by hand during the guardrails step? Key schema must be pk (S) + sk (S).
# import {
#   to = aws_dynamodb_table.showcase
#   id = "rudyrod-showcase"
# }

# Only one GitHub OIDC provider can exist per account.
# import {
#   to = aws_iam_openid_connect_provider.github
#   id = "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"
# }

# Existing $1 budget: import it (rename it to showcase-monthly first, or change `name` in
# main.tf to match), or delete the old one to avoid duplicate alerts.
# import {
#   to = aws_budgets_budget.monthly
#   id = "<ACCOUNT_ID>:<BUDGET_NAME>"
# }
