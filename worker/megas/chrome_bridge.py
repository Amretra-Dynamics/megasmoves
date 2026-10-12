"""Opt-in Chrome Native Messaging bridge for the locally paired MegasMoves operator.

A Chrome-launched host owns a loopback-only authenticated command server. The
extension itself decides which tabs can be controlled; this module never opens
Chrome profiles, reads cookies, or grants tab access.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shlex
import struct
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from typing import Any

HOST_NAME = "com.amretra.megasmoves"
MAX_MESSAGE_BYTES = 1_048_576
MAX_COMMAND_BYTES = 16_384
VALID_ACTIONS = frozenset({"status", "tabs", "observe", "click", "fill", "navigate", "screenshot"})
WRITE_ACTIONS = frozenset({"click", "fill", "navigate"})


def bridge_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Amretra" / "MegasMoves"
    else:
        base = Path(os.environ.get("MEGAS_HOME", str(Path.home() / ".megas"))).expanduser()
    return base / "chrome"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    fd, name = tempfile.mkstemp(prefix=".bridge-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_command(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("action") not in VALID_ACTIONS:
        raise ValueError("Unsupported Chrome command")
    if set(value) - {"action", "tabId", "selector", "value", "url"}:
        raise ValueError("Unexpected Chrome command fields")
    action = value["action"]
    clean: dict[str, Any] = {"action": action}
    if action not in {"status", "tabs"}:
        tab_id = value.get("tabId")
        if type(tab_id) is not int or not (0 <= tab_id <= 2**31 - 1):
            raise ValueError("Chrome command requires a numeric tabId")
        clean["tabId"] = tab_id
    if action in {"click", "fill"}:
        selector = value.get("selector")
        if not isinstance(selector, str) or not 0 < len(selector.strip()) <= 500:
            raise ValueError("A bounded CSS selector is required")
        clean["selector"] = selector
    if action == "fill":
        text = value.get("value")
        if not isinstance(text, str) or len(text) > 4000:
            raise ValueError("fill requires text up to 4000 characters")
        clean["value"] = text
    if action == "navigate":
        from urllib.parse import urlsplit
        url = value.get("url")
        if not isinstance(url, str) or len(url) > 2000:
            raise ValueError("navigate requires a bounded URL")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("navigate accepts only HTTP(S) URLs without credentials")
        clean["url"] = url
    return clean


def _native_write(payload: dict[str, Any], lock: threading.Lock) -> None:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("Native messaging response too large")
    with lock:
        sys.stdout.buffer.write(struct.pack("=I", len(raw)) + raw)
        sys.stdout.buffer.flush()


def _native_read() -> dict[str, Any] | None:
    header = sys.stdin.buffer.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise EOFError("Truncated native messaging header")
    (length,) = struct.unpack("=I", header)
    if length < 2 or length > MAX_MESSAGE_BYTES:
        raise ValueError("Invalid native messaging message length")
    data = sys.stdin.buffer.read(length)
    if len(data) != length:
        raise EOFError("Truncated native messaging message")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("Invalid native messaging payload")
    return result


class ChromeHost:
    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(48)
        self.lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.pending: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
        self.http: ThreadingHTTPServer | None = None
        self.discovery = bridge_root() / "bridge.json"

    def command(self, params: dict[str, Any]) -> dict[str, Any]:
        command = validate_command(params)
        request_id = secrets.token_hex(12)
        flag = threading.Event()
        holder: dict[str, Any] = {}
        with self.pending_lock:
            self.pending[request_id] = (flag, holder)
        try:
            _native_write({"kind": "command", "id": request_id, **command}, self.lock)
            if not flag.wait(timeout=12.0):
                raise TimeoutError("Chrome extension did not respond")
            return dict(holder)
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

    def accept(self, message: dict[str, Any]) -> None:
        if message.get("kind") != "result":
            return
        with self.pending_lock:
            item = self.pending.get(message.get("id"))
        if item is not None:
            event, holder = item
            holder.update({"ok": message.get("ok") is True})
            if message.get("ok") is True:
                holder["result"] = message.get("result", {})
            else:
                holder["error"] = str(message.get("error") or "Chrome command failed")[:300]
            event.set()

    def start_http(self) -> None:
        host = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def _allowed(self) -> bool:
                authority = self.headers.get("Host", "").split(":")[0].lower()
                return (
                    authority in {"localhost", "127.0.0.1"}
                    and not self.headers.get("Origin")
                    and secrets.compare_digest(
                        self.headers.get("Authorization", ""), "Bearer " + host.token
                    )
                )

            def do_POST(self) -> None:
                if self.path != "/command":
                    self.send_error(404)
                    return
                if not self._allowed():
                    self.send_error(403)
                    return
                if self.headers.get("Content-Type", "").split(";")[0].lower() != "application/json":
                    self.send_error(415)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                    if not 2 <= length <= MAX_COMMAND_BYTES:
                        raise ValueError("Invalid command size")
                    data = json.loads(self.rfile.read(length))
                    response = host.command(data)
                    code = 200 if response.get("ok") else 422
                except (ValueError, json.JSONDecodeError) as exc:
                    code, response = 400, {"ok": False, "error": str(exc)}
                except (TimeoutError, BrokenPipeError) as exc:
                    code, response = 504, {"ok": False, "error": str(exc)}
                raw = json.dumps(response, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                if len(raw) > MAX_MESSAGE_BYTES:
                    code, raw = 413, b'{"ok":false,"error":"Result exceeds safe native message size"}'
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.daemon_threads = True
        port = self.http.server_address[1]
        _atomic_json(self.discovery, {"port": port, "token": self.token, "pid": os.getpid()})
        threading.Thread(target=self.http.serve_forever, daemon=True, name="megas-chrome-http").start()

    def close(self) -> None:
        if self.http is not None:
            self.http.shutdown()
            self.http.server_close()
        try:
            current = json.loads(self.discovery.read_text(encoding="utf-8"))
            if current.get("token") == self.token:
                self.discovery.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


def native_host() -> None:
    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    server = ChromeHost()
    server.start_http()
    try:
        while True:
            message = _native_read()
            if message is None:
                break
            server.accept(message)
    finally:
        server.close()


def call_bridge(value: dict[str, Any]) -> dict[str, Any]:
    command = validate_command(value)
    path = bridge_root() / "bridge.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Chrome is disconnected; install and enable the extension") from exc
    token, port = config.get("token"), config.get("port")
    if not isinstance(token, str) or len(token) < 40 or type(port) is not int or not 0 < port < 65536:
        raise RuntimeError("Invalid local Chrome bridge discovery file")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/command",
        data=json.dumps(command).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=14) as response:
            result = json.loads(response.read(MAX_MESSAGE_BYTES + 1))
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read(MAX_MESSAGE_BYTES + 1))
            raise RuntimeError(str(data.get("error") or "Chrome command was rejected")) from exc
        except (ValueError, AttributeError) as err:
            raise RuntimeError("Chrome request failed") from err
    except (OSError, TimeoutError) as exc:
        raise RuntimeError("Chrome bridge is not responding") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Invalid response from Chrome")
    return dict(result.get("result") or {})


def chrome_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "chrome_status",
            "description": "Inspect locally paired Chrome extension status. No tab is accessed without the user's grant.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": "chrome_control",
            "description": "Control a user-enabled Chrome tab through local native messaging. Supported actions: tabs, observe, screenshot, click, fill, navigate. Navigation expires the tab grant. Browser writes require locally enabled Megas Operate.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["tabs", "observe", "screenshot", "click", "fill", "navigate"]},
                    "tabId": {"type": "integer", "minimum": 0},
                    "selector": {"type": "string", "maxLength": 500},
                    "value": {"type": "string", "maxLength": 4000},
                    "url": {"type": "string", "maxLength": 2000},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        },
    ]


def chrome_tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "chrome_status":
        if args:
            raise ValueError("chrome_status takes no arguments")
        command = {"action": "status"}
    elif name == "chrome_control":
        command = validate_command(args)
        if command["action"] == "status":
            raise ValueError("Use chrome_status")
        if command["action"] in WRITE_ACTIONS:
            from megas.operate.gate import is_operate_enabled
            if not is_operate_enabled():
                raise PermissionError("Enable Megas Operate locally before Chrome write operations")
    else:
        raise ValueError("Unknown Chrome tool")
    result = call_bridge(command)
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}


def install(extension_id: str) -> Path:
    if len(extension_id) != 32 or not all(ch in "abcdefghijklmnop" for ch in extension_id):
        raise ValueError("Provide the 32-character ID from chrome://extensions")
    root = bridge_root()
    root.mkdir(parents=True, exist_ok=True)
    python = str(Path(sys.executable).resolve())
    argv = ["-u", "-m", "megas.chrome_bridge"] if __package__ == "megas" else ["-u", str(Path(__file__).resolve())]
    if os.name == "nt":
        # Chrome launches the per-user batch wrapper, which calls the Python host in binary mode.
        launcher = root / "native-host.cmd"
        launcher.write_text("@echo off\r\n" + '"' + python + '" ' + " ".join('"' + item + '"' for item in argv) + " host\r\n", encoding="utf-8")
        manifest_path = root / (HOST_NAME + ".json")
    else:
        launcher = root / "native-host.sh"
        launcher.write_text("#!/bin/sh\nexec " + " ".join(shlex.quote(item) for item in [python, *argv, "host"]) + "\n", encoding="utf-8")
        launcher.chmod(0o700)
        if sys.platform == "darwin":
            manifest_path = Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts" / (HOST_NAME + ".json")
        else:
            manifest_path = Path.home() / ".config" / "google-chrome" / "NativeMessagingHosts" / (HOST_NAME + ".json")
    _atomic_json(manifest_path, {
        "name": HOST_NAME,
        "description": "MegasMoves local Chrome browser connector",
        "path": str(launcher.resolve()),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    })
    if os.name == "nt":
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\NativeMessagingHosts" + "\\" + HOST_NAME) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest_path.resolve()))
    return manifest_path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m megas.chrome_bridge", description="MegasMoves user-enabled Chrome connector")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("host", help="Used by Chrome native messaging; do not launch manually")
    setup = sub.add_parser("install", help="Register the local native host for an exact extension ID")
    setup.add_argument("--extension-id", required=True)
    for action in sorted(VALID_ACTIONS):
        if action == "status":
            sub.add_parser(action)
            continue
        item = sub.add_parser(action)
        if action not in {"tabs"}:
            item.add_argument("--tab-id", required=True, type=int)
        if action in {"click", "fill"}:
            item.add_argument("--selector", required=True)
        if action == "fill":
            item.add_argument("--value", required=True)
        if action == "navigate":
            item.add_argument("--url", required=True)
    args = parser.parse_args(argv)
    if args.action == "host":
        native_host()
        return
    if args.action == "install":
        print(json.dumps({"manifest": str(install(args.extension_id)), "registered": True}))
        return
    command: dict[str, Any] = {"action": args.action}
    if hasattr(args, "tab_id"):
        command["tabId"] = args.tab_id
    for arg in ("selector", "value", "url"):
        if hasattr(args, arg):
            command[arg] = getattr(args, arg)
    if args.action in WRITE_ACTIONS and __package__ == "megas":
        from megas.operate.gate import is_operate_enabled
        if not is_operate_enabled():
            raise SystemExit("Megas Operate is disabled; enable it locally before browser write commands")
    print(json.dumps(call_bridge(command), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
