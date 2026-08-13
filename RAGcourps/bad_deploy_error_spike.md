---
failure_type: bad_deploy_error_spike   # must match FailureMode.java exactly, lowercase
service: self-healing-ops-mock-service
risk_level: high   # low | medium | high
symptoms_summary: "Sudden spike in NullPointerExceptions immediately following a deploy"
---

# Bad Deploy Error Spike

## Symptoms
- Requests to `GET /api/orders/{id}` fail with a simulated NullPointerException immediately after activation (standing in for "immediately after a deploy" in the real system) — 73% failed in a captured 30-request run (22/30), higher than this mode's original ~60% estimate; treat the estimate as approximate rather than exact.
- Structured log line (captured from a real run):
```json
{"@timestamp":"2026-08-11T15:25:32.352542+05:30","@version":"1","message":"failure_mode=BAD_DEPLOY_ERROR_SPIKE http_status=500 message=\"NullPointerException in OrderService (regression from latest deploy)\"","logger_name":"com.opsagent.mock.exception.GlobalExceptionHandler","thread_name":"http-nio-8080-exec-10","level":"ERROR","level_value":40000,"failure_mode":"BAD_DEPLOY_ERROR_SPIKE","event_type":"simulated_failure","service":"self-healing-ops-mock-service"}
```
- The defining signature is timing: error rate jumps sharply at a point in time that lines up with a deploy event, not a gradual climb like a leak or a threshold-crossing like pool/thread exhaustion.
- Error type is a code-level exception (NPE), not an infrastructure symptom (timeout, connection refused) — points squarely at the newly shipped code rather than environment or capacity.

## Likely root causes
1. A null-safety bug in the newly deployed code (missing null check on a field that's sometimes absent).
2. An incompatible change between the new code and its data contract (e.g., a field the new code expects that older data doesn't have, or a schema migration that hasn't fully propagated).
3. A missing or misconfigured dependency/config value that the new code assumes exists.
4. A partial rollout (canary or rolling deploy) where only some instances got the bad build, producing a spike rather than 100% failure.

## Diagnosis steps
1. Confirm the error onset time lines up exactly with a deploy event — check deploy/release logs against the first occurrence of the error.
2. Check whether the error rate matches the rollout percentage (e.g., if 60% of instances got the new build and 60% of requests fail, that's strong confirmation it's the new code, not something environmental).
3. Get the actual stack trace, not just the exception type — identifies exactly which code path is null-unsafe.
4. Diff the new deploy against the previous known-good version to identify the specific change most likely responsible.

## Remediation (ranked by risk)
| Risk | Action | Notes |
|---|---|---|
| Low | Halt the rollout if it's still in progress (stop promoting the bad build to more instances) | Auto-executable per the risk threshold rule; contains blast radius without touching already-affected instances |
| High | Roll back to the previous known-good deploy | Needs approval — a rollback is a production change with its own risk (e.g., losing forward-compatible data changes) |
| High | Hotfix and redeploy the specific null-check/bug fix | Needs approval — new code change under incident pressure carries elevated regression risk |

## Rollback plan
- The primary remediation *is* a rollback; if the rollback itself fails to resolve the error spike, that's a strong signal the root cause isn't actually the deploy (re-open diagnosis rather than attempting a second rollback).
- If a hotfix is deployed instead of a full rollback and it doesn't resolve the issue, fall back to the full rollback to the previous known-good build rather than iterating further hotfixes live in production.

## References
- Google SRE Book, "Release Engineering" and "Managing Incidents" — background on canary/rollout practices and why rollback is generally preferred over a live hotfix under incident pressure.
- danluu/post-mortems (GitHub) — several publicly documented incidents follow this exact "deploy → error spike → rollback" pattern; useful for calibrating response speed expectations, not reproduced here.
