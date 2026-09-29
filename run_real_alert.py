r"""
Run one real alert end-to-end through the Phase 3 LangGraph agent — real
retrieval, real OpenRouter calls, no stubs. This is Step 3 on the Phase 3
checklist: it proves retrieval + LLM + graph logic actually work TOGETHER,
which test_agentic.py deliberately does not (everything there is stubbed).

Usage (PowerShell):
    python run_real_alert.py --failure-type CONNECTION_POOL_EXHAUSTION `
        --service orders-mock --log-file captured-logs\connection_pool_exhaustion.log

Or pipe a log snippet directly instead of --log-file:
    Get-Content captured-logs\connection_pool_exhaustion.log -Raw | python run_real_alert.py `
        --failure-type CONNECTION_POOL_EXHAUSTION --service orders-mock
"""

import argparse
import json
import sys

from agentic import build_alert, build_graph, initial_state, VALID_FAILURE_TYPES


def main():
    parser = argparse.ArgumentParser(description="Run one real alert through the Phase 3 agent.")
    parser.add_argument(
        "--failure-type", required=True, choices=sorted(VALID_FAILURE_TYPES),
        help="One of the 10 failure modes.",
    )
    parser.add_argument("--service", default="orders-mock", help="Service name in the alert.")
    parser.add_argument(
        "--log-file", help="Path to a captured-logs/ fixture. If omitted, reads from stdin.",
    )
    args = parser.parse_args()

    if args.log_file:
        with open(args.log_file, "r", encoding="utf-8") as f:
            log_snippet = f.read().strip()
    else:
        log_snippet = sys.stdin.read().strip()

    if not log_snippet:
        print("No log snippet provided — pass --log-file or pipe content via stdin.", file=sys.stderr)
        sys.exit(1)

    alert = build_alert(args.failure_type, args.service, log_snippet)

    print("--- Alert ---")
    print(json.dumps({k: v for k, v in alert.items() if k != "raw_payload"}, indent=2))
    print()

    graph = build_graph()
    print("Running graph (this makes real retrieval + LLM calls)...\n")
    final_state = graph.invoke(initial_state(alert))

    print("--- Retrieval ---")
    print(
        f"Confidence: {final_state['retrieval_confidence']:.2f}  |  "
        f"Used fallback: {final_state['used_fallback']}"
    )
    print(f"Chunks retrieved: {len(final_state['retrieved_chunks'])}")
    for chunk in final_state["retrieved_chunks"]:
        print(
            f"  - {chunk.get('runbook_id')} / {chunk.get('section')} "
            f"(score={chunk.get('similarity_score'):.2f})"
        )

    if final_state["web_search_results"]:
        print("\n--- Web search (confidence gate triggered fallback) ---")
        for r in final_state["web_search_results"]:
            print(f"  - {r['title']} ({r['url']})")

    print("\n--- Diagnosis ---")
    print(json.dumps(final_state["diagnosis"], indent=2))

    print("\n--- Remediation plan ---")
    print(json.dumps(final_state["remediation_plan"], indent=2))

    print(f"\n--- Final decision: {final_state['final_output']['status']} ---")
    print(json.dumps(final_state["final_output"], indent=2))

    if final_state["node_errors"]:
        print(f"\nWARNING: node_errors was non-empty: {final_state['node_errors']}", file=sys.stderr)


if __name__ == "__main__":
    main()