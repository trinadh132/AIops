"""
Tests for the step 3 Lambda path: tools.config, tools.logs, tools.runs and
lambda_handler.

DynamoDB and SSM run in-memory via moto; CloudWatch Logs is a small fake
(FilterLogEvents paging is the only behavior we rely on). No AWS account or
network needed.

    python -m unittest test_lambda_handler -v
"""

import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import boto3
from moto import mock_aws

from tools import config, logs
from tools.runs import RunStore

# Set before importing lambda_handler: it calls load_config() at import.
os.environ.pop("SSM_PARAMETER_PREFIX", None)
import lambda_handler as lh  # noqa: E402
import agentic as ag  # noqa: E402
from unittes import _stub_module, _stub_query_retrieval  # noqa: E402

AWS_ENV = {
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
}

ALARM_ARN = "arn:aws:cloudwatch:us-east-1:123456789012:alarm:ops-agent-disk_full"


def alarm_message(state="ALARM", metric="DISK_FULL", changed="2026-09-29T12:00:00.000+0000"):
    return {
        "AlarmName": "ops-agent-disk_full",
        "AlarmArn": ALARM_ARN,
        "NewStateValue": state,
        "NewStateReason": "Threshold Crossed: 1 datapoint [12.0] was greater than the threshold (5.0).",
        "StateChangeTime": changed,
        "Trigger": {"MetricName": metric, "Namespace": "OpsAgent/MockService"},
    }


def sns_event(*messages):
    return {"Records": [{"Sns": {"Message": m if isinstance(m, str) else json.dumps(m)}} for m in messages]}


class FakeLogsClient:
    """Serves pre-baked FilterLogEvents pages and records the requests."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def filter_log_events(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages.pop(0) if self.pages else {"events": []}


def log_events(*messages, start_ts=1_000):
    return [{"timestamp": start_ts + i, "message": m + "\n"} for i, m in enumerate(messages)]


class FakeGraph:
    """Returns a completed state; records what it was invoked with."""

    def __init__(self, risk="low", raises=None):
        self.risk, self.raises, self.invocations = risk, raises, []

    def invoke(self, state):
        self.invocations.append(state)
        if self.raises:
            raise self.raises
        diagnosis = {"root_cause": "disk full", "confidence": 0.9, "reasoning": "r", "sources": ["runbook"]}
        return {
            **state,
            "retrieved_chunks": [{"runbook_id": "disk_full.md", "section": "symptoms",
                                  "content": "c", "similarity_score": 0.91}],
            "retrieval_confidence": 0.91,
            "diagnosis": diagnosis,
            "remediation_plan": {"steps": [], "overall_risk_level": self.risk},
            "final_output": {"alert_id": state["alert"]["alert_id"], "status": "auto_recommend",
                             "diagnosis": diagnosis, "remediation_plan": {"steps": [], "overall_risk_level": self.risk},
                             "sources": ["runbook"]},
        }


class FakeRemediator:
    """Stands in for RemediationRouter; routing itself is tested in
    test_remediation.py."""

    def __init__(self):
        self.routed = []

    def route(self, run_id, failure_type, final_output, now=None):
        self.routed.append((run_id, failure_type, final_output.get("status")))
        return {"remediation_status": "awaiting_approval"}


class AwsTestCase(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, AWS_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def make_table(self):
        return boto3.resource("dynamodb").create_table(
            TableName="agent_runs",
            KeySchema=[{"AttributeName": "run_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "run_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )


# ---- tools.config -----------------------------------------------------------

class TestLoadConfig(AwsTestCase):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in ("SSM_PARAMETER_PREFIX", *config.REQUIRED_KEYS):
            os.environ.pop(key, None)
        self.ssm = boto3.client("ssm")

    def test_no_prefix_is_a_noop(self):
        config.load_config(ssm_client=None)  # would fail if it tried to call AWS without a client
        self.assertNotIn("OPENROUTER_API", os.environ)

    def test_loads_from_ssm_without_overriding_env(self):
        self.ssm.put_parameter(Name="/ops-agent/OPENROUTER_API", Value="from-ssm", Type="SecureString")
        self.ssm.put_parameter(Name="/ops-agent/DATABASE_URL", Value="postgres://ssm", Type="SecureString")
        os.environ["SSM_PARAMETER_PREFIX"] = "/ops-agent"
        os.environ["DATABASE_URL"] = "postgres://explicit"

        config.load_config(ssm_client=self.ssm)

        self.assertEqual(os.environ["OPENROUTER_API"], "from-ssm")  # decrypted SecureString
        self.assertEqual(os.environ["DATABASE_URL"], "postgres://explicit")  # env wins

    def test_missing_required_parameter_raises(self):
        self.ssm.put_parameter(Name="/ops-agent/OPENROUTER_API", Value="x", Type="SecureString")
        os.environ["SSM_PARAMETER_PREFIX"] = "/ops-agent"
        with self.assertRaisesRegex(RuntimeError, "DATABASE_URL"):
            config.load_config(ssm_client=self.ssm)


# ---- tools.logs ---------------------------------------------------------------

class TestFetchAlarmLogs(unittest.TestCase):
    START = datetime(2026, 9, 29, 11, 55, tzinfo=timezone.utc)
    END = datetime(2026, 9, 29, 12, 1, tzinfo=timezone.utc)

    def test_oom_kill_pattern_includes_memory_leak_growth_phase(self):
        pattern = logs.build_filter_pattern("OOM_KILL")
        self.assertIn('$.failure_mode = "OOM_KILL"', pattern)
        self.assertIn('$.failure_mode = "MEMORY_LEAK"', pattern)
        self.assertEqual(logs.build_filter_pattern("DISK_FULL"), '{ $.failure_mode = "DISK_FULL" }')

    def test_follows_pages_including_empty_ones_and_sorts(self):
        client = FakeLogsClient([
            {"events": log_events("b", start_ts=2_000), "nextToken": "t1"},
            {"events": [], "nextToken": "t2"},  # FilterLogEvents does this mid-scan
            {"events": log_events("a", start_ts=1_000)},
        ])
        lines = logs.fetch_alarm_logs(client, "/grp", "DISK_FULL", self.START, self.END)
        self.assertEqual(lines, ["a", "b"])
        self.assertEqual([c.get("nextToken") for c in client.calls], [None, "t1", "t2"])
        self.assertEqual(client.calls[0]["startTime"], int(self.START.timestamp() * 1000))

    def test_keeps_most_recent_events_when_over_cap(self):
        client = FakeLogsClient([{"events": log_events(*[f"l{i}" for i in range(10)])}])
        lines = logs.fetch_alarm_logs(client, "/grp", "DISK_FULL", self.START, self.END, max_events=3)
        self.assertEqual(lines, ["l7", "l8", "l9"])

    def test_page_cap_bounds_endless_tokens(self):
        class Endless:
            calls = 0
            def filter_log_events(self, **kw):
                Endless.calls += 1
                return {"events": [], "nextToken": "again"}
        self.assertEqual(logs.fetch_alarm_logs(Endless(), "/grp", "DISK_FULL", self.START, self.END), [])
        self.assertEqual(Endless.calls, logs.MAX_PAGES)


# ---- tools.runs ---------------------------------------------------------------

class TestRunStore(AwsTestCase):
    def setUp(self):
        super().setUp()
        self.table = self.make_table()
        self.store = RunStore(self.table, lease_seconds=900)

    def test_duplicate_claim_within_lease_is_rejected(self):
        self.assertIsNotNone(self.store.claim("run-1", {"failure_type": "DISK_FULL"}, now=1000))
        self.assertIsNone(self.store.claim("run-1", {"failure_type": "DISK_FULL"}, now=1100))

    def test_completed_run_cannot_be_reclaimed(self):
        claim = self.store.claim("run-1", {}, now=1000)
        self.assertTrue(self.store.complete("run-1", claim, {"outcome": "auto_recommend"}, now=1010))
        self.assertIsNone(self.store.claim("run-1", {}, now=99_999))

    def test_errored_run_can_be_retried(self):
        claim = self.store.claim("run-1", {}, now=1000)
        self.store.fail("run-1", claim, "RateLimitError: 429", now=1010)
        self.assertIsNotNone(self.store.claim("run-1", {}, now=1020))
        item = self.store.get("run-1")
        self.assertEqual(item["status"], "running")
        self.assertEqual(item["attempts"], 2)

    def test_stale_running_claim_is_taken_over_and_old_owner_cannot_finish(self):
        old = self.store.claim("run-1", {}, now=1000)
        new = self.store.claim("run-1", {}, now=1000 + 901)  # lease expired: old attempt presumed dead
        self.assertIsNotNone(new)

        self.assertFalse(self.store.complete("run-1", old, {"outcome": "stale"}))
        self.assertTrue(self.store.complete("run-1", new, {"outcome": "fresh"}))
        self.assertEqual(self.store.get("run-1")["outcome"], "fresh")

    def test_nested_floats_are_stored(self):
        claim = self.store.claim("run-1", {}, now=1000)
        self.store.complete("run-1", claim, {"retrieval_confidence": 0.91,
                                             "retrieved": [{"similarity_score": 0.5}]})
        item = self.store.get("run-1")
        self.assertEqual(float(item["retrieval_confidence"]), 0.91)
        self.assertIn("expires_at", item)  # TTL attribute set on claim


# ---- lambda_handler -------------------------------------------------------------

class TestHandleAlarm(AwsTestCase):
    def setUp(self):
        super().setUp()
        self.store = RunStore(self.make_table())
        self.remediator = FakeRemediator()
        self.logs = FakeLogsClient([{"events": log_events(
            '{"failure_mode":"DISK_FULL","message":"No space left on device"}',
            '{"failure_mode":"DISK_FULL","message":"No space left on device"}',
        )}])

    def handle(self, message, graph):
        return lh.handle_alarm(message, store=self.store, logs_client=self.logs, graph=graph,
                               remediator=self.remediator, log_group="/ecs/mock", service="mock-svc")

    def test_ok_transition_is_ignored(self):
        graph = FakeGraph()
        result = self.handle(alarm_message(state="OK"), graph)
        self.assertEqual(result["skipped"], "not_alarm_state")
        self.assertEqual(graph.invocations, [])

    def test_alarm_runs_graph_and_records_outcome(self):
        graph = FakeGraph()
        result = self.handle(alarm_message(), graph)

        alert = graph.invocations[0]["alert"]
        self.assertEqual(alert["failure_type"], "DISK_FULL")
        self.assertEqual(alert["alert_id"], result["run_id"])
        self.assertIn("No space left on device", alert["log_snippet"])
        # 5-minute window ending 1 minute past the state change
        self.assertEqual(self.logs.calls[0]["endTime"] - self.logs.calls[0]["startTime"], 6 * 60 * 1000)

        item = self.store.get(result["run_id"])
        self.assertEqual(item["status"], "completed")
        self.assertEqual(item["outcome"], "auto_recommend")
        self.assertEqual(item["remediation_status"], "awaiting_approval")
        self.assertEqual(self.remediator.routed, [(result["run_id"], "DISK_FULL", "auto_recommend")])
        self.assertEqual(item["log_line_count"], 2)
        self.assertIn("repeated 2x", item["log_excerpt"])  # condensed, not raw

    def test_duplicate_delivery_runs_graph_once(self):
        graph = FakeGraph()
        first = self.handle(alarm_message(), graph)
        second = self.handle(alarm_message(), graph)
        self.assertEqual(second, {"run_id": first["run_id"], "skipped": "duplicate"})
        self.assertEqual(len(graph.invocations), 1)

    def test_new_transition_of_same_alarm_is_a_new_run(self):
        graph = FakeGraph()
        a = self.handle(alarm_message(changed="2026-09-29T12:00:00.000+0000"), graph)
        b = self.handle(alarm_message(changed="2026-09-29T13:00:00.000+0000"), graph)
        self.assertNotEqual(a["run_id"], b["run_id"])

    def test_graph_failure_marks_error_reraises_and_retry_can_reclaim(self):
        with self.assertRaises(ConnectionError):
            self.handle(alarm_message(), FakeGraph(raises=ConnectionError("neon unreachable")))
        run_id = lh.run_id_for(alarm_message())
        item = self.store.get(run_id)
        self.assertEqual(item["status"], "error")
        self.assertIn("neon unreachable", item["error"])

        retry = self.handle(alarm_message(), FakeGraph())  # Lambda's async retry
        self.assertEqual(retry["outcome"], "auto_recommend")

    def test_ok_transition_records_recovery_on_latest_run(self):
        run = self.handle(alarm_message(changed="2026-09-29T12:00:00.000+0000"), FakeGraph())
        ok = {**alarm_message(state="OK", changed="2026-09-29T12:03:30.000+0000"), "OldStateValue": "ALARM"}

        result = self.handle(ok, FakeGraph())

        self.assertEqual(result["recovered_run_id"], run["run_id"])
        self.assertEqual(float(self.store.get(run["run_id"])["seconds_to_recover"]), 210.0)
        self.assertIsNone(self.handle(ok, FakeGraph())["recovered_run_id"])  # recorded once

    def test_ok_without_prior_run_is_harmless(self):
        ok = {**alarm_message(state="OK"), "OldStateValue": "ALARM"}
        self.assertIsNone(self.handle(ok, FakeGraph())["recovered_run_id"])

    def test_no_log_lines_falls_back_to_alarm_reason(self):
        self.logs = FakeLogsClient([{"events": []}])
        graph = FakeGraph()
        self.handle(alarm_message(), graph)
        self.assertIn("Threshold Crossed", graph.invocations[0]["alert"]["log_snippet"])


class TestHandlerEntrypoint(AwsTestCase):
    def test_parses_sns_and_skips_non_alarm_messages(self):
        store = RunStore(self.make_table())
        graph = FakeGraph()
        deps = {"store": store, "logs_client": FakeLogsClient([{"events": log_events("x")}]), "graph": graph,
                "remediator": FakeRemediator()}
        with patch.object(lh, "_deps", return_value=deps):
            results = lh.handler(sns_event("not json", {"hello": "world"}, alarm_message()), context=None)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["outcome"], "auto_recommend")

    def test_real_graph_end_to_end_with_stubbed_llm_and_retrieval(self):
        """The real compiled graph, so a mismatch between what the handler
        builds and what the graph expects fails here, not in AWS."""
        store = RunStore(self.make_table())
        fake_rows = [("disk_full", "symptoms", "disk_full.md", "c", "s", 0.05)]
        llm_response = {
            "diagnosis": {"root_cause": "disk full", "confidence": 0.8, "reasoning": "r", "sources": ["runbook"]},
            "remediation_plan": {"steps": [{"step_number": 1, "action": "clean up", "reversible": True}],
                                 "overall_risk_level": "high"},
        }
        stubs = {
            "query_retrieval": _stub_query_retrieval(lambda cur, emb, ft, k: (fake_rows, False)),
            "llm": _stub_module("llm", call_llm=lambda *a, **kw: llm_response),
        }
        with patch.dict(sys.modules, stubs), patch.dict(os.environ, {"OPENROUTER_API": "dummy"}):
            deps = {"store": store, "logs_client": FakeLogsClient([{"events": log_events("disk error")}]),
                    "graph": ag.build_graph(), "remediator": FakeRemediator()}
            with patch.object(lh, "_deps", return_value=deps):
                [result] = lh.handler(sns_event(alarm_message()), context=None)

        self.assertEqual(result["outcome"], "pending_approval")  # high risk escalates
        item = store.get(result["run_id"])
        self.assertEqual(item["final_output"]["remediation_plan"]["overall_risk_level"], "high")
        self.assertEqual(item["retrieved"][0]["runbook_id"], "disk_full.md")


if __name__ == "__main__":
    unittest.main(verbosity=2)
