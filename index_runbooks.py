"""
Embed chunks.json (from chunk_runbooks.py) via OpenRouter's unified embeddings
endpoint and upsert into the runbook_chunks table in pgvector.

OpenRouter exposes embeddings through an OpenAI-compatible API at a different
base_url, so we still use the `openai` Python SDK — just pointed elsewhere,
with model names prefixed by provider (e.g. "openai/text-embedding-3-small").

Requires OPENROUTER_API_KEY and DATABASE_URL in a .env file (see .env.example).

Usage (from self-healing-ops-retrieval/):
    python scripts/index_runbooks.py --chunks chunks.json
    python scripts/index_runbooks.py --chunks chunks.json --reset   # wipe table first
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from openai import OpenAI
from pgvector import Vector
from pgvector.psycopg2 import register_vector

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# nvidia/llama-nemotron-embed-vl-1b-v2:free outputs 2048-dim embeddings, NOT
# 1536 like OpenAI's text-embedding-3-small. EMBEDDING_DIM here must match
# the vector(...) column type in sql/init.sql exactly, or inserts will fail.
EMBEDDING_MODEL = "nvidia/llama-nemotron-embed-vl-1b-v2:free"
EMBEDDING_DIM = 2048
# Free-tier models on OpenRouter have tighter rate limits than paid ones, and
# this model is served via vLLM as a VLM-based encoder rather than OpenAI's
# batch-friendly embeddings backend — small batches are safer here than the
# 20-at-a-time batching that worked fine with text-embedding-3-small.
BATCH_SIZE = 5
REQUEST_DELAY_SECONDS = 1.0  # pause between batches to stay under free-tier rate limits


def get_db_connection():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL not set. Copy .env.example to .env and fill it in.")
    conn = psycopg2.connect(dsn)
    register_vector(conn)
    return conn


def embed_batch(client: OpenAI, texts: list[str]) -> list[list[float]]:
    # encoding_format="float" is required here: the openai SDK defaults to
    # requesting base64-encoded embeddings for efficiency, but this model
    # (served via OpenRouter) only supports float, not base64.
    response = client.embeddings.create(model=EMBEDDING_MODEL, input=texts, encoding_format="float")
    return [item.embedding for item in response.data]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks", default="chunks.json")
    parser.add_argument("--reset", action="store_true", help="Delete all rows before indexing")
    args = parser.parse_args()

    load_dotenv()

    api_key = os.environ.get("OPENROUTER_API")
    if not api_key:
        sys.exit("OPENROUTER_API_KEY not set. Copy .env.example to .env and fill it in.")

    chunks_path = Path(args.chunks)
    if not chunks_path.exists():
        sys.exit(f"{chunks_path} not found. Run chunk_runbooks.py first.")

    chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
    print(f"Loaded {len(chunks)} chunks from {chunks_path}")

    client = OpenAI(
        api_key=api_key,
        base_url=OPENROUTER_BASE_URL,
        # Optional but recommended by OpenRouter for usage attribution/rankings.
        default_headers={
            "HTTP-Referer": "https://github.com/self-healing-ops-agent",
            "X-Title": "self-healing-ops-agent",
        },
    )
    conn = get_db_connection()
    cur = conn.cursor()

    if args.reset:
        print("Resetting runbook_chunks table...")
        cur.execute("TRUNCATE TABLE runbook_chunks RESTART IDENTITY;")
        conn.commit()

    inserted = 0
    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        texts = [c["content"] for c in batch]

        # Small retry loop — embedding calls occasionally hiccup on rate limits.
        for attempt in range(3):
            try:
                embeddings = embed_batch(client, texts)
                break
            except Exception as e:
                if attempt == 2:
                    raise
                print(f"  retrying batch {i}//{BATCH_SIZE} after error: {e}")
                time.sleep(2 * (attempt + 1))

        for chunk, embedding in zip(batch, embeddings):
            cur.execute(
                """
                INSERT INTO runbook_chunks
                    (failure_type, service, risk_level, symptoms_summary,
                     section, chunk_index, content, source_file, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_file, section, chunk_index)
                DO UPDATE SET
                    failure_type = EXCLUDED.failure_type,
                    service = EXCLUDED.service,
                    risk_level = EXCLUDED.risk_level,
                    symptoms_summary = EXCLUDED.symptoms_summary,
                    content = EXCLUDED.content,
                    embedding = EXCLUDED.embedding,
                    created_at = now();
                """,
                (
                    chunk["failure_type"],
                    chunk["service"],
                    chunk["risk_level"],
                    chunk["symptoms_summary"],
                    chunk["section"],
                    chunk["chunk_index"],
                    chunk["content"],
                    chunk["source_file"],
                    Vector(embedding),
                ),
            )
            inserted += 1

        conn.commit()
        print(f"  Indexed {min(i + BATCH_SIZE, len(chunks))}/{len(chunks)} chunks")
        time.sleep(REQUEST_DELAY_SECONDS)

    cur.close()
    conn.close()
    print(f"\nDone. Upserted {inserted} chunks into runbook_chunks.")


if __name__ == "__main__":
    main()