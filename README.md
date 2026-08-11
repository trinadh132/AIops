# self-healing-ops-mock-service

Phase 0 of the Self-Healing Ops Agent project: a toy Spring Boot service that
stands in for "the production system" the agent will eventually diagnose and
fix. It exposes an admin surface to inject failure modes on demand, and a
business endpoint whose behavior degrades according to whichever modes are
active — with structured logs and Prometheus metrics so there's something
real to alert on.

This maps directly to the "Failure Taxonomy" sheet in
`self_healing_ops_agent_plan.xlsx`. If you add a failure mode to one, add it
to the other.

## Requirements

- Java 17+
- Maven 3.9+ (not bundled here — this was written and reviewed for syntax in
  a sandboxed environment without access to Maven Central, so **run `mvn
  clean verify` locally before you build on top of it** to confirm it
  compiles and the smoke test passes against your own network)

## Run it

```bash
mvn spring-boot:run
```

The service starts on `http://localhost:8080`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/orders/{id}` | "Real" traffic — behaves normally unless a failure mode is active |
| GET | `/admin/failures` | List currently active failure modes + simulated memory/in-flight state |
| POST | `/admin/failures/{mode}/activate` | Turn a failure mode on |
| POST | `/admin/failures/{mode}/deactivate` | Turn a failure mode off |
| POST | `/admin/failures/reset` | Clear all active failure modes |
| GET | `/actuator/health` | Standard health check |
| GET | `/actuator/prometheus` | Prometheus-format metrics |

`{mode}` is case-insensitive and matches the `FailureMode` enum:
`connection_pool_exhaustion`, `db_deadlock`, `slow_downstream_dependency`,
`memory_leak`, `disk_full`, `thread_pool_exhaustion`,
`bad_deploy_error_spike`, `config_drift`, `retry_storm`, `oom_kill`.

## Trying each failure mode

```bash
# Connection pool exhaustion — ~70% of requests fail with 503
curl -X POST localhost:8080/admin/failures/connection_pool_exhaustion/activate
curl localhost:8080/api/orders/123
curl -X POST localhost:8080/admin/failures/connection_pool_exhaustion/deactivate

# DB deadlock — ~40% of requests fail with a lock-wait timeout
curl -X POST localhost:8080/admin/failures/db_deadlock/activate

# Slow downstream dependency — every request takes 2-5s and logs a warning
curl -X POST localhost:8080/admin/failures/slow_downstream_dependency/activate

# Memory leak -> OOM kill — simulated heap grows 40MB every 5s until it
# crosses 1024MB, then logs a process_killed event and self-clears
curl -X POST localhost:8080/admin/failures/memory_leak/activate

# Disk full — ~50% of requests fail with a write error
curl -X POST localhost:8080/admin/failures/disk_full/activate

# Thread pool exhaustion — fires once concurrent in-flight requests exceed 20
# (send concurrent load, e.g. `hey -n 200 -c 40 http://localhost:8080/api/orders/1`)
curl -X POST localhost:8080/admin/failures/thread_pool_exhaustion/activate

# Bad deploy error spike — ~60% of requests fail with a simulated NPE
curl -X POST localhost:8080/admin/failures/bad_deploy_error_spike/activate

# Config drift — ~30% of requests fail citing a missing/invalid env var
curl -X POST localhost:8080/admin/failures/config_drift/activate

# Retry storm — fails once request rate exceeds 50 in a 10s window
curl -X POST localhost:8080/admin/failures/retry_storm/activate

# Clear everything
curl -X POST localhost:8080/admin/failures/reset
```

## What the logs look like

Every simulated failure logs a structured JSON line via
`logstash-logback-encoder`, tagged with `failure_mode` and `event_type` via
MDC, e.g.:

```json
{
  "message": "failure_mode=CONNECTION_POOL_EXHAUSTION http_status=503 message=\"No available connections in pool\"",
  "failure_mode": "CONNECTION_POOL_EXHAUSTION",
  "event_type": "simulated_failure",
  "service": "self-healing-ops-mock-service",
  "level": "ERROR"
}
```

This is the shape Phase 1 (RAG corpus / runbooks) and Phase 2 (retrieval)
will eventually consume — a runbook per `failure_mode`, retrieved by
matching against log lines like this one.

## What's next

- **Phase 0, Step 1** (define failure taxonomy) is effectively done — the 10
  modes above match the Failure Taxonomy sheet.
- **Phase 1**: write one runbook per failure mode (symptoms, root cause,
  diagnosis steps, remediation, rollback), using the exact log shape above as
  the "symptoms" section.
- If you add or rename a failure mode, update `FailureMode.java` **and** the
  Failure Taxonomy sheet together so the corpus and the code never drift
  apart.
