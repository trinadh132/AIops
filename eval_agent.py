"""
Honest evaluation of the agent, in two parts that answer different questions.

1. blind  - Can the system tell WHAT broke with no label?
   Each captured log is embedded and searched against the WHOLE runbook
   corpus, no failure_type filter. Scores recall@1, recall@k and MRR of the
   correct runbook. The mock service tags every log line with the answer
   (a failure_mode field, and failure_mode=X inside message text), which
   real services don't do, so --variant controls what the search sees:
     label_free (default)  injection lines dropped AND failure_mode tags
                           stripped: symptoms only. The honest number.
     tagged                injection lines dropped, tags kept: what the
                           agent actually receives from this mock service.
     leaky                 raw fixture, as before the injection-line fix.

2. e2e    - Given the failure type (production gets it from the alarm's
   metric name), is the diagnosis RIGHT and the risk call SAFE?
   Runs the full graph per failure mode and scores:
     - on_topic: root cause/reasoning names a concept from that mode's
       runbook (keyword rubric below, derived from "Likely root causes")
     - risk: the LLM's overall_risk_level vs the runbook's risk_level;
       "under" (LLM rated it LOWER) is the dangerous direction, because
       lower risk means less human oversight
     - decision: escalated exactly when the runbook says high risk
     - retrieval confidence vs RETRIEVAL_CONFIDENCE_THRESHOLD, to calibrate it

Needs OPENROUTER_API and DATABASE_URL (indexed runbooks). Results go to
eval_results/<utc timestamp>-<part>.json and a markdown table on stdout.

    python eval_agent.py blind [--variant label_free|tagged|leaky] [--k 3]
    python eval_agent.py e2e [--repeats 1] [--modes DISK_FULL,OOM_KILL]
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

import agentic as ag
from tools.logs import strip_excluded_events

REPO = Path(__file__).resolve().parent
LOG_DIR = REPO / "captured-logs"
RUNBOOK_DIR = REPO / "RAGcourps"
RESULTS_DIR = REPO / "eval_results"

# Concepts from each runbook's "Likely root causes". A diagnosis is on topic
# if its root_cause + reasoning matches any pattern for its mode. Deliberately
# lenient (the label is given in e2e); it catches answers about the wrong
# failure, not subtle mistakes. Case-insensitive.
RUBRIC = {
    "CONNECTION_POOL_EXHAUSTION": [r"connection pool", r"pool (size|exhaust|capacity)", r"connection leak",
                                   r"no available connections"],
    "DB_DEADLOCK": [r"deadlock", r"lock (order|wait|contention|timeout)", r"long-running transaction"],
    "SLOW_DOWNSTREAM_DEPENDENCY": [r"downstream", r"dependenc(y|ies)", r"latency"],
    "MEMORY_LEAK": [r"memory leak", r"\bleak", r"heap", r"unbounded (cache|collection|map)"],
    "DISK_FULL": [r"disk (is )?(full|space)", r"no space", r"log rotation", r"volume", r"storage"],
    "THREAD_POOL_EXHAUSTION": [r"thread pool", r"\bthreads?\b", r"concurren"],
    "BAD_DEPLOY_ERROR_SPIKE": [r"deploy", r"null ?pointer|\bnpe\b|null[- ]safety|null check", r"regression",
                               r"new (code|build|release|version)"],
    "CONFIG_DRIFT": [r"config(uration)?", r"env(ironment)? var", r"misconfigur"],
    "RETRY_STORM": [r"retr(y|ies)", r"backoff", r"jitter"],
    "OOM_KILL": [r"out of memory|\boom\b", r"memory (limit|leak|exhaust)", r"killed"],
}

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

VARIANTS = ("label_free", "tagged", "leaky")
# The ground-truth label as the mock service writes it: a JSON field (MDC)
# and, in many messages, inline text.
LABEL_PATTERNS = (re.compile(r'"failure_mode":"[A-Z_]+",?'), re.compile(r"failure_mode=[A-Z_]+\s*"))


# ---- scoring (pure; unit-tested in test_eval_agent.py) ----------------------------

def on_topic(failure_type: str, text: str) -> bool:
    return any(re.search(p, text or "", re.IGNORECASE) for p in RUBRIC[failure_type])


def compare_risk(llm_risk: str | None, runbook_risk: str) -> str:
    """'agree', 'over' (LLM more cautious), 'under' (LLM less cautious:
    the unsafe direction), or 'invalid' (missing/unknown value)."""
    if llm_risk not in RISK_ORDER:
        return "invalid"
    delta = RISK_ORDER[llm_risk] - RISK_ORDER[runbook_risk]
    return "agree" if delta == 0 else ("over" if delta > 0 else "under")


def expected_decision(runbook_risk: str) -> str:
    return "pending_approval" if runbook_risk in ag.RISK_LEVELS_REQUIRING_ESCALATION else "auto_recommend"


def strip_labels(text: str) -> str:
    for pattern in LABEL_PATTERNS:
        text = pattern.sub("", text)
    return text


def rank_of(expected: str, retrieved_types: list[str]) -> int | None:
    """1-based rank of the first chunk from the expected runbook, or None."""
    for i, ft in enumerate(retrieved_types, 1):
        if ft.upper() == expected:
            return i
    return None


def blind_metrics(ranks: list[int | None], k: int) -> dict:
    n = len(ranks) or 1
    return {
        "recall@1": sum(1 for r in ranks if r == 1) / n,
        f"recall@{k}": sum(1 for r in ranks if r is not None and r <= k) / n,
        "mrr": sum(1 / r for r in ranks if r) / n,
    }


# ---- data -------------------------------------------------------------------------------

def runbook_risk(failure_type: str) -> str:
    return frontmatter.load(RUNBOOK_DIR / f"{failure_type.lower()}.md")["risk_level"]


def load_log(failure_type: str, variant: str = "tagged") -> str:
    lines = (LOG_DIR / f"{failure_type.lower()}.log").read_text(encoding="utf-8").splitlines()
    if variant == "leaky":
        return "\n".join(lines)
    text = "\n".join(strip_excluded_events(lines))
    return strip_labels(text) if variant == "label_free" else text


# Results files are committed. psycopg errors embed the database host and IP
# ('connection to server at "ep-....neon.tech" (1.2.3.4)'), which would
# publish the database endpoint, so they're redacted before saving.
_DB_ENDPOINT = re.compile(r'(connection to server at )"[^"]+"(?: \([0-9a-fA-F.:]+\))?')


def redact(text: str | None) -> str | None:
    return _DB_ENDPOINT.sub(r'\1"<db-host>"', text) if text else text


def _save(part: str, payload: dict) -> Path:
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{part}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


# ---- part 1: blind retrieval ----------------------------------------------------------------

def run_blind(modes: list[str], k: int, variant: str) -> dict:
    rows = []
    for mode in modes:
        query = ag._condense_log_snippet(load_log(mode, variant))
        ag._reset_retrieval_deps_cache()
        chunks, _ = ag.retrieve_chunks(query, None, k=max(k, 10))  # rank deeper than k for MRR
        types = [c["runbook_id"].removesuffix(".md").upper() for c in chunks]
        rank = rank_of(mode, types)
        rows.append({"failure_type": mode, "rank": rank, "top": types[:k],
                     "top_similarity": round(chunks[0]["similarity_score"], 4) if chunks else None})
        print(f"  {mode:28s} rank={rank}  top{k}={types[:k]}", file=sys.stderr)
    ranks = [r["rank"] for r in rows]
    return {"part": "blind", "variant": variant, "k": k, "metrics": blind_metrics(ranks, k), "rows": rows}


# ---- part 2: end to end -----------------------------------------------------------------------

def run_e2e(modes: list[str], repeats: int, pause_seconds: float) -> dict:
    graph = ag.build_graph()
    rows = []
    for mode in modes:
        rb_risk = runbook_risk(mode)
        for attempt in range(repeats):
            ag._reset_retrieval_deps_cache()
            started = time.monotonic()
            try:
                # "tagged" is exactly what the Lambda handler passes the agent.
                log = load_log(mode, "tagged")
                final = graph.invoke(ag.initial_state(ag.build_alert(mode, "self-healing-ops-mock-service", log)))
                error = None
            except Exception as e:  # a crash is a result too, not a reason to stop
                final, error = {}, f"{type(e).__name__}: {e}"
            elapsed = round(time.monotonic() - started, 1)

            out = final.get("final_output") or {}
            diag = out.get("diagnosis") or {}
            plan = out.get("remediation_plan") or {}
            status = out.get("status", "crashed" if error else "unknown")
            completed = status in ("auto_recommend", "pending_approval")
            row = {
                "failure_type": mode, "attempt": attempt + 1, "status": status, "seconds": elapsed,
                "retrieval_confidence": round(final.get("retrieval_confidence", 0.0), 4),
                "web_search_used": bool(final.get("web_search_results")),
                "runbook_risk": rb_risk, "llm_risk": plan.get("overall_risk_level"),
                # what risk_gate decided on after flooring at the runbook's level
                "effective_risk": out.get("effective_risk_level"),
                "risk": compare_risk(plan.get("overall_risk_level"), rb_risk) if completed else None,
                "on_topic": on_topic(mode, f"{diag.get('root_cause', '')} {diag.get('reasoning', '')}") if completed else None,
                "decision_correct": status == expected_decision(rb_risk) if completed else None,
                "cites_runbook": "runbook" in (diag.get("sources") or []) if completed else None,
                "root_cause": (diag.get("root_cause") or "")[:200],
                "error": redact(error or ("; ".join(out.get("errors", [])) or None)),
            }
            rows.append(row)
            print(f"  {mode:28s} #{attempt + 1} {status:17s} risk={row['risk']} on_topic={row['on_topic']} "
                  f"{elapsed}s", file=sys.stderr)
            time.sleep(pause_seconds)  # free-tier rate limits

    done = [r for r in rows if r["risk"] is not None]
    n = len(done) or 1
    return {
        "part": "e2e", "repeats": repeats, "threshold": ag.RETRIEVAL_CONFIDENCE_THRESHOLD,
        "metrics": {
            "completed": f"{len(done)}/{len(rows)}",
            "on_topic": sum(r["on_topic"] for r in done) / n,
            "risk_agree": sum(r["risk"] == "agree" for r in done) / n,
            "risk_under_rated": sum(r["risk"] == "under" for r in done),
            "decision_correct": sum(r["decision_correct"] for r in done) / n,
            "cites_runbook": sum(r["cites_runbook"] for r in done) / n,
            "web_search_triggered": sum(r["web_search_used"] for r in rows),
            "median_seconds": sorted(r["seconds"] for r in rows)[len(rows) // 2] if rows else None,
        },
        "rows": rows,
    }


# ---- reporting ------------------------------------------------------------------------------------

def markdown(result: dict) -> str:
    m = result["metrics"]
    if result["part"] == "blind":
        head = f"**Blind retrieval** (variant: {result['variant']}): " + \
               ", ".join(f"{k} {v:.0%}" if k != "mrr" else f"MRR {v:.2f}" for k, v in m.items())
        lines = ["| Failure mode | Rank of correct runbook | Top results |", "|---|---|---|"]
        lines += [f"| {r['failure_type']} | {r['rank'] or 'miss'} | {', '.join(r['top'])} |" for r in result["rows"]]
    else:
        head = (f"**End to end**: completed {m['completed']}, on-topic {m['on_topic']:.0%}, "
                f"risk agrees with runbook {m['risk_agree']:.0%} (under-rated: {m['risk_under_rated']}), "
                f"decision correct {m['decision_correct']:.0%}, cites runbook {m['cites_runbook']:.0%}, "
                f"web search {m['web_search_triggered']}x, median {m['median_seconds']}s")
        lines = ["| Failure mode | Status | Conf. | Runbook risk | LLM risk | On topic | Decision OK |",
                 "|---|---|---|---|---|---|---|"]
        lines += [f"| {r['failure_type']} | {r['status']} | {r['retrieval_confidence']:.2f} | {r['runbook_risk']} | "
                  f"{r['llm_risk'] or '-'} | {r['on_topic']} | {r['decision_correct']} |" for r in result["rows"]]
    return head + "\n\n" + "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("part", choices=["blind", "e2e"])
    parser.add_argument("--modes", help="Comma-separated FailureMode names (default: all 10)")
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--variant", choices=VARIANTS, default="label_free", help="blind: what the search sees")
    parser.add_argument("--repeats", type=int, default=1, help="e2e: runs per mode (LLM output varies)")
    parser.add_argument("--pause", type=float, default=3.0, help="e2e: seconds between runs (rate limits)")
    args = parser.parse_args()

    modes = sorted(m.strip().upper() for m in args.modes.split(",")) if args.modes else sorted(RUBRIC)
    unknown = set(modes) - set(RUBRIC)
    if unknown:
        sys.exit(f"Unknown modes: {sorted(unknown)}")

    result = run_blind(modes, args.k, args.variant) if args.part == "blind" else run_e2e(modes, args.repeats, args.pause)
    result["run_at"] = datetime.now(timezone.utc).isoformat()
    path = _save(args.part + (f"-{args.variant}" if args.part == "blind" else ""), result)
    print(markdown(result))
    print(f"\nSaved {path.relative_to(REPO)}", file=sys.stderr)


if __name__ == "__main__":
    main()
