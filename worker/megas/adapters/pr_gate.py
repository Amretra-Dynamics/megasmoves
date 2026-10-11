from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import AsyncIterator

from ..adapters.base import WorkerEvent, BaseAgentAdapter
from ..evidence.git import get_head_commit, validate_changed_paths
from ..policy import resolve_allowed_workdir
from megas.adapters.base import prefer_console_free_tool_path, windows_subprocess_kwargs


class PrGateAdapter(BaseAgentAdapter):
    name = "pr_gate"
    adapter_id = "pr_gate"
    contract_version = "1.0"
    status = "discovered"
    supported_range = ">=0.0.0"

    def __init__(self, allowed_folders: list[str]) -> None:
        super().__init__(allowed_folders)

    def supports(self, command: dict) -> bool:
        return command.get("command_type") in ("create_draft_pr", "branch_cleanup")

    def prepare_context(self, context_pack: dict, workspace: Path, command: dict | None = None) -> Path:
        return workspace / "MEGAS.md"

    async def launch(self, command: dict) -> AsyncIterator[WorkerEvent]:
        session_id = str(command.get("session_id") or "")
        command_id = str(command.get("command_id") or "")
        lease_id = str(command.get("lease_id") or "")
        workdir = resolve_allowed_workdir(command.get("workdir"), self.allowed_folders)

        started = time.time()
        cmd_type = command.get("command_type") or "create_draft_pr"
        # Yield start event
        yield WorkerEvent("command_started", {
            "command_id": command_id,
            "command": [cmd_type],
            "workdir": str(workdir),
            "session_id": session_id
        })

        try:
            working_branch = command.get("working_branch")
            if not working_branch:
                raise ValueError("Missing working_branch")

            # Check if repo exists
            if not (workdir / ".git").exists():
                raise ValueError("Not a git repository")

            if cmd_type == "branch_cleanup":
                # 1. Verify clean workspace
                status_proc = await asyncio.create_subprocess_exec(
                    "git", "status", "--porcelain",
                    cwd=str(workdir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=prefer_console_free_tool_path(),
                    **windows_subprocess_kwargs(new_group=False),
                )
                stdout_status, _ = await status_proc.communicate()
                if stdout_status.strip():
                    raise ValueError("Cannot clean up: local workspace has uncommitted changes.")

                # 2. Find current branch
                branch_proc = await asyncio.create_subprocess_exec(
                    "git", "branch", "--show-current",
                    cwd=str(workdir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=prefer_console_free_tool_path(),
                    **windows_subprocess_kwargs(new_group=False),
                )
                stdout_br, _ = await branch_proc.communicate()
                current_branch = stdout_br.decode().strip()

                if current_branch == working_branch:
                    base_branch = command.get("base_branch")
                    candidates = []
                    if base_branch:
                        candidates.append(base_branch)
                    candidates.extend(["main", "master"])
                    
                    seen = set()
                    deduped_candidates = [x for x in candidates if not (x in seen or seen.add(x))]
                    
                    success_checkout = False
                    for candidate in deduped_candidates:
                        chk_proc = await asyncio.create_subprocess_exec(
                            "git", "checkout", candidate,
                            cwd=str(workdir),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            env=prefer_console_free_tool_path(),
                            **windows_subprocess_kwargs(new_group=False),
                        )
                        await chk_proc.communicate()
                        if chk_proc.returncode == 0:
                            success_checkout = True
                            break
                            
                    if not success_checkout:
                        raise ValueError(f"Could not checkout any safe base branch from candidates: {deduped_candidates}. Failing closed.")

                # 3. Delete only the local session branch
                if not working_branch.startswith("megas/session-") and not working_branch.startswith("feat/memory-"):
                    raise ValueError(f"For hygiene, only megas session branches starting with 'megas/session-' or session working branches can be cleaned up. Got: {working_branch}")

                del_proc = await asyncio.create_subprocess_exec(
                    "git", "branch", "-d", working_branch,
                    cwd=str(workdir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=prefer_console_free_tool_path(),
                    **windows_subprocess_kwargs(new_group=False),
                )
                await del_proc.communicate()
                if del_proc.returncode != 0:
                    del_proc = await asyncio.create_subprocess_exec(
                        "git", "branch", "-D", working_branch,
                        cwd=str(workdir),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=prefer_console_free_tool_path(),
                        **windows_subprocess_kwargs(new_group=False),
                    )
                    stdout_del, stderr_del = await del_proc.communicate()
                    if del_proc.returncode != 0:
                        raise ValueError(f"Failed to delete local branch {working_branch}: {stderr_del.decode(errors='replace')}")

                # Yield evidence_branch_cleanup
                yield WorkerEvent("evidence", {
                    "evidence_type": "evidence_branch_cleanup",
                    "summary": f"Cleaned up local branch {working_branch}",
                    "payload": {
                        "kind": "branch_cleanup",
                        "session_id": session_id,
                        "working_branch": working_branch,
                        "status": "deleted"
                    }
                })
            else:
                change_package_id = command.get("change_package_id")
                commit_sha = command.get("commit_sha")
                remote = command.get("remote") or "origin"

                if not change_package_id or not commit_sha:
                    raise ValueError("Missing change_package_id or commit_sha")

                if remote != "origin":
                    raise ValueError(f"Forbidden remote: {remote}. Only 'origin' is allowed.")

                if not working_branch.startswith("megas/session-") and not working_branch.startswith("feat/memory-"):
                    raise ValueError(f"Branch '{working_branch}' does not start with 'megas/session-' or feat/memory-")

                # 1. Verify current branch matches expected
                branch_proc = await asyncio.create_subprocess_exec(
                    "git", "branch", "--show-current",
                    cwd=str(workdir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=prefer_console_free_tool_path(),
                    **windows_subprocess_kwargs(new_group=False),
                )
                stdout_br, _ = await branch_proc.communicate()
                current_branch = stdout_br.decode().strip()
                if current_branch != working_branch:
                    raise ValueError(f"Current branch '{current_branch}' does not match expected working branch '{working_branch}'")

                # 2. Verify current HEAD commit matches commit_sha
                current_head = await get_head_commit(workdir)
                if current_head != commit_sha:
                    raise ValueError(f"Current HEAD commit '{current_head}' does not match expected commit_sha '{commit_sha}'")

                # 3. Validate committed files from the commit itself
                diff_proc = await asyncio.create_subprocess_exec(
                    "git", "diff-tree", "--no-commit-id", "--name-status", "-r", commit_sha,
                    cwd=str(workdir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=prefer_console_free_tool_path(),
                    **windows_subprocess_kwargs(new_group=False),
                )
                stdout_diff, stderr_diff = await diff_proc.communicate()
                if diff_proc.returncode != 0:
                    raise ValueError(f"Failed to inspect commit files: {stderr_diff.decode(errors='replace')}")

                changed_files = []
                for line in stdout_diff.decode(errors="replace").splitlines():
                    parts = line.strip().split("\t", 1)
                    if len(parts) == 2:
                        status, filepath = parts
                        changed_files.append({"path": filepath, "status": status})

                # Check protected paths
                validate_changed_paths(changed_files, workdir)

                # 4. Push safe branch using explicit refspec
                refspec = f"refs/heads/{working_branch}:refs/heads/{working_branch}"
                push_proc = await asyncio.create_subprocess_exec(
                    "git", "push", "--porcelain", remote, refspec,
                    cwd=str(workdir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=prefer_console_free_tool_path(),
                    **windows_subprocess_kwargs(new_group=False),
                )
                stdout_push, stderr_push = await push_proc.communicate()
                
                if push_proc.returncode != 0:
                    err_msg = stderr_push.decode(errors="replace") or stdout_push.decode(errors="replace")
                    raise ValueError(f"Git push failed: {err_msg}")

                # On success: yield evidence_branch_pushed
                yield WorkerEvent("evidence", {
                    "evidence_type": "evidence_branch_pushed",
                    "summary": f"Pushed branch {working_branch} to {remote}",
                    "payload": {
                        "kind": "branch_pushed",
                        "session_id": session_id,
                        "change_package_id": change_package_id,
                        "remote": remote,
                        "working_branch": working_branch,
                        "branch": working_branch,
                        "commit_sha": commit_sha,
                        "forced": False
                    }
                })

            duration_ms = int((time.time() - started) * 1000)
            yield WorkerEvent("command_finished", {
                "command_id": command_id,
                "exit_code": 0,
                "duration_ms": duration_ms
            })

        except Exception as exc:
            duration_ms = int((time.time() - started) * 1000)
            err_msg = str(exc)
            terminal_payload = {
                "adapter": self.name,
                "command": "git push" if cmd_type == "create_draft_pr" else "branch cleanup",
                "workdir": str(workdir),
                "exit_code": 1,
                "duration_ms": duration_ms,
                "output_tail": [{"stream": "stderr", "line": err_msg}],
                "git": {},
                "error": err_msg,
            }
            yield WorkerEvent("evidence", {
                "evidence_type": "terminal_result",
                "summary": f"Failed: {err_msg}",
                "payload": terminal_payload
            })
            yield WorkerEvent("pr_failed", {
                "session_id": session_id,
                "command_id": command_id,
                "error": err_msg
            })
            yield WorkerEvent("command_finished", {
                "command_id": command_id,
                "exit_code": 1,
                "duration_ms": duration_ms
            })
