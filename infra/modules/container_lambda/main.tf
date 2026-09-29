# A Lambda running one entrypoint of the agent image. The agent, approval
# and MCP functions are the same image with different commands, so the
# repeated parts (role, log group, image config, optional public URL) live
# here once.

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

variable "name" {
  type = string
}

variable "image_uri" {
  type = string
}

variable "handler" {
  description = "module.function, passed to awslambdaric."
  type        = string
}

variable "timeout" {
  type = number
}

variable "memory_size" {
  type = number
}

variable "environment" {
  type    = map(string)
  default = {}
}

variable "policy_json" {
  description = "Everything the function may do beyond writing its own logs."
  type        = string
}

variable "function_url" {
  description = "Expose a public Function URL (auth type NONE; the code authenticates requests itself)."
  type        = bool
  default     = false
}

variable "log_retention_days" {
  type = number
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name               = var.name
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

# Instead of AWSLambdaBasicExecutionRole (which allows creating log groups
# anywhere): write to this function's own, pre-created log group only.
data "aws_iam_policy_document" "logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.this.arn}:*"]
  }
}

resource "aws_iam_role_policy" "logs" {
  name   = "logs"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.logs.json
}

resource "aws_iam_role_policy" "app" {
  name   = "app"
  role   = aws_iam_role.this.id
  policy = var.policy_json
}

resource "aws_lambda_function" "this" {
  function_name = var.name
  role          = aws_iam_role.this.arn
  package_type  = "Image"
  image_uri     = var.image_uri
  architectures = ["x86_64"]
  timeout       = var.timeout
  memory_size   = var.memory_size

  # The image has no ENTRYPOINT so `docker compose run agent python ...`
  # works locally; Lambda supplies the runtime client here.
  image_config {
    entry_point = ["python", "-m", "awslambdaric"]
    command     = [var.handler]
  }

  environment {
    variables = var.environment
  }

  depends_on = [aws_cloudwatch_log_group.this, aws_iam_role_policy.logs]
}

resource "aws_lambda_function_url" "this" {
  count              = var.function_url ? 1 : 0
  function_name      = aws_lambda_function.this.function_name
  authorization_type = "NONE"
}

# A public URL needs a resource policy with BOTH permissions: current Lambda
# rules require lambda:InvokeFunction alongside lambda:InvokeFunctionUrl for
# public URLs. The second is conditioned on the call arriving through the
# URL, so it doesn't make the function directly invokable by anyone via the
# Invoke API.
resource "aws_lambda_permission" "public_url" {
  count                  = var.function_url ? 1 : 0
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.this.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "public_url_invoke" {
  count                    = var.function_url ? 1 : 0
  statement_id             = "AllowPublicInvokeViaFunctionUrl"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.this.function_name
  principal                = "*"
  invoked_via_function_url = true
}

output "function_name" {
  value = aws_lambda_function.this.function_name
}

output "function_arn" {
  value = aws_lambda_function.this.arn
}

output "url" {
  value = var.function_url ? aws_lambda_function_url.this[0].function_url : null
}
