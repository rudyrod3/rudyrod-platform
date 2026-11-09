variable "region" {
  description = "The one AWS region everything lives in."
  type        = string
  default     = "us-west-2"
}

variable "github_repo" {
  description = "owner/name of the platform repo (used for tags)."
  type        = string
  default     = "rudyrod3/rudyrod-platform"
}

variable "github_oidc_sub_prefix" {
  description = <<-EOT
    Prefix of the `sub` claim in this repo's GitHub OIDC tokens. The repo uses immutable
    subjects (owner and repo IDs, not just names), so a deleted-and-recreated repo with the
    same name can't assume these roles. Read it with:
      gh api repos/rudyrod3/rudyrod-platform/actions/oidc/customization/sub --jq .sub_claim_prefix
  EOT
  type        = string
  default     = "repo:rudyrod3@169416057/rudyrod-platform@1436720550"
}

variable "state_bucket" {
  description = "S3 bucket holding this Terraform state (same value as -backend-config bucket=)."
  type        = string
}

variable "budget_email" {
  description = "Where AWS Budgets sends alerts. Set via TF_VAR_budget_email; kept out of the public repo."
  type        = string
  sensitive   = true
}

variable "monthly_budget_usd" {
  description = "Monthly cost budget; alerts at 50% and 100%."
  type        = string
  default     = "1"
}

variable "table_name" {
  type    = string
  default = "rudyrod-showcase"
}

variable "table_read_capacity" {
  description = "Provisioned RCU. Free tier covers 25 across all tables; the Lambda caches reads."
  type        = number
  default     = 1
}

variable "table_write_capacity" {
  description = "Provisioned WCU. Free tier covers 25 across all tables."
  type        = number
  default     = 1
}

variable "lambda_reserved_concurrency" {
  description = <<-EOT
    Hard cap on concurrent Lambda executions, so a flood is throttled instead of billed.
    New accounts with a total limit of 10 can't reserve any (AWS keeps 10 unreserved);
    use -1 there until a quota increase lands. The account limit is then the cap.
  EOT
  type        = number
  default     = 2
}

variable "allowed_origins" {
  description = "Exact browser origins allowed by CORS."
  type        = list(string)
  default     = ["https://rudyrod.me", "https://www.rudyrod.me", "https://staging.rudyrod.me"]
}

variable "preview_origin_regex" {
  description = "Regex for Vercel preview origins (full match). Tighten to your team slug suffix."
  type        = string
  default     = "^https://rudyrod-me-[a-z0-9-]+\\.vercel\\.app$"
}
