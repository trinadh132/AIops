"""
What the agent is allowed to *do*, and how a run's remediation is routed.

The LLM's remediation plan is free text for humans. It never decides what
executes: the executable action comes from a fixed table keyed on
failure_type, and the executor in the mock service re-checks the same
allowlist before applying anything. A prompt-injected log line can therefore
change what the plan *says*, but not what runs.

Routing after a diagnosis:
  auto_recommend + AUTO_REMEDIATE on  -> queue the action now
  auto_recommend + AUTO_REMEDIATE off -> ask for approval ("shadow mode")
  pending_approval                    -> ask for approval
  anything else (rejected, failed)    -> nothing to execute
"""

import json
import time

from agentic import VALID_FAILURE_TYPES
from tools import approval

ALLOWED_ACTIONS = frozenset({"deactivate_failure_mode"})

# Clearing an OOM_KILL alone would leave the leak that caused it running.
FAILURE_MODES_TO_CLEAR = {
    "OOM_KILL": ["OOM_KILL", "MEMORY_LEAK"],
}


def action_for(failure_type: str) -> dict:
    return {
        "action": "deactivate_failure_mode",
        "failure_modes": FAILURE_MODES_TO_CLEAR.get(failure_type, [failure_type]),
    }


def validate_action(action: dict) -> None:
    if action.get("action") not in ALLOWED_ACTIONS:
        raise ValueError(f"Action not allowlisted: {action.get('action')!r}")
    modes = action.get("failure_modes") or []
    unknown = [m for m in modes if m not in VALID_FAILURE_TYPES]
    if not modes or unknown:
        raise ValueError(f"Invalid failure_modes: {modes!r}")


class RemediationQueue:
    """The only way anything reaches the executor. Validates on the way in;
    the executor validates again on the way out."""

    def __init__(self, sqs_client, queue_url: str):
        self._sqs, self._queue_url = sqs_client, queue_url

    def enqueue(self, run_id: str, action: dict, now: float | None = None) -> None:
        validate_action(action)
        body = {"run_id": run_id, "issued_at": int(time.time() if now is None else now), **action}
        self._sqs.send_message(QueueUrl=self._queue_url, MessageBody=json.dumps(body))


class RemediationRouter:
    def __init__(self, *, queue: RemediationQueue, sns_client, topic_arn: str,
                 approval_base_url: str, hmac_key: str, auto_remediate: bool = False,
                 link_ttl_seconds: int = approval.DEFAULT_LINK_TTL_SECONDS):
        self._queue = queue
        self._sns, self._topic_arn = sns_client, topic_arn
        self._approval_base_url, self._hmac_key = approval_base_url, hmac_key
        self._auto_remediate = auto_remediate
        self._link_ttl = link_ttl_seconds

    def route(self, run_id: str, failure_type: str, final_output: dict, now: float | None = None) -> dict:
        """Acts on a finished diagnosis and returns the fields to record on
        the run. Side effects happen before the caller records them, so a
        crash in between leads to a retry, never to a record that claims an
        action was queued when it wasn't."""
        now = time.time() if now is None else now
        outcome = final_output.get("status")
        if outcome not in ("auto_recommend", "pending_approval"):
            return {"remediation_status": "none"}

        action = action_for(failure_type)
        validate_action(action)

        if outcome == "auto_recommend" and self._auto_remediate:
            self._queue.enqueue(run_id, action, now)
            return {"remediation_action": action, "remediation_status": "queued",
                    "approval_status": "not_required"}

        expires_at = int(now + self._link_ttl)
        self.send_approval_request(run_id, failure_type, final_output, expires_at)
        return {"remediation_action": action, "remediation_status": "awaiting_approval",
                "approval_status": "pending", "approval_expires_at": expires_at}

    def resend_approval(self, store, run_id: str, now: float | None = None) -> dict:
        """Fresh links for a run that's still pending (lost or expired email).
        Old links stay valid until their own expiry; single use still holds
        because every link funnels through the same conditional decide()."""
        from tools.runs import from_dynamo

        run = store.get(run_id)
        if not run:
            raise LookupError(f"No run {run_id!r}")
        if run.get("approval_status") != "pending":
            raise ValueError(f"Run {run_id} is {run.get('approval_status')!r}, not pending approval")
        now = time.time() if now is None else now
        expires_at = int(now + self._link_ttl)
        self.send_approval_request(run_id, run["failure_type"], from_dynamo(run.get("final_output") or {}), expires_at)
        store.set_fields(run_id, {"approval_expires_at": expires_at})
        return {"run_id": run_id, "approval_expires_at": expires_at}

    def send_approval_request(self, run_id, failure_type, final_output, expires_at):
        links = {d: approval.build_link(self._approval_base_url, self._hmac_key, run_id, d, expires_at)
                 for d in approval.DECISIONS}
        diagnosis = final_output.get("diagnosis") or {}
        plan = final_output.get("remediation_plan") or {}
        steps = "\n".join(f"  {s.get('step_number')}. {s.get('action')}" for s in plan.get("steps", []))
        message = (
            f"The ops agent wants approval to remediate {failure_type}.\n\n"
            f"Run: {run_id}\n"
            f"Risk: {final_output.get('effective_risk_level') or plan.get('overall_risk_level', '?')} "
            f"(model said {plan.get('overall_risk_level', '?')}; floored at the runbook's level)\n"
            f"Root cause: {diagnosis.get('root_cause', '?')}\n"
            f"Confidence: {diagnosis.get('confidence', '?')}\n\n"
            f"Proposed plan (advisory):\n{steps or '  (none)'}\n\n"
            f"Action that will actually run if approved: {json.dumps(action_for(failure_type))}\n\n"
            f"Approve: {links['approve']}\n"
            f"Reject:  {links['reject']}\n\n"
            f"Links expire at {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(expires_at))} "
            f"and work once. Opening a link shows a confirmation page; nothing "
            f"happens until you confirm."
        )
        self._sns.publish(
            TopicArn=self._topic_arn,
            # SNS email subjects: <=100 chars, no newlines.
            Subject=f"[ops-agent] Approval needed: {failure_type}"[:100],
            Message=message,
        )
