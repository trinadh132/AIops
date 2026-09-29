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
