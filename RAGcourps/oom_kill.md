---
failure_type: oom_kill   # must match FailureMode.java exactly, lowercase
service: self-healing-ops-mock-service
risk_level: high   # low | medium | high
symptoms_summary: "Process killed by the OS after heap usage crossed the OOM threshold"
---

# OOM Kill

## Symptoms
- The process is killed outright rather than returning errors — visible as a hard availability gap (connection refused / no response) rather than a specific HTTP error code.
- Structured log line at the kill (captured from a real run — activating `OOM_KILL` directly, not via a prior `MEMORY_LEAK` activation):
```json
{"@timestamp":"2026-08-11T15:29:42.9104072+05:30","@version":"1","message":"memory_usage_mb=1056 message=\"Simulated OOM: process killed by OS, container restarting\"","logger_name":"com.opsagent.mock.service.BackgroundSimulator","thread_name":"scheduling-1","level":"ERROR","level_value":40000,"failure_mode":"OOM_KILL","event_type":"process_killed","service":"self-healing-ops-mock-service"}
```
- This is almost always the terminal event of a Memory Leak that went unaddressed — usage climbs steadily and then crosses the threshold, at which point this event fires. Worth noting from the real capture: even when `OOM_KILL` is activated directly (not via `MEMORY_LEAK`), the growth ticks leading up to the kill are still tagged `"failure_mode":"MEMORY_LEAK"` in the log — only the final `process_killed` line switches to `"failure_mode":"OOM_KILL"`. Don't assume the `failure_mode` field is constant across an incident's timeline; anything filtering logs by that field alone will miss the growth phase of an OOM incident.
- After the kill, the process/container restarts and memory usage resets to baseline — so by the time someone investigates, the live symptom may already be gone, and only logs/metrics history shows what happened.

## Likely root causes
1. An unaddressed memory leak that was allowed to run to completion — see the Memory Leak runbook for the underlying causes.
2. A single request or batch job with a genuinely large legitimate memory requirement that exceeds the container's memory limit (not a leak, just undersized limits for a real workload).
3. A memory limit set too low for normal operation, so even healthy usage patterns occasionally cross it under peak load.
4. A sudden spike in concurrent requests each holding a non-trivial amount of memory (e.g., large response payloads), multiplying normal per-request usage beyond capacity.

## Diagnosis steps
1. Check memory usage history in the minutes leading up to the kill — a steady climb points at a leak (go to that runbook); a sudden step-up points at a legitimate burst instead.
2. Check whether this is a repeated/periodic event (kill, restart, climb, kill again) versus a one-off — repetition strongly confirms an active leak rather than a one-time burst.
3. If the container/orchestrator captured a heap dump or core dump at kill time, use it the same way as in the Memory Leak runbook to identify what was retained.
4. Check the configured memory limit against actual healthy-state usage — if healthy usage is already close to the limit, the limit itself may simply be undersized.

## Remediation (ranked by risk)
| Risk | Action | Notes |
|---|---|---|
| Low | Confirm the automatic restart succeeded and the instance is healthy again | Auto-executable per the risk threshold rule; usually already handled by the orchestrator, but confirm rather than assume |
| Medium | Raise the memory limit if healthy-state usage is genuinely close to the current limit | Needs approval — infrastructure/cost change, and can mask a real leak if applied without diagnosis |
| High | Treat as a Memory Leak incident and follow that runbook's remediation (restart cadence, cache bounding, code fix) | Needs approval — see the Memory Leak runbook for the specific risk tiers of each option |

## Rollback plan
- If raising the memory limit doesn't stop repeated kills, revert the limit change — a bigger limit alone won't fix an active leak, it just delays the same outcome.
- Follow the Memory Leak runbook's rollback plan if remediation proceeds down that path instead.

## References
- Google SRE Book, "Effective Troubleshooting" — general methodology, referenced from the Memory Leak runbook as well since these two failure modes share a root cause chain.
- Kubernetes / container orchestrator OOM-kill documentation (vendor docs) — standard reference for how OOM kill detection and container restart behavior works, not reproduced here.
