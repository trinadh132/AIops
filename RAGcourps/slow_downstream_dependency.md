---
failure_type: slow_downstream_dependency   # must match FailureMode.java exactly, lowercase
service: self-healing-ops-mock-service
risk_level: low   # low | medium | high
symptoms_summary: "Every request latent 2-5s, logged as a high_latency warning followed by a normal success"
---

# Slow Downstream Dependency

## Symptoms
- Every request to `GET /api/orders/{id}` succeeds, but latency balloons to 2-5s (captured range: 2186-4673ms across a 5-request sample) vs. a normal baseline well under 100ms.
- Each slow request logs two lines, not one: a `high_latency` WARN carrying the actual delay, immediately followed by the normal `request_success` INFO line for the same `request_id` — the request never errors, it's purely a latency symptom.
- Structured log line (captured from a real run — the `high_latency` warning):
```json
{"@timestamp":"2026-08-11T15:25:13.2044154+05:30","@version":"1","message":"delay_ms=3810 message=\"Downstream dependency degraded\"","logger_name":"com.opsagent.mock.controller.OrdersController","thread_name":"http-nio-8080-exec-8","level":"WARN","level_value":30000,"event_type":"high_latency","request_id":"ce581d44-fbc1-4cc5-ac3e-db0254ed9678","failure_mode":"SLOW_DOWNSTREAM_DEPENDENCY","service":"self-healing-ops-mock-service"}
```
- No error rate increase — this is a pure latency degradation, which makes it easy to miss on error-rate-only alerting and easy to confuse with generic "the service is slow" complaints.
- p50/p99 latency dashboards shift together (every request affected) rather than just the tail, distinguishing this from an isolated slow query or GC pause.

## Likely root causes
1. The downstream dependency (DB, cache, third-party API) itself is degraded — its own resource saturation, not a problem in this service.
2. Network-level latency or packet loss between this service and the dependency (bad routing, an AZ/region issue, a saturated link).
3. The dependency is healthy but this service is calling it inefficiently — e.g., N+1 calls, no connection reuse, missing caching that used to exist.
4. A downstream capacity change (e.g., the dependency was scaled down, or moved to a smaller instance type) that wasn't communicated.

## Diagnosis steps
1. Check the downstream dependency's own health/latency dashboards directly — if it shows the same elevated latency independently, the root cause is upstream of this service, not in it.
2. Check network-level metrics (RTT, retransmits, DNS resolution time) between this service and the dependency to rule out a network path issue before assuming an application problem.
3. Compare current call volume/pattern against baseline — a change in call shape (more calls, larger payloads) can look identical to a dependency slowdown from the caller's side.
4. Check whether a circuit breaker or timeout is already configured and whether it's actually tripping — if latency is elevated but no timeouts fire, requests may be queuing rather than failing fast.

## Remediation (ranked by risk)
| Risk | Action | Notes |
|---|---|---|
| Low | Tighten client-side timeouts so slow calls fail fast instead of holding connections/threads for the full 2-5s | Auto-executable per the risk threshold rule; trades a slow success for a fast, retriable failure |
| Low | Enable/verify a circuit breaker around the downstream call so repeated slow calls stop compounding into thread pool pressure elsewhere | Auto-executable if a circuit breaker library is already wired in |
| Medium | Add or extend caching for the slow downstream call | Needs approval — cache staleness has correctness implications |
| High | Fail over to a backup/secondary instance of the dependency, if one exists | Needs approval — failover carries its own risk (data consistency, split-brain) |

## Rollback plan
- If tightened timeouts cause an unacceptable rise in false-positive failures (dependency was actually fine, just briefly slow), loosen the timeout back toward its previous value and rely on the circuit breaker instead.
- If a failover was triggered and the secondary dependency shows problems of its own, fail back to primary once primary latency has recovered.

## References
- Google SRE Book, "Addressing Cascading Failures" and "Managing Load" — background on why latency-only degradations are dangerous and how timeouts/circuit breakers contain them.
- AWS "Timeouts, retries, and backoff with jitter" builders' library article.
