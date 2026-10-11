from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PosixPath, WindowsPath
from typing import AsyncIterator, Dict, Any, List, Optional, Protocol

logger = logging.getLogger("megas.adapter.base")


def which_with_path(binary: str, path: str | None = None) -> str | None:
    """Call ``shutil.which`` with a PATH override and old-compatible fallback.

    A few provider wrappers and test doubles intentionally expose the historic
    one-argument ``which`` shape.  A TypeError here must not quarantine every
    adapter on Windows after the PATH isolation change.
    """

    try:
        return shutil.which(binary, path=path)
    except TypeError:
        return shutil.which(binary)

# Capture the concrete filesystem Path class at import time. Tests may mock
# ``os.name`` to exercise the Windows argv branch on Linux; ``pathlib.Path``
# re-reads ``os.name`` in ``__new__`` and would then try to construct
# ``WindowsPath`` on a non-Windows host (NotImplementedError).
_FSPath = WindowsPath if os.name == "nt" else PosixPath


def windows_creationflags(*, new_group: bool = True, breakaway: bool = False) -> int:
    """Flags so Worker-spawned CLIs do not flash a console window on Windows.

    Local Worker runs headless; agents (agy/codex/git/node) must stay attached to
    pipes only. CREATE_NO_WINDOW hides the console; CREATE_NEW_PROCESS_GROUP
    keeps tree kill working for long agent runs. CREATE_BREAKAWAY_FROM_JOB
    keeps agent trees from taking down the Worker job on exit.
    """
    if os.name != "nt":
        return 0
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    if new_group:
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    if breakaway:
        flags |= 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
    return flags


def windows_startupinfo() -> Any | None:
    """STARTUPINFO that forces SW_HIDE for console apps that still allocate a window."""
    if os.name != "nt":
        return None
    try:
        info = subprocess.STARTUPINFO()
        info.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0x1))
        info.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
        return info
    except Exception:
        return None


def windows_subprocess_kwargs(*, new_group: bool = False, breakaway: bool = False) -> dict[str, Any]:
    """Kwargs for subprocess/asyncio spawn: no window + optional process group.

    Do not default breakaway=True for agent spawns — CREATE_BREAKAWAY_FROM_JOB
    often returns WinError 5 (Access is denied) under interactive user jobs and
    aborts coding sessions mid-run.
    """
    if os.name != "nt":
        return {}
    kwargs: dict[str, Any] = {
        "creationflags": windows_creationflags(new_group=new_group, breakaway=breakaway)
    }
    startupinfo = windows_startupinfo()
    if startupinfo is not None:
        kwargs["startupinfo"] = startupinfo
    return kwargs


def subprocess_hide_window_kwargs() -> dict[str, Any]:
    """Extra kwargs for ``subprocess.run`` / ``Popen`` to avoid console popups."""
    return windows_subprocess_kwargs(new_group=False)


def is_runnable_native_binary(path: Path | str | None) -> bool:
    """Return True if *path* is safe to subprocess on this OS.

    WinError 193 ("%1 is not a valid Win32 application") happens when we try to
    exec a non-PE file (e.g. Unix ``grok`` shim, node script without .cmd).
    Discovery must skip those so bootstrap / health probes never explode.
    """
    if not path:
        return False
    try:
        p = Path(path)
    except (TypeError, ValueError):
        return False
    try:
        if not p.is_file():
            return False
    except OSError:
        return False
    if os.name != "nt":
        return os.access(p, os.X_OK)
    suffix = p.suffix.lower()
    # Shell wrappers are fine for version probes.
    if suffix in {".cmd", ".bat", ".ps1", ".com"}:
        return True
    # Reject known non-PE scripts Windows cannot CreateProcess.
    if suffix in {".js", ".mjs", ".cjs", ".ts", ".py", ".sh", ".bash", ".zsh"}:
        return False
    try:
        with p.open("rb") as handle:
            magic = handle.read(2)
    except OSError:
        return False
    # PE executables start with MZ.
    if magic == b"MZ":
        return True
    # Extensionless Unix binaries / scripts → WinError 193.
    return False


def prefer_console_free_tool_path(env: dict[str, str] | None = None) -> dict[str, str]:
    """Rewrite PATH so Windows Git uses mingw64 git.exe, not Git\\cmd wrappers.

    ``Git\\cmd\\git.exe`` is a tiny launcher that re-execs the real binary and
    frequently flashes a console for every git invocation (agents call git a
    lot). Prefer ``mingw64\\bin`` first and drop ``Git\\cmd`` from PATH.
    """
    base = dict(env or os.environ)
    if os.name != "nt":
        return base

    explicit = (os.getenv("MEGAS_GIT_BINARY") or "").strip()
    prefer: list[str] = []
    if explicit:
        parent = str(Path(explicit).expanduser().resolve().parent)
        prefer.append(parent)
    for candidate in (
        Path(r"C:\Program Files\Git\mingw64\bin"),
        Path(r"C:\Program Files\Git\mingw64\libexec\git-core"),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "mingw64" / "bin",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "mingw64" / "bin",
    ):
        try:
            if candidate.is_dir():
                prefer.append(str(candidate.resolve()))
        except OSError:
            continue

    seen: set[str] = set()
    ordered: list[str] = []
    for part in prefer:
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(part)
    git_prefix_len = len(ordered)

    original = base.get("PATH") or base.get("Path") or ""
    for part in original.split(os.pathsep):
        if not part:
            continue
        normalized = part.replace("/", "\\").rstrip("\\").lower()
        # Drop Git\cmd wrapper dir so bare `git` never hits the flasher.
        if normalized.endswith("\\git\\cmd") or normalized.endswith("/git/cmd"):
            continue
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(part)

    # Prefer real node.exe ahead of npm cmd shims when present, but keep Git
    # mingw64 first so console-free git stays the primary rewrite.
    node_parts: list[str] = []
    for node_dir in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "node",
    ):
        try:
            if (node_dir / "node.exe").is_file():
                resolved = str(node_dir.resolve())
                key = resolved.lower()
                if key not in seen:
                    seen.add(key)
                    node_parts.append(resolved)
        except OSError:
            continue
    if node_parts:
        ordered[git_prefix_len:git_prefix_len] = node_parts

    # Include standard agent CLI installation directories on Windows / POSIX
    agent_dirs: list[Path] = []
    appdata = os.getenv("APPDATA")
    localappdata = os.getenv("LOCALAPPDATA")
    home = Path.home()
    if appdata:
        agent_dirs.append(Path(appdata) / "npm")
    if localappdata:
        agent_dirs.append(Path(localappdata) / "agy" / "bin")
        agent_dirs.append(Path(localappdata) / "Kiro-Cli")
        agent_dirs.append(Path(localappdata) / "Microsoft" / "WindowsApps")
    agent_dirs.extend([
        home / ".grok" / "bin",
        home / ".kimi-code" / "bin",
        home / ".local" / "bin",
        home / ".opencode" / "bin",
        home / ".cargo" / "bin",
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),
    ])
    for adir in agent_dirs:
        try:
            if adir.is_dir():
                resolved = str(adir.resolve())
                key = resolved.lower() if os.name == "nt" else resolved
                if key not in seen:
                    seen.add(key)
                    ordered.append(resolved)
        except OSError:
            continue

    base["PATH"] = os.pathsep.join(ordered)
    # Quiet CLI noise that some tools treat as "interactive terminal".
    base.setdefault("TERM", "dumb")
    base.setdefault("NO_COLOR", "1")
    base.setdefault("CI", "1")
    base.setdefault("GIT_TERMINAL_PROMPT", "0")
    base.setdefault("GCM_INTERACTIVE", "never")
    base.setdefault("GIT_ASKPASS", "")
    # Discourage Windows console allocators that re-open a TTY for child tools.
    base.setdefault("MSYSTEM", "")
    return base

@dataclass
class WorkerEvent:
    event_type: str
    payload: dict

@dataclass
class AdapterResult:
    exit_code: int
    duration_ms: int
    output_tail: list[dict[str, str]]


def _windows_unwrap_npm_cmd(path: Path) -> list[str] | None:
    """Resolve an npm global ``.cmd`` shim to ``[node, entry]`` when possible.

    Spawning ``cmd.exe /c codex.CMD ... "prompt with spaces"`` breaks argument
    boundaries on Windows (Codex sees ``git`` as a separate argv). Running the
    underlying Node entrypoint preserves one argv element per CLI argument.

    Important: only unwrap to the package named by the shim (never fall through
    to an unrelated tool like codex when unwrapping kilocode.cmd).
    """
    script: Path | None = None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    # Prefer the path literally embedded in the shim (js or extensionless node bin).
    match = re.search(
        r'(node_modules[\\/][^\s"\']+)',
        text,
        re.IGNORECASE,
    )
    if match:
        relative = match.group(1).replace("\\", "/").lstrip("/")
        # Drop trailing shell noise
        relative = relative.split("%")[0].rstrip("\"'")
        candidate = path.parent.joinpath(*relative.split("/")).resolve()
        if candidate.is_file():
            script = candidate
    if script is None:
        # Stem-specific layouts only (never cross-wire kilocode → codex).
        stem = path.stem.lower()
        common: list[Path] = []
        if stem in {"codex", "codex-cli"}:
            common.append(path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js")
        elif stem in {"claude", "claude-code"}:
            common.append(path.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js")
        elif stem in {"gemini", "gemini-cli"}:
            common.append(path.parent / "node_modules" / "@google" / "gemini-cli" / "dist" / "index.js")
        elif stem in {"kilocode", "kilo"}:
            common.extend(
                [
                    path.parent / "node_modules" / "@kilocode" / "cli" / "bin" / "kilo",
                    path.parent / "node_modules" / "@kilocode" / "cli" / "bin" / "kilocode",
                ]
            )
        common.extend(
            [
                path.parent / "node_modules" / stem / "bin" / f"{stem}.js",
                path.parent / "node_modules" / stem / f"{stem}.js",
                path.parent / "node_modules" / stem / "bin" / stem,
            ]
        )
        for candidate in common:
            if candidate.is_file():
                script = candidate.resolve()
                break
    if script is None or not script.is_file():
        return None
    if script.suffix.lower() == ".exe" or is_runnable_native_binary(script):
        return [str(script.resolve())]
    node = shutil.which("node")
    if not node:
        local_node = path.parent / "node.exe"
        if local_node.is_file():
            node = str(local_node.resolve())
        else:
            return None
    return [str(_FSPath(node).resolve()), str(script)]


def resolve_cli_argv(binary_name: str, *cli_args: str) -> list[str]:
    """Build argv for asyncio.create_subprocess_exec that works with Windows npm shims.

    On Windows, ``shutil.which("codex")`` often returns a ``.cmd``/``.ps1`` wrapper.
    ``CreateProcess`` cannot run those without a shell (WinError 2), and
    ``cmd.exe /c`` often mangles quoted prompts. Prefer unwrapping npm shims to
    Node entrypoints when possible.
    """
    name = (binary_name or "").strip()
    if not name:
        raise FileNotFoundError("CLI binary name is empty.")
    # Absolute / relative file paths must win over PATH (Windows often finds the
    # OpenAI desktop Codex first; MEGAS_CODEX_BINARY points at standalone exec).
    direct = _FSPath(name)
    if direct.is_file():
        which = str(direct.resolve())
    else:
        env_path = prefer_console_free_tool_path(os.environ).get("PATH", "")
        which = which_with_path(name, env_path)
    if not which:
        raise FileNotFoundError(f"CLI binary not found on PATH: {name}")
    path = _FSPath(which)
    args = [str(arg) for arg in cli_args]
    if os.name == "nt":
        suffix = path.suffix.lower()
        if suffix in {".cmd", ".bat"}:
            unwrapped = _windows_unwrap_npm_cmd(path)
            if unwrapped:
                return [*unwrapped, *args]
            # Last resort: shell out. May still break multi-word prompts.
            command_line = subprocess.list2cmdline([str(path), *args])
            return ["cmd.exe", "/d", "/s", "/c", command_line]
        if suffix == ".ps1":
            return [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(path),
                *args,
            ]
    return [str(path), *args]


def parse_version(v_str: str) -> tuple[int, ...]:
    parts = []
    # Extract only numbers from each dot-separated segment
    for part in v_str.split("."):
        clean = "".join(c for c in part if c.isdigit())
        parts.append(int(clean) if clean else 0)
    return tuple(parts)

def is_version_compatible(version_str: str, range_str: str) -> bool:
    if not range_str or range_str == "*":
        return True
    try:
        ver = parse_version(version_str)
        bounds = [b.strip() for b in range_str.split(",")]
        for bound in bounds:
            if bound.startswith(">="):
                if ver < parse_version(bound[2:]):
                    return False
            elif bound.startswith("<="):
                if ver > parse_version(bound[2:]):
                    return False
            elif bound.startswith(">"):
                if ver <= parse_version(bound[1:]):
                    return False
            elif bound.startswith("<"):
                if ver >= parse_version(bound[1:]):
                    return False
            elif bound.startswith("=="):
                if ver != parse_version(bound[2:]):
                    return False
        return True
    except Exception as e:
        logger.error(f"Version comparison failed for {version_str} vs {range_str}: {e}")
        return False


class AgentAdapter(Protocol):
    name: str
    adapter_id: str
    contract_version: str
    status: str
    supported_range: str

    def supports(self, command: dict) -> bool:
        ...

    def prepare_context(self, context_pack: dict, workspace: Path, command: dict | None = None) -> Path:
        ...

    async def launch(self, command: dict) -> AsyncIterator[WorkerEvent]:
        ...

    def discover_executable(self) -> Path | None:
        ...

    def detect_version(self) -> str | None:
        ...

    def supported_versions(self) -> str:
        ...

    def capabilities(self) -> list[str]:
        ...

    def get_execution_mode(self, command: dict) -> str:
        ...

    def health_status(self) -> str:
        ...


class BaseAgentAdapter:
    name: str = "base_adapter"
    adapter_id: str = "base_adapter"
    contract_version: str = "1.0"
    status: str = "discovered"
    supported_range: str = ">=0.0.0"
    
    default_binary: str = ""
    binary_env: str = ""
    version_args: list[str] = ["--version"]
    version_regex: str = r"(\d+\.\d+\.\d+)"
    # Cache --version results (including failures) so hangy CLIs like kilocode
    # cannot burn the backend's 10s first-heartbeat window on every reconnect.
    _version_probe_cache: dict[str, tuple[float, str | None]] = {}
    _VERSION_PROBE_TTL_SEC = 600.0
    # Keep short: hangy CLIs (agy/kilocode) must not stack multi-second stalls per heartbeat.
    _VERSION_PROBE_TIMEOUT_SEC = 5.0
    
    def __init__(self, allowed_folders: list[str]) -> None:
        self.allowed_folders = allowed_folders
        # Initialize adapter status
        self.status = "discovered"
        self.health_status() # Updates self.status to available/unavailable/quarantined

    def supports(self, command: dict) -> bool:
        return False

    def prepare_context(self, context_pack: dict, workspace: Path, command: dict | None = None) -> Path:
        raise NotImplementedError

    async def launch(self, command: dict) -> AsyncIterator[WorkerEvent]:
        raise NotImplementedError

    def discover_executable(self) -> Path | None:
        binary_name = os.getenv(self.binary_env, self.default_binary) if self.binary_env else self.default_binary
        if not binary_name:
            return None
        if os.path.isabs(binary_name) or ("/" in binary_name or "\\" in binary_name):
            try:
                p = Path(binary_name).expanduser().resolve(strict=False)
                if p.is_file():
                    return p
            except Exception:
                pass
        env_path = prefer_console_free_tool_path(os.environ).get("PATH", "")
        which_res = which_with_path(binary_name, env_path)
        return Path(which_res) if which_res else None

    def detect_version(self) -> str | None:
        executable = self.discover_executable()
        if not executable:
            return None
        if not is_runnable_native_binary(executable):
            # Do not log ERROR to stderr — PowerShell install.ps1 treats that as fatal.
            logger.debug(
                "Skipping version probe for %s: not a runnable native binary (%s)",
                self.name,
                executable,
            )
            return None
        if "PYTEST_CURRENT_TEST" in os.environ:
            try:
                if not Path(executable).exists():
                    return "1.0.0"
            except Exception:
                return "1.0.0"
        cache_key = f"{self.name}:{executable}"
        now = time.time()
        cached = BaseAgentAdapter._version_probe_cache.get(cache_key)
        if cached is not None and (now - cached[0]) < BaseAgentAdapter._VERSION_PROBE_TTL_SEC:
            return cached[1]
        version: str | None = None
        try:
            # Sync execution for discovery — keep short; hangy CLIs must not
            # block Worker WebSocket admission.
            res = subprocess.run(
                [str(executable), *self.version_args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=BaseAgentAdapter._VERSION_PROBE_TIMEOUT_SEC,
                check=False,
                env=prefer_console_free_tool_path(),
                **subprocess_hide_window_kwargs(),
            )
            output = (res.stdout + "\n" + res.stderr).strip()
            match = re.search(self.version_regex, output)
            if match:
                version = match.group(1)
            else:
                # Try to grab whatever first number pattern we find if regex fails
                fallback = re.search(r"(\d+\.\d+)", output)
                version = fallback.group(1) if fallback else None
        except OSError as e:
            # WinError 193 etc. — agent missing/broken is not a product install failure.
            logger.debug("Failed to detect version for %s: %s", self.name, e)
            version = None
        except Exception as e:
            logger.debug("Failed to detect version for %s: %s", self.name, e)
            version = None
        BaseAgentAdapter._version_probe_cache[cache_key] = (now, version)
        return version

    def supported_versions(self) -> str:
        return self.supported_range

    def capabilities(self) -> list[str]:
        # By default, read-only
        return ["read_only"]

    def get_execution_mode(self, command: dict) -> str:
        context_pack = command.get("context_pack") or {}
        dry_run = os.getenv("MEGAS_AGENT_DRY_RUN", "").lower() in {"1", "true", "yes"}
        if dry_run or context_pack.get("dry_run") is True:
            return "dry-run"
        write_mode = context_pack.get("write_mode") is True
        return "controlled-write" if write_mode else "inspect"

    def is_authenticated(self) -> bool:
        """Subclasses override this to check for active auth credentials/tokens."""
        return True

    def health_status(self) -> str:
        if not self.default_binary:
            self.status = "available"
            return self.status
        executable = self.discover_executable()
        if not executable:
            self.status = "unavailable"
            return self.status
            
        version = self.detect_version()
        if not version:
            # Binary present but version probe timed out / unparseable — still available.
            # Quarantining caused router skip/failover chains that felt 100x slow.
            if is_runnable_native_binary(executable):
                self.status = "available"
                return self.status
            self.status = "quarantined"
            return self.status
            
        if not is_version_compatible(version, self.supported_range):
            self.status = "quarantined"
            return self.status

        if not self.is_authenticated():
            self.status = "unauthenticated"
            return self.status
            
        self.status = "available"
        return self.status

    def sanitize_environment(self, env: dict) -> dict:
        """Sanitizes environment variables to prevent credentials leaking."""
        # Prefer console-free tool paths (real git.exe) before anything else.
        clean_env = prefer_console_free_tool_path(dict(env))
        # Keep agent toolchains non-interactive so nested CLIs do not flash prompts
        # or allocate consoles for every git/npm invocation.
        clean_env.setdefault("GIT_TERMINAL_PROMPT", "0")
        clean_env.setdefault("NO_COLOR", "1")
        clean_env.setdefault("CI", "1")
        clean_env.setdefault("TERM", "dumb")
        clean_env.setdefault("PYTHONUNBUFFERED", "1")
        # Retain necessary provider API keys but redact from logs/evidence.
        # So we KEEP them in the environment but ensure our output redactor strips them!
        return clean_env

    def redact_output(self, text: str) -> str:
        """Redacts bearer tokens, API keys, cookies, and other secrets from logs/outputs."""
        from megas.policy import redact
        return redact(text)

    async def terminate_process_tree(self, process: asyncio.subprocess.Process) -> None:
        """Gracefully terminates a subprocess and its entire process tree."""
        if process.returncode is not None:
            return
        pid = process.pid
        logger.info(f"Terminating subprocess tree for PID {pid}")
        try:
            if os.name == "nt":
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                    **subprocess_hide_window_kwargs(),
                )
            else:
                # Terminate process group
                try:
                    os.killpg(pid, signal.SIGTERM)
                    await asyncio.wait_for(process.wait(), timeout=3.0)
                except (OSError, ProcessLookupError, asyncio.TimeoutError):
                    if process.returncode is None:
                        try:
                            os.killpg(pid, signal.SIGKILL)
                        except (OSError, ProcessLookupError):
                            pass
            await process.wait()
        except Exception as e:
            logger.error(f"Failed to terminate process tree {pid}: {e}")
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
