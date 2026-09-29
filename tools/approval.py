"""
Signed, expiring approve/reject links for escalated runs.

A link carries run_id, decision and expires_at, plus an HMAC-SHA256 over
those three fields keyed by APPROVAL_HMAC_KEY. Anyone holding the link can
use it, so it's a bearer credential: short-lived, bound to one run and one
decision, and single-use (the run's approval_status can only leave
"pending" once — enforced by a conditional write in RunStore.decide).

The signature makes links unforgeable; it does not make them secret. Email
is the delivery channel, so treat inbox access as approval access.
"""

import hashlib
import hmac
from urllib.parse import urlencode

DECISIONS = ("approve", "reject")
DEFAULT_LINK_TTL_SECONDS = 24 * 3600


def _payload(run_id: str, decision: str, expires_at: int) -> bytes:
    return f"{run_id}|{decision}|{expires_at}".encode()


def sign(key: str, run_id: str, decision: str, expires_at: int) -> str:
    return hmac.new(key.encode(), _payload(run_id, decision, expires_at), hashlib.sha256).hexdigest()


def build_link(base_url: str, key: str, run_id: str, decision: str, expires_at: int) -> str:
    query = urlencode({
        "run_id": run_id,
        "decision": decision,
        "exp": expires_at,
        "sig": sign(key, run_id, decision, expires_at),
    })
    return f"{base_url.rstrip('/')}/?{query}"


def verify(key: str, params: dict, now: float) -> str | None:
    """Returns None if params are a valid, unexpired link; otherwise a short
    reason. Reasons are for logs; the page shown to users stays generic so
    it doesn't help anyone probing for valid links."""
    run_id, decision, exp, sig = (params.get(k) for k in ("run_id", "decision", "exp", "sig"))
    if not all((run_id, decision, exp, sig)):
        return "missing_fields"
    if decision not in DECISIONS:
        return "bad_decision"
    try:
        expires_at = int(exp)
    except ValueError:
        return "bad_expiry"
    # Constant-time compare, so response timing doesn't leak how many
    # leading characters of a guessed signature were right.
    if not hmac.compare_digest(sign(key, run_id, decision, expires_at), sig):
        return "bad_signature"
    if now > expires_at:
        return "expired"
    return None
