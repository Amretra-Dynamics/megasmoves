from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Optional

from megas.adapters.base import WorkerEvent
from megas.adapters.codex_app_protocol import codex_app_server_message_to_events
from megas.adapters.process_control import (
    agent_process_group_kwargs,
    terminate_process_tree,
)

ProcessCallback = Callable[[asyncio.subprocess.Process], Optional[Awaitable[None]]]
_MAX_FRAME_BYTES = 8 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


async def _invoke_process_callback(
    callback: Optional[ProcessCallback],
    process: asyncio.subprocess.Process,
) -> None:
    if callback is None:
        return
    result = callback(process)
    if inspect.isawaitable(result):
        await result


async def _bounded_stream_lines(
    reader: asyncio.StreamReader,
) -> AsyncIterator[bytes | None]:
    pending = bytearray()
    discarding = False
    while True:
        chunk = await reader.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        offset = 0
        while offset < len(chunk):
            newline = chunk.find(b"\n", offset)
            end = len(chunk) if newline < 0 else newline + 1
            if discarding:
                if newline < 0:
                    break
                discarding = False
                offset = end
                continue
            part = chunk[offset:end]
            if len(pending) + len(part) > _MAX_FRAME_BYTES:
                pending.clear()
                yield None
                if newline < 0:
                    discarding = True
                    break
                offset = end
                continue
            pending.extend(part)
            offset = end
            if newline >= 0:
                yield bytes(pending)
                pending.clear()
    if pending and not discarding:
        yield bytes(pending)


async def stream_codex_app_server(
    *,
    binary: str,
    prompt: str,
    cwd: Path,
    environment: dict[str, str],
    provider_session_id: str = "",
    write_mode: bool = False,
    model: str = "",
    effort: str = "",
    timeout_sec: float = 900.0,
    redact_text: Callable[[str], str] = lambda value: value,
    strict_local_resume: bool = False,
    command_prefix: Optional[list[str]] = None,
    on_process_started: Optional[ProcessCallback] = None,
    on_process_finished: Optional[ProcessCallback] = None,
) -> AsyncIterator[WorkerEvent]:
    """Run one native Codex turn.

    ``command_prefix`` preserves npm-shim resolution such as
    ``[node.exe, codex.js]``. Cloud may retain its historical stale-thread
    fallback. Strict Local callers never silently fork a new provider thread.
    """
    prefix = [str(value) for value in (command_prefix or [binary]) if str(value)]
    if not prefix:
        raise FileNotFoundError("Codex app-server command is empty")
    process = await asyncio.create_subprocess_exec(
        *prefix,
        "app-server",
        "--stdio",
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        **agent_process_group_kwargs(),
    )
    await _invoke_process_callback(on_process_started, process)
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue(maxsize=1024)

    async def pump(stream: str, reader: asyncio.StreamReader) -> None:
        try:
            async for frame in _bounded_stream_lines(reader):
                if frame is None:
                    await queue.put(
                        (
                            stream,
                            json.dumps(
                                {
                                    "method": "error",
                                    "params": {
                                        "message": (
                                            "Codex app-server frame exceeded the bounded "
                                            "transport limit."
                                        )
                                    },
                                }
                            ),
                        )
                    )
                else:
                    await queue.put((stream, frame.decode(errors="replace")))
        finally:
            await queue.put(None)

    readers = [
        asyncio.create_task(pump("stdout", process.stdout)),
        asyncio.create_task(pump("stderr", process.stderr)),
    ]

    async def send(payload: dict[str, Any]) -> None:
        process.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        await process.stdin.drain()

    request_id = 1
    stage = "initialize"
    active_thread_id = provider_session_id.strip()
    turn_id = ""
    completed = False
    started = time.monotonic()

    try:
        await send(
            {
                "id": request_id,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "megas_moves",
                        "title": "Megas Moves",
                        "version": "0.2.17",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            }
        )

        closed = 0
        while closed < 2 and not completed:
            remaining = timeout_sec - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError(f"Codex app-server turn timed out after {timeout_sec:g}s")
            queued = await asyncio.wait_for(queue.get(), timeout=remaining)
            if queued is None:
                closed += 1
                continue
            stream, raw = queued
            if stream == "stderr":
                text = redact_text(raw)
                if text.strip():
                    yield WorkerEvent(
                        "terminal_stderr",
                        {"text": text, "source": "codex_app_server", "phase": stage},
                    )
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                yield WorkerEvent(
                    "terminal_stdout",
                    {"text": redact_text(raw), "source": "codex_app_server", "phase": stage},
                )
                continue

            if "id" in message and "method" in message:
                method = str(message.get("method") or "")
                params = message.get("params") if isinstance(message.get("params"), dict) else {}
                yield WorkerEvent(
                    "approval_request",
                    {
                        "source": "codex_app_server",
                        "request_id": message["id"],
                        "method": method,
                        "thread_id": params.get("threadId"),
                        "turn_id": params.get("turnId"),
                        "item_id": params.get("itemId"),
                        "command": params.get("command"),
                        "cwd": params.get("cwd"),
                        "reason": params.get("reason"),
                        "available_decisions": params.get("availableDecisions") or [],
                    },
                )
                if method == "item/tool/requestUserInput":
                    result: dict[str, Any] = {"answers": {}}
                elif method.startswith("mcpServer/elicitation"):
                    result = {"action": "decline", "content": None}
                else:
                    result = {"decision": "decline"}
                await send({"id": message["id"], "result": result})
                continue

            if message.get("id") == request_id:
                if message.get("error"):
                    error = message["error"]
                    if stage == "thread_resume" and not strict_local_resume:
                        active_thread_id = ""
                        request_id += 1
                        stage = "thread_start"
                        params: dict[str, Any] = {
                            "cwd": str(cwd),
                            "approvalPolicy": "never",
                            "sandbox": "workspace-write" if write_mode else "read-only",
                            "serviceName": "megas_moves",
                        }
                        if model:
                            params["model"] = model
                        await send({"id": request_id, "method": "thread/start", "params": params})
                        continue
                    if stage == "thread_resume" and strict_local_resume:
                        raise RuntimeError(f"Codex app-server strict resume failed: {error}")
                    raise RuntimeError(f"Codex app-server {stage} failed: {error}")

                result = message.get("result") if isinstance(message.get("result"), dict) else {}
                if stage == "initialize":
                    await send({"method": "initialized"})
                    request_id += 1
                    if active_thread_id:
                        stage = "thread_resume"
                        await send(
                            {
                                "id": request_id,
                                "method": "thread/resume",
                                "params": {
                                    "threadId": active_thread_id,
                                    "cwd": str(cwd),
                                    "approvalPolicy": "never",
                                    "sandbox": "workspace-write" if write_mode else "read-only",
                                },
                            }
                        )
                    else:
                        stage = "thread_start"
                        params = {
                            "cwd": str(cwd),
                            "approvalPolicy": "never",
                            "sandbox": "workspace-write" if write_mode else "read-only",
                            "serviceName": "megas_moves",
                        }
                        if model:
                            params["model"] = model
                        await send({"id": request_id, "method": "thread/start", "params": params})
                elif stage in {"thread_start", "thread_resume"}:
                    thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
                    active_thread_id = str(thread.get("id") or active_thread_id)
                    if not active_thread_id:
                        raise RuntimeError("Codex app-server did not return a thread id")
                    yield WorkerEvent(
                        "agent_thread_started",
                        {
                            "source": "codex_app_server",
                            "thread_id": active_thread_id,
                            "provider_session_id": active_thread_id,
                            "resumed": stage == "thread_resume",
                        },
                    )
                    request_id += 1
                    stage = "turn_start"
                    params = {
                        "threadId": active_thread_id,
                        "input": [{"type": "text", "text": prompt}],
                    }
                    if model:
                        params["model"] = model
                    if effort:
                        params["effort"] = effort
                    await send({"id": request_id, "method": "turn/start", "params": params})
                elif stage == "turn_start":
                    turn = result.get("turn") if isinstance(result.get("turn"), dict) else {}
                    turn_id = str(turn.get("id") or "")
                    stage = "streaming"
                continue

            for event in codex_app_server_message_to_events(message, redact_text=redact_text):
                yield event
            if message.get("method") == "turn/completed":
                params = message.get("params") if isinstance(message.get("params"), dict) else {}
                completed_turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                completed_turn_id = str(params.get("turnId") or completed_turn.get("id") or "")
                if not turn_id or not completed_turn_id or completed_turn_id == turn_id:
                    completed = True
    finally:
        if process.returncode is None:
            await terminate_process_tree(process, timeout=5.0)
        for task in readers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        await _invoke_process_callback(on_process_finished, process)

    if not completed:
        raise RuntimeError(
            f"Codex app-server exited before turn completion (exit={process.returncode})"
        )
