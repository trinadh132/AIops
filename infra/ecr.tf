locals {
  repositories = toset(["mock-service", "agent"])
}

resource "aws_ecr_repository" "this" {
  for_each = local.repositories
  name     = "${var.project}/${each.key}"

  # Immutable tags: CI pushes the git SHA, so "what's deployed" is always an
  # exact commit, and a tag can't be silently repointed.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  # Portfolio environment: `terraform destroy` should work without first
  # emptying repositories by hand.
  force_delete = true
}

resource "aws_ecr_lifecycle_policy" "keep_recent" {
  for_each   = aws_ecr_repository.this
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 10 most recent images (storage is billed per GB)"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

# Lambda pulls container images with its service principal. Scoped to this
# project's functions only.
data "aws_iam_policy_document" "lambda_pull" {
  statement {
    sid     = "LambdaPull"
    actions = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "aws:sourceArn"
      values   = ["arn:aws:lambda:${var.region}:${data.aws_caller_identity.current.account_id}:function:${var.project}-*"]
    }
  }
}

resource "aws_ecr_repository_policy" "agent_lambda_pull" {
  repository = aws_ecr_repository.this["agent"].name
  policy     = data.aws_iam_policy_document.lambda_pull.json
}

locals {
  mock_image  = "${aws_ecr_repository.this["mock-service"].repository_url}:${var.image_tag}"
  agent_image = "${aws_ecr_repository.this["agent"].repository_url}:${var.image_tag}"
}
