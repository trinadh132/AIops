# One-time: the S3 bucket that holds the main stack's Terraform state, and
# the IAM roles GitHub Actions assumes via OIDC.
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

# ---- GitHub Actions OIDC --------------------------------------------------------
#
# CI assumes these roles with short-lived OIDC tokens; no AWS keys are stored
# in GitHub. They live here, not in the main stack, so CI can never modify
# its own permissions by applying the stack it deploys.

variable "github_repository" {
  description = "owner/repo allowed to assume the CI roles."
  type        = string
  default     = "trinadh132/AIops"
}

variable "create_oidc_provider" {
  description = "An account holds one provider per URL; set false if GitHub's already exists."
  type        = bool
  default     = true
}

locals {
  oidc_host = "token.actions.githubusercontent.com"
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_oidc_provider ? 1 : 0
  url            = "https://${local.oidc_host}"
  client_id_list = ["sts.amazonaws.com"]
  # No thumbprint_list: AWS validates GitHub's certificate against its own
  # trusted CAs for this provider.
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 0 : 1
  url   = "https://${local.oidc_host}"
}

locals {
  oidc_provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.github[0].arn
}

data "aws_iam_policy_document" "github_trust" {
  for_each = {
    # Only jobs that declare `environment: production`. The environment's
    # deployment-branch rule (set in GitHub) limits those to main.
    deploy = "repo:${var.github_repository}:environment:production"
    # Pull requests from branches of this repo (fork PRs get no OIDC token).
    plan = "repo:${var.github_repository}:pull_request"
  }

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
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

# Deploy: applies the whole stack (VPC, IAM, Lambda, ECS...), so it gets
# broad rights. The control is WHO can assume it: only production-
# environment jobs of this repository. Next hardening step: a permissions
# boundary on the roles Terraform creates.
resource "aws_iam_role" "github_deploy" {
  name                 = "${var.project}-github-deploy"
  assume_role_policy   = data.aws_iam_policy_document.github_trust["deploy"].json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "github_deploy" {
  role       = aws_iam_role.github_deploy.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# Plan (pull requests): anyone who can push a branch can run code with this
# role, so it gets almost nothing. PR plans run with -refresh=false and
# -lock=false, which needs only the state file and one data source lookup.
# Safe to hand out because the state contains no secrets (write-only
# attributes in the main stack).
resource "aws_iam_role" "github_plan" {
  name                 = "${var.project}-github-plan"
  assume_role_policy   = data.aws_iam_policy_document.github_trust["plan"].json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "github_plan" {
  statement {
    sid       = "ReadState"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.state.arn}/${var.project}/*"]
  }
  statement {
    sid       = "ListState"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
  }
  statement {
    sid       = "DataSources"
    actions   = ["ec2:DescribeAvailabilityZones"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "github_plan" {
  role   = aws_iam_role.github_plan.id
  policy = data.aws_iam_policy_document.github_plan.json
}

output "github_deploy_role_arn" {
  value = aws_iam_role.github_deploy.arn
}

output "github_plan_role_arn" {
  value = aws_iam_role.github_plan.arn
}
