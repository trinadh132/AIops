---
failure_type: memory_leak   # must match FailureMode.java exactly, lowercase
service: self-healing-ops-mock-service
risk_level: medium   # low | medium | high
symptoms_summary: "Heap usage climbing steadily toward the OOM threshold"
---

# Memory Leak

## Symptoms
- Simulated heap usage climbs by ~40MB every ~5 seconds with no corresponding drop, rather than the normal sawtooth pattern of allocation followed by GC reclaiming memory. A captured run climbed from 296MB to 1056MB over exactly 21 growth ticks (~105s) before crossing the 1024MB threshold.
- Structured log line during growth (captured from a real run):
```json
{"@timestamp":"2026-08-11T15:25:47.9098864+05:30","@version":"1","message":"memory_usage_mb=296 threshold_mb=1024 message=\"Simulated heap usage growing\"","logger_name":"com.opsagent.mock.service.BackgroundSimulator","thread_name":"scheduling-1","level":"WARN","level_value":30000,"failure_mode":"MEMORY_LEAK","event_type":"memory_growth","service":"self-healing-ops-mock-service"}
```
- If left unaddressed, usage eventually crosses the configured threshold and escalates into an OOM_KILL event — see that runbook for the terminal failure. Worth noting: the terminal event itself is tagged `"failure_mode":"OOM_KILL"` even though this incident started as a `MEMORY_LEAK` — don't assume the `failure_mode` field stays constant across an incident's full timeline:
```json
{"@timestamp":"2026-08-11T15:27:22.9143141+05:30","@version":"1","message":"memory_usage_mb=1056 message=\"Simulated OOM: process killed by OS, container restarting\"","logger_name":"com.opsagent.mock.service.BackgroundSimulator","thread_name":"scheduling-1","level":"ERROR","level_value":40000,"failure_mode":"OOM_KILL","event_type":"process_killed","service":"self-healing-ops-mock-service"}
```
- The `ops_mock_active_failure_modes` gauge stays elevated the entire time the leak is active, unlike request-driven failure modes that only show up under load.

## Likely root causes
1. An unbounded in-memory cache or collection (e.g., a `Map` that's appended to but never evicted or cleared).
2. Listener/callback registration without corresponding deregistration, keeping otherwise-dead objects reachable.
3. A third-party library with a known leak (large object graphs held via static fields, thread-local values never cleared).
4. Increased request volume exposing an existing small per-request leak that was previously too slow to matter.

## Diagnosis steps
1. Confirm the growth pattern is monotonic (only up, no GC-driven drops) rather than a normal high-but-stable working set — a heap that's merely large but stable is not a leak.
2. Pull heap dumps at two points in time and diff the retained object counts/sizes — the object type with disproportionate growth is almost always the leak source.
3. Check for recent deploys that introduced new caching, new listener registrations, or a new dependency version around the time growth started.
4. Correlate growth rate against request volume — a leak that scales with request count points at a per-request object being retained; a leak that grows even with zero traffic points at a background job or scheduled task.

## Remediation (ranked by risk)
| Risk | Action | Notes |
|---|---|---|
| Low | Restart the affected instance(s) before the threshold is crossed, to avoid an uncontrolled OOM kill | Auto-executable per the risk threshold rule; buys time, does not fix root cause |
| Medium | Add a size bound / eviction policy (e.g., LRU with a max size) to the suspected unbounded cache | Needs approval — behavior change to caching semantics |
| Medium | Roll back to the previous deploy if growth correlates with a recent release | Needs approval per the general deploy-rollback policy |
| High | Patch the leaking code path (fix listener cleanup, bound collection growth) and redeploy | Needs approval — code change carries regression risk |

## Rollback plan
- If a preventive restart doesn't stop the leak from recurring shortly after, that confirms the leak is active-traffic-driven rather than one-off — escalate to root-cause investigation rather than repeated restarts.
- If a rollback to a previous deploy resolves the growth, keep the rollback in place and re-attempt the newer release only after the leak is fixed and verified under load.

## References
- Google SRE Book, "Effective Troubleshooting" — general methodology for progressive-degradation issues like leaks.
- JVM heap dump analysis practices (Eclipse MAT / VisualVM documentation) — standard tooling reference, not reproduced here.
