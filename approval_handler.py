"""
AWS Lambda Function URL for approving or rejecting an escalated run.

  GET  ?run_id&decision&exp&sig  -> confirmation page, changes nothing
  POST (same fields, form body)  -> records the decision; on approve, queues
                                    the run's stored remediation action

GET never acts because mail security scanners and link previewers fetch
URLs in email automatically; if GET approved, a scanner could approve a
remediation with nobody looking.

The Function URL is public (auth type NONE): the HMAC signature on each link
is the authentication. See tools/approval.py.

Same image as the agent; Lambda image config:
  entry_point = ["python", "-m", "awslambdaric"]
  command     = ["approval_handler.handler"]
"""

import base64
import html
import json
import logging
import os
import time
from urllib.parse import parse_qs

from tools.config import APPROVAL_KEYS, load_config

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger("self_healing_ops.approval")

load_config(required=APPROVAL_KEYS)

from tools import approval  # noqa: E402
from tools.remediation import RemediationQueue  # noqa: E402
from tools.runs import RunStore  # noqa: E402

SECURITY_HEADERS = {
    "Content-Type": "text/html; charset=utf-8",
    # Never cache a page tied to a live credential.
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    # No scripts at all; the form may only post back to this same URL.
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
}

# One generic message for every link failure, so the page doesn't tell a
# prober whether a guessed link was close (bad signature vs expired, etc.).
INVALID_LINK = "This link is invalid or has expired."


def _page(status: int, title: str, body_html: str) -> dict:
    return {
        "statusCode": status,
        "headers": SECURITY_HEADERS,
        "body": (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{html.escape(title)}</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem;"
            "line-height:1.5}pre{white-space:pre-wrap;background:#f4f4f4;padding:.75rem}"
            "button{font-size:1rem;padding:.5rem 1.25rem}</style></head>"
            f"<body><h1>{html.escape(title)}</h1>{body_html}</body></html>"
        ),
    }


def _run_summary(run: dict) -> str:
    """Everything here came from an LLM or from logs, so every value is
    escaped. Unescaped, a prompt-injected diagnosis could run script in the
    approver's browser."""
    output = run.get("final_output") or {}
    diagnosis = output.get("diagnosis") or {}
    plan = output.get("remediation_plan") or {}
    steps = "\n".join(f"{s.get('step_number')}. {s.get('action')}" for s in plan.get("steps", []))
    e = lambda v: html.escape(str(v))  # noqa: E731
    return (
        f"<p><b>Failure:</b> {e(run.get('failure_type', '?'))}<br>"
        f"<b>Risk:</b> {e(plan.get('overall_risk_level', '?'))}<br>"
        f"<b>Root cause:</b> {e(diagnosis.get('root_cause', '?'))}</p>"
        f"<p><b>Proposed plan (advisory):</b></p><pre>{e(steps or '(none)')}</pre>"
        f"<p><b>Action that will run if approved:</b></p>"
        f"<pre>{e(json.dumps(run.get('remediation_action'), default=str))}</pre>"
    )


def handle_request(method: str, params: dict, *, store, queue, hmac_key: str,
                   now: float | None = None) -> dict:
    now = time.time() if now is None else now
    reason = approval.verify(hmac_key, params, now)
    if reason:
        logger.warning("Rejected approval request: %s (run_id=%s)", reason, params.get("run_id"))
        return _page(403, "Link not valid", f"<p>{INVALID_LINK}</p>")

    run_id, decision = params["run_id"], params["decision"]
    run = store.get(run_id)
    if not run:
        return _page(404, "Run not found", f"<p>{INVALID_LINK}</p>")
    if run.get("approval_status") != "pending":
        return _page(409, "Already decided",
                     f"<p>This run is already <b>{html.escape(str(run.get('approval_status')))}</b>.</p>")

    verb = "Approve" if decision == "approve" else "Reject"
    if method == "GET":
        hidden = "".join(
            f"<input type='hidden' name='{html.escape(k)}' value='{html.escape(params[k])}'>"
            for k in ("run_id", "decision", "exp", "sig")
        )
        return _page(200, f"{verb} remediation?", _run_summary(run) + (
            f"<form method='post'>{hidden}<button type='submit'>{verb} run "
            f"{html.escape(run_id)}</button></form>"
        ))

    if method != "POST":
        return _page(405, "Method not allowed", "")

    if not store.decide(run_id, decision, now):
        # Lost a race with another click on this or the opposite link.
        return _page(409, "Already decided", "<p>This run was decided by another request.</p>")

    if decision == "reject":
        store.set_fields(run_id, {"remediation_status": "rejected"})
        logger.info("Run %s rejected by approver", run_id)
        return _page(200, "Rejected", "<p>Nothing will be executed for this run.</p>")

    try:
        queue.enqueue(run_id, run["remediation_action"], now)
    except Exception:
        # Put the run back to pending so the same link can be retried,
        # rather than leaving it 'approved' with nothing queued.
        logger.exception("Run %s approved but queueing failed; reverting to pending", run_id)
        store.revert_decision(run_id)
        return _page(502, "Could not queue remediation", "<p>Nothing was executed. Try the link again.</p>")

    store.set_fields(run_id, {"remediation_status": "queued"})
    logger.info("Run %s approved; remediation queued", run_id)
    return _page(200, "Approved", "<p>The remediation has been queued. Recovery is recorded "
                                  "when the alarm returns to OK.</p>")


def _params_from_event(event: dict) -> tuple[str, dict]:
    """Function URL events use API Gateway's payload format 2.0."""
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET").upper()
    if method == "POST":
        body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode()
        params = {k: v[0] for k, v in parse_qs(body).items()}
    else:
        params = dict(event.get("queryStringParameters") or {})
    return method, params


_deps_cache: dict | None = None


def _deps() -> dict:
    global _deps_cache
    if _deps_cache is None:
        import boto3
        _deps_cache = {
            "store": RunStore(boto3.resource("dynamodb").Table(os.environ["RUNS_TABLE_NAME"])),
            "queue": RemediationQueue(boto3.client("sqs"), os.environ["REMEDIATION_QUEUE_URL"]),
            "hmac_key": os.environ["APPROVAL_HMAC_KEY"],
        }
    return _deps_cache


def handler(event, context):
    method, params = _params_from_event(event)
    return handle_request(method, params, **_deps())
