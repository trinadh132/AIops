# ---- remediation queue ------------------------------------------------------

resource "aws_sqs_queue" "remediation_dlq" {
  name                      = "${var.project}-remediation-dlq"
  message_retention_seconds = 1209600 # 14 days to look at what failed
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "remediation" {
  name                    = "${var.project}-remediation"
  sqs_managed_sse_enabled = true
  # Executor processing is instant; 60s just has to outlast one poll cycle.
  visibility_timeout_seconds = 60
  # Mirrors the executor's own 1h staleness check: an action nobody picked
  # up within the hour shouldn't linger.
  message_retention_seconds = 3600
  receive_wait_time_seconds = 20 # long polling by default

  # Messages that throw in the executor 3 times are parked, not retried
  # forever. (Invalid messages are deleted by the executor, not retried.)
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.remediation_dlq.arn
    maxReceiveCount     = 3
  })
}

# ---- run records --------------------------------------------------------------

resource "aws_dynamodb_table" "runs" {
  name         = "${var.project}-runs"
  billing_mode = "PAY_PER_REQUEST" # effectively free at a handful of runs/day
  hash_key     = "run_id"

  attribute {
    name = "run_id"
    type = "S"
  }

  # The runs are the audit trail of what the agent decided and did;
  # point-in-time recovery costs pennies for a table this small.
  point_in_time_recovery {
    enabled = true
  }

  # Runs and alarm# pointer items both set expires_at (tools/runs.py).
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

# ---- secrets (SSM Parameter Store) ---------------------------------------------
#
# All four live under /<project>/ and are loaded by tools/config.py at cold
# start. None of their values are in Terraform state:
#  - Generated secrets use an ephemeral random_password written through the
#    write-only `value_wo` attribute. Terraform sends the value once and
#    never stores or reads it back. Bump var.secret_version to rotate.
#  - Secrets that come from outside (OpenRouter key, Neon URL) are created
#    with a placeholder and set out of band:
#      aws ssm put-parameter --overwrite --type SecureString \
#        --name /ops-agent/OPENROUTER_API --value '...'
#    Because value_wo is never read back, Terraform won't revert them.

ephemeral "random_password" "approval_hmac" {
  length  = 48
  special = false
}

ephemeral "random_password" "mcp_token" {
  length  = 48
  special = false
}

resource "aws_ssm_parameter" "approval_hmac_key" {
  name             = "/${var.project}/APPROVAL_HMAC_KEY"
  type             = "SecureString"
  value_wo         = ephemeral.random_password.approval_hmac.result
  value_wo_version = var.secret_version
}

resource "aws_ssm_parameter" "mcp_auth_token" {
  name             = "/${var.project}/MCP_AUTH_TOKEN"
  type             = "SecureString"
  value_wo         = ephemeral.random_password.mcp_token.result
  value_wo_version = var.secret_version
}

resource "aws_ssm_parameter" "external" {
  for_each         = toset(["OPENROUTER_API", "DATABASE_URL"])
  name             = "/${var.project}/${each.key}"
  type             = "SecureString"
  value_wo         = "SET_ME_OUT_OF_BAND"
  value_wo_version = 1
  description      = "Set with aws ssm put-parameter --overwrite; Terraform only creates the placeholder."
}
