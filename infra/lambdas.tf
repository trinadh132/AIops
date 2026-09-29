locals {
  ssm_prefix     = "/${var.project}"
  ssm_path_arn   = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.ssm_prefix}"
  mock_log_group = aws_cloudwatch_log_group.mock_service.name
}

# Every function loads its secrets with GetParametersByPath at cold start
# (tools/config.py). Trade-off: SSM authorizes that call on the path, not
# per parameter, so each function can read every secret under /<project>/.
# Per-function sub-paths would tighten this at the cost of duplicating the
# shared HMAC key.
data "aws_iam_policy_document" "read_secrets" {
  statement {
    actions   = ["ssm:GetParametersByPath"]
    resources = [local.ssm_path_arn]
  }

  # SecureStrings use the AWS-managed aws/ssm key. Scoped via kms:ViaService
  # rather than by key ARN, because that alias only exists after the first
  # SecureString is created, so looking it up would break a fresh account.
  statement {
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }
}

# ---- agent: alarm -> diagnosis -> route remediation -------------------------

data "aws_iam_policy_document" "agent" {
  source_policy_documents = [data.aws_iam_policy_document.read_secrets.json]

  statement {
    sid       = "RunRecords"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.runs.arn]
  }
  statement {
    sid       = "AlarmLogs"
    actions   = ["logs:FilterLogEvents"]
    resources = ["${aws_cloudwatch_log_group.mock_service.arn}:*"]
  }
  statement {
    sid       = "QueueRemediation"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.remediation.arn]
  }
  statement {
    sid       = "RequestApproval"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.approvals.arn]
  }
  statement {
    sid       = "EncryptedApprovalTopic"
    actions   = ["kms:GenerateDataKey*", "kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["sns.${var.region}.amazonaws.com"]
    }
  }
}

module "agent_lambda" {
  source             = "./modules/container_lambda"
  name               = "${var.project}-agent"
  image_uri          = local.agent_image
  handler            = "lambda_handler.handler"
  timeout            = 300  # retrieval + LLM with a summarize-and-retry loop and model fallbacks
  memory_size        = 1024 # also buys CPU; langgraph import dominates cold start
  policy_json        = data.aws_iam_policy_document.agent.json
  log_retention_days = var.log_retention_days

  environment = {
    SSM_PARAMETER_PREFIX  = local.ssm_prefix
    RUNS_TABLE_NAME       = aws_dynamodb_table.runs.name
    LOG_GROUP_NAME        = local.mock_log_group
    REMEDIATION_QUEUE_URL = aws_sqs_queue.remediation.url
    APPROVAL_TOPIC_ARN    = aws_sns_topic.approvals.arn
    APPROVAL_BASE_URL     = module.approval_lambda.url
    AUTO_REMEDIATE        = tostring(var.auto_remediate)
  }
}

# SNS invokes asynchronously. Two retries on failure (the handler's claim
# makes them safe), and an alarm older than an hour isn't worth diagnosing.
resource "aws_lambda_function_event_invoke_config" "agent" {
  function_name                = module.agent_lambda.function_name
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = 3600
}

# ---- approval: signed links -> decision -> queue ---------------------------

data "aws_iam_policy_document" "approval" {
  source_policy_documents = [data.aws_iam_policy_document.read_secrets.json]

  statement {
    sid       = "Decide"
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.runs.arn]
  }
  statement {
    sid       = "QueueApprovedRemediation"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.remediation.arn]
  }
}

module "approval_lambda" {
  source             = "./modules/container_lambda"
  name               = "${var.project}-approval"
  image_uri          = local.agent_image
  handler            = "approval_handler.handler"
  timeout            = 15
  memory_size        = 512
  function_url       = true # the HMAC signature on each link is the auth
  policy_json        = data.aws_iam_policy_document.approval.json
  log_retention_days = var.log_retention_days

  environment = {
    SSM_PARAMETER_PREFIX  = local.ssm_prefix
    RUNS_TABLE_NAME       = aws_dynamodb_table.runs.name
    REMEDIATION_QUEUE_URL = aws_sqs_queue.remediation.url
  }
}

# ---- MCP server --------------------------------------------------------------

data "aws_iam_policy_document" "mcp" {
  source_policy_documents = [data.aws_iam_policy_document.read_secrets.json]

  statement {
    sid       = "ReadRuns"
    actions   = ["dynamodb:GetItem", "dynamodb:Scan"]
    resources = [aws_dynamodb_table.runs.arn]
  }
  statement {
    sid       = "RecentLogs"
    actions   = ["logs:FilterLogEvents"]
    resources = ["${aws_cloudwatch_log_group.mock_service.arn}:*"]
  }

  # request_approval: re-send the email and record the new expiry.
  dynamic "statement" {
    for_each = var.mcp_write_tools ? [1] : []
    content {
      sid       = "ResendApproval"
      actions   = ["dynamodb:UpdateItem", "sns:Publish"]
      resources = [aws_dynamodb_table.runs.arn, aws_sns_topic.approvals.arn]
    }
  }
  dynamic "statement" {
    for_each = var.mcp_write_tools ? [1] : []
    content {
      sid       = "EncryptedApprovalTopic"
      actions   = ["kms:GenerateDataKey*", "kms:Decrypt"]
      resources = ["*"]
      condition {
        test     = "StringEquals"
        variable = "kms:ViaService"
        values   = ["sns.${var.region}.amazonaws.com"]
      }
    }
  }
}

module "mcp_lambda" {
  count              = var.enable_mcp ? 1 : 0
  source             = "./modules/container_lambda"
  name               = "${var.project}-mcp"
  image_uri          = local.agent_image
  handler            = "mcp_server.handler"
  timeout            = 120 # diagnose_alert runs the whole graph
  memory_size        = 1024
  function_url       = true # bearer token (MCP_AUTH_TOKEN) checked in-app
  policy_json        = data.aws_iam_policy_document.mcp.json
  log_retention_days = var.log_retention_days

  environment = merge(
    {
      SSM_PARAMETER_PREFIX   = local.ssm_prefix
      RUNS_TABLE_NAME        = aws_dynamodb_table.runs.name
      LOG_GROUP_NAME         = local.mock_log_group
      MCP_ENABLE_WRITE_TOOLS = tostring(var.mcp_write_tools)
    },
    # No MOCK_SERVICE_URL: the Spot task has no stable address, so
    # list_failures/inject_failure report "not configured" in AWS.
    var.mcp_write_tools ? {
      APPROVAL_TOPIC_ARN    = aws_sns_topic.approvals.arn
      APPROVAL_BASE_URL     = module.approval_lambda.url
      REMEDIATION_QUEUE_URL = aws_sqs_queue.remediation.url
    } : {},
  )
}
