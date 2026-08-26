-- Phase 2 retrieval schema
-- One row per (runbook section) chunk, per the locked-in Phase 1 frontmatter schema:
-- failure_type, service, risk_level, symptoms_summary

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS runbook_chunks (
    id              SERIAL PRIMARY KEY,
    failure_type    TEXT NOT NULL,          -- exact lowercase enum value, e.g. connection_pool_exhaustion
    service         TEXT NOT NULL,          -- self-healing-ops-mock-service
    risk_level      TEXT NOT NULL,          -- low | medium | high
    symptoms_summary TEXT,                  -- from frontmatter, useful for eyeballing results
    section         TEXT NOT NULL,          -- symptoms | root_causes | diagnosis_steps | remediation | rollback_plan | references
    chunk_index     INTEGER NOT NULL DEFAULT 0,  -- position within section, in case a section is split further
    content         TEXT NOT NULL,          -- the actual chunk text embedded
    source_file     TEXT NOT NULL,          -- e.g. runbooks/connection_pool_exhaustion.md
    embedding       vector(2048),           -- nvidia/llama-nemotron-embed-vl-1b-v2:free dimensionality
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Exact-match pre-filter is the primary retrieval path, so this index matters most.
CREATE INDEX IF NOT EXISTS idx_runbook_chunks_failure_type ON runbook_chunks (failure_type);

-- HNSW index for semantic search within (or across, as a fallback) failure_type groups.
-- Cosine distance since OpenAI embeddings are normalized for cosine similarity.
CREATE INDEX IF NOT EXISTS idx_runbook_chunks_embedding
    ON runbook_chunks USING hnsw (embedding vector_cosine_ops);

-- Prevents duplicate rows if you re-run indexing without a --reset
CREATE UNIQUE INDEX IF NOT EXISTS idx_runbook_chunks_unique_chunk
    ON runbook_chunks (source_file, section, chunk_index);