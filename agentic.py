"""
Phase 3 LangGraph agent for the Self-Healing Ops Agent — single-file version.
"""

from __future__ import annotations  # must be the first statement in the file

import logging
import operator
import os
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional, get_args

from typing_extensions import Annotated, TypedDict

from langgraph.graph import StateGraph, END

# ---- Schema --------------------------------------------------------------
# (no more `from schema import ...` — these are defined right here now)

FailureType = Literal[
    "CONNECTION_POOL_EXHAUSTION", "DB_DEADLOCK", "SLOW_DOWNSTREAM_DEPENDENCY",
    "MEMORY_LEAK", "DISK_FULL", "THREAD_POOL_EXHAUSTION", "BAD_DEPLOY_ERROR_SPIKE",
    "CONFIG_DRIFT", "RETRY_STORM", "OOM_KILL",
]

# Literal[...] isn't iterable/checkable with `in` at runtime — get_args()
# pulls the actual string values out of it so validation works.
VALID_FAILURE_TYPES = set(get_args(FailureType))


class Alert(TypedDict):
    alert_id: str
    failure_type: FailureType
    service: str
    timestamp: str
    log_snippet: str
    raw_payload: dict


class RetrievedChunk(TypedDict):
    runbook_id: str
    section: str
    content: str
    similarity_score: float


class WebSearchResult(TypedDict):
    query: str
    title: str
    url: str
    snippet: str


class Diagnosis(TypedDict):
    root_cause: str
    confidence: float
    reasoning: str
    sources: list[Literal["runbook", "web"]]


class RemediationStep(TypedDict):
    step_number: int
    action: str
    reversible: bool


class RemediationPlan(TypedDict):
    steps: list[RemediationStep]
    overall_risk_level: Literal["low", "medium", "high"]


class AgentState(TypedDict):
    alert: Alert
    retrieved_chunks: list[RetrievedChunk]
    retrieval_confidence: float
    used_fallback: bool
    needs_web_search: bool
    web_search_results: list[WebSearchResult]
    diagnosis: Optional[Diagnosis]
    remediation_plan: Optional[RemediationPlan]
    risk_decision: Optional[Literal["auto_recommend", "escalate"]]
    # max(LLM risk, runbook risk): what risk_gate actually decided on
    effective_risk_level: Optional[str]
    final_output: Optional[dict]
    node_errors: Annotated[list[str], operator.add]

    # populated by diagnose_and_plan on failure; drives the summarize/retry loop
    diagnose_error: Optional[str]
    diagnose_retry_count: int
    # set when summarize_log's own LLM call fails; ends the run in diagnosis_failed
    summarize_failed: bool


logger = logging.getLogger("self_healing_ops.agent")

# ---- Config ------------------------------------------------------------
# Set this from your real Phase 2 recall@k eval numbers, not a guess.
RETRIEVAL_CONFIDENCE_THRESHOLD = 0.72
RISK_LEVELS_REQUIRING_ESCALATION = {"high"}
# How many times to try summarize-and-retry before giving up on a diagnosis.
# Keep this small — each retry is a full extra LLM round trip (summarize +
# re-diagnose), not free.
MAX_DIAGNOSE_RETRIES = 1


# ---- Node implementations ----------------------------------------------

def ingest_and_validate(state: AgentState) -> dict:
    """Parse + validate the incoming alert. raw_payload stays in state for
    audit only — it is never passed into a prompt unsanitized."""
    alert = state["alert"]
    errors = []

    if alert.get("failure_type") not in VALID_FAILURE_TYPES:
        errors.append(f"Unknown failure_type: {alert.get('failure_type')}")
    if not alert.get("service"):
        errors.append("Missing service field")

    if errors:
        logger.warning("Alert validation issues: %s", errors)

    return {"node_errors": errors}


def route_after_ingest(state: AgentState) -> Literal["reject_invalid_alert", "retrieve_runbook_context"]:
    """Conditional edge function. A malformed alert (unknown failure_type,
    missing service) never reaches retrieval or the LLM — it short-circuits
    to a rejection response instead of flowing through the whole graph."""
    return "reject_invalid_alert" if state["node_errors"] else "retrieve_runbook_context"


def reject_invalid_alert(state: AgentState) -> dict:
    """Terminal node for alerts that failed validation. No retrieval or LLM
    calls ever happen on this path — the point is to fail fast and cheaply
    on bad input rather than spend a diagnosis call on it."""
    return {
        "final_output": {
            "alert_id": state["alert"].get("alert_id", "unknown"),
            "status": "rejected_invalid_alert",
            "diagnosis": None,
            "remediation_plan": None,
            "sources": [],
            "errors": state["node_errors"],
        }
    }


def _retrieval_deps():
    """Lazily construct and cache the DB cursor + embedding client that
    query_retrieval.py's retrieve() actually needs. Cached at module level
    so we don't open a new DB connection on every single alert."""
    if not hasattr(_retrieval_deps, "_cache"):
        from dotenv import load_dotenv
        load_dotenv()  # query_retrieval.py's own main() does this; we're
                        # importing its functions directly, so it's on us here.

        from query_retrieval import get_db_connection, embed_query, retrieve, EMBEDDING_MODEL
        from openai import OpenAI

        conn = get_db_connection()
        cur = conn.cursor()
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ["OPENROUTER_API"],
        )
        _retrieval_deps._cache = (cur, client, embed_query, retrieve)
    return _retrieval_deps._cache


def _reset_retrieval_deps_cache():
    """Drop the cached DB cursor + client so the next alert builds fresh ones.

    Tests call this before patching sys.modules['query_retrieval'], or they'd
    get the stale cached values instead of their stub. The Lambda handler
    calls it once per alert: a warm container can sit idle long enough for
    the server (Neon scales to zero) to drop the cached connection, and alerts
    are rare enough that reconnecting each time costs nothing noticeable."""
    if hasattr(_retrieval_deps, "_cache"):
        del _retrieval_deps._cache


def retrieve_runbook_context(state: AgentState) -> dict:
    """Thin wrapper around Phase 2's real retrieve(). Note the real interface
    is (cursor, precomputed embedding, lowercase failure_type, k) -> (rows,
    used_fallback), where rows are raw tuples ordered by cosine DISTANCE
    (lower = more similar) — not the object-shaped API originally assumed here."""
    alert = state["alert"]

    # Same reasoning as _build_diagnosis_prompt: raw captured logs can exceed
    # the EMBEDDING model's own context limit too. This is a separate model
    # from the diagnosis LLM with no fallback array and no retry logic —
    # THREAD_POOL_EXHAUSTION's raw log was large enough to break this call
    # before diagnosis ever got a turn.
    condensed_log = _condense_log_snippet(alert["log_snippet"])
    chunks, used_fallback = retrieve_chunks(condensed_log, alert["failure_type"], k=3)

    top_score = chunks[0]["similarity_score"] if chunks else 0.0

    return {
        "retrieved_chunks": chunks,
        "retrieval_confidence": top_score,
        "used_fallback": used_fallback,
    }


def retrieve_chunks(text: str, failure_type: Optional[str], k: int = 3) -> tuple[list[RetrievedChunk], bool]:
    """Embed text and fetch the k closest runbook chunks, pre-filtered to
    failure_type when given. Shared by the retrieve node and the MCP
    search_runbooks tool, so both paths rank identically."""
    cur, client, embed_query, retrieve = _retrieval_deps()
    # runbook_chunks.failure_type is stored lowercase (Phase 1 schema
    # convention); Alert.failure_type is the uppercase enum name.
    rows, used_fallback = retrieve(cur, embed_query(client, text), failure_type.lower() if failure_type else None, k=k)
    chunks = [
        {
            "runbook_id": source_file,
            "section": section,
            "content": content,
            # <=> is cosine DISTANCE (0 = identical). Flip to a similarity-style
            # score so confidence_gate's "score >= threshold" reads naturally.
            "similarity_score": 1 - distance,
        }
        for (ftype, section, source_file, content, symptoms_summary, distance) in rows
    ]
    return chunks, used_fallback


def confidence_gate(state: AgentState) -> dict:
    """Non-LLM decision node: does retrieval need a web search supplement?"""
    low_confidence = state["retrieval_confidence"] < RETRIEVAL_CONFIDENCE_THRESHOLD
    return {"needs_web_search": low_confidence or state["used_fallback"]}


def route_after_confidence_gate(state: AgentState) -> Literal["web_search", "diagnose_and_plan"]:
    """Conditional edge function — reads state, returns the next node's name."""
    return "web_search" if state["needs_web_search"] else "diagnose_and_plan"


def web_search(state: AgentState) -> dict:
    """External context lookup for failure signatures the runbook corpus
    doesn't confidently cover. Build the query from structured fields —
    never format raw alert text directly into a search query."""
    from searchllm import search  # matches searchllm.py

    alert = state["alert"]
    query = f"{alert['service']} {alert['failure_type']} {alert['log_snippet'][:200]}"
    results = search(query, max_results=5)

    return {"web_search_results": results}


def diagnose_and_plan(state: AgentState) -> dict:
    """Single LLM call: root cause + ranked remediation plan. Runbook and
    web context are kept in clearly separate prompt sections, and the LLM
    is required to tag each claim's source for auditability.

    On failure (context-length errors, schema drift, etc.) this does NOT
    raise — it records the error in state and lets route_after_diagnose
    decide whether to retry via summarize_log or give up. A bare exception
    here would crash the whole graph run over what's often a fixable
    input-size problem."""
    from llm import call_llm  # matches llm.py

    prompt = _build_diagnosis_prompt(
        alert=state["alert"],
        runbook_chunks=state["retrieved_chunks"],
        web_results=state.get("web_search_results", []),
    )

    try:
        response = call_llm(prompt, response_format="json")
    except Exception as e:
        logger.warning("diagnose_and_plan failed (attempt %d): %s", state["diagnose_retry_count"], e)
        return {"diagnose_error": f"{type(e).__name__}: {e}"}

    diagnosis: Diagnosis = response["diagnosis"]
    plan: RemediationPlan = response["remediation_plan"]

    # Explicitly clear diagnose_error on success — a prior failed attempt
    # (before a summarize_log retry) may have set it.
    return {"diagnosis": diagnosis, "remediation_plan": plan, "diagnose_error": None}


def route_after_diagnose(state: AgentState) -> Literal["risk_gate", "summarize_log", "diagnosis_failed"]:
    """Conditional edge function. Success goes straight to risk_gate. A
    failure retries via summarize_log up to MAX_DIAGNOSE_RETRIES times,
    then gives up and goes to diagnosis_failed."""
    if state.get("diagnose_error") is None:
        return "risk_gate"
    if state["diagnose_retry_count"] < MAX_DIAGNOSE_RETRIES:
        return "summarize_log"
    return "diagnosis_failed"


def summarize_log(state: AgentState) -> dict:
    """Called when diagnose_and_plan's prompt was too large for whichever
    provider served the request (context-length error, or a suspiciously
    empty response). Uses a cheap LLM call to compress the raw log into a
    short factual summary, then loops back into diagnose_and_plan with the
    shrunk input. Bounded by MAX_DIAGNOSE_RETRIES via route_after_diagnose."""
    from llm import call_llm

    alert = state["alert"]
    summary_prompt = f"""Summarize the key facts from this log for a debugging
context. Preserve: error types and counts, first/last occurrence timestamps,
and any distinct message patterns. Be terse — plain text, not JSON, under
300 words.

Log:
{alert['log_snippet'][:8000]}"""

    try:
        response = call_llm(summary_prompt, response_format="text")
    except Exception as e:
        # Same contract as diagnose_and_plan: record, don't raise. This call
        # usually runs right after a diagnose failure, often for the same
        # reason (rate limit, provider outage), and a raise here would crash
        # the graph instead of ending in diagnosis_failed.
        logger.warning("summarize_log failed: %s", e)
        return {
            "diagnose_error": f"{state.get('diagnose_error')}; then summarize_log failed: {type(e).__name__}: {e}",
            "summarize_failed": True,
        }
    summarized_alert = {**alert, "log_snippet": response["content"]}

    logger.info(
        "summarize_log: condensed %d chars to %d chars (retry %d)",
        len(alert["log_snippet"]), len(response["content"]), state["diagnose_retry_count"] + 1,
    )

    return {
        "alert": summarized_alert,
        "diagnose_retry_count": state["diagnose_retry_count"] + 1,
    }


def route_after_summarize(state: AgentState) -> Literal["diagnose_and_plan", "diagnosis_failed"]:
    """No point retrying the diagnosis with a log we failed to shrink."""
    return "diagnosis_failed" if state.get("summarize_failed") else "diagnose_and_plan"


def diagnosis_failed(state: AgentState) -> dict:
    """Terminal node for when diagnose_and_plan couldn't produce a usable
    result even after the summarize/retry loop. Same shape as
    reject_invalid_alert — a clear failure status, not a crash."""
    return {
        "final_output": {
            "alert_id": state["alert"]["alert_id"],
            "status": "diagnosis_failed",
            "diagnosis": None,
            "remediation_plan": None,
            "sources": [],
            "errors": [state.get("diagnose_error", "unknown diagnose_and_plan failure")],
        }
    }


RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
RUNBOOK_DIR = Path(__file__).resolve().parent / "RAGcourps"


@lru_cache(maxsize=None)
def runbook_risk_level(failure_type: str) -> Optional[str]:
    """risk_level from the failure mode's runbook frontmatter, or None."""
    path = RUNBOOK_DIR / f"{failure_type.lower()}.md"
    if not path.exists():
        return None
    import frontmatter
    level = frontmatter.load(path).get("risk_level")
    return level if level in RISK_ORDER else None


def risk_gate(state: AgentState) -> dict:
    """Non-LLM decision node. The LLM's risk rating is FLOORED at the
    runbook's: in the e2e eval the LLM agreed with the runbook on only 3 of
    8 incidents and rated 2 LOWER, including a high-risk bad deploy it
    called medium, which would have auto-remediated with no human. Rating
    higher than the runbook is allowed (more caution is safe). A missing or
    invalid LLM rating counts as high: a malformed answer mustn't earn less
    oversight."""
    llm_risk = state["remediation_plan"].get("overall_risk_level")
    floor = runbook_risk_level(state["alert"]["failure_type"])
    candidates = [llm_risk if llm_risk in RISK_ORDER else "high"] + ([floor] if floor else [])
    risk = max(candidates, key=RISK_ORDER.__getitem__)
    if risk != llm_risk:
        logger.info("risk_gate: LLM said %r, runbook floor %r -> using %r", llm_risk, floor, risk)
    decision = "escalate" if risk in RISK_LEVELS_REQUIRING_ESCALATION else "auto_recommend"
    return {"risk_decision": decision, "effective_risk_level": risk}


def route_after_risk_gate(state: AgentState) -> Literal["recommend_action", "escalate_for_approval"]:
    return "escalate_for_approval" if state["risk_decision"] == "escalate" else "recommend_action"


def recommend_action(state: AgentState) -> dict:
    return {"final_output": _format_output(state, status="auto_recommend")}


def escalate_for_approval(state: AgentState) -> dict:
    return {"final_output": _format_output(state, status="pending_approval")}


def respond(state: AgentState) -> dict:
    """Terminal node. Both branches already wrote final_output — this is
    where per-run structured observability logging should hang."""
    logger.info(
        "Agent run complete: alert_id=%s status=%s",
        state["alert"]["alert_id"],
        state["final_output"]["status"],
    )
    return {}


# ---- Helpers -------------------------------------------------------------

def build_alert(
    failure_type: str,
    service: str,
    log_snippet: str,
    alert_id: Optional[str] = None,
    timestamp: Optional[str] = None,
    raw_payload: Optional[dict] = None,
) -> Alert:
    """Single place alerts are built, so the CLI scripts and the Lambda
    handler can't drift apart on shape."""
    return {
        "alert_id": alert_id or f"alert-{uuid.uuid4().hex[:8]}",
        "failure_type": failure_type,
        "service": service,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "log_snippet": log_snippet,
        "raw_payload": raw_payload if raw_payload is not None else {"log_snippet": log_snippet},
    }


def initial_state(alert: Alert) -> AgentState:
    return {
        "alert": alert,
        "retrieved_chunks": [],
        "retrieval_confidence": 0.0,
        "used_fallback": False,
        "needs_web_search": False,
        "web_search_results": [],
        "diagnosis": None,
        "remediation_plan": None,
        "risk_decision": None,
        "effective_risk_level": None,
        "final_output": None,
        "node_errors": [],
        "diagnose_error": None,
        "diagnose_retry_count": 0,
        "summarize_failed": False,
    }

def _condense_log_snippet(log_snippet: str, max_chars: int = 4000) -> str:
    """Captured logs are often dozens of near-identical repeated lines during
    a failure burst (see connection_pool_exhaustion.log) — collapsing exact
    consecutive repeats keeps the diagnostic signal while cutting tokens
    dramatically. Direct fix for two real failures: THREAD_POOL_EXHAUSTION
    exceeded an 8192-token input limit on one free-tier provider, and
    MEMORY_LEAK got back an empty `{}` completion — most likely the same
    root cause on a different provider (json_object's grammar-constrained
    decoding produces valid-but-empty JSON when generation is cut short by
    running out of context, rather than erroring outright)."""
    lines = log_snippet.splitlines()
    condensed = []
    i = 0
    while i < len(lines):
        line = lines[i]
        j = i + 1
        while j < len(lines) and lines[j] == line:
            j += 1
        repeat_count = j - i
        condensed.append(f"{line}  [^ repeated {repeat_count}x]" if repeat_count > 1 else line)
        i = j

    result = "\n".join(condensed)

    if len(result) > max_chars:
        half = max_chars // 2
        result = (
            f"{result[:half]}\n"
            f"... [truncated {len(result) - max_chars} chars] ...\n"
            f"{result[-half:]}"
        )

    return result


def _build_diagnosis_prompt(alert, runbook_chunks, web_results) -> str:
    """Build the diagnose_and_plan prompt. Keep runbook and web context in
    clearly separate, labeled sections — do not interleave them."""
    # Cap each chunk defensively too — a single oversized runbook section
    # shouldn't be able to blow the budget on its own.
    runbook_section = "\n\n".join(c["content"][:1500] for c in runbook_chunks) or "(none retrieved)"
    web_section = "\n\n".join(f"{r['title']}: {r['snippet']}" for r in web_results) or "(not used)"
    log_snippet = _condense_log_snippet(alert["log_snippet"])

    return f"""You are diagnosing a production incident. Use the runbook context as
primary ground truth. Only use web context to fill gaps the runbook doesn't cover,
and label every claim's source as "runbook" or "web" in the sources field.

## Alert
service: {alert['service']}
failure_type: {alert['failure_type']}
log_snippet: {log_snippet}

## Runbook context
{runbook_section}

## Web context
{web_section}

Return JSON matching EXACTLY this structure — use these exact key names and
types, not synonyms or alternative shapes:

{{
  "diagnosis": {{
    "root_cause": "<string>",
    "confidence": <float between 0.0 and 1.0, NOT a word like "high">,
    "reasoning": "<string>",
    "sources": ["runbook" and/or "web"]
  }},
  "remediation_plan": {{
    "steps": [
      {{
        "step_number": <int, starting at 1>,
        "action": "<string>",
        "reversible": <true or false, NOT a risk level>
      }}
    ],
    "overall_risk_level": "low" or "medium" or "high"
  }}
}}"""


def summarize_run(final: dict, log_line_count: Optional[int] = None) -> dict:
    """Compact, JSON-safe view of a finished graph run: what goes into
    agent_runs and what the MCP diagnose tool returns. Keeps a condensed log
    excerpt rather than the raw log: DynamoDB items cap at 400 KB, and the
    audit trail needs what the agent saw, not every repeated line."""
    final_output = final.get("final_output") or {}
    return {
        "outcome": final_output.get("status", "unknown"),
        "final_output": final_output,
        "retrieval_confidence": final.get("retrieval_confidence", 0.0),
        "used_fallback": final.get("used_fallback", False),
        "retrieved": [
            {"runbook_id": c["runbook_id"], "section": c["section"], "similarity_score": c["similarity_score"]}
            for c in final.get("retrieved_chunks", [])
        ],
        "web_sources": [r["url"] for r in final.get("web_search_results", [])],
        "node_errors": final.get("node_errors", []),
        "log_line_count": log_line_count if log_line_count is not None
                          else len(final["alert"]["log_snippet"].splitlines()),
        "log_excerpt": _condense_log_snippet(final["alert"]["log_snippet"]),
    }


def _format_output(state: AgentState, status: str) -> dict:
    return {
        "alert_id": state["alert"]["alert_id"],
        "status": status,
        "diagnosis": state["diagnosis"],
        "remediation_plan": state["remediation_plan"],
        "effective_risk_level": state.get("effective_risk_level"),
        "sources": state["diagnosis"]["sources"],
    }


# ---- Graph construction ---------------------------------------------------

def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("ingest_and_validate", ingest_and_validate)
    graph.add_node("retrieve_runbook_context", retrieve_runbook_context)
    graph.add_node("confidence_gate", confidence_gate)
    graph.add_node("web_search", web_search)
    graph.add_node("diagnose_and_plan", diagnose_and_plan)
    graph.add_node("risk_gate", risk_gate)
    graph.add_node("recommend_action", recommend_action)
    graph.add_node("escalate_for_approval", escalate_for_approval)
    graph.add_node("respond", respond)
    graph.add_node("reject_invalid_alert", reject_invalid_alert)
    graph.add_node("summarize_log", summarize_log)
    graph.add_node("diagnosis_failed", diagnosis_failed)

    graph.set_entry_point("ingest_and_validate")
    graph.add_conditional_edges(
        "ingest_and_validate",
        route_after_ingest,
        {"reject_invalid_alert": "reject_invalid_alert", "retrieve_runbook_context": "retrieve_runbook_context"},
    )
    graph.add_edge("reject_invalid_alert", "respond")
    graph.add_edge("retrieve_runbook_context", "confidence_gate")

    graph.add_conditional_edges(
        "confidence_gate",
        route_after_confidence_gate,
        {"web_search": "web_search", "diagnose_and_plan": "diagnose_and_plan"},
    )
    graph.add_edge("web_search", "diagnose_and_plan")

    graph.add_conditional_edges(
        "diagnose_and_plan",
        route_after_diagnose,
        {"risk_gate": "risk_gate", "summarize_log": "summarize_log", "diagnosis_failed": "diagnosis_failed"},
    )
    graph.add_conditional_edges(
        "summarize_log",
        route_after_summarize,
        {"diagnose_and_plan": "diagnose_and_plan", "diagnosis_failed": "diagnosis_failed"},
    )
    graph.add_edge("diagnosis_failed", "respond")

    graph.add_conditional_edges(
        "risk_gate",
        route_after_risk_gate,
        {"recommend_action": "recommend_action", "escalate_for_approval": "escalate_for_approval"},
    )
    graph.add_edge("recommend_action", "respond")
    graph.add_edge("escalate_for_approval", "respond")
    graph.add_edge("respond", END)

    return graph.compile()


if __name__ == "__main__":
    # Smoke test wiring only — replace with a real alert fixture from
    # captured-logs/ before trusting this.
    app = build_graph()
    print(app.get_graph().draw_ascii())