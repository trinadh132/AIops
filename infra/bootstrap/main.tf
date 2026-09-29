# One-time: the S3 bucket that holds the main stack's Terraform state.
# Uses local state itself (a state bucket can't store its own creation).
#
#   cd infra/bootstrap && terraform init && terraform apply
#
# Locking needs no DynamoDB table: the main stack uses S3 native lockfiles
# (use_lockfile = true, Terraform >= 1.10).

terraform {
  required_version = ">= 1.11"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "ops-agent"
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "state" {
  # Account ID keeps the globally unique name predictable per account.
  bucket = "${var.project}-tfstate-${data.aws_caller_identity.current.account_id}"

  lifecycle {
    prevent_destroy = true # losing state orphans every resource it tracks
  }
}

# Versioning = undo for a corrupted or accidentally overwritten state file.
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-S3, not a customer-managed KMS key: single account, access already
# limited by IAM, and a CMK adds $1/month plus key-policy upkeep.
#trivy:ignore:AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Old state versions pile up with every apply; keep 30 days of history.
resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

output "state_bucket" {
  value = aws_s3_bucket.state.bucket
}
