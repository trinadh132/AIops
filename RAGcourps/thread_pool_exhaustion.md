---
failure_type: thread_pool_exhaustion   # must match FailureMode.java exactly, lowercase
service: self-healing-ops-mock-service
risk_level: low   # low | medium | high
symptoms_summary: "Requests failing once concurrent in-flight count exceeds pool capacity"
---

# Thread Pool Exhaustion

## Symptoms
- Requests to `GET /api/orders/{id}` start failing once concurrent in-flight requests exceed the configured pool capacity (20 in the mock service) — 60% failed in a captured 100-request concurrent burst (60/100), once in-flight count climbed past 20.
- Structured log line (captured from a real run):
```json
{"@timestamp":"2026-08-11T15:50:51.2238032+05:30","@version":"1","message":"failure_mode=THREAD_POOL_EXHAUSTION http_status=503 message=\"Thread pool exhausted: 37 concurrent requests exceed capacity 20\"","logger_name":"com.opsagent.mock.exception.GlobalExceptionHandler","thread_name":"http-nio-8080-exec-24","level":"ERROR","level_value":40000,"failure_mode":"THREAD_POOL_EXHAUSTION","event_type":"simulated_failure","service":"self-healing-ops-mock-service"}
```
- The error message reports the actual concurrent count at rejection time (e.g. `37`, `38`, `60` in the captured run) rather than a fixed number — useful signal for how far past the 20-request threshold the burst pushed.
- Failures appear specifically under concurrent load (e.g., a load test or traffic burst) and disappear once concurrency drops back below the threshold — unlike connection pool exhaustion, this tracks in-flight request count, not connections held over time.
- `GET /admin/failures` shows the current `inFlightRequests` count, useful for confirming the pool is genuinely saturated versus some other 503 cause.

## Likely root causes
1. Thread pool sized too small for legitimate peak concurrency.
2. Threads held longer than expected because a downstream call is slow (see Slow Downstream Dependency) — same pool size now supports much lower effective concurrency.
3. A traffic burst (retry storm, batch job, marketing spike) pushing concurrency well above normal peak.
4. A blocking operation on the request thread that should be async (e.g., synchronous I/O on a thread meant for fast request handling).

## Diagnosis steps
1. Check in-flight request count / thread pool active count at the time of the incident against the configured pool size — confirms genuine exhaustion versus another 503 cause.
2. Check whether request duration increased around the same time (points at a slow downstream holding threads longer) versus request volume increased (points at a genuine traffic burst).
3. Check for any recently introduced blocking calls on request-handling threads.
4. Check whether the burst correlates with a specific caller/client (helps determine whether this is a capacity problem or a misbehaving upstream consumer).

## Remediation (ranked by risk)
| Risk | Action | Notes |
|---|---|---|
| Low | Increase the thread pool size within the box's available resources | Auto-executable per the risk threshold rule; verify CPU/memory headroom exists first |
| Low | Shed load by rejecting excess requests fast (already partially happening via the 503) rather than queuing indefinitely | Auto-executable; confirm queue depth limit is sane, not zero and not unbounded |
| Medium | Add horizontal scaling (more instances) to absorb the burst | Needs approval — infrastructure/cost change |
| High | Move a blocking operation off the request thread to an async/background path | Needs approval — code change, requires testing |

## Rollback plan
- If a larger thread pool causes CPU contention or increased context-switch overhead instead of helping, revert to the previous pool size and address the underlying blocking/slow call instead.
- If added instances don't reduce per-instance saturation (e.g., the load balancer isn't distributing evenly), investigate the load balancer configuration rather than scaling further.

## References
- Google SRE Book, "Managing Load" — background on load shedding and capacity planning that informs the remediation ranking here.
- AWS "Managing Latency, Health, and Availability" builders' library articles.
