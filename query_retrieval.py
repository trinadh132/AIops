"""
Manual retrieval test tool: feed in raw alert/log text, get back the runbook
chunks retrieval would surface. This is the Phase 2 "manually test retrieval"
step — run this BEFORE wiring anything into LangGraph in Phase 3.

Retrieval strategy: failure_type is a HARD pre-filter when provided (matches
the Phase 1 plan: exact-match filtering on top of semantic search). If a
failure_type is given but returns nothing indexed, the script automatically
falls back to a full-corpus semantic search and flags it — so you see a
degraded result instead of a silent empty list, without abandoning the
pre-filter as the primary strategy.

Usage:
    # Single ad-hoc query
    python query_retrieval.py --query "503 errors, no available connections in pool" \\
        --failure-type connection_pool_exhaustion

    # Semantic-only, no filter (useful when you don't know the failure_type yet,
    # e.g. simulating a real alert pipeline)
    python query_retrieval.py --query "orders API returning 503s under load"

    # Batch eval against labeled cases -> simple recall@k
    python query_retrieval.py --eval eval_cases.json --k 3
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from openai import OpenAI
from pgvector import HalfVector
from pgvector.psycopg2 import register_vector

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Must match the model used in index_runbooks.py — query and corpus embeddings
# have to come from the same model or the distances are meaningless.
EMBEDDING_MODEL = "nvidia/llama-nemotron-embed-vl-1b-v2:free"

logger = logging.getLogger("self_healing_ops.retrieval")

load_dotenv()
def get_db_connection():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL not set. Copy .env.example to .env and fill it in.")
    conn = psycopg2.connect(dsn)
    register_vector(conn)
    return conn


def embed_query(client: OpenAI, text: str) -> list[float]:
    # See index_runbooks.py — this model requires encoding_format="float"
    # explicitly; the openai SDK's default (base64) isn't supported here.
    response = client.embeddings.create(model=EMBEDDING_MODEL, input=[text], encoding_format="float")
    return response.data[0].embedding


def retrieve(cur, query_embedding, failure_type: str | None, k: int):
    """Returns (rows, used_fallback). Hard pre-filter on failure_type when given."""
    # Wrap in HalfVector(...) — a bare Python list gets adapted by psycopg2 as a
    # generic Postgres array (numeric[]), which the <=> operator can't compare
    # against a `halfvec` column. HalfVector(...) forces the correct adaptation.
    query_embedding = HalfVector(query_embedding)

    if failure_type:
        cur.execute(
            """
            SELECT failure_type, section, source_file, content, symptoms_summary,
                   embedding <=> %s AS distance
            FROM runbook_chunks
            WHERE failure_type = %s
            ORDER BY distance
            LIMIT %s;
            """,
            (query_embedding, failure_type, k),
        )
        rows = cur.fetchall()
        if rows:
            return rows, False
        # Logged, not printed: this function also runs inside the MCP stdio
        # server, where stdout is the protocol stream.
        logger.warning("No indexed chunks for failure_type='%s' — falling back to "
                       "full-corpus semantic search.", failure_type)

    cur.execute(
        """
        SELECT failure_type, section, source_file, content, symptoms_summary,
               embedding <=> %s AS distance
        FROM runbook_chunks
        ORDER BY distance
        LIMIT %s;
        """,
        (query_embedding, k),
    )
    return cur.fetchall(), bool(failure_type)


def print_results(rows, used_fallback: bool):
    if used_fallback:
        print("  (fallback: semantic search across full corpus, no failure_type filter)")
    for i, (ftype, section, source_file, content, symptoms_summary, distance) in enumerate(rows, 1):
        snippet = content.strip().replace("\n", " ")
        snippet = snippet[:160] + ("..." if len(snippet) > 160 else "")
        print(f"  {i}. [{ftype} / {section}] (distance={distance:.4f}) {source_file}")
        print(f"     {snippet}")


def run_single_query(client, cur, query: str, failure_type: str | None, k: int):
    print(f"\nQuery: {query!r}")
    if failure_type:
        print(f"Filter: failure_type={failure_type}")
    embedding = embed_query(client, query)
    rows, used_fallback = retrieve(cur, embedding, failure_type, k)
    if not rows:
        print("  No results.")
        return
    print_results(rows, used_fallback)


def run_eval(client, cur, eval_path: Path, k: int):
    """
    eval_cases.json format:
    [
      {"query": "503s with no available connections in pool", "expected_failure_type": "connection_pool_exhaustion"},
      ...
    ]
    Reports recall@k treating a hit as: expected_failure_type appears among
    the top-k results' failure_type values (with no failure_type filter applied,
    since this simulates NOT knowing the answer ahead of time).
    """
    cases = json.loads(eval_path.read_text(encoding="utf-8"))
    hits = 0

    for case in cases:
        query = case["query"]
        expected = case["expected_failure_type"]
        embedding = embed_query(client, query)
        rows, _ = retrieve(cur, embedding, failure_type=None, k=k)
        retrieved_types = [r[0] for r in rows]
        hit = expected in retrieved_types
        hits += hit
        status = "HIT " if hit else "MISS"
        print(f"[{status}] expected={expected:35s} got={retrieved_types}")
        print(f"        query: {query!r}")

    total = len(cases)
    recall = hits / total if total else 0.0
    print(f"\nrecall@{k}: {hits}/{total} = {recall:.2%}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", help="Ad-hoc query text")
    parser.add_argument("--failure-type", help="Exact-match pre-filter (optional)")
    parser.add_argument("--eval", help="Path to eval_cases.json for batch recall@k")
    parser.add_argument("--k", type=int, default=3)
    args = parser.parse_args()

    if not args.query and not args.eval:
        sys.exit("Provide either --query or --eval.")

    load_dotenv()
    api_key = os.environ.get("OPENROUTER_API")
    if not api_key:
        sys.exit("OPENROUTER_API not set. Copy .env.example to .env and fill it in.")

    client = OpenAI(
        api_key=api_key,
        base_url=OPENROUTER_BASE_URL,
        default_headers={
            "HTTP-Referer": "https://github.com/self-healing-ops-agent",
            "X-Title": "self-healing-ops-agent",
        },
    )
    conn = get_db_connection()
    cur = conn.cursor()

    if args.eval:
        run_eval(client, cur, Path(args.eval), args.k)
    else:
        run_single_query(client, cur, args.query, args.failure_type, args.k)

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()