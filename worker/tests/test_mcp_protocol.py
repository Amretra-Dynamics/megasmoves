from __future__ import annotations

import json
import queue
import sys
import threading
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

import pytest

from megas.mcp.capabilities import CapabilityStore
from megas.mcp.cli import _parser, _run
from megas.mcp.model import MCPServer
from megas.mcp.protocol import call_tool, call_tool_stdio, probe_server, probe_stdio
from megas.mcp.runtime import MCPRuntime
from megas.mcp.store import MCPStore


FAKE_SERVER = r'''
import json
import os
import sys


def send(payload):
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


schema_variant = os.environ.get("ECHO_SCHEMA", "1")
for line in sys.stdin:
    if not line.strip():
        continue
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")

    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "fake", "version": schema_variant},
                "capabilities": {"tools": {}},
            },
        })
    elif method == "tools/list":
        schema = {"type": "object"}
        if schema_variant == "2":
            schema = {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            }
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [{
                    "name": "echo",
                    "description": "Echo arguments",
                    "inputSchema": schema,
                }]
            },
        })
    elif method == "resources/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"resources": []}})
    elif method == "prompts/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"prompts": []}})
    elif method == "tools/call":
        params = request.get("params") or {}
        arguments = params.get("arguments") or {}
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "isError": False,
                "content": [{"type": "text", "text": json.dumps(arguments, sort_keys=True)}],
                "structuredContent": {"echo": arguments},
            },
        })
'''


def _fake_server(tmp_path, *, schema: str = "1") -> MCPServer:
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    return MCPServer(
        name="fake",
        command=sys.executable,
        args=[str(script)],
        env={"ECHO_SCHEMA": schema},
    )


def _remote_result(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if request_id is None:
        return None
    if method == "initialize":
        result: dict[str, Any] = {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "remote-fake", "version": "1.0"},
            "capabilities": {"tools": {}},
        }
    elif method == "tools/list":
        result = {
            "tools": [{
                "name": "echo",
                "description": "Echo arguments",
                "inputSchema": {"type": "object"},
            }]
        }
    elif method == "resources/list":
        result = {"resources": []}
    elif method == "prompts/list":
        result = {"prompts": []}
    elif method == "tools/call":
        params = request.get("params") or {}
        arguments = params.get("arguments") or {}
        result = {
            "isError": False,
            "content": [{"type": "text", "text": json.dumps(arguments, sort_keys=True)}],
            "structuredContent": {"echo": arguments},
        }
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"unknown method: {method}"},
        }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


class _HTTPMCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send(self, status: int, payload: dict[str, Any] | None, *, session: bool = False) -> None:
        data = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        if session:
            self.send_header("Mcp-Session-Id", "session-1")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_POST(self) -> None:
        if self.path != "/mcp":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        method = request.get("method")
        if method != "initialize" and self.headers.get("Mcp-Session-Id") != "session-1":
            self._send(400, {"error": "missing session"})
            return
        response = _remote_result(request)
        if response is None:
            self._send(202, None)
        else:
            self._send(200, response, session=method == "initialize")

    def do_DELETE(self) -> None:
        if self.path != "/mcp" or self.headers.get("Mcp-Session-Id") != "session-1":
            self._send(400, {"error": "bad session"})
            return
        self._send(200, {})


class _SSEMCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path != "/sse":
            self.send_error(404)
            return
        server = self.server
        client_id = uuid.uuid4().hex
        events: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
        with server.clients_lock:  # type: ignore[attr-defined]
            server.clients[client_id] = events  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            endpoint = f"/messages?client={client_id}"
            self.wfile.write(f"event: endpoint\ndata: {endpoint}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    message = events.get(timeout=0.2)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if message is None:
                    break
                data = json.dumps(message, separators=(",", ":"))
                self.wfile.write(f"event: message\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with server.clients_lock:  # type: ignore[attr-defined]
                server.clients.pop(client_id, None)  # type: ignore[attr-defined]

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/messages":
            self.send_error(404)
            return
        client_id = (parse_qs(parsed.query).get("client") or [""])[0]
        with self.server.clients_lock:  # type: ignore[attr-defined]
            events = self.server.clients.get(client_id)  # type: ignore[attr-defined]
        if events is None:
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        response = _remote_result(request)
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.end_headers()
        if response is not None:
            events.put(response)


@contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    server.clients = {}  # type: ignore[attr-defined]
    server.clients_lock = threading.Lock()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        with server.clients_lock:  # type: ignore[attr-defined]
            queues = list(server.clients.values())  # type: ignore[attr-defined]
        for events in queues:
            events.put(None)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_stdio_probe_and_tool_call(tmp_path):
    server = _fake_server(tmp_path)

    probe = probe_stdio(server, timeout=5.0)
    assert probe["server_info"]["name"] == "fake"
    assert probe["tools"]["count"] == 1
    assert probe["tools"]["items"][0]["name"] == "echo"

    result = call_tool_stdio(server, "echo", {"value": 7}, timeout=5.0)
    assert result["is_error"] is False
    assert result["structured_content"] == {"echo": {"value": 7}}


@pytest.mark.parametrize("transport", ["http", "streamable-http"])
def test_http_mcp_transports_probe_and_tool_call(transport):
    with _serve(_HTTPMCPHandler) as base:
        server = MCPServer(name="remote", transport=transport, url=f"{base}/mcp")
        probe = probe_server(server, timeout=5.0)
        assert probe["server_info"]["name"] == "remote-fake"
        assert probe["tools"]["items"][0]["name"] == "echo"
        result = call_tool(server, "echo", {"value": 11}, timeout=5.0)
        assert result["structured_content"] == {"echo": {"value": 11}}


def test_legacy_sse_mcp_transport_probe_and_tool_call():
    with _serve(_SSEMCPHandler) as base:
        server = MCPServer(name="legacy", transport="sse", url=f"{base}/sse")
        probe = probe_server(server, timeout=5.0)
        assert probe["server_info"]["name"] == "remote-fake"
        assert probe["tools"]["items"][0]["name"] == "echo"
        result = call_tool(server, "echo", {"value": 12}, timeout=5.0)
        assert result["structured_content"] == {"echo": {"value": 12}}


def test_remote_cli_uses_same_probe_grant_and_call_policy(tmp_path):
    with _serve(_HTTPMCPHandler) as base:
        store = MCPStore(tmp_path / "megas")
        server = MCPServer(
            name="remote",
            transport="streamable-http",
            url=f"{base}/mcp",
        )
        store.save_server(server)
        runtime = MCPRuntime(store)

        probe = _run(
            _parser().parse_args(["mcp", "probe", "remote", "--timeout", "5"]),
            store,
            runtime,
        )
        assert probe["source"] == "remote-streamable-http"
        assert CapabilityStore(store).latest(server.id) is not None

        call_args = _parser().parse_args(
            ["mcp", "call", "remote", "echo", "--arguments", '{"value":13}', "--timeout", "5"]
        )
        with pytest.raises(RuntimeError, match="denied by local policy"):
            _run(call_args, store, runtime)

        _run(_parser().parse_args(["mcp", "allow", "remote", "echo"]), store, runtime)
        result = _run(call_args, store, runtime)
        assert result["source"] == "remote-streamable-http"
        assert result["structured_content"] == {"echo": {"value": 13}}


def test_cli_denies_tool_until_exact_local_grant(tmp_path):
    store = MCPStore(tmp_path / "megas")
    server = _fake_server(tmp_path)
    store.save_server(server)
    runtime = MCPRuntime(store)

    args = _parser().parse_args(
        ["mcp", "call", "fake", "echo", "--arguments", '{"value":7}', "--timeout", "5"]
    )

    with pytest.raises(RuntimeError, match="denied by local policy"):
        _run(args, store, runtime)

    snapshot = CapabilityStore(store).latest(server.id)
    assert snapshot is not None
    assert snapshot.tools[0]["name"] == "echo"

    allow = _run(_parser().parse_args(["mcp", "allow", "fake", "echo"]), store, runtime)
    assert allow["snapshot_id"] == snapshot.id
    result = _run(args, store, runtime)
    assert result["structured_content"] == {"echo": {"value": 7}}

    events = [row["event"] for row in store.history(40)]
    assert "capability.snapshot.saved" in events
    assert "tool.permission.granted" in events
    assert "tool.called" in events

    store.revoke_tool("fake", "echo")
    with pytest.raises(RuntimeError, match="denied by local policy"):
        _run(args, store, runtime)


def test_cli_allow_cannot_preapprove_uninspected_or_unknown_tool(tmp_path):
    store = MCPStore(tmp_path / "megas")
    server = _fake_server(tmp_path)
    store.save_server(server)
    runtime = MCPRuntime(store)

    with pytest.raises(RuntimeError, match="no inspected capability snapshot"):
        _run(_parser().parse_args(["mcp", "allow", "fake", "echo"]), store, runtime)

    _run(_parser().parse_args(["mcp", "probe", "fake", "--timeout", "5"]), store, runtime)
    with pytest.raises(ValueError, match="not present"):
        _run(_parser().parse_args(["mcp", "allow", "fake", "future-tool"]), store, runtime)


def test_capability_upgrade_revokes_stale_tool_approval_before_dispatch(tmp_path):
    store = MCPStore(tmp_path / "megas")
    server = _fake_server(tmp_path, schema="1")
    store.save_server(server)
    runtime = MCPRuntime(store)

    probe_v1 = _run(
        _parser().parse_args(["mcp", "probe", "fake", "--timeout", "5"]),
        store,
        runtime,
    )
    first_snapshot_id = probe_v1["snapshot"]["id"]
    _run(_parser().parse_args(["mcp", "allow", "fake", "echo"]), store, runtime)
    assert store.is_tool_allowed(server.id, "echo") is True

    call_args = _parser().parse_args(
        ["mcp", "call", "fake", "echo", "--arguments", '{"value":7}', "--timeout", "5"]
    )
    assert _run(call_args, store, runtime)["structured_content"] == {"echo": {"value": 7}}

    upgraded = store.get_server("fake")
    upgraded.env["ECHO_SCHEMA"] = "2"
    store.save_server(upgraded)

    with pytest.raises(RuntimeError, match="denied by local policy"):
        _run(call_args, store, runtime)

    latest = CapabilityStore(store).latest(upgraded.id)
    assert latest is not None
    assert latest.id != first_snapshot_id
    history = _run(
        _parser().parse_args(["mcp", "capabilities", "fake", "--history"]),
        store,
        runtime,
    )
    assert len(history) == 2
    assert store.is_tool_allowed(upgraded.id, "echo") is False
    events = store.history(60)
    snapshots = [row for row in events if row["event"] == "capability.snapshot.saved"]
    assert snapshots[-1]["permissions_revoked"] == ["echo"]

    _run(_parser().parse_args(["mcp", "allow", "fake", "echo"]), store, runtime)
    assert _run(call_args, store, runtime)["structured_content"] == {"echo": {"value": 7}}


def test_custom_server_authenticated_pagination_and_redaction(monkeypatch):
    token = "owner-test-bearer-fixture"
    monkeypatch.setenv("EXAMPLE_MCP_TEST_TOKEN", token)
    class PagedServer(_HTTPMCPHandler):
        def do_POST(self):
            if self.headers.get("Authorization") != "Bearer " + token:
                self._send(401, {})
                return
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            reply = _remote_result(req)
            if reply and req["method"] == "tools/list":
                if (req.get("params") or {}).get("cursor") == "page-two":
                    reply["result"]["tools"][0]["description"] = token
                else:
                    reply["result"] = {"tools": [], "nextCursor": "page-two"}
            if reply and req["method"] == "tools/call":
                reply["result"]["structuredContent"]["echoed_credential"] = token
            self._send(202 if reply is None else 200, reply, session=req["method"] == "initialize")

    with _serve(PagedServer) as base:
        server = MCPServer(name="paged", transport="streamable-http", url=base + "/mcp",
                           env_refs={"MCP_BEARER_TOKEN": "EXAMPLE_MCP_TEST_TOKEN"})
        probe = probe_server(server)
        assert [tool["name"] for tool in probe["tools"]["items"]] == ["echo"]
        assert token not in json.dumps(probe)
        result = call_tool(server, "echo", {"value": "checked"})
        assert result["structured_content"]["echo"] == {"value": "checked"}
        assert token not in json.dumps(result)


def test_custom_server_replacement_revokes_old_permissions(tmp_path):
    from megas.mcp.store import MCPStore
    from megas.mcp.secret_release import SecretReleasePolicy
    store = MCPStore(tmp_path)
    server = store.save_server(MCPServer(name="external", transport="streamable-http",
                                         url="https://example.org/mcp",
                                         env_refs={"MCP_BEARER_TOKEN": "SAVED_PROFILE"}))
    store.grant_tool(server.id, "report")
    policy = SecretReleasePolicy(store)
    policy.grant(server.id, "MCP_BEARER_TOKEN")
    replacement = store.save_server(MCPServer(name="external", transport="streamable-http",
                                               url="https://example.com/mcp"))
    assert replacement.id == server.id
    assert not store.is_tool_allowed(server.id, "report")
    assert not policy.list(server.id)


def test_authenticated_mcp_rejects_redirect_to_another_origin(monkeypatch):
    from megas.mcp.protocol import _remote_open
    from urllib.request import Request
    monkeypatch.setenv("PLUGIN_FIXTURE_TOKEN", "example-fixture-value")
    with _serve(_HTTPMCPHandler) as target:
        class Moved(_HTTPMCPHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", target + "/mcp")
                self.send_header("Content-Length", "0")
                self.end_headers()
        with _serve(Moved) as original:
            server = MCPServer(name="moved", transport="http", url=original,
                               env_refs={"MCP_BEARER_TOKEN": "PLUGIN_FIXTURE_TOKEN"})
            with pytest.raises(PermissionError, match="destination MCP endpoint"):
                _remote_open(Request(original), server, timeout=3)


def test_credential_redaction_in_rpc_errors_and_structured_keys(monkeypatch):
    import secrets
    marker = secrets.token_hex(16)
    monkeypatch.setenv("EXAMPLE_MCP_TEST_TOKEN", marker)

    class EchoCredential(_HTTPMCPHandler):
        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            reply = _remote_result(req)
            if req["method"] == "tools/call":
                if req["params"]["arguments"]["value"] == "fail":
                    reply.pop("result")
                    reply["error"] = {"code": -32000, "data": {marker: [marker]}}
                else:
                    reply["result"]["structuredContent"] = {marker: [{"text": marker}]}
            self._send(202 if reply is None else 200, reply, session=req["method"] == "initialize")

    with _serve(EchoCredential) as base:
        server = MCPServer(name="echo", transport="http", url=base + "/mcp",
                           env_refs={"MCP_BEARER_TOKEN": "EXAMPLE_MCP_TEST_TOKEN"})
        result = call_tool(server, "echo", {"value": "ok"})
        assert marker not in json.dumps(result)
        with pytest.raises(RuntimeError) as error:
            call_tool(server, "echo", {"value": "fail"})
        assert marker not in str(error.value)


def test_registered_stdio_inherits_only_declared_settings(monkeypatch):
    from megas.mcp.protocol import StdioMCPClient
    monkeypatch.setenv("UNRELATED_TEST_SETTING", "ambient")
    monkeypatch.setenv("APPROVED_TEST_SETTING", "permitted")
    server = MCPServer(name="fixture", command=sys.executable, source="owner-plugin",
                       env_refs={"INPUT_VALUE": "APPROVED_TEST_SETTING"})
    env = StdioMCPClient(server)._resolved_env()
    assert env["INPUT_VALUE"] == "permitted"
    assert "UNRELATED_TEST_SETTING" not in env and "APPROVED_TEST_SETTING" not in env
