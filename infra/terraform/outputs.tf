output "function_url" {
  description = "Goes into the site panel and k8s/overlays/live (STATUS_API_URL)."
  value       = aws_lambda_function_url.status_api.function_url
}

output "table_name" {
  value = aws_dynamodb_table.showcase.name
}

output "region" {
  value = var.region
}

output "build_writer_role_arns" {
  description = "For platform-ci.yml: the role each branch's build job assumes."
  value       = { for env, role in aws_iam_role.gha_build_writer : env => role.arn }
}

output "terraform_apply_role_arn" {
  value = aws_iam_role.gha_terraform.arn
}

output "terraform_plan_role_arn" {
  value = aws_iam_role.gha_terraform_plan.arn
}
