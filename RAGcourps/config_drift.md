---
failure_type: config_drift   # must match FailureMode.java exactly, lowercase
service: self-healing-ops-mock-service
risk_level: medium   # low | medium | high
symptoms_summary: "Requests failing citing a missing or invalid required config value"
---

# Config Drift

## Symptoms
- Requests to `GET /api/orders/{id}` fail citing a missing or invalid required config value — 20% failed in a captured 30-request run (6/30), lower than this mode's original ~30% estimate; treat the estimate as approximate rather than exact.
- Structured log line (captured from a real run):
```json
{"@timestamp":"2026-08-11T15:25:35.6059464+05:30","@version":"1","message":"failure_mode=CONFIG_DRIFT http_status=500 message=\"Missing or invalid required configuration: ORDERS_DB_URL\"","logger_name":"com.opsagent.mock.exception.GlobalExceptionHandler","thread_name":"http-nio-8080-exec-8","level":"ERROR","level_value":40000,"failure_mode":"CONFIG_DRIFT","event_type":"simulated_failure","service":"self-healing-ops-mock-service"}
```
- Failures are intermittent rather than total, which is a useful tell: it suggests inconsistency between instances (some have the correct config, some don't) rather than a config value that's wrong everywhere.
- No corresponding code deploy — this is a configuration-only change, which narrows the investigation away from application logic.

## Likely root causes
1. A manual config change applied to some instances/environments but not others (classic drift between what's declared in source control and what's actually running).
2. A config management tool (config server, secrets manager, environment injection step) partially failing during a rollout, so only some instances picked up the latest value.
3. An environment-specific override (staging value leaking into prod, or vice versa) that wasn't caught before rollout.
4. A config value that's technically present but has drifted out of valid range/format (e.g., a timeout set to a non-numeric string).

## Diagnosis steps
1. Diff the actual running config on affected instances against the source-of-truth (source control, config management system) to find exactly what's different and where.
2. Check whether the failure rate matches the proportion of instances with drifted config — strong confirmation if it lines up.
3. Check the config management/deployment pipeline's own logs for partial-failure events around the time drift started.
4. Confirm the config value in question is valid in format and range, not just present — "present but malformed" is a common variant of this failure.

## Remediation (ranked by risk)
| Risk | Action | Notes |
|---|---|---|
| Low | Re-sync affected instances to the source-of-truth config (re-run the config push/deployment for just the drifted value) | Auto-executable per the risk threshold rule if the source-of-truth value is already known-good |
| Medium | Restart affected instances after re-sync to ensure the new config is actually picked up | Needs approval if restart causes a brief availability dip; otherwise low |
| High | Change the actual config value itself (not just re-sync to existing source-of-truth) | Needs approval — this is a genuine production config change, not a drift correction, and needs the same review as any config change |

## Rollback plan
- If re-syncing to source-of-truth doesn't resolve the errors, the source-of-truth value itself may be wrong — revert the config management system to the last known-good state before the drift was introduced.
- If a direct config value change was made and causes new problems, revert to the previous value and treat the original drift as the lower-risk state until a proper fix is planned.

## References
- Google SRE Book, "Configuration Design and Best Practices" — background on treating config as code and why drift between declared and running state is a common incident source.
