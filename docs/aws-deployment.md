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
  `event_type`. One metric filter per failure mode, matching on **both**,
  e.g. `{ $.event_type = "simulated_failure" && $.failure_mode = "DISK_FULL" }`.
  Signal event types, read off `captured-logs/`: `high_latency` (slow
  downstream), `memory_growth` (memory leak), `process_killed` (OOM),
  `simulated_failure` (the other seven).
- **Why both fields:** `failure_injection` (admin API) and
  `remediation_applied` (executor) events also carry `failure_mode`. A
  filter on `failure_mode` alone would raise an alarm when a failure is
  injected and again when it's fixed.
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
- **Answer leakage:** that fetch excludes `failure_injection` events, which
  literally say "failure mode X activated", so the LLM can't read the label
  off its input. Measured afterwards with `eval_agent.py blind`: for
  retrieval the leak was harmless (one line among ~30 barely moves an
  embedding; recall@1 was 70% with it, 80% without). See the README's
  Evaluation section.

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

### Infrastructure — Terraform (`infra/`)
- Remote state in S3 with native lockfile locking (`use_lockfile = true`,
  Terraform ≥ 1.10). No DynamoDB lock table needed. `infra/bootstrap`
  creates the versioned, encrypted, private state bucket once.
- **Layout:** one root module with a file per concern (`network`, `ecr`,
  `ecs`, `alerting`, `state` for SQS/DynamoDB/SSM, `lambdas`, `budget`),
  plus one real module, `modules/container_lambda`, used three times
  (agent, approval, MCP). A module per concern would mostly be variable
  and output plumbing for a single environment; the Lambda module removes
  genuine triplication (role, log group, image config, public URL).
- **Secrets never enter state:** generated secrets (approval HMAC key, MCP
  token) are ephemeral `random_password`s written through the write-only
  `value_wo` attribute (Terraform ≥ 1.11); bump `secret_version` to
  rotate. External secrets get a placeholder and are set with
  `aws ssm put-parameter --overwrite`; `value_wo` is never read back, so
  Terraform won't revert them.
- **Public Function URLs** need two resource-policy statements:
  `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` conditioned on
  `invoked_via_function_url`. The first alone yields 403s under current
  Lambda rules.
- **Least-privilege notes:** each Lambda writes only to its own
  pre-created log group (not `AWSLambdaBasicExecutionRole`); the ECS task
  role can only receive/delete on the remediation queue; ECR lets only this
  project's functions pull. Known gap: SSM authorizes
  `GetParametersByPath` per path, so every function can read every secret
  under `/ops-agent/`.
- **Security scan (Trivy) exceptions**, each commented in place: alarms topic
  unencrypted (CloudWatch can't publish to an `aws/sns`-encrypted topic);
  AWS-managed rather than customer-managed keys for the approvals topic and
  state bucket; egress `0.0.0.0/0` narrowed to TCP 443 (AWS endpoint IPs
  aren't fixed without ~$7/month VPC endpoints). Skipped for cost: flow
  logs, X-Ray, Container Insights, CMKs on logs/ECR/DynamoDB.
- Verified with `terraform validate` (AWS provider 6.66, random 3.9) and a
  Trivy config scan; not yet applied to a real account.

### Deploying (first time)
Container Lambdas can't be created until their image exists in ECR, so the
first deploy is two applies around an image push:

```bash
cd infra/bootstrap && terraform init && terraform apply     # state bucket, once
cd .. && cp backend.hcl.example backend.hcl                  # fill in bucket
cp terraform.tfvars.example terraform.tfvars                 # fill in
terraform init -backend-config=backend.hcl
terraform apply -target=aws_ecr_repository.this              # repos only
# build and push both images tagged with image_tag (step 7 automates this)
terraform apply                                              # everything else
# then: set the two external secrets (see `terraform output set_external_secrets`),
# confirm the SNS subscription email, apply init.sql on Neon and index runbooks.
```

### CI/CD — GitHub Actions with OIDC (`.github/`)
- **No AWS keys in GitHub.** Jobs exchange a short-lived OIDC token for one
  of two roles, both defined in `infra/bootstrap` (outside the stack CI
  deploys, so CI can't edit its own permissions):

  | Role | Trusted subject | Permissions |
  |---|---|---|
  | `ops-agent-github-deploy` | `repo:trinadh132/AIops:environment:production` | Administrator. Controlled by *who* can assume it: only jobs in the `production` environment, which GitHub restricts to `main`. Next step: a permissions boundary. |
  | `ops-agent-github-plan` | `repo:trinadh132/AIops:pull_request` | Read the state file + `ec2:DescribeAvailabilityZones`. Anyone who can push a branch can run code as this role, so it's nearly empty. |

- **PR plans are `-refresh=false -lock=false`:** they diff config against
  recorded state without AWS read APIs or the lock. Safe to expose because
  the state holds no secrets (write-only attributes). The deploy job does
  the full refreshing plan.
- **`ci.yml`** (pull requests; also called by deploy): `mvn verify`, Python
  unit tests, `terraform fmt`/`validate` for both stacks, Trivy config scan
  (fails on undocumented HIGH/CRITICAL), both Docker builds, and the PR
  plan once AWS is set up.
- **`deploy.yml`** (push to `main`): full CI, then ensure ECR repos exist,
  build + push both images tagged with the commit SHA, full plan + apply,
  wait for ECS to stabilize, then smoke tests: the approval URL must
  answer 403 without a signed link and the MCP URL 401 without a token,
  which proves both functions start and their public-URL permissions work.
- **Supply chain:** every action is pinned to a full commit SHA (tag in a
  comment); Dependabot bumps actions, pip, Maven, Docker base images and
  Terraform providers weekly, grouped per ecosystem.
- **Lambda image gotcha:** builds set `provenance: false` and `sbom: false`.
  Buildx attestations make the push an image index, which Lambda rejects.
- Verified locally: actionlint clean; the Python job reproduced in a clean
  `python:3.13-slim` container (91 tests), which caught tests that only
  passed because `load_dotenv()` found a real `.env` in a parent folder.

#### One-time GitHub setup
1. `terraform -chdir=infra/bootstrap apply` → note `github_deploy_role_arn`,
   `github_plan_role_arn`, `state_bucket`.
2. Repository **variables** (Settings → Secrets and variables → Actions):
   `AWS_REGION`, `AWS_ACCOUNT_ID`, `TF_STATE_BUCKET`, `AWS_DEPLOY_ROLE_ARN`,
   `AWS_PLAN_ROLE_ARN`. Until `AWS_DEPLOY_ROLE_ARN` exists, deploy skips
   itself and CI still runs.
3. Repository **secrets**: `ALERT_EMAIL`, `BUDGET_EMAIL`, `ADMIN_CIDR`
   (secrets so they're masked in logs).
4. **Environment** `production` (Settings → Environments): deployment
   branches = `main` only; optionally a required reviewer for a manual gate.
5. Push to `main`. The first deploy creates ECR, pushes images, applies.
   Then set the two external SSM secrets and confirm the SNS email.

### MCP layer — one tools module, two front doors
- The capabilities (runbook search, diagnosis, run lookup, approvals, email,
  log fetch, failure injection) live in one plain Python `tools` package.
  The LangGraph nodes import it directly; an MCP server (`MCPServer`, SDK 2.x) is a thin
  adapter that exposes the same functions to MCP clients (desktop
  assistants, IDE agents).
- **Why not have `agentic.py` call its own tools over MCP:** every node would
  pay a protocol hop and gain a new failure mode (server down, transport
  errors) for no benefit — the graph already runs in the same process as the
  code. MCP is the interface for *external* clients; the shared module keeps
  both paths calling identical code, so there's no drift between them.
- Tools (`mcp_server.py`, MCP Python SDK 2.x `MCPServer`):

  | Tool | Kind | Backed by |
  |---|---|---|
  | `search_runbooks(query, failure_type?, k)` | read, calls embeddings | `agentic.retrieve_chunks` (same code as the graph's retrieve node) |
  | `get_runbook(failure_type)` | read | `RAGcourps/<mode>.md` |
  | `get_recent_logs(failure_type, minutes, max_lines)` | read | CloudWatch if `LOG_GROUP_NAME` set, else `captured-logs/` |
  | `diagnose_alert(failure_type, log_snippet)` | read, calls LLM | the full graph; advisory, records nothing |
  | `get_run(run_id)` / `list_runs(limit, approval_status?)` | read | `agent_runs` (`RUNS_TABLE_NAME`) |
  | `list_failures()` | read | mock service admin API (`MOCK_SERVICE_URL`) |
  | `inject_failure(failure_type)` | write, opt-in | mock service admin API; demo only, no matching "clear" |
  | `request_approval(run_id)` | write, opt-in | re-sends the signed-link email for a *pending* run |

- **Guardrails:**
  - No tool can approve or execute a remediation. Approval stays with the
    human clicking the HMAC-signed link; otherwise any MCP client (or a
    prompt-injected log line steering one) could bypass the human-in-the-loop.
  - Write tools exist only when `MCP_ENABLE_WRITE_TOOLS=true`.
  - Every `failure_type` is validated against the enum before use, so it can
    never become a path (`get_runbook` reads a file named after it).
  - Tool annotations mark read/write and open-world (LLM-calling) tools, so
    clients can prompt before the expensive or mutating ones.
- **Transports:**
  - **stdio** locally: the client launches `python mcp_server.py`. Nothing on
    the process's stdout except protocol: `query_retrieval.retrieve` logs its
    fallback warning instead of printing it for this reason.
  - **Streamable HTTP**, stateless with JSON responses:
    `python mcp_server.py --http` or `docker compose --profile mcp up -d mcp`
    (loopback port 8765). Bearer token (`MCP_AUTH_TOKEN`) required.
  - **Lambda Function URL** (AWS): same image, command `mcp_server.handler`
    (Mangum adapter), auth type NONE + bearer token from SSM
    (`/ops-agent/MCP_AUTH_TOKEN`). Two SDK behaviors handled there: the ASGI
    app is rebuilt per invocation because the SDK's session manager can only
    start once per instance; and the SDK's DNS-rebinding guard is disabled,
    since by default it only admits localhost Host headers and would reject
    the Function URL's own hostname.
- **Client setup:**
  - Any stdio MCP client: register a server whose command is
    `python /abs/path/mcp_server.py`. In the common JSON config format:
    `{"mcpServers": {"ops-agent": {"command": "python", "args": ["/abs/path/mcp_server.py"]}}}`
  - HTTP clients: `http://localhost:8765/mcp` (compose) or the Function URL
    + `/mcp`, with header `Authorization: Bearer <MCP_AUTH_TOKEN>`.
  - `.env` next to `mcp_server.py` supplies `OPENROUTER_API` / `DATABASE_URL`.

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
5. **MCP server** *(done)* — `MCPServer` (SDK 2.x) adapter over `tools`; stdio locally, then a
   Function URL entrypoint in the agent image.
6. **Terraform** *(done: validated, not yet applied)* — all infrastructure above, plus the budget.
7. **GitHub Actions** *(done: linted and reproduced locally, not yet run on GitHub)* — CI on PRs, OIDC deploy on `main`.
8. **Neon** *(done: PostgreSQL 18.6, pgvector 0.8.6, us-east-1 pooled endpoint)* — apply `init.sql`, index the runbooks.
9. **Game day** — inject each failure mode in AWS and record alarm → diagnosis
   → approval → remediation → recovery, with timings, in the README.
