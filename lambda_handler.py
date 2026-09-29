"""
AWS Lambda entrypoint: CloudWatch alarm -> SNS -> one agent run.

Flow per alarm:
  1. ignore anything that isn't an OK->ALARM transition
  2. claim a run_id derived from the alarm (duplicate deliveries collide)
  3. pull the alarm's log lines from CloudWatch Logs
  4. run the LangGraph agent
  5. record the outcome in DynamoDB (agent_runs)

Convention shared with the Terraform alarms (step 6): each alarm's metric
name is the FailureMode enum name, e.g. DISK_FULL. That's how an alarm maps
back to a failure_type without parsing alarm names.

Image config: the agent image has no ENTRYPOINT (so `docker compose run
agent python ...` keeps working); Lambda sets it instead:
  entry_point = ["python", "-m", "awslambdaric"]
  command     = ["lambda_handler.handler"]
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta

from tools.config import load_config

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger("self_healing_ops.handler")

# Cold start: resolve secrets before anything imports llm.py, which reads
# OPENROUTER_API at import time. agentic imports llm lazily, so importing
# agentic here is safe.
load_config()

import agentic as ag  # noqa: E402
from tools.logs import fetch_alarm_logs  # noqa: E402
from tools.runs import RunStore  # noqa: E402

LOG_GROUP_NAME = os.environ.get("LOG_GROUP_NAME", "/ecs/self-healing-ops-mock-service")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "self-healing-ops-mock-service")
LOG_WINDOW_MINUTES = int(os.environ.get("LOG_WINDOW_MINUTES", "5"))
# Log delivery to CloudWatch lags a few seconds behind the metric that
# tripped the alarm, so look slightly past the state change too.
LOG_LAG_ALLOWANCE = timedelta(minutes=1)


def run_id_for(message: dict) -> str:
    """Deterministic per alarm transition: SNS redeliveries and Lambda
    retries of the same alarm produce the same run_id."""
    key = f"{message['AlarmArn']}|{message['StateChangeTime']}"
    return "run-" + hashlib.sha256(key.encode()).hexdigest()[:16]


def parse_state_change_time(value: str) -> datetime:
    # CloudWatch sends e.g. "2026-09-29T12:00:00.000+0000"
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


def handle_alarm(message: dict, *, store, logs_client, graph,
                 log_group: str = LOG_GROUP_NAME, service: str = SERVICE_NAME,
                 window_minutes: int = LOG_WINDOW_MINUTES) -> dict:
    alarm_name = message.get("AlarmName", "unknown")
    if message.get("NewStateValue") != "ALARM":
        logger.info("Ignoring %s transition to %s", alarm_name, message.get("NewStateValue"))
        return {"alarm": alarm_name, "skipped": "not_alarm_state"}

    failure_type = (message.get("Trigger") or {}).get("MetricName", "")
    changed_at = parse_state_change_time(message["StateChangeTime"])
    run_id = run_id_for(message)

    claim_id = store.claim(run_id, {
        "alarm_name": alarm_name,
        "failure_type": failure_type,
        "state_change_time": message["StateChangeTime"],
    })
    if claim_id is None:
        logger.info("Run %s already claimed or completed; skipping duplicate delivery", run_id)
        return {"run_id": run_id, "skipped": "duplicate"}

    try:
        lines = fetch_alarm_logs(
            logs_client, log_group, failure_type,
            start=changed_at - timedelta(minutes=window_minutes),
            end=changed_at + LOG_LAG_ALLOWANCE,
        )
        if lines:
            log_snippet = "\n".join(lines)
        else:
            # Better a thin diagnosis than none: the alarm reason still says
            # what crossed which threshold.
            logger.warning("Run %s: no log lines found for %s; using alarm reason", run_id, failure_type)
            log_snippet = message.get("NewStateReason", "")

        alert = ag.build_alert(
            failure_type=failure_type,
            service=service,
            log_snippet=log_snippet,
            alert_id=run_id,
            timestamp=changed_at.isoformat(),
            raw_payload={"alarm_name": alarm_name, "alarm_arn": message.get("AlarmArn")},
        )

        ag._reset_retrieval_deps_cache()
        final = graph.invoke(ag.initial_state(alert))
        result = summarize_run(final, log_line_count=len(lines))
    except Exception as e:
        store.fail(run_id, claim_id, f"{type(e).__name__}: {e}")
        raise  # let Lambda's async retry (bounded to 2) have a go

    if not store.complete(run_id, claim_id, result):
        logger.warning("Run %s: claim was taken over before completion; result not recorded", run_id)
    logger.info("Run %s finished: outcome=%s", run_id, result["outcome"])
    return {"run_id": run_id, "outcome": result["outcome"]}


def summarize_run(final: dict, log_line_count: int) -> dict:
    """What goes into agent_runs. Keeps a condensed log excerpt rather than
    the raw log: DynamoDB items cap at 400 KB, and the audit trail needs what
    the agent saw, not every repeated line."""
    final_output = final.get("final_output") or {}
    return {
        "outcome": final_output.get("status", "unknown"),
        "final_output": final_output,
        "retrieval_confidence": final.get("retrieval_confidence", 0.0),
        "used_fallback": final.get("used_fallback", False),
        "retrieved": [
            {"runbook_id": c["runbook_id"], "section": c["section"], "similarity_score": c["similarity_score"]}
            for c in final.get("retrieved_chunks", [])
        ],
        "web_sources": [r["url"] for r in final.get("web_search_results", [])],
        "node_errors": final.get("node_errors", []),
        "log_line_count": log_line_count,
        "log_excerpt": ag._condense_log_snippet(final["alert"]["log_snippet"]),
    }


def _parse_record(record: dict) -> dict | None:
    try:
        message = json.loads(record["Sns"]["Message"])
    except (KeyError, TypeError, json.JSONDecodeError):
        message = None
    if not isinstance(message, dict) or "AlarmArn" not in message:
        # Not a CloudWatch alarm (e.g. a manual test publish). Retrying won't
        # change that, so skip rather than raise.
        logger.warning("Skipping SNS record that isn't a CloudWatch alarm")
        return None
    return message


_deps_cache: dict | None = None


def _deps() -> dict:
    """AWS clients and the compiled graph, built once per warm container."""
    global _deps_cache
    if _deps_cache is None:
        import boto3
        _deps_cache = {
            "store": RunStore(boto3.resource("dynamodb").Table(os.environ["RUNS_TABLE_NAME"])),
            "logs_client": boto3.client("logs"),
            "graph": ag.build_graph(),
        }
    return _deps_cache


def handler(event, context):
    results = []
    for record in event.get("Records", []):
        message = _parse_record(record)
        if message is not None:
            results.append(handle_alarm(message, **_deps()))
    return results
