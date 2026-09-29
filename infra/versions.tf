terraform {
  # 1.10: S3 backend native locking (use_lockfile), no DynamoDB lock table.
  # 1.11: write-only attributes, used to keep generated secrets out of state.
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source = "hashicorp/random"
      # 3.7 adds the ephemeral random_password used in state.tf.
      version = "~> 3.7"
    }
  }

  # Partial configuration: bucket/key/region come from backend.hcl
  # (see backend.hcl.example), created once by infra/bootstrap.
  backend "s3" {
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Repo      = "self-healing-ops-mock-service"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
