"""
Unit tests for every node method in agentic.py.

Run from the same directory as agentic.py:
    python -m unittest test_agentic.py -v

These tests stub out the three deferred-import modules (query_retrieval,
searchllm, llm) so they run without a real OpenRouter key,
a running retrieval service, or captured runbook data. That's deliberate:
this validates the graph's LOGIC AND WIRING, not real diagnosis quality.
Real quality validation still needs actual captured-logs/ fixtures and a
live API key — that's Step 5/6 on the Phase 3 checklist, not this file.
"""

import os
import sys
import types
import unittest
from unittest.mock import patch

# Hermetic: agentic._retrieval_deps() calls load_dotenv(), which searches
# parent directories for a .env. Without an explicit value here, these tests
# silently picked up a developer's real .env (and failed in CI, which has
# none). load_dotenv never overrides a variable that's already set, so this
# dummy also keeps real credentials out of the test process.
os.environ["OPENROUTER_API"] = "test-dummy-key"

import agentic as ag  # noqa: E402


def _stub_module(name: str, **attrs):
    """Build a fake module object so agentic.py's deferred
    `from X import Y` imports resolve to it instead of the real dependency."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _stub_query_retrieval(retrieve_fn):
    """_retrieval_deps() imports FOUR names from query_retrieval
    (get_db_connection, embed_query, retrieve, EMBEDDING_MODEL) — a stub
    that only defines `retrieve` isn't enough and raises ImportError.
    This builds a complete fake module; only `retrieve_fn` varies per test."""
    fake_conn = types.SimpleNamespace(cursor=lambda: "fake-cursor")
    return _stub_module(
        "query_retrieval",
        get_db_connection=lambda: fake_conn,
        embed_query=lambda client, text: [0.0] * 8,
        retrieve=retrieve_fn,
        EMBEDDING_MODEL="fake-embedding-model",
    )


def _base_alert(**overrides) -> dict:
    alert = {
        "alert_id": "alert-001",
        "failure_type": "CONNECTION_POOL_EXHAUSTION",
        "service": "orders-mock",
        "timestamp": "2026-09-05T12:00:00Z",
        "log_snippet": "HikariPool-1 - Connection is not available, request timed out after 30000ms",
        "raw_payload": {"raw": "..."},
    }
    alert.update(overrides)
    return alert


def _base_state(**overrides) -> dict:
    state = {
        "alert": _base_alert(),
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
    state.update(overrides)
    return state


class TestIngestAndValidate(unittest.TestCase):
    def test_valid_alert_has_no_errors(self):
        result = ag.ingest_and_validate(_base_state())
        self.assertEqual(result["node_errors"], [])

    def test_unknown_failure_type_is_flagged(self):
        state = _base_state(alert=_base_alert(failure_type="NOT_A_REAL_FAILURE_TYPE"))
        result = ag.ingest_and_validate(state)
        self.assertEqual(len(result["node_errors"]), 1)
        self.assertIn("Unknown failure_type", result["node_errors"][0])

    def test_missing_service_is_flagged(self):
        state = _base_state(alert=_base_alert(service=""))
        result = ag.ingest_and_validate(state)
        self.assertIn("Missing service field", result["node_errors"])

    def test_both_errors_at_once(self):
        state = _base_state(alert=_base_alert(failure_type="BOGUS", service=""))
        result = ag.ingest_and_validate(state)
        self.assertEqual(len(result["node_errors"]), 2)


class TestRejectInvalidAlert(unittest.TestCase):
    def test_route_after_ingest_rejects_on_errors(self):
        self.assertEqual(
            ag.route_after_ingest(_base_state(node_errors=["Missing service field"])),
            "reject_invalid_alert",
        )

    def test_route_after_ingest_proceeds_when_clean(self):
        self.assertEqual(
            ag.route_after_ingest(_base_state(node_errors=[])),
            "retrieve_runbook_context",
        )

    def test_reject_invalid_alert_sets_status_and_carries_errors(self):
        state = _base_state(node_errors=["Unknown failure_type: BOGUS"])
        result = ag.reject_invalid_alert(state)
        self.assertEqual(result["final_output"]["status"], "rejected_invalid_alert")
        self.assertEqual(result["final_output"]["errors"], ["Unknown failure_type: BOGUS"])
        self.assertIsNone(result["final_output"]["diagnosis"])

    def test_full_graph_short_circuits_on_malformed_alert(self):
        """The real point of this test: prove retrieval/LLM nodes never ran
        at all, not just that the final status happens to match."""
        bad_alert = _base_alert(failure_type="NOT_A_REAL_FAILURE_TYPE")
        # Deliberately stub retrieval/search/llm with functions that raise —
        # if the short-circuit is broken, this test fails LOUDLY instead of
        # silently making a real network call.
        ag._reset_retrieval_deps_cache()
        stubs = {
            "query_retrieval": _stub_query_retrieval(
                lambda cur, embedding, failure_type, k: (_ for _ in ()).throw(
                    AssertionError("retrieve() should never be called for a rejected alert")
                )
            ),
            "llm": _stub_module(
                "llm",
                call_llm=lambda *a, **kw: (_ for _ in ()).throw(
                    AssertionError("call_llm() should never be called for a rejected alert")
                ),
            ),
        }
        with patch.dict(sys.modules, stubs):
            graph = ag.build_graph()
            final = graph.invoke(_base_state(alert=bad_alert))

        self.assertEqual(final["final_output"]["status"], "rejected_invalid_alert")
        self.assertEqual(final["retrieved_chunks"], [])  # never touched
        self.assertIsNone(final["diagnosis"])  # never touched


class TestRetrieveRunbookContext(unittest.TestCase):
    def test_calls_retrieval_and_maps_fields(self):
        # Real retrieve() returns (rows, used_fallback) — rows are raw tuples
        # (failure_type, section, source_file, content, symptoms_summary,
        # distance), ordered by cosine DISTANCE, not an object with .chunks.
        fake_rows = [
            ("connection_pool_exhaustion", "diagnosis_steps", "connection_pool_exhaustion.md",
             "some runbook content", "symptoms summary text", 0.09),  # distance 0.09 -> similarity 0.91
        ]
        ag._reset_retrieval_deps_cache()
        stub = _stub_query_retrieval(lambda cur, embedding, failure_type, k: (fake_rows, False))

        with patch.dict(sys.modules, {"query_retrieval": stub}):
            result = ag.retrieve_runbook_context(_base_state())

        self.assertAlmostEqual(result["retrieval_confidence"], 0.91)
        self.assertFalse(result["used_fallback"])
        self.assertEqual(len(result["retrieved_chunks"]), 1)
        self.assertEqual(result["retrieved_chunks"][0]["runbook_id"], "connection_pool_exhaustion.md")


class TestConfidenceGate(unittest.TestCase):
    def test_high_confidence_no_fallback_skips_search(self):
        state = _base_state(retrieval_confidence=0.95, used_fallback=False)
        self.assertFalse(ag.confidence_gate(state)["needs_web_search"])

    def test_low_confidence_triggers_search(self):
        state = _base_state(retrieval_confidence=0.40, used_fallback=False)
        self.assertTrue(ag.confidence_gate(state)["needs_web_search"])

    def test_fallback_triggers_search_even_if_confidence_high(self):
        # used_fallback means the failure_type exact-match filter was
        # bypassed — that alone should route to web search regardless
        # of the raw similarity score.
        state = _base_state(retrieval_confidence=0.95, used_fallback=True)
        self.assertTrue(ag.confidence_gate(state)["needs_web_search"])

    def test_route_after_confidence_gate(self):
        self.assertEqual(
            ag.route_after_confidence_gate(_base_state(needs_web_search=True)), "web_search"
        )
        self.assertEqual(
            ag.route_after_confidence_gate(_base_state(needs_web_search=False)), "diagnose_and_plan"
        )


class TestWebSearch(unittest.TestCase):
    def test_builds_query_and_returns_results(self):
        fake_results = [{"query": "x", "title": "t", "url": "http://x", "snippet": "s"}]
        stub = _stub_module("searchllm", search=lambda *a, **kw: fake_results)

        with patch.dict(sys.modules, {"searchllm": stub}):
            result = ag.web_search(_base_state())

        self.assertEqual(result["web_search_results"], fake_results)


class TestDiagnoseAndPlan(unittest.TestCase):
    def test_parses_diagnosis_and_plan_from_llm_response(self):
        fake_response = {
            "diagnosis": {
                "root_cause": "Connection pool undersized for load",
                "confidence": 0.87,
                "reasoning": "...",
                "sources": ["runbook"],
            },
            "remediation_plan": {
                "steps": [{"step_number": 1, "action": "Increase pool size", "reversible": True}],
                "overall_risk_level": "low",
            },
        }
        stub = _stub_module("llm", call_llm=lambda *a, **kw: fake_response)

        with patch.dict(sys.modules, {"llm": stub}):
            result = ag.diagnose_and_plan(_base_state())

        self.assertEqual(result["diagnosis"]["root_cause"], "Connection pool undersized for load")
        self.assertEqual(result["remediation_plan"]["overall_risk_level"], "low")
        self.assertIsNone(result["diagnose_error"])  # explicitly cleared on success

    def test_catches_llm_failure_instead_of_crashing(self):
        """The real point of this test: a raised exception from call_llm
        must NOT propagate out of diagnose_and_plan — it should be captured
        into diagnose_error so route_after_diagnose can decide what to do."""
        def raise_context_length_error(*a, **kw):
            raise ValueError("Input length 12222 exceeds maximum allowed token size 8192")

        stub = _stub_module("llm", call_llm=raise_context_length_error)

        with patch.dict(sys.modules, {"llm": stub}):
            result = ag.diagnose_and_plan(_base_state())  # must not raise

        self.assertIn("exceeds maximum allowed token size", result["diagnose_error"])
        self.assertNotIn("diagnosis", result)


class TestRouteAfterDiagnose(unittest.TestCase):
    def test_success_routes_to_risk_gate(self):
        state = _base_state(diagnose_error=None, diagnose_retry_count=0)
        self.assertEqual(ag.route_after_diagnose(state), "risk_gate")

    def test_failure_with_retries_remaining_routes_to_summarize(self):
        state = _base_state(diagnose_error="some error", diagnose_retry_count=0)
        self.assertEqual(ag.route_after_diagnose(state), "summarize_log")

    def test_failure_with_retries_exhausted_routes_to_failed(self):
        state = _base_state(diagnose_error="some error", diagnose_retry_count=ag.MAX_DIAGNOSE_RETRIES)
        self.assertEqual(ag.route_after_diagnose(state), "diagnosis_failed")


class TestSummarizeLog(unittest.TestCase):
    def test_shrinks_log_and_increments_retry_count(self):
        fake_summary = {"content": "Condensed: HikariCP pool exhausted, 40 errors between 15:49:23-15:49:24."}
        stub = _stub_module("llm", call_llm=lambda *a, **kw: fake_summary)

        state = _base_state(
            alert=_base_alert(log_snippet="x" * 20000),
            diagnose_retry_count=0,
        )
        with patch.dict(sys.modules, {"llm": stub}):
            result = ag.summarize_log(state)

        self.assertEqual(result["alert"]["log_snippet"], fake_summary["content"])
        self.assertLess(len(result["alert"]["log_snippet"]), 20000)
        self.assertEqual(result["diagnose_retry_count"], 1)
        # Everything else about the alert should be untouched.
        self.assertEqual(result["alert"]["failure_type"], "CONNECTION_POOL_EXHAUSTION")


class TestDiagnosisFailed(unittest.TestCase):
    def test_sets_failure_status_and_carries_error(self):
        state = _base_state(diagnose_error="RateLimitError: 429")
        result = ag.diagnosis_failed(state)
        self.assertEqual(result["final_output"]["status"], "diagnosis_failed")
        self.assertEqual(result["final_output"]["errors"], ["RateLimitError: 429"])
        self.assertIsNone(result["final_output"]["diagnosis"])


class TestRiskGate(unittest.TestCase):
    def _state_with_risk(self, risk_level):
        return _base_state(remediation_plan={"steps": [], "overall_risk_level": risk_level})

    def test_low_risk_routes_to_auto_recommend(self):
        self.assertEqual(ag.risk_gate(self._state_with_risk("low"))["risk_decision"], "auto_recommend")

    def test_medium_risk_routes_to_auto_recommend(self):
        self.assertEqual(ag.risk_gate(self._state_with_risk("medium"))["risk_decision"], "auto_recommend")

    def test_high_risk_routes_to_escalate(self):
        self.assertEqual(ag.risk_gate(self._state_with_risk("high"))["risk_decision"], "escalate")

    def test_runbook_floor_overrides_an_llm_that_under_rates(self):
        """The e2e eval case: bad deploy (runbook: high) rated medium."""
        state = _base_state(alert=_base_alert(failure_type="BAD_DEPLOY_ERROR_SPIKE"),
                            remediation_plan={"steps": [], "overall_risk_level": "medium"})
        result = ag.risk_gate(state)
        self.assertEqual(result, {"risk_decision": "escalate", "effective_risk_level": "high"})

    def test_llm_may_rate_higher_than_the_runbook(self):
        # CONNECTION_POOL_EXHAUSTION's runbook says low
        result = ag.risk_gate(self._state_with_risk("high"))
        self.assertEqual(result["effective_risk_level"], "high")

    def test_invalid_llm_risk_fails_safe_to_high(self):
        for bad in ("critical", None, "LOW"):
            with self.subTest(bad=bad):
                self.assertEqual(ag.risk_gate(self._state_with_risk(bad))["risk_decision"], "escalate")

    def test_route_after_risk_gate(self):
        self.assertEqual(
            ag.route_after_risk_gate(_base_state(risk_decision="escalate")), "escalate_for_approval"
        )
        self.assertEqual(
            ag.route_after_risk_gate(_base_state(risk_decision="auto_recommend")), "recommend_action"
        )


class TestOutcomeNodes(unittest.TestCase):
    def _state_with_diagnosis_and_plan(self):
        return _base_state(
            diagnosis={"root_cause": "x", "confidence": 0.9, "reasoning": "y", "sources": ["runbook"]},
            remediation_plan={"steps": [], "overall_risk_level": "low"},
        )

    def test_recommend_action_sets_status(self):
        result = ag.recommend_action(self._state_with_diagnosis_and_plan())
        self.assertEqual(result["final_output"]["status"], "auto_recommend")

    def test_escalate_for_approval_sets_status(self):
        result = ag.escalate_for_approval(self._state_with_diagnosis_and_plan())
        self.assertEqual(result["final_output"]["status"], "pending_approval")

    def test_respond_does_not_raise_on_populated_state(self):
        state = self._state_with_diagnosis_and_plan()
        state["final_output"] = ag.recommend_action(state)["final_output"]
        # respond() only logs and returns {} — this checks it doesn't
        # raise when the state is fully populated.
        self.assertEqual(ag.respond(state), {})


class TestFullGraphRetryLoop(unittest.TestCase):
    """Proves the actual loop wiring in build_graph(), not just the
    individual node functions in isolation."""

    def test_diagnose_failure_triggers_summarize_then_succeeds(self):
        call_count = {"n": 0}

        def flaky_call_llm(prompt, response_format="json"):
            if response_format == "text":
                return {"content": "condensed log summary"}
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ValueError("Input length exceeds maximum allowed token size")
            return {
                "diagnosis": {"root_cause": "x", "confidence": 0.8, "reasoning": "y", "sources": ["runbook"]},
                "remediation_plan": {"steps": [], "overall_risk_level": "low"},
            }

        fake_rows = [("r", "s", "source.md", "c", "symptoms", 0.05)]  # distance 0.05 -> similarity 0.95
        ag._reset_retrieval_deps_cache()
        stubs = {
            "query_retrieval": _stub_query_retrieval(lambda cur, embedding, failure_type, k: (fake_rows, False)),
            "llm": _stub_module("llm", call_llm=flaky_call_llm),
        }
        with patch.dict(sys.modules, stubs):
            graph = ag.build_graph()
            final = graph.invoke(_base_state())

        self.assertEqual(final["final_output"]["status"], "auto_recommend")
        self.assertEqual(final["diagnose_retry_count"], 1)
        self.assertIsNone(final["diagnose_error"])

    def test_diagnose_failure_exhausts_retries_and_fails_cleanly(self):
        def always_fails(prompt, response_format="json"):
            if response_format == "text":
                return {"content": "condensed"}
            raise ValueError("still too big")

        fake_rows = [("r", "s", "source.md", "c", "symptoms", 0.05)]
        ag._reset_retrieval_deps_cache()
        stubs = {
            "query_retrieval": _stub_query_retrieval(lambda cur, embedding, failure_type, k: (fake_rows, False)),
            "llm": _stub_module("llm", call_llm=always_fails),
        }
        with patch.dict(sys.modules, stubs):
            graph = ag.build_graph()
            final = graph.invoke(_base_state())

        self.assertEqual(final["final_output"]["status"], "diagnosis_failed")
        self.assertIn("still too big", final["final_output"]["errors"][0])

    def test_summarize_failure_ends_in_diagnosis_failed_instead_of_crashing(self):
        """Regression: a rate limit hit diagnose, then the summarize retry
        hit it too and raised out of the graph. Found by eval_agent.py."""
        calls = {"json": 0}

        def rate_limited(prompt, response_format="json"):
            if response_format == "json":
                calls["json"] += 1
            raise RuntimeError("Error code: 429 - Rate limit exceeded")

        fake_rows = [("r", "s", "source.md", "c", "symptoms", 0.05)]
        ag._reset_retrieval_deps_cache()
        stubs = {
            "query_retrieval": _stub_query_retrieval(lambda cur, embedding, failure_type, k: (fake_rows, False)),
            "llm": _stub_module("llm", call_llm=rate_limited),
        }
        with patch.dict(sys.modules, stubs):
            final = ag.build_graph().invoke(_base_state())  # must not raise

        self.assertEqual(final["final_output"]["status"], "diagnosis_failed")
        self.assertIn("summarize_log failed", final["final_output"]["errors"][0])
        self.assertEqual(calls["json"], 1)  # no second diagnose with an unshrunk log


class TestFullGraphAllBranches(unittest.TestCase):
    """Runs the whole compiled graph through all four gate combinations,
    with retrieval/search/llm stubbed so no external services are needed."""

    def _run_graph(self, retrieval_confidence, used_fallback, risk_level):
        # retrieve() returns (rows, used_fallback); rows are raw tuples
        # ordered by cosine DISTANCE (0 = identical). Convert the desired
        # similarity back to a distance so retrieve_runbook_context's
        # `1 - distance` computation lands on the value this test wants.
        fake_distance = 1 - retrieval_confidence
        fake_rows = [("r", "s", "source.md", "c", "symptoms", fake_distance)]
        fake_llm_response = {
            "diagnosis": {"root_cause": "x", "confidence": 0.8, "reasoning": "y", "sources": ["runbook"]},
            "remediation_plan": {"steps": [], "overall_risk_level": risk_level},
        }
        ag._reset_retrieval_deps_cache()
        stubs = {
            "query_retrieval": _stub_query_retrieval(
                lambda cur, embedding, failure_type, k, _rows=fake_rows, _uf=used_fallback: (_rows, _uf)
            ),
            "searchllm": _stub_module(
                "searchllm",
                search=lambda *a, **kw: [{"query": "q", "title": "t", "url": "http://x", "snippet": "s"}],
            ),
            "llm": _stub_module("llm", call_llm=lambda *a, **kw: fake_llm_response),
        }

        with patch.dict(sys.modules, stubs):
            graph = ag.build_graph()
            return graph.invoke(_base_state())

    def test_high_confidence_low_risk_skips_search_auto_recommends(self):
        final = self._run_graph(retrieval_confidence=0.95, used_fallback=False, risk_level="low")
        self.assertEqual(final["final_output"]["status"], "auto_recommend")
        self.assertEqual(final["web_search_results"], [])  # search node never ran

    def test_low_confidence_low_risk_runs_search_auto_recommends(self):
        final = self._run_graph(retrieval_confidence=0.30, used_fallback=False, risk_level="low")
        self.assertEqual(final["final_output"]["status"], "auto_recommend")
        self.assertTrue(len(final["web_search_results"]) > 0)  # search node ran

    def test_high_confidence_high_risk_escalates(self):
        final = self._run_graph(retrieval_confidence=0.95, used_fallback=False, risk_level="high")
        self.assertEqual(final["final_output"]["status"], "pending_approval")

    def test_low_confidence_high_risk_runs_search_and_escalates(self):
        final = self._run_graph(retrieval_confidence=0.30, used_fallback=False, risk_level="high")
        self.assertEqual(final["final_output"]["status"], "pending_approval")
        self.assertTrue(len(final["web_search_results"]) > 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)