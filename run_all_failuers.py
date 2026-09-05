"""
Run all 10 failure modes through the real Phase 3 pipeline and summarize
results. This is the actual Step 5/6 acceptance check for Phase 3 — does
retrieval + diagnosis behave sensibly across the WHOLE failure-mode
taxonomy, not just the one (CONNECTION_POOL_EXHAUSTION) already validated.

No stubs here, same as run_real_alert.py — real retrieval, real OpenRouter
calls, real DB. That's deliberate: the point is to catch schema drift,
retrieval misses, or bad diagnoses across the full set, not to re-prove
the wiring (test_agentic.py already does that).

Usage:
    python run_all_failure_modes.py --log-dir captured-logs

Expects one log file per failure mode, named <failure_type_lowercase>.log
in --log-dir (e.g. captured-logs/connection_pool_exhaustion.log). Edit
FAILURE_TYPE_TO_FILENAME below if your actual naming convention differs.
"""

import argparse
import sys
from pathlib import Path

from agentic import build_graph, VALID_FAILURE_TYPES
from run_real_alert import build_alert, initial_state

FAILURE_TYPE_TO_FILENAME = {ft: f"{ft.lower()}.log" for ft in VALID_FAILURE_TYPES}


def run_one(graph, failure_type: str, log_path: Path) -> dict:
    if not log_path.exists():
        return {"failure_type": failure_type, "status": "MISSING_FIXTURE", "detail": str(log_path)}

    log_snippet = log_path.read_text(encoding="utf-8").strip()
    alert = build_alert(failure_type, "orders-mock", log_snippet)

    try:
        final_state = graph.invoke(initial_state(alert))
    except Exception as e:
        # Catches the ValueErrors from llm.py's schema validation too — a
        # schema-drift failure on one failure mode shows up as a row here,
        # not a crash that kills the whole batch.
        return {"failure_type": failure_type, "status": "ERROR", "detail": f"{type(e).__name__}: {e}"}

    diagnosis = final_state.get("diagnosis") or {}
    plan = final_state.get("remediation_plan") or {}
    final_output = final_state.get("final_output") or {}

    return {
        "failure_type": failure_type,
        "status": "OK",
        "retrieval_confidence": final_state.get("retrieval_confidence", 0.0),
        "used_fallback": final_state.get("used_fallback", False),
        "risk_level": plan.get("overall_risk_level", "?"),
        "decision": final_output.get("status", "?"),
        "root_cause": (diagnosis.get("root_cause") or "")[:100],
    }


def print_summary(results: list[dict]) -> int:
    print("\n" + "=" * 110)
    print(f"{'failure_type':30s} {'status':10s} {'conf':6s} {'fallback':9s} {'risk':7s} {'decision':16s} root_cause")
    print("-" * 110)

    ok_count = 0
    for r in results:
        if r["status"] == "OK":
            ok_count += 1
            print(
                f"{r['failure_type']:30s} {r['status']:10s} "
                f"{r['retrieval_confidence']:.2f}   {str(r['used_fallback']):9s} "
                f"{r['risk_level']:7s} {r['decision']:16s} {r['root_cause']}"
            )
        else:
            print(f"{r['failure_type']:30s} {r['status']:10s} {r.get('detail', '')}")

    print("=" * 110)
    print(f"{ok_count}/{len(results)} failure modes completed without errors.\n")
    return ok_count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-dir", default="captured-logs")
    args = parser.parse_args()
    log_dir = Path(args.log_dir)

    graph = build_graph()
    results = []
    for failure_type in sorted(VALID_FAILURE_TYPES):
        filename = FAILURE_TYPE_TO_FILENAME[failure_type]
        print(f"Running {failure_type} ({filename})...", file=sys.stderr)
        results.append(run_one(graph, failure_type, log_dir / filename))

    ok_count = print_summary(results)

    if ok_count < len(results):
        sys.exit(1)  # non-zero exit if anything failed — useful for CI later


if __name__ == "__main__":
    main()