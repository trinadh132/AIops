---
failure_type: db_deadlock   # must match FailureMode.java exactly, lowercase
service: self-healing-ops-mock-service
risk_level: medium   # low | medium | high
symptoms_summary: "Requests failing with a database lock-wait timeout"
---

# DB Deadlock

## Symptoms
- Requests to `GET /api/orders/{id}` fail with a lock-wait timeout error — 63% failed in a captured 30-request run (19/30), noticeably higher than this mode's original ~40% estimate; treat the estimate as approximate rather than exact.
- Structured log line (captured from a real run):
```json
{"@timestamp":"2026-08-11T15:25:06.2447283+05:30","@version":"1","message":"failure_mode=DB_DEADLOCK http_status=500 message=\"Lock wait timeout exceeded on orders table\"","logger_name":"com.opsagent.mock.exception.GlobalExceptionHandler","thread_name":"http-nio-8080-exec-7","level":"ERROR","level_value":40000,"failure_mode":"DB_DEADLOCK","event_type":"simulated_failure","service":"self-healing-ops-mock-service"}
```
- Failure rate is roughly constant rather than climbing — distinguishes this from pool exhaustion or a leak.
- No corresponding disk, memory, or CPU pressure on the DB host; the DB itself is healthy, transactions are just blocking each other.

## Likely root causes
1. Two or more transactions acquiring the same rows/tables in inconsistent order, so the DB's deadlock detector periodically has to kill one to unblock the other.
2. A long-running transaction (batch job, report query, ad-hoc admin query) holding locks far longer than the request path expects.
3. A recent schema or index change that altered lock granularity (e.g., a dropped index forcing a table-level lock scan instead of row-level).
4. Increased write concurrency on a hot row (e.g., many requests updating the same order or inventory counter simultaneously).

## Diagnosis steps
1. Pull the DB's deadlock log (e.g. `SHOW ENGINE INNODB STATUS` or equivalent) to see the exact statements and lock order involved, not just the app-side timeout.
2. Check whether the transactions involved touch the same tables in a different order — the most common structural cause, usually visible directly in the deadlock graph.
3. Look for any long-running or idle-in-transaction sessions active during the incident window; a single stuck transaction can be the common denominator across many otherwise-unrelated deadlocks.
4. Check recent schema/migration history and index changes against the incident start time.
5. Check write volume on the specific rows involved (e.g., a single popular order or shared counter) to see if this is hot-row contention rather than a general deadlock pattern.

## Remediation (ranked by risk)
| Risk | Action | Notes |
|---|---|---|
| Low | Let existing automatic retry-with-backoff on deadlock-killed transactions absorb the errors | Auto-executable; deadlocks are expected to be transient and self-resolving when retry logic exists |
| Medium | Kill a specific long-running or blocking session identified in diagnosis | Needs approval — killing the wrong session can cause data loss or a bigger outage |
| Medium | Add a missing index or adjust query patterns to reduce lock scope | Needs approval — schema changes need review even when low-risk in principle |
| High | Reorder transaction logic in application code to enforce consistent lock acquisition order | Needs approval — code change, requires testing before deploy |

## Rollback plan
- If killing a session doesn't resolve the deadlock rate, the session wasn't the root cause — re-diagnose rather than killing further sessions speculatively.
- If a schema/index change increases load or causes lock contention elsewhere, revert the migration and restore the previous index configuration.

## References
- Google SRE Book, "Testing for Reliability" and dependency-management sections — general framing for reasoning about compounding failure interactions like lock contention.
- MySQL/PostgreSQL deadlock documentation (vendor docs) — standard reference for reading deadlock graphs, not reproduced here.
