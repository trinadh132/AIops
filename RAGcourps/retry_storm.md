---
failure_type: retry_storm   # must match FailureMode.java exactly, lowercase
service: self-healing-ops-mock-service
risk_level: medium   # low | medium | high
symptoms_summary: "Request rate spikes past capacity as retries compound on top of retries"
---

# Retry Storm

## Symptoms
- Requests to `GET /api/orders/{id}` start failing once request rate exceeds 50 requests in a 10-second window — confirmed in a captured 60-request concurrent burst, where the first 50 succeeded and the remaining 10 failed once the window count crossed the threshold.
- Structured log line (captured from a real run):
```json
{"@timestamp":"2026-08-11T15:25:41.8711662+05:30","@version":"1","message":"failure_mode=RETRY_STORM http_status=503 message=\"Request rate 51/window exceeds gateway capacity; clients are retrying and amplifying load\"","logger_name":"com.opsagent.mock.exception.GlobalExceptionHandler","thread_name":"http-nio-8080-exec-32","level":"ERROR","level_value":40000,"failure_mode":"RETRY_STORM","event_type":"simulated_failure","service":"self-healing-ops-mock-service"}
```
- Request volume climbs faster than any plausible organic traffic growth — a sharp, sustained step-up rather than a gradual trend, the signature of retries compounding on top of an initial failure rather than genuine new demand.
- Often follows shortly after an unrelated transient failure (a blip that triggered client-side retries), so the timeline usually shows a small initial error spike followed by a much larger volume spike.

## Likely root causes
1. Clients retrying failed requests without backoff, so every failure immediately generates another attempt, compounding load on an already-struggling service.
2. Retries without jitter, causing many clients to retry in near-lockstep and produce synchronized bursts rather than smoothed-out load.
3. Retries at multiple layers of the stack simultaneously (e.g., both the client SDK and a proxy/gateway in front of it retry the same logical request), multiplying the effective retry count.
4. No retry budget/cap, so a single sustained failure can generate unbounded retry volume rather than giving up after a reasonable number of attempts.

## Diagnosis steps
1. Confirm the volume spike timeline: does it follow a smaller initial error event? A retry storm typically has a clear "trigger, then amplify" shape rather than starting large.
2. Check whether requests are genuinely distinct (new user actions) or the same logical request repeated many times — request/trace IDs or idempotency keys make this visible if present.
3. Check for retry configuration (max attempts, backoff strategy, jitter) in the calling client(s) — absence of backoff/jitter is the most common structural cause.
4. Check whether multiple layers in the call path are each independently retrying, which multiplies the effective retry count beyond what any single layer's config would suggest.

## Remediation (ranked by risk)
| Risk | Action | Notes |
|---|---|---|
| Low | Enable/verify rate limiting or load shedding at the service boundary to protect it while the storm subsides | Auto-executable per the risk threshold rule; contains impact without touching client behavior |
| Medium | Return a `Retry-After` header / explicit backoff signal to reduce client-side retry aggressiveness | Needs approval — changes client-visible contract |
| High | Coordinate with calling teams to add backoff + jitter to their retry logic | Needs approval — cross-team change, can't be executed unilaterally |

## Rollback plan
- If rate limiting causes legitimate traffic to be dropped along with the retry storm, raise the limit or add prioritization (e.g., by client ID) rather than removing the limit entirely.
- Once the originating trigger event is resolved and backoff/jitter is in place client-side, monitor request volume to confirm it returns to baseline rather than assuming the storm won't recur.

## References
- AWS "Timeouts, retries, and backoff with jitter" builders' library article — the standard reference for why unjittered retries cause exactly this failure mode.
- Google SRE Book, "Addressing Cascading Failures" — covers retry storms as a specific instance of cascading failure.
