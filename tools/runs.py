"""
DynamoDB-backed record of agent runs: audit log and idempotency guard.

SNS delivers to Lambda at-least-once, and Lambda retries failed async
invocations, so the same alarm can arrive more than once. Every run starts
by *claiming* its run_id (derived from the alarm, so duplicates collide):

- new run_id                          -> claim succeeds
- status "error" (a previous attempt failed cleanly) -> claim succeeds (retry)
- status "running", claim older than the lease       -> claim succeeds
  (the previous attempt died mid-run: timeout or OOM, never marked itself)
- status "running" within the lease, or "completed"  -> claim fails, skip

Each claim writes a fresh claim_id, and complete()/fail() only succeed while
that claim_id still matches. So if an attempt overruns its lease and gets
taken over, the stale attempt can't overwrite the newer attempt's result.
"""

import json
import time
import uuid
from decimal import Decimal

from botocore.exceptions import ClientError

# Must exceed the Lambda timeout, or a slow-but-alive run could be taken
# over. 900s is Lambda's maximum timeout, so this is safe for any setting.
DEFAULT_LEASE_SECONDS = 900
DEFAULT_TTL_DAYS = 90


def to_dynamo(value):
    """DynamoDB rejects Python floats; round-trip through JSON so every
    float becomes a Decimal and anything non-serializable becomes a string."""
    return json.loads(json.dumps(value, default=str), parse_float=Decimal)


def _is_conditional_failure(err: ClientError) -> bool:
    return err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class RunStore:
    def __init__(self, table, lease_seconds: int = DEFAULT_LEASE_SECONDS, ttl_days: int = DEFAULT_TTL_DAYS):
        self._table = table
        self._lease_seconds = lease_seconds
        self._ttl_seconds = ttl_days * 86400

    def claim(self, run_id: str, attrs: dict, now: float | None = None) -> str | None:
        """Returns a claim_id if this caller now owns the run, else None."""
        now = time.time() if now is None else now
        claim_id = uuid.uuid4().hex

        names = {"#status": "status"}
        values = {
            ":running": "running",
            ":error": "error",
            ":now": to_dynamo(now),
            ":stale": to_dynamo(now - self._lease_seconds),
            ":claim_id": claim_id,
            ":zero": 0,
            ":one": 1,
            ":expires_at": int(now + self._ttl_seconds),
        }
        sets = [
            "#status = :running",
            "claimed_at = :now",
            "claim_id = :claim_id",
            "attempts = if_not_exists(attempts, :zero) + :one",
            "created_at = if_not_exists(created_at, :now)",
            "expires_at = :expires_at",
        ]
        for i, (key, value) in enumerate(attrs.items()):
            names[f"#a{i}"] = key
            values[f":a{i}"] = to_dynamo(value)
            sets.append(f"#a{i} = :a{i}")

        try:
            self._table.update_item(
                Key={"run_id": run_id},
                UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression=(
                    "attribute_not_exists(run_id) OR #status = :error "
                    "OR (#status = :running AND claimed_at < :stale)"
                ),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except ClientError as err:
            if _is_conditional_failure(err):
                return None
            raise
        return claim_id

    def complete(self, run_id: str, claim_id: str, result: dict, now: float | None = None) -> bool:
        return self._finish(run_id, claim_id, "completed", result, now)

    def fail(self, run_id: str, claim_id: str, error: str, now: float | None = None) -> bool:
        return self._finish(run_id, claim_id, "error", {"error": error}, now)

    def get(self, run_id: str) -> dict | None:
        return self._table.get_item(Key={"run_id": run_id}).get("Item")

    # ---- approvals --------------------------------------------------------

    def decide(self, run_id: str, decision: str, now: float | None = None) -> bool:
        """pending -> approved/rejected, exactly once. This conditional write
        is what makes approval links single-use: a second click, a replayed
        link, or an approve racing a reject all lose here."""
        status = {"approve": "approved", "reject": "rejected"}[decision]
        return self._conditional_set(
            run_id, {"approval_status": status, "decided_at": time.time() if now is None else now},
            "#approval = :expected", {"#approval": "approval_status"}, {":expected": "pending"},
        )

    def revert_decision(self, run_id: str) -> bool:
        """approved -> pending, used only when queueing the approved action
        failed, so the approver's link works again instead of the run being
        stuck as 'approved' with nothing queued."""
        return self._conditional_set(
            run_id, {"approval_status": "pending"},
            "#approval = :expected", {"#approval": "approval_status"}, {":expected": "approved"},
        )

    def set_fields(self, run_id: str, fields: dict) -> bool:
        return self._conditional_set(run_id, fields, "attribute_exists(run_id)", {}, {})

    # ---- recovery (alarm back to OK) ----------------------------------------

    def link_alarm(self, alarm_arn: str, run_id: str, now: float | None = None) -> None:
        """Pointer item alarm#<arn> -> latest run_id. The OK notification
        doesn't say which ALARM transition it ends, so this is how recovery
        finds its run. Same table, distinct key prefix (single-table design)."""
        now = time.time() if now is None else now
        self._table.put_item(Item={
            "run_id": f"alarm#{alarm_arn}",
            "latest_run_id": run_id,
            "expires_at": int(now + self._ttl_seconds),
        })

    def mark_recovered(self, alarm_arn: str, recovered_at: float) -> str | None:
        """Records when the alarm returned to OK, and time-to-recover measured
        from the run's alarm_raised_at. Returns the run_id, or None if there's
        no run for this alarm or it was already marked."""
        pointer = self.get(f"alarm#{alarm_arn}")
        run = self.get(pointer["latest_run_id"]) if pointer else None
        if not run:
            return None
        run_id = run["run_id"]
        fields = {"recovered_at": recovered_at}
        if "alarm_raised_at" in run:
            fields["seconds_to_recover"] = round(recovered_at - float(run["alarm_raised_at"]), 1)
        recorded = self._conditional_set(
            run_id,
            fields,
            "attribute_exists(run_id) AND attribute_not_exists(recovered_at)", {}, {},
        )
        return run_id if recorded else None

    def _conditional_set(self, run_id: str, fields: dict, condition: str,
                         condition_names: dict, condition_values: dict) -> bool:
        names, values, sets = dict(condition_names), dict(condition_values), []
        for i, (key, value) in enumerate(fields.items()):
            names[f"#u{i}"] = key
            values[f":u{i}"] = to_dynamo(value)
            sets.append(f"#u{i} = :u{i}")
        kwargs = {
            "Key": {"run_id": run_id},
            "UpdateExpression": "SET " + ", ".join(sets),
            "ConditionExpression": condition,
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
        try:
            self._table.update_item(**kwargs)
        except ClientError as err:
            if _is_conditional_failure(err):
                return False
            raise
        return True

    def _finish(self, run_id, claim_id, status, fields, now) -> bool:
        """Returns False (instead of raising) when the claim was lost, so the
        caller can log it; the newer attempt owns the record now."""
        now = time.time() if now is None else now
        names = {"#status": "status"}
        values = {":status": status, ":now": to_dynamo(now), ":claim_id": claim_id}
        sets = ["#status = :status", "finished_at = :now"]
        for i, (key, value) in enumerate(fields.items()):
            names[f"#f{i}"] = key
            values[f":f{i}"] = to_dynamo(value)
            sets.append(f"#f{i} = :f{i}")
        try:
            self._table.update_item(
                Key={"run_id": run_id},
                UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression="claim_id = :claim_id",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except ClientError as err:
            if _is_conditional_failure(err):
                return False
            raise
        return True
