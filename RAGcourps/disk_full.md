---
failure_type: disk_full   # must match FailureMode.java exactly, lowercase
service: self-healing-ops-mock-service
risk_level: high   # low | medium | high
symptoms_summary: "Requests failing with a write error, ~70% error rate"
---

# Disk Full

## Symptoms
- Requests to `GET /api/orders/{id}` fail with a write error — 70% failed in a captured 30-request run (21/30), higher than this mode's original ~50% estimate; treat the estimate as approximate rather than exact.
- Structured log line (captured from a real run):
```json
{"@timestamp":"2026-08-11T15:25:29.1347059+05:30","@version":"1","message":"failure_mode=DISK_FULL http_status=500 message=\"Write failed: no space left on device\"","logger_name":"com.opsagent.mock.exception.GlobalExceptionHandler","thread_name":"http-nio-8080-exec-7","level":"ERROR","level_value":40000,"failure_mode":"DISK_FULL","event_type":"simulated_failure","service":"self-healing-ops-mock-service"}
```
- Disk usage metrics (if available) show the volume near or at 100% utilization.
- Errors are specific to operations that write to disk (logs, temp files, local writes); read-only or in-memory operations are unaffected, which helps distinguish this from a general outage.

## Likely root causes
1. Log files growing unbounded — no rotation, or rotation misconfigured/broken.
2. A runaway process writing temp files or core dumps that were never cleaned up.
3. Application-level data growth (e.g., an ever-growing local cache-to-disk file, orphaned upload artifacts) that outpaced provisioned volume size.
4. A legitimate but unanticipated volume of data (batch import, large attachment uploads) exceeding capacity sized for normal traffic.

## Diagnosis steps
1. Check disk usage by directory (a `du`-style breakdown) to identify what's actually consuming space — don't assume logs without checking.
2. Check log rotation configuration and confirm it's actually running (a misconfigured rotation is a very common root cause and quick to rule in or out).
3. Look for unusually large or unusually numerous files with recent modification times — points at whatever's actively writing.
4. Check whether disk usage growth correlates with a specific deploy, feature flag, or traffic pattern change.

## Remediation (ranked by risk)
| Risk | Action | Notes |
|---|---|---|
| Low | Trigger log rotation / compress and archive old logs to free immediate space | Auto-executable per the risk threshold rule; safe, non-destructive to current data |
| Medium | Delete confirmed-safe temp files (e.g., files in a known scratch directory older than the retention window) | Needs approval — deleting the wrong thing is high-blast-radius even if the intent is safe |
| High | Delete or move application data files to free space | Needs approval — direct risk of permanent data loss if the wrong files are targeted |
| High | Expand the volume size | Needs approval — infrastructure change, may require downtime depending on platform |

## Rollback plan
- Because most remediation here is deletion, there's generally no "rollback" for data actually removed — this is why anything beyond log rotation requires explicit human approval and a second pair of eyes before executing.
- If a volume expansion doesn't resolve the issue (something is still consuming space faster than expected), treat that as a signal of an active runaway writer and re-run diagnosis rather than expanding again.

## References
- Google SRE Book, "Postmortem Culture" and "Practical Alerting" — background on why destructive remediations need a higher approval bar than restarts or config tweaks.
- AWS EBS/EFS volume monitoring and expansion documentation — standard reference for the expansion step, not reproduced here.
