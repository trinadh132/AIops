"""Runbook lookup: semantic search over the indexed chunks, and whole-runbook
reads from the corpus on disk."""

import os
from pathlib import Path

import frontmatter

import agentic as ag

RUNBOOK_DIR = Path(os.environ.get("RUNBOOK_DIR", Path(__file__).resolve().parent.parent / "RAGcourps"))
MAX_K = 10


def normalize_failure_type(value: str) -> str:
    """Case-insensitive match against the FailureMode enum. Anything that
    isn't an exact enum name is rejected, which also means a failure_type can
    never smuggle a path (e.g. "../../.env") into get_runbook's file read."""
    upper = (value or "").strip().upper()
    if upper not in ag.VALID_FAILURE_TYPES:
        raise ValueError(f"Unknown failure_type {value!r}. Valid: {', '.join(sorted(ag.VALID_FAILURE_TYPES))}")
    return upper


def search_runbooks(query: str, failure_type: str | None = None, k: int = 3) -> dict:
    ft = normalize_failure_type(failure_type) if failure_type else None
    k = max(1, min(k, MAX_K))
    # Long-lived callers (an MCP server) would otherwise hold one connection
    # forever and break when the database drops it; see agentic's docstring.
    ag._reset_retrieval_deps_cache()
    chunks, used_fallback = ag.retrieve_chunks(query, ft, k)
    return {
        "failure_type_filter": ft,
        "used_fallback": used_fallback,
        "results": [
            {**c, "similarity_score": round(c["similarity_score"], 4)} for c in chunks
        ],
    }


def get_runbook(failure_type: str) -> dict:
    ft = normalize_failure_type(failure_type)
    post = frontmatter.load(RUNBOOK_DIR / f"{ft.lower()}.md")
    return {
        "failure_type": ft,
        "risk_level": post.get("risk_level"),
        "symptoms_summary": post.get("symptoms_summary"),
        "markdown": post.content,
    }
