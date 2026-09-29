"""
MCP server: the ops agent's capabilities for MCP clients (desktop
assistants, IDE agents, anything that speaks MCP).

A thin adapter. Every tool is a few lines over tools/ and agentic.py, the
same code the Lambda handlers run, so what a human sees through MCP and what
the agent does in production can't drift apart. The LangGraph agent itself
does NOT call these tools over MCP: it runs in the same process as the code,
so a protocol hop would add latency and a failure mode for nothing.

Transports:
  python mcp_server.py                          stdio (clients launch it)
  python mcp_server.py --http [--host --port]   streamable HTTP, bearer auth
  Lambda, command "mcp_server.handler"          Function URL, bearer auth

Guardrails:
  - Write tools (inject_failure, request_approval) exist only when
    MCP_ENABLE_WRITE_TOOLS=true.
  - No tool can approve or execute a remediation. Approval stays with a
    human clicking a signed link; otherwise any MCP client, or a
    prompt-injected log line steering one, could skip the human.
  - diagnose_alert is advisory: it neither records a run nor routes a fix.
"""

import argparse
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from functools import cached_property
from pathlib import Path
from typing import Any

from tools.config import REQUIRED_KEYS, load_config

logger = logging.getLogger("self_healing_ops.mcp")

MCP_KEYS = ("MCP_AUTH_TOKEN",)
# Only resolved from SSM when SSM_PARAMETER_PREFIX is set (i.e. in Lambda).
load_config(required=REQUIRED_KEYS + MCP_KEYS)

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

import agentic as ag  # noqa: E402
from tools import logs as logs_tools  # noqa: E402
from tools import mock_admin, runbooks  # noqa: E402
from tools.runs import from_dynamo  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent
MAX_LOG_SNIPPET_CHARS = 50_000
READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
READ_EXTERNAL = ToolAnnotations(read_only_hint=True, open_world_hint=True)  # calls out (LLM, embeddings)


class EnvBackends:
    """What the tools talk to, built lazily from the environment. The server
    starts with whatever is configured; a tool whose backend is missing fails
    with a message saying which setting it needs."""

    def __init__(self, env=os.environ):
        self.env = env
        self.log_group = env.get("LOG_GROUP_NAME")
        self.fixtures_dir = env.get("CAPTURED_LOGS_DIR", str(REPO_ROOT / "captured-logs"))
        self.mock_url = env.get("MOCK_SERVICE_URL")

    @cached_property
    def graph(self):
        return ag.build_graph()

    @cached_property
    def logs_client(self):
        import boto3
        return boto3.client("logs") if self.log_group else None

    @cached_property
    def store(self):
        if not self.env.get("RUNS_TABLE_NAME"):
            return None
        import boto3
        from tools.runs import RunStore
        return RunStore(boto3.resource("dynamodb").Table(self.env["RUNS_TABLE_NAME"]))

    @cached_property
    def router(self):
        needed = ("APPROVAL_TOPIC_ARN", "APPROVAL_BASE_URL", "APPROVAL_HMAC_KEY", "REMEDIATION_QUEUE_URL")
        if not all(self.env.get(k) for k in needed):
            return None
        import boto3
        from tools.remediation import RemediationQueue, RemediationRouter
        return RemediationRouter(
            queue=RemediationQueue(boto3.client("sqs"), self.env["REMEDIATION_QUEUE_URL"]),
            sns_client=boto3.client("sns"), topic_arn=self.env["APPROVAL_TOPIC_ARN"],
            approval_base_url=self.env["APPROVAL_BASE_URL"], hmac_key=self.env["APPROVAL_HMAC_KEY"],
        )


def _require(backend, setting: str):
    if backend is None:
        raise ToolError(f"Not configured on this server: set {setting}.")
    return backend


def _failure_type(value: str) -> str:
    try:
        return runbooks.normalize_failure_type(value)
    except ValueError as e:
        raise ToolError(str(e)) from e


def build_server(backends, enable_write_tools: bool = False) -> MCPServer:
    # Tools return dict[str, Any], not bare dict: the SDK only publishes a
    # structured output schema (and fills structured_content) for the
    # parameterized form; bare dict silently degrades to plain text.
    server = MCPServer(
        "self-healing-ops",
        instructions=(
            "Tools for a self-healing ops agent watching a mock orders service. "
            "Failure types are FailureMode enum names such as DISK_FULL or OOM_KILL. "
            "Typical flow: get_recent_logs -> search_runbooks or get_runbook -> diagnose_alert. "
            "Remediations cannot be approved or executed through these tools."
        ),
    )

    @server.tool(annotations=READ_EXTERNAL)
    def search_runbooks(query: str, failure_type: str | None = None, k: int = 3) -> dict[str, Any]:
        """Semantic search over runbook sections. failure_type, if given,
        restricts results to that failure mode's runbook (falls back to the
        whole corpus if nothing is indexed for it)."""
        ft = _failure_type(failure_type) if failure_type else None
        return runbooks.search_runbooks(query, ft, k)

    @server.tool(annotations=READ)
    def get_runbook(failure_type: str) -> dict[str, Any]:
        """The full runbook for one failure mode: symptoms, root causes,
        diagnosis steps, remediation, rollback."""
        return runbooks.get_runbook(_failure_type(failure_type))

    @server.tool(annotations=READ)
    def get_recent_logs(failure_type: str, minutes: int = 15, max_lines: int = 200) -> dict[str, Any]:
        """Recent structured log lines tagged with this failure mode: from
        CloudWatch when LOG_GROUP_NAME is set, otherwise the captured-logs/
        fixture for that mode."""
        ft = _failure_type(failure_type)
        max_lines = max(1, min(max_lines, 1000))
        if backends.log_group:
            end = datetime.now(timezone.utc)
            lines = logs_tools.fetch_alarm_logs(
                backends.logs_client, backends.log_group, ft,
                start=end - timedelta(minutes=max(1, min(minutes, 24 * 60))), end=end, max_events=max_lines,
            )
            return {"source": f"cloudwatch:{backends.log_group}", "lines": lines}
        lines = logs_tools.read_fixture_logs(backends.fixtures_dir, ft, max_lines)
        return {"source": "fixture:captured-logs", "lines": lines}

    @server.tool(annotations=READ_EXTERNAL)
    def diagnose_alert(failure_type: str, log_snippet: str,
                       service: str = "self-healing-ops-mock-service") -> dict[str, Any]:
        """Run the full diagnosis graph (retrieval, optional web search, LLM)
        on a log snippet. Advisory only: nothing is recorded, queued or
        emailed. Makes real LLM calls."""
        ft = _failure_type(failure_type)
        if len(log_snippet) > MAX_LOG_SNIPPET_CHARS:
            raise ToolError(f"log_snippet is over {MAX_LOG_SNIPPET_CHARS} characters; send the relevant part.")
        ag._reset_retrieval_deps_cache()
        final = backends.graph.invoke(ag.initial_state(ag.build_alert(ft, service, log_snippet)))
        return ag.summarize_run(final)

    @server.tool(annotations=READ)
    def get_run(run_id: str) -> dict[str, Any]:
        """Full record of one agent run: diagnosis, plan, approval and
        remediation status, time to recover."""
        store = _require(backends.store, "RUNS_TABLE_NAME")
        run = store.get(run_id)
        if not run or run_id.startswith("alarm#"):
            raise ToolError(f"No run {run_id!r}.")
        return from_dynamo(run)

    @server.tool(annotations=READ)
    def list_runs(limit: int = 20, approval_status: str | None = None) -> dict[str, Any]:
        """Newest agent runs. approval_status filters, e.g. "pending"."""
        store = _require(backends.store, "RUNS_TABLE_NAME")
        return {"runs": from_dynamo(store.list_runs(max(1, min(limit, 100)), approval_status))}

    @server.tool(annotations=READ)
    def list_failures() -> dict[str, Any]:
        """Failure modes currently active on the mock service."""
        return mock_admin.list_failures(_require(backends.mock_url, "MOCK_SERVICE_URL"))

    if enable_write_tools:
        @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                                 idempotent_hint=True, open_world_hint=False))
        def inject_failure(failure_type: str) -> dict[str, Any]:
            """Demo only: activate a failure mode on the mock service to
            start an incident. There is deliberately no matching 'clear'
            tool; fixes go through the approval flow."""
            return mock_admin.inject_failure(_require(backends.mock_url, "MOCK_SERVICE_URL"), _failure_type(failure_type))

        @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                                 idempotent_hint=False, open_world_hint=True))
        def request_approval(run_id: str) -> dict[str, Any]:
            """Re-send the approval email (fresh signed links) for a run
            that is still pending. Does not approve anything."""
            router = _require(backends.router, "APPROVAL_TOPIC_ARN/APPROVAL_BASE_URL/APPROVAL_HMAC_KEY/REMEDIATION_QUEUE_URL")
            try:
                return router.resend_approval(_require(backends.store, "RUNS_TABLE_NAME"), run_id)
            except (LookupError, ValueError) as e:
                raise ToolError(str(e)) from e

    return server


# ---- HTTP transports ----------------------------------------------------------

class BearerAuth:
    """ASGI middleware: every HTTP request needs `Authorization: Bearer
    <MCP_AUTH_TOKEN>`. Compared as bytes in constant time."""

    def __init__(self, app, token: str):
        if not token:
            raise RuntimeError("MCP_AUTH_TOKEN must be set for the HTTP transport")
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            supplied = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": json.dumps({"error": "unauthorized"}).encode()})
                return
        await self.app(scope, receive, send)


def http_app(server: MCPServer, token: str, *, host: str = "127.0.0.1",
             transport_security: TransportSecuritySettings | None = None):
    # Stateless + plain JSON responses: no server-side session to lose
    # between Lambda invocations or container restarts, and no long-lived
    # SSE stream that a buffered Lambda response couldn't carry.
    app = server.streamable_http_app(stateless_http=True, json_response=True, host=host,
                                     transport_security=transport_security)
    return BearerAuth(app, token)


_lambda_server: MCPServer | None = None


def handler(event, context):
    """Lambda Function URL entrypoint."""
    from mangum import Mangum

    global _lambda_server
    if _lambda_server is None:
        _lambda_server = build_server(EnvBackends(), os.environ.get("MCP_ENABLE_WRITE_TOOLS", "false").lower() == "true")
    # A fresh ASGI app per invocation: the SDK's session manager may only be
    # started once per instance, and Mangum runs the app's lifespan on every
    # invocation. The MCPServer (tools, cached backends) is reused.
    app = http_app(
        _lambda_server, os.environ.get("MCP_AUTH_TOKEN", ""),
        # The SDK's DNS-rebinding guard only admits localhost Host headers by
        # default, which would reject the Function URL's own hostname. That
        # guard protects servers on a developer's machine from malicious web
        # pages; a public endpoint behind a bearer token doesn't need it.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return Mangum(app, lifespan="auto")(event, context)


def main():
    parser = argparse.ArgumentParser(description="Self-healing ops MCP server")
    parser.add_argument("--http", action="store_true", help="Serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server = build_server(EnvBackends(), os.environ.get("MCP_ENABLE_WRITE_TOOLS", "false").lower() == "true")
    if not args.http:
        server.run("stdio")
        return

    import uvicorn
    uvicorn.run(http_app(server, os.environ.get("MCP_AUTH_TOKEN", ""), host=args.host), host=args.host, port=args.port)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
