# Phase 2 — Retrieval Layer

Section-chunked runbooks, embedded with `nvidia/llama-nemotron-embed-vl-1b-v2`
(2048-dim, via OpenRouter), stored in Postgres/pgvector as `halfvec(2048)`.
`failure_type` is a **hard pre-filter**: if it's known
(the normal case — your alert payloads carry it), retrieval only searches
within that failure mode's chunks, then ranks by semantic similarity. If a
filter is given but nothing's indexed for it yet, the query script falls back
to full-corpus semantic search and tells you it did, so you get a degraded
result instead of nothing — but the pre-filter stays the default behavior.

All scripts live in the repo root and are run from there.

## Layout

```
docker-compose.yml        # Postgres + pgvector
init.sql                  # schema, applied automatically on first container start
requirements.txt
.env.example
RAGcourps/*.md            # the runbook corpus, one per failure mode
eval_cases.json           # starter labeled set, one per failure mode
chunk_runbooks.py         # RAGcourps/*.md -> chunks.json (section-level)
index_runbooks.py         # chunks.json -> embedded rows in pgvector
query_retrieval.py        # manual test tool + batch recall@k eval
```

## Setup (PowerShell)

```powershell
# 1. Start Postgres + pgvector
#    init.sql only runs on a FRESH volume. If you created the volume before
#    the halfvec schema change, wipe it first: docker compose down -v
docker compose up -d

# 2. Python env
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 3. Configure secrets
Copy-Item .env.example .env
# then edit .env and paste in your OPENROUTER_API key
```

## Run the pipeline

```powershell
# Chunk the corpus
python chunk_runbooks.py --runbooks-dir RAGcourps --out chunks.json

# Embed + index (use --reset if you're re-running after schema/content changes)
python index_runbooks.py --chunks chunks.json --reset

# Ad-hoc manual test — this is the "feed in raw logs, confirm the right runbook
# comes back" step from the plan, before any LangGraph wiring
python query_retrieval.py --query "503s, no available connections in pool" --failure-type connection_pool_exhaustion

# Same query with no filter, to see how semantic-only ranks it
python query_retrieval.py --query "503s, no available connections in pool"

# Batch eval -> recall@k against eval_cases.json
python query_retrieval.py --eval eval_cases.json --k 3
```

## Notes / open decisions

- **`eval_cases.json` is a starter set**, written from failure mode names, not
  your actual captured log text. Swap in real log excerpts from
  `captured-logs/` for a meaningful recall@k number — this is also where the
  "undefined recall@k targets" open item from the plan gets resolved: run the
  eval, see where it actually lands, then decide if that's good enough before
  Phase 3.
- Chunking matches headers against `Symptoms`, `Root Causes`,
  `Diagnosis Steps`, `Remediation`, `Rollback Plan`, `References`
  (case-insensitive, trailing parentheticals ignored — e.g. "Remediation
  (Ranked by Risk Tier)" still matches). If a runbook has a header that
  doesn't match, the chunker skips it and prints a warning — check that
  output after your first run in case a section title drifted from the
  template.
- `README.md` and `TEMPLATE.md` in `RAGcourps/` are excluded from chunking automatically.
- Distance in results is cosine distance (`<=>` operator) — lower is more similar.
