"""
Tests for the MCP server (step 5), through the real MCP protocol: an
in-process mcp.Client for the tools, and synthetic Lambda Function URL
events through the Mangum entrypoint for HTTP auth and hosting.

    python -m unittest test_mcp_server -v
"""

import asyncio
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.pop("SSM_PARAMETER_PREFIX", None)

from mcp import Client  # noqa: E402

import mcp_server as ms  # noqa: E402
from test_lambda_handler import AwsTestCase, FakeGraph  # noqa: E402
from test_remediation import KEY, BASE_URL, RemediationAwsCase  # noqa: E402
from tools.remediation import RemediationRouter  # noqa: E402
from tools.runs import RunStore  # noqa: E402
from unittes import _stub_query_retrieval  # noqa: E402

TOKEN = "test-mcp-token"
REPO = os.path.dirname(os.path.abspath(__file__))


def backends(**overrides):
    base = dict(log_group=None, logs_client=None, fixtures_dir=os.path.join(REPO, "captured-logs"),
                mock_url=None, graph=FakeGraph(), store=None, router=None)
    base.update(overrides)
    return SimpleNamespace(**base)


def call(server, name, args=None):
    async def go():
        async with Client(server) as client:
            return await client.call_tool(name, args or {})
    return asyncio.run(go())


def list_tools(server):
    async def go():
        async with Client(server) as client:
            return (await client.list_tools()).tools
    return asyncio.run(go())


def error_text(result):
    return " ".join(getattr(c, "text", "") for c in result.content)


class TestToolSurface(unittest.TestCase):
    def test_read_only_by_default_and_never_an_approve_tool(self):
        read_only = {t.name for t in list_tools(ms.build_server(backends()))}
        with_writes = {t.name for t in list_tools(ms.build_server(backends(), enable_write_tools=True))}

        self.assertEqual(read_only, {"search_runbooks", "get_runbook", "get_recent_logs", "diagnose_alert",
                                     "get_run", "list_runs", "list_failures"})
        self.assertEqual(with_writes - read_only, {"inject_failure", "request_approval"})
        for name in with_writes:
            self.assertNotRegex(name, "approve|execute|remediat|deactivate|clear")

    def test_annotations_mark_read_and_write_tools(self):
        tools = {t.name: t for t in list_tools(ms.build_server(backends(), enable_write_tools=True))}
        self.assertTrue(tools["get_runbook"].annotations.read_only_hint)
        self.assertFalse(tools["inject_failure"].annotations.read_only_hint)
        self.assertTrue(tools["diagnose_alert"].annotations.open_world_hint)  # makes LLM calls


class TestReadTools(unittest.TestCase):
    def setUp(self):
        self.server = ms.build_server(backends())

    def test_get_runbook_reads_the_corpus(self):
        result = call(self.server, "get_runbook", {"failure_type": "oom_kill"})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["risk_level"], "high")
        self.assertIn("## Symptoms", result.structured_content["markdown"])

    def test_failure_type_cannot_be_a_path(self):
        for bad in ("../../.env", "NOT_A_MODE", ""):
            with self.subTest(bad=bad):
                result = call(self.server, "get_runbook", {"failure_type": bad})
                self.assertTrue(result.is_error)
                self.assertIn("Unknown failure_type", error_text(result))

    def test_recent_logs_fall_back_to_fixtures_without_cloudwatch(self):
        result = call(self.server, "get_recent_logs", {"failure_type": "DISK_FULL", "max_lines": 5})
        self.assertEqual(result.structured_content["source"], "fixture:captured-logs")
        self.assertEqual(len(result.structured_content["lines"]), 5)

    def test_recent_logs_use_cloudwatch_when_configured(self):
        logs_client = MagicMock()
        logs_client.filter_log_events.return_value = {"events": [{"timestamp": 1, "message": "cw line"}]}
        server = ms.build_server(backends(log_group="/ecs/mock", logs_client=logs_client))
        result = call(server, "get_recent_logs", {"failure_type": "OOM_KILL"})
        self.assertEqual(result.structured_content, {"source": "cloudwatch:/ecs/mock", "lines": ["cw line"]})
        self.assertIn("MEMORY_LEAK", logs_client.filter_log_events.call_args.kwargs["filterPattern"])

    def test_search_runbooks_uses_the_agents_retrieval_path(self):
        seen = {}

        def fake_retrieve(cur, emb, failure_type, k):
            seen.update(failure_type=failure_type, k=k)
            return [("disk_full", "remediation", "disk_full.md", "free space", "s", 0.12345)], False

        with patch.dict(sys.modules, {"query_retrieval": _stub_query_retrieval(fake_retrieve)}), \
                patch.dict(os.environ, {"OPENROUTER_API": "dummy"}):
            result = call(self.server, "search_runbooks", {"query": "no space left", "failure_type": "disk_full", "k": 50})

        self.assertEqual(seen, {"failure_type": "disk_full", "k": 10})  # lowercased, k capped
        top = result.structured_content["results"][0]
        self.assertEqual((top["runbook_id"], top["similarity_score"]), ("disk_full.md", 0.8765))

    def test_diagnose_is_advisory_and_size_capped(self):
        graph = FakeGraph()
        server = ms.build_server(backends(graph=graph))
        result = call(server, "diagnose_alert", {"failure_type": "DISK_FULL", "log_snippet": "disk full\ndisk full"})
        self.assertEqual(result.structured_content["outcome"], "auto_recommend")
        self.assertEqual(graph.invocations[0]["alert"]["failure_type"], "DISK_FULL")

        too_big = call(server, "diagnose_alert", {"failure_type": "DISK_FULL", "log_snippet": "x" * 50_001})
        self.assertTrue(too_big.is_error)
        self.assertEqual(len(graph.invocations), 1)

    def test_missing_backend_names_the_setting(self):
        result = call(self.server, "list_runs")
        self.assertTrue(result.is_error)
        self.assertIn("RUNS_TABLE_NAME", error_text(result))

    def test_mock_admin_tools_hit_the_service(self):
        server = ms.build_server(backends(mock_url="http://mock:8080"), enable_write_tools=True)
        with patch("tools.mock_admin.requests") as requests:
            requests.post.return_value.json.return_value = {"status": "activated"}
            call(server, "inject_failure", {"failure_type": "disk_full"})
        requests.post.assert_called_once_with("http://mock:8080/admin/failures/disk_full/activate", timeout=5)


class TestRunTools(RemediationAwsCase):
    def server(self, write=False):
        router = RemediationRouter(queue=self.queue, sns_client=self.sns, topic_arn=self.topic_arn,
                                   approval_base_url=BASE_URL, hmac_key=KEY)
        return ms.build_server(backends(store=self.store, router=router), enable_write_tools=write)

    def test_get_and_list_runs_return_plain_json_and_hide_pointers(self):
        self.seed_pending_run("run-1")
        self.store.link_alarm("arn:alarm", "run-1")

        run = call(self.server(), "get_run", {"run_id": "run-1"}).structured_content
        self.assertEqual(run["approval_status"], "pending")
        self.assertIsInstance(run["final_output"]["diagnosis"]["confidence"], float)  # not Decimal

        runs = call(self.server(), "list_runs", {"approval_status": "pending"}).structured_content["runs"]
        self.assertEqual([r["run_id"] for r in runs], ["run-1"])
        self.assertTrue(call(self.server(), "get_run", {"run_id": "alarm#arn:alarm"}).is_error)

    def test_request_approval_resends_links_but_only_for_pending_runs(self):
        self.seed_pending_run("run-1")
        result = call(self.server(write=True), "request_approval", {"run_id": "run-1"})
        self.assertFalse(result.is_error)
        self.assertEqual(len(self.emails()), 1)
        self.assertEqual(self.drain(self.queue_url), [])  # nothing queued, nothing approved

        self.store.decide("run-1", "approve")
        again = call(self.server(write=True), "request_approval", {"run_id": "run-1"})
        self.assertTrue(again.is_error)
        self.assertIn("not pending", error_text(again))


def function_url_event(body: dict, token: str | None = TOKEN, host="abc123.lambda-url.us-east-1.on.aws"):
    headers = {"host": host, "content-type": "application/json",
               "accept": "application/json, text/event-stream"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return {
        "version": "2.0", "rawPath": "/mcp", "rawQueryString": "", "headers": headers,
        "requestContext": {"http": {"method": "POST", "path": "/mcp", "sourceIp": "1.2.3.4",
                                    "protocol": "HTTP/1.1"},
                           "domainName": host, "requestId": "req-1", "stage": "$default"},
        "body": json.dumps(body), "isBase64Encoded": False,
    }


INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "test", "version": "0"}}}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


class TestLambdaHttpEntrypoint(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"MCP_AUTH_TOKEN": TOKEN})
        env.start()
        self.addCleanup(env.stop)
        server = patch.object(ms, "_lambda_server", ms.build_server(backends()))
        server.start()
        self.addCleanup(server.stop)

    def test_missing_or_wrong_token_is_rejected(self):
        self.assertEqual(ms.handler(function_url_event(INITIALIZE, token=None), None)["statusCode"], 401)
        self.assertEqual(ms.handler(function_url_event(INITIALIZE, token="wrong"), None)["statusCode"], 401)

    def test_authorized_requests_work_across_warm_invocations(self):
        """Several invocations in one process = one warm Lambda container.
        Would fail with a shared app, since the session manager runs once."""
        for _ in range(3):
            init = ms.handler(function_url_event(INITIALIZE), None)
            self.assertEqual(init["statusCode"], 200, init.get("body"))
            tools = ms.handler(function_url_event(LIST_TOOLS), None)
            self.assertEqual(tools["statusCode"], 200, tools.get("body"))
            names = {t["name"] for t in json.loads(tools["body"])["result"]["tools"]}
            self.assertIn("get_runbook", names)

    def test_sdk_default_would_reject_the_function_url_host(self):
        """Documents why handler() turns the DNS-rebinding guard off."""
        from mangum import Mangum
        app = ms.http_app(ms.build_server(backends()), TOKEN)  # SDK defaults: localhost only
        response = Mangum(app, lifespan="auto")(function_url_event(INITIALIZE), None)
        self.assertNotEqual(response["statusCode"], 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
