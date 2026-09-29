# AWS Deployment Plan

Budget portfolio deployment for the Self-Healing Ops Agent. Target: **under
$25/month**, with an AWS Budgets alert at **$20**. This is a demo/portfolio
environment, not a multi-tenant production system — the choices below trade
availability for cost wherever the two conflict, and each trade-off is
called out so it can be defended (or revisited) in an interview.

## Architecture

```
                         ┌──────────────────────────── AWS ────────────────────────────┐
                         │                                                              │
  failure injection ───► │  ECS Fargate Spot task (mock service, public IP, no ALB)     │
  (curl /admin/...)      │        │ stdout JSON logs (awslogs driver)          ▲         │
                         │        ▼                                             │ polls   │
                         │  CloudWatch Logs ─► metric filter per failure_mode   │         │
                         │                          │                           │         │
                         │                          ▼                           │         │
                         │                  CloudWatch alarms ─► SNS topic      │         │
                         │                                          │           │         │
                         │                                          ▼           │         │
                         │  Agent Lambda (container image, outside VPC) ───► SQS remediation queue
                         │    │   │   │                                                 │
                         │    │   │   └─► DynamoDB `agent_runs` (state, idempotency)    │
                         │    │   └─────► SSM Parameter Store (secrets)                 │
                         │    │                                                          │
                         │    └─ escalation ─► SNS email ─► HMAC-signed Function URL ────┘
                         │                                  (approve / reject)          │
                         └──────────────────────────────────────────────────────────────┘
                                 │                                   │
                                 ▼                                   ▼
                     Neon free tier (Postgres + pgvector)      OpenRouter (LLM, embeddings,
                     runbook_chunks, halfvec(2048)            web search)
```

## Components and trade-offs

### Mock service — ECS Fargate Spot, public IP, no ALB
- 0.25 vCPU / 0.5–1 GB task, `desiredCount = 1`, public subnet, public IP.
- **Why Spot:** ~70% cheaper than on-demand Fargate. An interruption (2-minute
  warning, then replacement) is acceptable for a demo target — and is itself
  a realistic incident to talk about.
- **Why no ALB:** an ALB is ~$16/month on its own, most of the budget.
- **Consequence:** the task's public IP changes on every replacement, so
  nothing may depend on a stable address for it. See *Remediation path* below.
- Security group allows inbound 8080 only from your own IP (for failure
  injection). `/admin` stays unauthenticated for now; locking it down is a
  separate hardening item.

### Alerting — CloudWatch log metric filters → alarms → SNS
- The service already emits structured JSON with `failure_mode` and
  `event_type`. One metric filter per failure mode, e.g.
  `{ $.event_type = "simulated_failure" && $.failure_mode = "DISK_FULL" }`
  (OOM uses `event_type = "process_killed"`).
- One alarm per metric (threshold tuned per mode), all publishing to a
  single SNS topic via **both `alarm_actions` and `ok_actions`**. The OK
  transition is how recovery gets recorded; without `ok_actions` runs never
  show `seconds_to_recover`.
- **Why not Prometheus/Grafana:** a self-hosted metrics stack needs another
  always-on task. CloudWatch's free tier covers 10 custom metrics and 10
  alarms — exactly one per failure mode.
- **Naming contract:** each alarm's metric name is the `FailureMode` enum
  name (e.g. `DISK_FULL`). The handler maps an alarm back to a
  `failure_type` from `Trigger.MetricName`, not by parsing alarm names.
- **Gotcha:** the alarm payload carries no log lines. The agent must call
  `logs:FilterLogEvents` for the alarm window to build its `log_snippet`.

### Agent — Lambda, container image, outside a VPC
- Container image (not a zip) because `langgraph` + `openai` + `psycopg2`
  exceed comfortable zip-layer sizes and a container keeps local and cloud
  runtimes identical.
- **Why outside a VPC:** it only talks to public endpoints (Neon, OpenRouter,
  AWS APIs). Putting it in a VPC would require a NAT gateway (~$32/month) to
  reach them.
- Timeout ~5 min: diagnosis can involve a summarize-and-retry loop plus
  model fallbacks.
- **Idempotency:** SNS delivers at-least-once and Lambda retries failed async
  invocations, so each run first *claims* `run-<sha256(alarm ARN | state
  change time)>` with a conditional write. A duplicate delivery is a no-op; a
  clean failure (`error`) can be retried; a run stuck in `running` past a
  900s lease (the attempt died from a timeout or running out of memory) can be taken over. Each claim writes a
  fresh `claim_id`, and results are only recorded while it still matches, so
  an overrunning attempt can't overwrite its replacement.
- **Runtime contract** (implemented in `lambda_handler.py`):

  | Setting | Value |
  |---|---|
  | Image entry point / command | `python -m awslambdaric` / `lambda_handler.handler` |
  | `SSM_PARAMETER_PREFIX` | e.g. `/ops-agent` (holds `OPENROUTER_API`, `DATABASE_URL`, `APPROVAL_HMAC_KEY`) |
  | `RUNS_TABLE_NAME` | DynamoDB table, partition key `run_id` (S), TTL on `expires_at` |
  | `LOG_GROUP_NAME` | the mock service's log group |
  | `LOG_WINDOW_MINUTES` | log look-back before the alarm (default 5) |
  | `REMEDIATION_QUEUE_URL` | SQS queue the mock service polls |
  | `APPROVAL_TOPIC_ARN` | SNS topic with the approver's email subscription |
  | `APPROVAL_BASE_URL` | the approval Lambda's Function URL |
  | `AUTO_REMEDIATE` | `false` (default) sends every fix to approval; `true` auto-queues low/medium risk |
  | IAM | `ssm:GetParametersByPath` + `kms:Decrypt`, `dynamodb:UpdateItem`/`GetItem`/`PutItem`, `logs:FilterLogEvents`, `sqs:SendMessage`, `sns:Publish` |

### Remediation path — SQS, pulled by the mock service
- The agent never calls the mock service directly. Approved or auto-approved
  actions are written to an SQS queue; the service long-polls it
  (`RemediationExecutor`, its own thread) and applies them.
- **Recovery is judged by the metric, not the executor.** The executor only
  logs `remediation_applied`; when the alarm returns to OK, the agent handler
  stamps `recovered_at` and `seconds_to_recover` on the alarm's latest run
  (found via an `alarm#<arn>` pointer item in the same table). "The action
  ran" and "the service recovered" are different claims, and only the second
  one matters.
- **Rollout:** `AUTO_REMEDIATE=false` initially ("shadow mode"): every fix,
  even low risk, needs approval until the agent's diagnoses have earned
  trust. Flip it per environment, no redeploy of code.
- Queue: standard (not FIFO), visibility timeout 60s, dead-letter queue after
  3 receives. Duplicates are harmless because deactivating a failure mode is
  idempotent. Mock service task role: `sqs:ReceiveMessage`,
  `sqs:DeleteMessage`; env `REMEDIATION_QUEUE_URL`.
- **Why:** the service has no stable address (no ALB), and a pull model
  means the admin API never needs to be reachable from the agent at all.
- **Alternatives rejected:**
  - Resolve the task's current public IP via `ecs:DescribeTasks` at call
    time — works, but requires the admin API to be internet-reachable.
  - Add an ALB — stable address, but ~$16/month.
- **Allowlist, enforced twice:** the LLM's plan is advisory text. The
  executable action comes from a fixed table keyed on `failure_type`
  (`tools/remediation.py`), is validated before queueing, and is validated
  again by the executor against its own allowlist, all-or-nothing, rejecting
  messages older than an hour. A prompt-injected log line can change what
  the plan *says*, never what runs.

### Human approval — SNS email + HMAC-signed Lambda Function URL
- Escalated runs are stored with `approval_status = pending` and emailed via
  SNS with approve/reject links to a second Lambda (`approval_handler.py`,
  same image, command `approval_handler.handler`) behind a public Function
  URL. The email and page show the advisory plan *and* the exact action that
  will run.
- Links carry `run_id`, `decision`, `expires_at`, and an HMAC-SHA256
  signature over those fields, keyed by a secret in SSM. The handler checks
  the signature, the expiry, and that the run is still `pending_approval`
  (conditional update, so a link only works once).
- **Gotcha:** corporate mail scanners prefetch links. A `GET` must only
  render a confirmation page; the actual approval happens on a `POST` from
  that page. Otherwise a scanner can approve a remediation by itself.
- **Page hardening:** all LLM-derived text is HTML-escaped (a prompt-injected
  diagnosis must not become script in the approver's browser); CSP allows no
  scripts and only same-origin form posts; `Cache-Control: no-store`; every
  invalid link gets the same generic 403 so probes learn nothing.
- **Failure handling:** if queueing fails after approval, the run reverts to
  `pending` so the same link can be retried.
- **Why not Cognito/API Gateway:** a signed, expiring, single-use link is
  enough for a single approver and costs nothing. The trade-off: the link is
  a bearer credential, so inbox access equals approval access.
- Approval Lambda: env `RUNS_TABLE_NAME`, `REMEDIATION_QUEUE_URL`,
  `SSM_PARAMETER_PREFIX`; IAM `dynamodb:GetItem`/`UpdateItem`,
  `sqs:SendMessage`, `ssm:GetParametersByPath` + `kms:Decrypt`.

### Vector store — Neon free tier (Postgres + pgvector)
- Supports `halfvec`, so the existing `init.sql` works unchanged.
- **Why not RDS:** the smallest RDS instance is ~$12–15/month; Neon's free
  tier is $0.
- **Trade-off:** Neon scales compute to zero when idle, so the first query
  after idle pays a cold start (a few hundred ms). Acceptable for an alert
  pipeline. Connect with `sslmode=require`.

### Run state — DynamoDB `agent_runs` (on-demand)
- Partition key `run_id`; stores alert, retrieval summary, diagnosis, plan,
  decision, approval status, remediation result, timestamps.
- Doubles as the audit log and the idempotency guard. On-demand capacity is
  effectively free at this volume.

### Secrets — SSM Parameter Store (SecureString)
- `OPENROUTER_API`, `DATABASE_URL`, the HMAC approval key.
- **Why not Secrets Manager:** $0.40/secret/month with rotation this project
  doesn't need. Standard SSM parameters are free.
- The Lambda reads them at cold start and caches them for the container's
  lifetime.

### Infrastructure — Terraform
- Remote state in S3 with native lockfile locking (`use_lockfile = true`,
  Terraform ≥ 1.10). No DynamoDB lock table needed.
- Modules: `network` (VPC with public subnets only, no NAT), `ecr`, `ecs`,
  `alerting`, `agent`, `approval`, `budget`.

### CI/CD — GitHub Actions with OIDC
- An IAM role trusts GitHub's OIDC provider, scoped to this repository and
  its `main` branch. No long-lived AWS keys in GitHub secrets.
- PRs: `mvn verify`, Python unit tests, `terraform fmt` / `validate` / `plan`.
- Merges to `main`: build and push both images to ECR, `terraform apply`,
  update the Lambda image and the ECS service.

### MCP layer — one tools module, two front doors
- The capabilities (runbook search, diagnosis, run lookup, approvals, email,
  log fetch, failure injection) live in one plain Python `tools` package.
  The LangGraph nodes import it directly; an MCP server (`FastMCP`) is a thin
  adapter that exposes the same functions to MCP clients such as Claude
  Desktop and Claude Code.
- **Why not have `agentic.py` call its own tools over MCP:** every node would
  pay a protocol hop and gain a new failure mode (server down, transport
  errors) for no benefit — the graph already runs in the same process as the
  code. MCP is the interface for *external* clients; the shared module keeps
  both paths calling identical code, so there's no drift between them.
- Planned tools:

  | Tool | Kind | Notes |
  |---|---|---|
  | `search_runbooks(query, failure_type?, k)` | read | wraps `query_retrieval.retrieve` |
  | `get_runbook(failure_type)` | read | full runbook markdown |
  | `get_recent_logs(failure_type, minutes)` | read | CloudWatch `FilterLogEvents` (local: captured-logs) |
  | `diagnose_alert(failure_type, log_snippet)` | read* | runs the graph; *costs LLM calls |
  | `get_run(run_id)` / `list_runs(status?)` | read | `agent_runs` table |
  | `send_notification(run_id)` | write | SNS email for a run |
  | `list_failures` / `inject_failure(mode)` | write | mock service admin; dev/demo only |
  | `request_approval(run_id)` | write | sends the signed link, never approves |

- **Guardrail:** no MCP tool can approve a remediation. Approval stays with
  the human clicking the HMAC-signed link; otherwise any MCP client (or a
  prompt-injected log line reaching one) could bypass the human-in-the-loop.
  Write tools are opt-in via a server flag and off by default.
- **Hosting:** stdio transport locally (zero infrastructure). In AWS, a
  stateless streamable-HTTP server on a second Lambda Function URL, behind a
  bearer token from SSM. Same image as the agent, different entrypoint.

### Cost guardrail — AWS Budgets
- Monthly cost budget at $20 with email alerts at 80% actual and 100%
  forecasted.

## Estimated monthly cost

Rough, us-east-1, always-on. Check with the AWS Pricing Calculator for your
region; free-tier eligibility depends on account age.

| Item | Estimate |
|---|---|
| Fargate Spot, 0.25 vCPU / 1 GB, 24×7 | ~$3–4 |
| Public IPv4 address for the task | ~$3.60 |
| CloudWatch Logs ingest/storage (low volume, 7-day retention) | ~$0–1 |
| CloudWatch metrics + alarms (10 each) | $0 (free tier) |
| ECR storage (two images) | ~$0.20 |
| Lambda, SNS, SQS, DynamoDB on-demand | ~$0 (free tier) |
| SSM standard parameters | $0 |
| Neon free tier | $0 |
| OpenRouter free models | $0 |
| **Total** | **~$7–9** |

The largest risks to the budget are unbounded log volume (set retention and
avoid debug logging) and accidentally adding a NAT gateway or ALB.

## Build order

1. **Repo fixes** — schema, dependencies, paths, fixtures. *(done)*
2. **Containerize** *(done)* — Dockerfiles for the mock service and the agent, plus a
   local `docker compose` that runs the service, pgvector, and the agent.
3. **Tools module + agent Lambda handler** *(done)* — extract the shared `tools`
   package; SNS event → alert (via `FilterLogEvents`), `agent_runs` writes
   with idempotency, SSM config loading.
4. **Remediation + approval** *(done)* — SQS executor in the mock service with an
   action allowlist; HMAC-signed approval Function URL.
5. **MCP server** — FastMCP adapter over `tools`; stdio locally, then a
   Function URL entrypoint in the agent image.
6. **Terraform** — all infrastructure above, plus the budget.
7. **GitHub Actions** — CI on PRs, OIDC deploy on `main`.
8. **Neon** — apply `init.sql`, index the runbooks.
9. **Game day** — inject each failure mode in AWS and record alarm → diagnosis
   → approval → remediation → recovery, with timings, in the README.
