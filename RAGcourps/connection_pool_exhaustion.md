---
failure_type: connection_pool_exhaustion   # must match FailureMode.java exactly, lowercase
service: self-healing-ops-mock-service
risk_level: low   # low | medium | high
symptoms_summary: "503s with 'No available connections in pool'"
---

# Connection Pool Exhaustion

## Symptoms
- HTTP 503 responses from `GET /api/orders/{id}` — 70% of requests failed in a captured 30-request run (21/30), matching the documented ~70% rate.
- Structured log line (captured from a real run):
```json
{"@timestamp":"2026-08-11T15:25:03.0731514+05:30","@version":"1","message":"failure_mode=CONNECTION_POOL_EXHAUSTION http_status=503 message=\"No available connections in pool\"","logger_name":"com.opsagent.mock.exception.GlobalExceptionHandler","thread_name":"http-nio-8080-exec-3","level":"ERROR","level_value":40000,"failure_mode":"CONNECTION_POOL_EXHAUSTION","event_type":"simulated_failure","service":"self-healing-ops-mock-service"}
```
- `ops_mock_failure_injections_total{failure_mode="CONNECTION_POOL_EXHAUSTION"}` increments on `/actuator/prometheus`.
- No corresponding rise in latency for requests that *do* succeed — the failure is binary (get a connection or get rejected), not a slowdown.

## Likely root causes
1. Pool size configured too small for current traffic — no code change needed, just a limit.
2. A connection leak — a code path acquiring a connection without releasing it (missing `close()`/try-with-resources), so the pool slowly fills with connections that never come back.
3. A slow or hung downstream dependency (DB, cache) holding connections open far longer than normal, so the same pool size now supports much less concurrent throughput.
4. A recent traffic spike (marketing push, batch job, retry storm from a caller) that legitimately exceeds provisioned capacity.

## Diagnosis steps
1. Check `GET /admin/failures` (or the equivalent pool metrics endpoint in a real service) to confirm which failure mode is active and how many requests are in flight — rules out a coincidental unrelated issue.
2. Look at the pool's active/idle/waiting connection counts over time. A pool pinned at max with zero idle connections and a growing wait queue points at exhaustion; a pool that cycles normally but occasionally spikes points at bursty traffic instead.
3. Check whether active connection count grows monotonically even during quiet periods — that's the signature of a leak, not legitimate demand.
4. Correlate with downstream dependency latency/error dashboards. If a downstream DB or service degraded around the same time, connections are likely being held open waiting on it rather than leaking in application code.
5. Check recent deploys and traffic volume graphs for the same window — rule out a legitimate demand increase before assuming a bug.

## Remediation (ranked by risk)
| Risk | Action | Notes |
|---|---|---|
| Low | Restart the affected instance(s) to release any leaked connections and reset the pool | Auto-executable per the risk threshold rule; buys time while root cause is found |
| Low | Bump the connection pool `max-size` config within existing DB connection limits | Auto-executable if within a pre-approved ceiling; verify the DB can support the new max concurrent connections first |
| Medium | Add or tighten a connection-acquisition timeout so requests fail fast instead of queuing indefinitely | Needs approval — changes client-visible error behavior |
| High | Patch and redeploy the code path suspected of leaking connections | Needs approval — code change carries regression risk |

## Rollback plan
- If a pool size increase doesn't help or the DB starts rejecting connections, revert the pool config to its previous value and restart the affected instance(s).
- If a code deploy for a leak fix makes things worse (new errors, other regressions), roll back to the previous known-good build/image and re-open the incident before retrying.

## References
- Google SRE Book, "Managing Load" and "Addressing Cascading Failures" chapters — background on connection saturation and load shedding.
- AWS "Timeouts, retries, and backoff with jitter" builders' library article — informs the timeout/backoff remediation option above.
