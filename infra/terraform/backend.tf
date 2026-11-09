terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.71"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.8"
    }
  }

  # Partial config: the bucket is account-specific, so it is passed at init time:
  #   terraform init -backend-config="bucket=$TF_STATE_BUCKET"
  # S3-native locking (use_lockfile) means no DynamoDB lock table.
  backend "s3" {
    key          = "rudyrod-platform/terraform.tfstate"
    region       = "us-west-2"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "rudyrod-showcase"
      ManagedBy = "terraform"
      Repo      = var.github_repo
    }
  }
}
