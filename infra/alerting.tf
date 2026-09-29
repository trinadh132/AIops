# One metric filter + alarm per failure mode. Contract with
# lambda_handler.py: the METRIC NAME is the FailureMode enum name, which is
# how the agent maps an alarm back to a failure_type.
#
# Filters match on event_type as well as failure_mode, because several
# non-failure events also carry failure_mode: `failure_injection` (the admin
# API) and `remediation_applied` (the executor). Matching failure_mode alone
# would raise an alarm when a failure is injected and again when it's fixed.
# Signal event types were read off captured-logs/, not assumed.

locals {
  metric_namespace = "OpsAgent/MockService"

  signal_event_type = {
    SLOW_DOWNSTREAM_DEPENDENCY = "high_latency"
    MEMORY_LEAK                = "memory_growth"
    OOM_KILL                   = "process_killed"
  }

  failure_modes = {
    for mode, threshold in var.alarm_thresholds : mode => {
      threshold  = threshold
      event_type = lookup(local.signal_event_type, mode, "simulated_failure")
    }
  }
}

resource "aws_cloudwatch_log_metric_filter" "failure" {
  for_each       = local.failure_modes
  name           = "${var.project}-${lower(each.key)}"
  log_group_name = aws_cloudwatch_log_group.mock_service.name
  pattern        = "{ $.event_type = \"${each.value.event_type}\" && $.failure_mode = \"${each.key}\" }"

  metric_transformation {
    name      = each.key
    namespace = local.metric_namespace
    value     = "1"
    unit      = "Count"
  }
}

# Unencrypted on purpose. CloudWatch alarms cannot publish to a topic that
# uses the AWS-managed aws/sns key; it would take a customer-managed key
# ($1/month plus a key policy for cloudwatch.amazonaws.com) to protect
# alarm-state metadata that isn't secret.
#trivy:ignore:AWS-0095
resource "aws_sns_topic" "alarms" {
  name = "${var.project}-alarms"
}

resource "aws_cloudwatch_metric_alarm" "failure" {
  for_each          = local.failure_modes
  alarm_name        = "${var.project}-${lower(each.key)}"
  alarm_description = "${each.key} signal events >= ${each.value.threshold}/min. Handled by the ops agent."

  namespace           = local.metric_namespace
  metric_name         = each.key
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = each.value.threshold
  # No events = healthy. Without this the alarm sits in INSUFFICIENT_DATA
  # and never produces the ALARM -> OK transition recovery depends on.
  treat_missing_data = "notBreaching"

  # Both directions: ALARM starts a run, OK records recovery on it. Leaving
  # out ok_actions silently breaks seconds_to_recover.
  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]

  depends_on = [aws_cloudwatch_log_metric_filter.failure]
}

resource "aws_sns_topic_subscription" "alarms_to_agent" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "lambda"
  endpoint  = module.agent_lambda.function_arn
}

resource "aws_lambda_permission" "alarms_invoke_agent" {
  statement_id  = "AllowAlarmTopic"
  action        = "lambda:InvokeFunction"
  function_name = module.agent_lambda.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.alarms.arn
}

# Approval requests go to a human by email. SNS sends a confirmation email
# first; until it's clicked, approval emails are silently dropped.
# Encrypted: messages carry signed approval links. The AWS-managed key is
# free; publishers need kms:GenerateDataKey via SNS (see lambdas.tf). A
# customer-managed key would add key-policy control this single-account
# setup doesn't use, for $1/month.
#trivy:ignore:AWS-0136
resource "aws_sns_topic" "approvals" {
  name              = "${var.project}-approvals"
  kms_master_key_id = "alias/aws/sns"
}

resource "aws_sns_topic_subscription" "approvals_email" {
  topic_arn = aws_sns_topic.approvals.arn
  protocol  = "email"
  endpoint  = var.alert_email
}
