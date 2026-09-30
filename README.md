# Self-Healing Ops Agent

An AIOps agent that watches a service, diagnoses incidents with RAG over
runbooks, and fixes them: automatically when the risk is low, after a
human approves a signed link when it isn't. It then confirms the fix
worked by watching the alarm clear.

The "production system" is a Spring Boot mock service with ten injectable
failure modes (connection pool exhaustion, deadlocks, memory leak → OOM,
disk full, retry storms, ...) that emit realistic structured logs and
metrics. Everything else is built as it would be for a real service:
alarms, an event-driven agent, a remediation queue, least-privilege IAM,
Terraform, and OIDC-based CI/CD.

```mermaid
flowchart LR
    svc["Mock service<br/>(Fargate Spot)"] -- JSON logs --> cw["CloudWatch<br/>metric filters + alarms"]
    cw -- ALARM / OK --> sns[SNS]
    sns --> agent["Agent Lambda<br/>(LangGraph)"]
    agent -- retrieve --> pg[("Neon Postgres<br/>pgvector")]
    agent -- diagnose --> llm["LLM<br/>(OpenRouter)"]
    agent -- record --> ddb[("DynamoDB<br/>agent_runs")]
    agent -- "low risk" --> q[SQS]
    agent -- "needs approval" --> mail[Email with<br/>signed links]
    mail --> appr[Approval Lambda] --> q
    q -- long poll --> svc
```

## How an incident flows

1. A failure starts; the service logs `event_type=simulated_failure` lines.
2. A CloudWatch metric filter counts them; the alarm fires into SNS.
3. The agent Lambda claims the run (idempotent against duplicate delivery),
   pulls the alarm's log lines, and runs the LangGraph pipeline:
   validate → retrieve runbook sections → web search if retrieval is weak →
   diagnose and plan with the LLM → risk gate.
4. Low/medium risk with auto-remediation on: the fix goes on an SQS queue.
   Otherwise an email with approve/reject links goes out; the links are
   HMAC-signed, expire, work once, and opening one changes nothing until
   you confirm.
5. The service pulls the action from the queue, re-checks it against its own
   allowlist, and applies it.
6. When the alarm returns to OK, the run is stamped with `seconds_to_recover`.
   Recovery is judged by the metric, not by the executor saying it ran.

## Evaluation

Measured with [`eval_agent.py`](eval_agent.py) against the real pipeline
(live embeddings and LLM, pgvector), on the ten captured failure logs in
[`captured-logs/`](captured-logs/). Raw results are in
[`eval_results/`](eval_results/).

**Blind classification: can it tell *what* broke with no label?** Each log
is searched against the whole runbook corpus, no failure-type filter.

| Input the search sees | recall@1 | recall@3 | MRR |
|---|---|---|---|
| **Symptoms only** (injection lines and every `failure_mode` tag stripped) | **80%** | **100%** | **0.90** |
| As the agent receives it (tags kept) | 80% | 100% | 0.90 |
| Raw fixture (before the injection-line fix) | 70% | 100% | 0.85 |

The two misses rank second behind their nearest neighbour: OOM kill behind
memory leak, thread-pool exhaustion behind connection-pool exhaustion.

**Retrieval confidence calibration.** Symptoms-only top similarities span
0.715–0.774 and tagged inputs 0.743–0.813. The agent's web-search threshold
(`RETRIEVAL_CONFIDENCE_THRESHOLD = 0.72`) sits inside the first range, so
on label-free input it would trigger web search for about a third of
incidents even when retrieval ranked the right runbook first. It needs
re-deriving from these numbers.

**End to end: is the diagnosis right and the risk call safe?** The full
graph per failure mode, retrieving from Neon, diagnosing with a free
OpenRouter model. Scored against each runbook: root cause on topic
(keyword rubric from "Likely root causes"), LLM risk vs runbook risk, and
whether the agent escalated exactly when the runbook says high risk.

| Failure mode | Runbook risk | LLM risk | Floored risk | Decision | Root cause on topic |
|---|---|---|---|---|---|
| BAD_DEPLOY_ERROR_SPIKE | high | medium ⚠ | high | escalate | ✓ |
| CONFIG_DRIFT | medium | low ⚠ | medium | auto | ✓ |
| CONNECTION_POOL_EXHAUSTION | low | high | high | escalate (over) | ✓ |
| DB_DEADLOCK | medium | high | high | escalate (over) | ✓ |
| DISK_FULL | high | low ⚠ | high | escalate | ✓ |
| MEMORY_LEAK | medium | medium | medium | auto | ✓ |
| OOM_KILL | high | high | high | escalate | ✓ |
| RETRY_STORM | medium | high | high | escalate (over) | ✓ |
| SLOW_DOWNSTREAM_DEPENDENCY | low | low | low | auto | ✓ |
| THREAD_POOL_EXHAUSTION | low | low | low | auto | ✓ |

- **Diagnosis:** 10/10 root causes on topic, 10/10 cite the runbook.
- **Risk rating is the weak point:** the LLM matched the runbook 4/10,
  rated lower 3 times (⚠), higher 3 times. Acting on its own rating, **2 of
  the 3 high-risk incidents** (bad deploy, disk full) would have skipped
  human approval.
- **Fix: the risk gate floors the LLM at the runbook's level** (`max` of
  the two; a malformed rating counts as high). With the floor: **0 unsafe
  decisions**, 7/10 exactly per policy, 3 over-escalations (costs a human
  click, not safety). Disk full was re-run after the change and escalated
  despite the LLM calling it low.
- **Latency:** median ~71 s, 26–228 s on free-tier models. Retrieval
  confidence 0.72–0.81 on agent input; web search never triggered.

The same run exposed three robustness bugs, all fixed with tests: a Neon
connection dropped during the TLS handshake (connects now retry), an
upstream provider error returned an empty completion that crashed with
`'NoneType' object is not subscriptable` (now retried once, then a clear
error), and a rate limit in the summarize-and-retry step raised out of the
graph instead of ending in `diagnosis_failed`.

Raw data: `eval_results/20260930T054713Z-e2e.json` (all ten, before the
floor and fixes) and `20260930T055417Z-e2e.json` (the two re-runs after).
Floored risk for the first eight is `max(LLM, runbook)` applied to the
recorded ratings, the same rule the gate now runs.

**What these numbers don't show:** ten fixtures, one run each; free-tier
models vary between runs; the on-topic check is a keyword rubric (catches
answers about the wrong failure, not subtle reasoning errors). In
production the alarm's metric name already tells the agent the failure
type, so the end-to-end score is about root cause and risk, not
classification.

### Deployed on AWS

`DISK_FULL` injected into the Fargate Spot service under steady traffic,
full loop in production, one human approval by email. Run 2 is after the
alarm fix described below.

| Phase | Run 1 | Run 2 |
|---|---|---|
| Failure starts → CloudWatch alarm | 2 min 02 s | 1 min 17 s |
| Alarm → agent run claimed | 3 s | 3 s |
| Diagnosis (Neon retrieval + LLM + risk gate) | 1 min 21 s | 59 s |
| Waiting for the human to approve | 1 min 31 s | 22 s |
| Approval → fix applied by the service (SQS) | < 1 s | < 1 s |
| **Fix applied → alarm back to OK** | **7 min 05 s** | **1 min 36 s** |
| **Recorded `seconds_to_recover`** (alarm raised → OK) | **600 s** | **180 s** |

- Both times the model rated the incident **low** risk; the runbook floor
  made it **high**, so it waited for approval instead of auto-remediating.
- Root cause both times: log growth without working rotation filling the
  disk (on topic).
- **Run 1: 71% of recovery time was CloudWatch noticing, not the system
  fixing.** A log-derived metric stops producing datapoints once errors
  stop, and a `notBreaching` alarm keeps judging by its last real
  (breaching) datapoint until it ages out. Fix: `default_value = "0"` on
  the metric filters, so healthy traffic emits real zeros. **Fix → OK went
  from 425 s to 96 s (−77%).** The rest of the drop is a faster approval
  click and LLM latency variance, not the fix.
- Detection time depends on where in the 1-minute metric period the
  failure starts, so it varies run to run.

### Honesty fixes the evaluation drove
- **The agent could read the answer off its input.** The admin API logs
  "failure mode X activated", and that line reached the agent. It's now
  filtered out everywhere the agent gets logs. (For retrieval this turned
  out harmless: one line among ~30 barely moves an embedding.)
- **The mock tags every log line with the answer** (`failure_mode` field),
  which real services don't. The blind test strips it; tags raise
  similarity by 0.02–0.06 but never changed a ranking.
- **Tests only passed because a real `.env` was reachable** from a parent
  folder. They now set a dummy key and pass on a clean CI runner.

## Quick start (local)

Requirements: Docker, Java 17 + Maven 3.9, Python 3.12+.

```bash
cp .env.example .env                 # add OPENROUTER_API
docker compose up -d --build         # mock service :8080 + pgvector :5432
docker compose run --rm agent python index_runbooks.py --reset
```

Break something and diagnose it:

```bash
curl -X POST localhost:8080/admin/failures/disk_full/activate
curl localhost:8080/api/orders/1     # ~50% fail with a write error
docker compose run --rm agent python run_real_alert.py \
    --failure-type DISK_FULL --log-file captured-logs/disk_full.log
curl -X POST localhost:8080/admin/failures/reset
```

Evaluate:

```bash
docker compose run --rm agent python eval_agent.py blind   # symptoms only
docker compose run --rm agent python eval_agent.py e2e
```

## The failure modes

| Mode | Signal the alarm counts | Runbook risk |
|---|---|---|
| `CONNECTION_POOL_EXHAUSTION` | `simulated_failure` (503s) | low |
| `DB_DEADLOCK` | `simulated_failure` (lock timeouts) | medium |
| `SLOW_DOWNSTREAM_DEPENDENCY` | `high_latency` (2–5 s requests) | low |
| `MEMORY_LEAK` | `memory_growth` (+40 MB / 5 s) | medium |
| `OOM_KILL` | `process_killed` | high |
| `DISK_FULL` | `simulated_failure` (write errors) | high |
| `THREAD_POOL_EXHAUSTION` | `simulated_failure` (>20 in flight) | low |
| `BAD_DEPLOY_ERROR_SPIKE` | `simulated_failure` (NPEs) | high |
| `CONFIG_DRIFT` | `simulated_failure` (missing env var) | medium |
| `RETRY_STORM` | `simulated_failure` (>50 req / 10 s) | medium |

Inject with `POST /admin/failures/{mode}/activate`; list with
`GET /admin/failures`; clear with `POST /admin/failures/reset`. Metrics at
`/actuator/prometheus`. Runbooks: [`RAGcourps/`](RAGcourps/).

## Design decisions worth asking about

- **The LLM never chooses what executes.** Its plan is advisory text; the
  executable action comes from a fixed table keyed on failure type, is
  validated before queueing, and validated again by the service against its
  own allowlist. A prompt-injected log line can change what the plan says,
  not what runs.
- **The LLM's risk rating is only allowed to raise caution.** The gate uses
  `max(LLM risk, runbook risk)`, because the evaluation showed the model
  under-rating 3 of 10 incidents, two of them high risk.
- **Shadow mode by default.** `AUTO_REMEDIATE=false` sends every fix,
  even low risk, to a human until the diagnoses have earned trust.
- **Idempotency under at-least-once delivery.** Runs are claimed with a
  conditional write plus a lease and an ownership token: duplicates are
  no-ops, crashed runs can be taken over, a stale attempt can't overwrite
  its replacement.
- **Approval links are bearer credentials,** so: HMAC-signed, expiring,
  single use (conditional write), GET only renders a confirmation page
  (mail scanners prefetch links), all LLM text HTML-escaped on the page.
- **Pull, not push, for remediation.** The service polls SQS, so it needs
  no stable address, no load balancer (~$16/month saved), and its admin
  API is never reachable by the agent.
- **Secrets never enter Terraform state** (ephemeral values via write-only
  attributes), which is also why the pull-request CI role can read state
  safely.
- **One tools layer, two front doors.** The LangGraph agent and the
  [MCP server](mcp_server.py) call the same functions; no MCP tool can
  approve or execute a fix.

Full rationale, alternatives rejected and costs:
[`docs/aws-deployment.md`](docs/aws-deployment.md).

## Deployment (AWS, ~$7–9/month)

Fargate Spot, CloudWatch alarms, three container Lambdas (agent, approval,
MCP), SQS, DynamoDB, SSM, Neon's free Postgres, and a $20 budget alarm, all
in Terraform ([`infra/`](infra/)). GitHub Actions deploys on push to `main`
using OIDC (no stored AWS keys), with every action pinned to a commit SHA.
Setup steps: [`docs/aws-deployment.md`](docs/aws-deployment.md#deploying-first-time).

## MCP server

Exposes runbook search, logs, diagnosis and run history to any MCP client
over stdio (`python mcp_server.py`), HTTP with a bearer token
(`docker compose --profile mcp up -d mcp`), or a Lambda Function URL. Write
tools are opt-in (`MCP_ENABLE_WRITE_TOOLS=true`).

## Project layout

| Path | What |
|---|---|
| `src/` | Mock service (Spring Boot): failure injection, SQS remediation executor |
| `agentic.py`, `llm.py`, `searchllm.py` | LangGraph agent, LLM client, web search |
| `lambda_handler.py`, `approval_handler.py`, `mcp_server.py` | The three Lambda entrypoints |
| `tools/` | Shared layer: config, logs, runs store, remediation, approval, runbooks |
| `chunk_runbooks.py`, `index_runbooks.py`, `query_retrieval.py`, `init.sql` | RAG pipeline |
| `RAGcourps/` | Runbooks, one per failure mode |
| `captured-logs/`, `capture-logs.ps1` | Real log fixtures and the script that captures them |
| `eval_agent.py`, `eval_results/` | Evaluation harness and results |
| `infra/` | Terraform (+ `bootstrap/` for state bucket and CI roles) |
| `.github/` | CI and deploy workflows, Dependabot |
| `test_*.py`, `unittes.py`, `src/test/` | 103 Python + 8 Java tests; no network or AWS account needed |

## Tests

```bash
mvn verify
pip install -r requirements-dev.txt
python -m unittest test_eval_agent test_mcp_server test_remediation test_lambda_handler unittes
```

AWS is emulated in-memory (moto) and every LLM/embedding call is stubbed, so
the suite runs offline and on forks.

## Known limitations

- Remediation is limited to what the mock can do (clearing a failure mode);
  a real deployment would add restart, scale-out and rollback actions to the
  allowlist.
- The evaluation is ten fixtures, one run each; LLM risk ratings in
  particular vary between runs. Repeats (`--repeats 3`) need more than the
  free tier's 50 requests/day.
- Over-escalation: with the risk floor, 3 of 10 incidents asked for approval
  the runbook says they didn't need. Safe, but it's human toil.
- One scenario so far (disk full); the other nine failure modes
  are verified locally and in the evaluation, not yet on AWS.
