from __future__ import annotations

from megas.adapters.base import windows_subprocess_kwargs

import json
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterator

from .model import MCPServer
from .validation import validate_tool_arguments


DEFAULT_PROTOCOL_VERSION = os.environ.get("MEGAS_MCP_PROTOCOL_VERSION", "2024-11-05")
MAX_REMOTE_BYTES = 4 * 1024 * 1024


def _reader(stream: Any, out: "queue.Queue[dict[str, Any] | None]") -> None:
    try:
        for line in iter(stream.readline, ""):
            text = line.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                out.put(value)
    finally:
        out.put(None)


def _json_result(message: dict[str, Any], request_id: int, server: MCPServer) -> dict[str, Any]:
    if message.get("id") != request_id:
        raise RuntimeError(
            f"MCP response id mismatch for request {request_id}"
        )
    if "error" in message:
        error = _redact_bound_values(server, message["error"])
        raise RuntimeError(f"MCP request failed: {error}")
    result = message.get("result")
    return result if isinstance(result, dict) else {"value": result}


def _read_bounded(response: Any, limit: int = MAX_REMOTE_BYTES) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("remote MCP response exceeds maximum size")
    return data


def _iter_sse(response: Any) -> Iterator[tuple[str, str]]:
    event = "message"
    data: list[str] = []
    total = 0
    while True:
        raw = response.readline(MAX_REMOTE_BYTES + 1)
        if not raw:
            if data:
                yield event, "\n".join(data)
            return
        total += len(raw)
        if total > MAX_REMOTE_BYTES:
            raise RuntimeError("remote MCP SSE response exceeds maximum size")
        line = raw.decode("utf-8", errors="strict").rstrip("\r\n")
        if line == "":
            if data:
                yield event, "\n".join(data)
            event = "message"
            data = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("remote MCP URL must use http:// or https://")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname.lower(), port


def _redact_bound_values(server, value):
    from .gateway import _redact
    return _redact(value, [os.getenv(ref, "") for ref in server.env_refs.values()]) if server.env_refs else value


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(newurl) != _origin(req.full_url):
            raise PermissionError("Configure the destination MCP endpoint directly")
        next_request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if next_request is not None and req.get_header("Authorization"):
            next_request.add_unredirected_header("Authorization", req.get_header("Authorization"))
        return next_request


def _remote_open(request, server, *, timeout):
    # Environment references have already passed the host's secret-release policy.
    reference = server.env_refs.get("MCP_BEARER_TOKEN")
    if reference:
        scheme, host, _port = _origin(request.full_url)
        if scheme != "https" and host not in {"localhost", "127.0.0.1", "::1"}:
            raise PermissionError("Remote MCP bearer authentication requires HTTPS")
        token = os.environ.get(reference, "")
        if not token or not token.isascii() or any(ord(ch) < 33 or ord(ch) > 126 for ch in token):
            raise PermissionError("The configured MCP bearer reference is unavailable or invalid")
        request.add_unredirected_header("Authorization", "Bearer " + token)
    return urllib.request.build_opener(_SameOriginRedirect()).open(request, timeout=timeout)


def _listed(request, method, key, timeout):
    """Read a bounded paginated MCP collection within one deadline."""
    end = time.monotonic() + timeout
    items = []
    params = {}
    seen = set()
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("MCP list deadline expired")
        page = request(method, params, timeout=remaining)
        values = page.get(key, [])
        if not isinstance(values, list) or len(values) + len(items) > 4096:
            raise RuntimeError("MCP list is invalid or exceeds the catalog limit")
        items.extend(item for item in values if isinstance(item, dict))
        cursor = page.get("nextCursor")
        if cursor is None:
            return items
        if not isinstance(cursor, str) or not cursor or len(cursor) > 2048 or cursor in seen or len(seen) >= 63:
            raise RuntimeError("MCP list returned an invalid or excessive continuation")
        seen.add(cursor)
        params = {"cursor": cursor}


class StdioMCPClient:
    """Deterministic MCP stdio client session."""

    def __init__(
        self,
        server: MCPServer,
        *,
        timeout: float = 10.0,
        stderr: Any = subprocess.DEVNULL,
        client_info: dict[str, str] | None = None,
    ) -> None:
        server.validate()
        if server.transport != "stdio":
            raise ValueError("StdioMCPClient requires a stdio server")
        self.server = server
        self.timeout = timeout
        self.stderr = stderr
        self.client_info = dict(client_info) if client_info else {"name": "Megas", "version": "0.3.0"}
        self.proc: subprocess.Popen[str] | None = None
        self.inbox: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
        self.next_id = 1
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.protocol_version: str | None = None
        self.instructions: str = ""

    def _resolved_env(self) -> dict[str, str]:
        if self.server.source == "owner-plugin":
            # User-added programs receive their declared settings and granted bindings.
            system_vars = {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "PATHEXT",
                           "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE"}
            env = {key: value for key, value in os.environ.items() if key.upper() in system_vars}
        else:
            env = os.environ.copy()
        env.pop("MEGAS_RUNTIME_TOKEN", None)
        env.update(self.server.env)
        for target, source in self.server.env_refs.items():
            if source not in os.environ:
                raise RuntimeError(f"required environment variable is not set: {source}")
            env[target] = os.environ[source]
        return env

    def __enter__(self) -> "StdioMCPClient":
        self.proc = subprocess.Popen(
            [self.server.command or "", *self.server.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            text=True,
            bufsize=1,
            cwd=self.server.cwd or None,
            env=self._resolved_env(),
            **windows_subprocess_kwargs(),
        )
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        threading.Thread(target=_reader, args=(self.proc.stdout, self.inbox), daemon=True).start()
        try:
            self.initialize()
        except Exception:
            self.close()
            raise
        return self

    def _send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        notification: bool = False,
    ) -> int | None:
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("MCP client session is not open")
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        request_id: int | None = None
        if not notification:
            request_id = self.next_id
            self.next_id += 1
            payload["id"] = request_id
        self.proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()
        return request_id

    def _wait(self, request_id: int, timeout: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            try:
                item = self.inbox.get(timeout=remaining)
            except queue.Empty:
                break
            if item is None:
                code = None if self.proc is None else self.proc.poll()
                raise RuntimeError(f"MCP server exited before replying (code={code})")
            if item.get("id") == request_id:
                return _json_result(item, request_id, self.server)
        raise TimeoutError(f"MCP server did not answer request {request_id} within the timeout")

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        request_id = self._send(method, params)
        assert request_id is not None
        return self._wait(request_id, timeout)

    def initialize(self) -> None:
        result = self.request(
            "initialize",
            {
                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": self.client_info,
            },
        )
        self.protocol_version = result.get("protocolVersion")
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        self.instructions = str(result.get("instructions") or "")
        self._send("notifications/initialized", {}, notification=True)

    def close(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class HTTPMCPClient:
    """MCP JSON/streamable HTTP client using only the Python standard library."""

    def __init__(self, server: MCPServer, *, timeout: float = 10.0) -> None:
        server.validate()
        if server.transport not in {"http", "streamable-http"}:
            raise ValueError("HTTPMCPClient requires http or streamable-http transport")
        assert server.url is not None
        _origin(server.url)
        self.server = server
        self.timeout = timeout
        self.next_id = 1
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.instructions: str = ""

    def __enter__(self) -> "HTTPMCPClient":
        try:
            self.initialize()
        except Exception:
            self.close()
            raise
        return self

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _exchange(
        self,
        payload: dict[str, Any],
        *,
        expected_id: int | None,
        timeout: float,
    ) -> dict[str, Any] | None:
        assert self.server.url is not None
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_REMOTE_BYTES:
            raise ValueError("remote MCP request exceeds maximum size")
        request = urllib.request.Request(
            self.server.url,
            data=encoded,
            headers=self._headers(),
            method="POST",
        )
        try:
            response = _remote_open(request, self.server, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read(65536).decode("utf-8", errors="replace").strip()
            suffix = f": {_redact_bound_values(self.server, detail)}" if detail else ""
            raise RuntimeError(f"remote MCP HTTP {exc.code}{suffix}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"remote MCP connection failed: {_redact_bound_values(self.server, str(exc.reason))}") from None

        with response:
            session_id = response.headers.get("Mcp-Session-Id")
            if session_id:
                self.session_id = session_id.strip()
            status = int(getattr(response, "status", 200) or 200)
            if status == 202 or expected_id is None:
                return None
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/event-stream" in content_type:
                for _event, data in _iter_sse(response):
                    if not data:
                        continue
                    try:
                        message = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(message, dict) and message.get("id") == expected_id:
                        return message
                raise RuntimeError(
                    f"remote MCP stream ended before response {expected_id} arrived"
                )
            raw = _read_bounded(response)
            if not raw:
                raise RuntimeError("remote MCP returned an empty response")
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("remote MCP returned malformed JSON") from exc
            if not isinstance(message, dict):
                raise RuntimeError("remote MCP response must be a JSON object")
            return message

    def _send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        notification: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        request_id: int | None = None
        if not notification:
            request_id = self.next_id
            self.next_id += 1
            payload["id"] = request_id
        response = self._exchange(
            payload,
            expected_id=request_id,
            timeout=self.timeout if timeout is None else timeout,
        )
        if notification:
            return None
        if response is None or request_id is None:
            raise RuntimeError("remote MCP request returned no response")
        return _json_result(response, request_id, self.server)

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        result = self._send(method, params, timeout=timeout)
        assert result is not None
        return result

    def initialize(self) -> None:
        result = self.request(
            "initialize",
            {
                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "Megas", "version": "0.3.0"},
            },
        )
        self.protocol_version = result.get("protocolVersion")
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        self.instructions = str(result.get("instructions") or "")
        self._send("notifications/initialized", {}, notification=True)

    def close(self) -> None:
        if self.server.transport != "streamable-http" or not self.session_id:
            return
        assert self.server.url is not None
        request = urllib.request.Request(
            self.server.url,
            headers=self._headers(),
            method="DELETE",
        )
        try:
            with _remote_open(request, self.server, timeout=min(self.timeout, 2.0)) as response:
                _read_bounded(response, 65536)
        except Exception:
            pass

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class SSEMCPClient:
    """Legacy MCP HTTP+SSE client with same-origin endpoint enforcement."""

    def __init__(self, server: MCPServer, *, timeout: float = 10.0) -> None:
        server.validate()
        if server.transport != "sse":
            raise ValueError("SSEMCPClient requires sse transport")
        assert server.url is not None
        self.server = server
        self.timeout = timeout
        self.next_id = 1
        self.message_url: str | None = None
        self.inbox: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
        self.endpoint_ready = threading.Event()
        self._error: Exception | None = None
        self._response: Any = None
        self._thread: threading.Thread | None = None
        self.protocol_version: str | None = None
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.instructions: str = ""

    def __enter__(self) -> "SSEMCPClient":
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()
        if not self.endpoint_ready.wait(self.timeout):
            self.close()
            raise TimeoutError("SSE MCP server did not advertise a message endpoint")
        if self._error is not None:
            self.close()
            raise RuntimeError(f"SSE MCP connection failed: {_redact_bound_values(self.server, str(self._error))}")
        if not self.message_url:
            self.close()
            raise RuntimeError("SSE MCP server did not provide a message endpoint")
        try:
            self.initialize()
        except Exception:
            self.close()
            raise
        return self

    def _listen(self) -> None:
        assert self.server.url is not None
        request = urllib.request.Request(
            self.server.url,
            headers={"Accept": "text/event-stream"},
            method="GET",
        )
        try:
            response = _remote_open(request, self.server, timeout=max(self.timeout, 60.0))
            self._response = response
            with response:
                content_type = (response.headers.get("Content-Type") or "").lower()
                if "text/event-stream" not in content_type:
                    raise RuntimeError("SSE MCP endpoint did not return text/event-stream")
                for event, data in _iter_sse(response):
                    if event == "endpoint":
                        candidate = urllib.parse.urljoin(self.server.url, data.strip())
                        if _origin(candidate) != _origin(self.server.url):
                            raise RuntimeError(
                                "SSE MCP message endpoint changed origin and was rejected"
                            )
                        self.message_url = candidate
                        self.endpoint_ready.set()
                        continue
                    if not data:
                        continue
                    try:
                        value = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        self.inbox.put(value)
        except Exception as exc:
            self._error = exc
            self.endpoint_ready.set()
        finally:
            self.inbox.put(None)

    def _post(self, payload: dict[str, Any], timeout: float) -> None:
        if not self.message_url:
            raise RuntimeError("SSE MCP message endpoint is unavailable")
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_REMOTE_BYTES:
            raise ValueError("remote MCP request exceeds maximum size")
        request = urllib.request.Request(
            self.message_url,
            data=encoded,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with _remote_open(request, self.server, timeout=timeout) as response:
                _read_bounded(response, 65536)
        except urllib.error.HTTPError as exc:
            detail = exc.read(65536).decode("utf-8", errors="replace").strip()
            suffix = f": {_redact_bound_values(self.server, detail)}" if detail else ""
            raise RuntimeError(f"SSE MCP HTTP {exc.code}{suffix}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"SSE MCP connection failed: {_redact_bound_values(self.server, str(exc.reason))}") from None

    def _send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        notification: bool = False,
        timeout: float | None = None,
    ) -> int | None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        request_id: int | None = None
        if not notification:
            request_id = self.next_id
            self.next_id += 1
            payload["id"] = request_id
        self._post(payload, self.timeout if timeout is None else timeout)
        return request_id

    def _wait(self, request_id: int, timeout: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            try:
                item = self.inbox.get(timeout=remaining)
            except queue.Empty:
                break
            if item is None:
                if self._error is not None:
                    raise RuntimeError(f"SSE MCP stream failed: {_redact_bound_values(self.server, str(self._error))}")
                raise RuntimeError("SSE MCP stream closed before the response arrived")
            if item.get("id") == request_id:
                return _json_result(item, request_id, self.server)
        raise TimeoutError(f"SSE MCP server did not answer request {request_id} within the timeout")

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        request_id = self._send(method, params, timeout=timeout)
        assert request_id is not None
        return self._wait(request_id, timeout)

    def initialize(self) -> None:
        result = self.request(
            "initialize",
            {
                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "Megas", "version": "0.3.0"},
            },
        )
        self.protocol_version = result.get("protocolVersion")
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        self.instructions = str(result.get("instructions") or "")
        self._send("notifications/initialized", {}, notification=True)

    def close(self) -> None:
        response = self._response
        self._response = None
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def _summarize_tool(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": item.get("name"),
        "title": item.get("title") or "",
        "description": item.get("description") or "",
        "input_schema": item.get("inputSchema") or {},
        "output_schema": item.get("outputSchema") or {},
        "annotations": dict(item.get("annotations") or {}) if isinstance(item.get("annotations"), dict) else {},
        "security_schemes": list(item.get("securitySchemes") or []) if isinstance(item.get("securitySchemes"), list) else [],
        "_meta": dict(item.get("_meta") or {}) if isinstance(item.get("_meta"), dict) else {},
    }


def _summarize_resource(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": item.get("name") or "",
        "title": item.get("title") or "",
        "uri": item.get("uri") or "",
        "description": item.get("description") or "",
        "mime_type": item.get("mimeType") or "",
        "_meta": dict(item.get("_meta") or {}) if isinstance(item.get("_meta"), dict) else {},
    }


def _summarize_prompt(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": item.get("name"),
        "title": item.get("title") or "",
        "description": item.get("description") or "",
        "arguments": item.get("arguments") or [],
        "_meta": dict(item.get("_meta") or {}) if isinstance(item.get("_meta"), dict) else {},
    }


def _client_for(server: MCPServer, *, timeout: float) -> Any:
    if server.transport == "stdio":
        return StdioMCPClient(server, timeout=timeout)
    if server.transport == "sse":
        return SSEMCPClient(server, timeout=timeout)
    if server.transport in {"http", "streamable-http"}:
        return HTTPMCPClient(server, timeout=timeout)
    raise ValueError(f"unsupported MCP transport: {server.transport}")


def probe_server(server: MCPServer, *, timeout: float = 5.0) -> dict[str, Any]:
    """Inspect protocol-reported capabilities without any AI/model call."""

    with _client_for(server, timeout=timeout) as client:
        collected: dict[str, Any] = {}
        for key, method, value_key, summarizer in (
            ("tools", "tools/list", "tools", _summarize_tool),
            ("resources", "resources/list", "resources", _summarize_resource),
            ("prompts", "prompts/list", "prompts", _summarize_prompt),
        ):
            try:
                items = _listed(client.request, method, value_key, timeout)
                collected[key] = {
                    "supported": True,
                    "count": len(items),
                    "items": [summarizer(item) for item in items],
                }
            except (RuntimeError, TimeoutError) as exc:
                collected[key] = {
                    "supported": False,
                    "count": 0,
                    "items": [],
                    "error": str(exc),
                }

        return _redact_bound_values(server, {
            "protocol_version": client.protocol_version,
            "server_info": client.server_info,
            "capabilities": client.capabilities,
            "instructions": client.instructions,
            **collected,
        })


def call_tool(
    server: MCPServer,
    tool: str,
    arguments: dict[str, Any],
    *,
    timeout: float = 30.0,
    verify: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Call one explicitly approved MCP tool through the server's configured transport."""

    if not tool:
        raise ValueError("tool name must be non-empty")
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be a JSON object")

    with _client_for(server, timeout=timeout) as client:
        tools = _listed(client.request, "tools/list", "tools", timeout)
        selected = next(
            (item for item in tools if str(item.get("name") or "") == tool),
            None,
        )
        if selected is None:
            raise ValueError(f"MCP server {server.name} does not report tool: {tool}")
        schema = selected.get("inputSchema")
        if not isinstance(schema, dict):
            raise ValueError(f"MCP server {server.name} reported invalid inputSchema for {tool}")
        validate_tool_arguments(tool, schema, arguments)
        if verify is not None:
            verify(selected)
        result = client.request(
            "tools/call",
            {"name": tool, "arguments": arguments},
            timeout=timeout,
        )
        return _redact_bound_values(server, {
            "server": server.name,
            "tool": tool,
            "is_error": bool(result.get("isError", False)),
            "content": result.get("content") or [],
            "structured_content": result.get("structuredContent"),
        })


def probe_stdio(server: MCPServer, *, timeout: float = 5.0) -> dict[str, Any]:
    if server.transport != "stdio":
        raise ValueError("probe_stdio requires a stdio server")
    return probe_server(server, timeout=timeout)


def call_tool_stdio(
    server: MCPServer,
    tool: str,
    arguments: dict[str, Any],
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    if server.transport != "stdio":
        raise ValueError("call_tool_stdio requires a stdio server")
    return call_tool(server, tool, arguments, timeout=timeout)
