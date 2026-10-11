from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEV_PROJECT_FORMAT = "megas-dev-project"
DEV_PROJECT_VERSION = 1
DEV_REGISTRY_VERSION = 1
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_LOG_BYTES = 8 * 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _megas_home() -> Path:
    configured = os.environ.get("MEGAS_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".megas"


def _dev_dir() -> Path:
    path = _megas_home() / "dev"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _registry_path() -> Path:
    return _dev_dir() / "projects.json"


def _runtime_dir() -> Path:
    path = _dev_dir() / "runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _logs_dir() -> Path:
    path = _dev_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _name(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must be non-empty")
    if len(text) > 128:
        raise ValueError(f"{field_name} is too long")
    return text


def _key(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    clean = "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")[:40]
    return f"{clean or 'project'}-{digest}"


def _local_url(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"{field_name} must use http or https")
    if (parsed.hostname or "").lower() not in LOOPBACK_HOSTS:
        raise ValueError(f"{field_name} must target localhost/loopback only")
    return text


def _relative_cwd(root: Path, value: str | None) -> Path:
    candidate = root if value in {None, "", "."} else (root / str(value)).resolve()
    root_resolved = root.resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("service cwd must stay inside the project root") from exc
    if not candidate.is_dir():
        raise ValueError(f"service cwd does not exist: {candidate}")
    return candidate


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in ("token", "secret", "password", "passwd", "api_key", "apikey", "credential"))


@dataclass(slots=True)
class DevService:
    name: str
    command: list[str]
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)
    env_refs: dict[str, str] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    ports: list[int] = field(default_factory=list)
    health_url: str | None = None
    open_url: str | None = None
    startup_timeout_seconds: float = 45.0

    def validate(self) -> None:
        self.name = _name(self.name, field_name="service name")
        if not self.command or any(not str(value).strip() for value in self.command):
            raise ValueError(f"service {self.name} command must contain executable tokens")
        self.command = [str(value) for value in self.command]
        self.cwd = str(self.cwd or ".")
        self.env = {str(key): str(value) for key, value in self.env.items()}
        self.env_refs = {str(key): str(value) for key, value in self.env_refs.items()}
        for key in self.env:
            if _is_sensitive_key(key):
                raise ValueError(
                    f"service {self.name} env key {key} looks secret-like; use env_refs instead"
                )
        for target, source in self.env_refs.items():
            _name(target, field_name=f"service {self.name} env_refs target")
            _name(source, field_name=f"service {self.name} env_refs source")
        self.depends_on = [
            _name(value, field_name=f"service {self.name} dependency") for value in self.depends_on
        ]
        normalized_ports: list[int] = []
        for value in self.ports:
            port = int(value)
            if not (1 <= port <= 65535):
                raise ValueError(f"service {self.name} has invalid port: {port}")
            normalized_ports.append(port)
        self.ports = sorted(set(normalized_ports))
        self.health_url = _local_url(self.health_url, field_name=f"service {self.name} health_url")
        self.open_url = _local_url(self.open_url, field_name=f"service {self.name} open_url")
        self.startup_timeout_seconds = float(self.startup_timeout_seconds)
        if not (0 < self.startup_timeout_seconds <= 300):
            raise ValueError("startup_timeout_seconds must be between 0 and 300")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "name": self.name,
            "command": list(self.command),
            "cwd": self.cwd,
            "env": dict(sorted(self.env.items())),
            "env_refs": dict(sorted(self.env_refs.items())),
            "depends_on": list(self.depends_on),
            "ports": list(self.ports),
            "health_url": self.health_url,
            "open_url": self.open_url,
            "startup_timeout_seconds": self.startup_timeout_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DevService":
        service = cls(
            name=str(data.get("name") or ""),
            command=[str(value) for value in (data.get("command") or [])],
            cwd=str(data.get("cwd") or "."),
            env={str(key): str(value) for key, value in (data.get("env") or {}).items()},
            env_refs={str(key): str(value) for key, value in (data.get("env_refs") or {}).items()},
            depends_on=[str(value) for value in (data.get("depends_on") or [])],
            ports=[int(value) for value in (data.get("ports") or [])],
            health_url=str(data["health_url"]) if data.get("health_url") is not None else None,
            open_url=str(data["open_url"]) if data.get("open_url") is not None else None,
            startup_timeout_seconds=float(data.get("startup_timeout_seconds") or 45.0),
        )
        service.validate()
        return service


@dataclass(slots=True)
class DevGit:
    enabled: bool = False
    remote: str = "origin"
    branch: str = "main"
    sync_before_start: bool = False

    def validate(self) -> None:
        self.remote = _name(self.remote, field_name="git remote")
        self.branch = _name(self.branch, field_name="git branch")
        if any(ch.isspace() for ch in self.remote + self.branch):
            raise ValueError("git remote/branch may not contain whitespace")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "enabled": bool(self.enabled),
            "remote": self.remote,
            "branch": self.branch,
            "sync_before_start": bool(self.sync_before_start),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DevGit":
        data = data or {}
        value = cls(
            enabled=bool(data.get("enabled", False)),
            remote=str(data.get("remote") or "origin"),
            branch=str(data.get("branch") or "main"),
            sync_before_start=bool(data.get("sync_before_start", False)),
        )
        value.validate()
        return value


@dataclass(slots=True)
class DevProject:
    name: str
    root: str
    services: list[DevService]
    description: str = ""
    open_url: str | None = None
    git: DevGit = field(default_factory=DevGit)
    format: str = DEV_PROJECT_FORMAT
    version: int = DEV_PROJECT_VERSION

    def validate(self) -> None:
        if self.format != DEV_PROJECT_FORMAT:
            raise ValueError(f"unsupported developer project format: {self.format}")
        if self.version != DEV_PROJECT_VERSION:
            raise ValueError(f"unsupported developer project version: {self.version}")
        self.name = _name(self.name, field_name="project name")
        root = Path(self.root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"project root does not exist: {root}")
        self.root = str(root)
        self.open_url = _local_url(self.open_url, field_name="project open_url")
        self.git.validate()
        names: set[str] = set()
        for service in self.services:
            service.validate()
            _relative_cwd(root, service.cwd)
            if service.name in names:
                raise ValueError(f"duplicate service name: {service.name}")
            names.add(service.name)
        for service in self.services:
            missing = [name for name in service.depends_on if name not in names]
            if missing:
                raise ValueError(
                    f"service {service.name} depends on unknown service(s): {', '.join(missing)}"
                )
            if service.name in service.depends_on:
                raise ValueError(f"service {service.name} cannot depend on itself")
        self.start_order()

    def service(self, name: str) -> DevService:
        for service in self.services:
            if service.name == name:
                return service
        raise KeyError(f"unknown developer service: {name}")

    def start_order(self, selected: str | None = None) -> list[DevService]:
        by_name = {service.name: service for service in self.services}
        targets = list(by_name) if selected is None else [selected]
        if selected is not None and selected not in by_name:
            raise KeyError(f"unknown developer service: {selected}")
        visiting: set[str] = set()
        visited: set[str] = set()
        result: list[DevService] = []

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                raise ValueError(f"developer service dependency cycle includes: {name}")
            visiting.add(name)
            service = by_name[name]
            for dependency in service.depends_on:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)
            result.append(service)

        for target in targets:
            visit(target)
        return result

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "format": self.format,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "root": self.root,
            "open_url": self.open_url,
            "git": self.git.to_dict(),
            "services": [service.to_dict() for service in self.services],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DevProject":
        project = cls(
            format=str(data.get("format") or ""),
            version=int(data.get("version") or 0),
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            root=str(data.get("root") or ""),
            open_url=str(data["open_url"]) if data.get("open_url") is not None else None,
            git=DevGit.from_dict(data.get("git") if isinstance(data.get("git"), dict) else None),
            services=[
                DevService.from_dict(row)
                for row in (data.get("services") or [])
                if isinstance(row, dict)
            ],
        )
        if len(project.services) != len(data.get("services") or []):
            raise ValueError("developer project contains malformed service records")
        project.validate()
        return project


def load_project_manifest(path: Path | str) -> DevProject:
    file_path = Path(path).expanduser()
    raw = file_path.read_bytes()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("developer project manifest exceeds 2 MiB")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("developer project manifest must be a JSON object")
    return DevProject.from_dict(data)


def _read_registry() -> dict[str, Any]:
    path = _registry_path()
    if not path.exists():
        return {"version": DEV_REGISTRY_VERSION, "projects": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or int(data.get("version") or 0) != DEV_REGISTRY_VERSION:
        raise ValueError("unsupported developer project registry")
    projects = data.get("projects") or []
    if not isinstance(projects, list):
        raise ValueError("developer project registry is malformed")
    return data


def _write_registry(rows: list[dict[str, str]]) -> None:
    _atomic_json(
        _registry_path(),
        {
            "version": DEV_REGISTRY_VERSION,
            "projects": sorted(rows, key=lambda row: row["name"].lower()),
        },
    )


def register_project(path: Path | str, *, replace: bool = False) -> dict[str, Any]:
    manifest_path = Path(path).expanduser().resolve()
    project = load_project_manifest(manifest_path)
    registry = _read_registry()
    rows = [row for row in registry["projects"] if isinstance(row, dict)]
    existing = next((row for row in rows if str(row.get("name")) == project.name), None)
    if existing is not None and not replace:
        raise FileExistsError(f"developer project already registered: {project.name}")
    rows = [row for row in rows if str(row.get("name")) != project.name]
    rows.append({"name": project.name, "manifest": str(manifest_path)})
    _write_registry(rows)
    return {"registered": project.name, "manifest": str(manifest_path), "services": [s.name for s in project.services]}


def _registered_project(reference: str) -> tuple[DevProject, Path]:
    name = _name(reference, field_name="project")
    rows = [row for row in _read_registry()["projects"] if isinstance(row, dict)]
    row = next((row for row in rows if str(row.get("name")) == name), None)
    if row is None:
        raise KeyError(f"developer project not registered: {name}")
    manifest_path = Path(str(row.get("manifest") or "")).expanduser()
    return load_project_manifest(manifest_path), manifest_path


def list_projects() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in _read_registry()["projects"]:
        if not isinstance(row, dict):
            continue
        try:
            project = load_project_manifest(str(row.get("manifest") or ""))
            result.append({
                "name": project.name,
                "description": project.description,
                "manifest": str(row.get("manifest") or ""),
                "root": project.root,
                "services": [service.name for service in project.services],
                "status": project_status(project.name)["state"],
            })
        except Exception as exc:
            result.append({
                "name": str(row.get("name") or ""),
                "manifest": str(row.get("manifest") or ""),
                "status": "invalid",
                "error_type": type(exc).__name__,
            })
    return result


def _state_path(project: DevProject) -> Path:
    return _runtime_dir() / f"{_key(project.name)}.json"


def _read_state(project: DevProject) -> dict[str, Any]:
    path = _state_path(project)
    if not path.exists():
        return {"version": 1, "project": project.name, "services": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("developer runtime state is malformed")
    value.setdefault("services", {})
    return value


def _write_state(project: DevProject, state: dict[str, Any]) -> None:
    state["version"] = 1
    state["project"] = project.name
    _atomic_json(_state_path(project), state)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _port_open(port: int, *, host: str = "127.0.0.1", timeout: float = 0.15) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _health_ready(url: str, *, timeout: float = 0.5) -> bool:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= int(response.status) < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _service_ready(service: DevService) -> bool:
    if service.health_url:
        return _health_ready(service.health_url)
    if service.ports:
        return all(_port_open(port) for port in service.ports)
    return True


def _secret_values(service: DevService) -> list[str]:
    return [os.environ.get(source, "") for source in service.env_refs.values() if os.environ.get(source)]


def _redact(text: str, secrets: list[str]) -> str:
    value = text
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value


def _service_status(project: DevProject, service: DevService, state: dict[str, Any]) -> dict[str, Any]:
    record = (state.get("services") or {}).get(service.name) or {}
    pid = int(record.get("pid") or 0)
    alive = _pid_alive(pid)
    ports = [
        {"port": port, "open": _port_open(port), "host": "127.0.0.1"}
        for port in service.ports
    ]
    ready = alive and _service_ready(service)
    return {
        "service": service.name,
        "pid": pid if alive else None,
        "process_alive": alive,
        "ready": ready,
        "ports": ports,
        "health_url": service.health_url,
        "open_url": service.open_url,
        "started_at": record.get("started_at") if alive else None,
        "log": record.get("log") if alive else None,
    }


def project_status(reference: str) -> dict[str, Any]:
    project, manifest_path = _registered_project(reference)
    state = _read_state(project)
    services = [_service_status(project, service, state) for service in project.services]
    alive = [row for row in services if row["process_alive"]]
    ready = [row for row in services if row["ready"]]
    if not alive:
        overall = "stopped"
    elif len(ready) == len(project.services):
        overall = "ready"
    elif len(alive) == len(project.services):
        overall = "starting-or-degraded"
    else:
        overall = "partial"
    return {
        "project": project.name,
        "manifest": str(manifest_path),
        "root": project.root,
        "state": overall,
        "services": services,
    }


def _build_env(service: DevService) -> dict[str, str]:
    env = os.environ.copy()
    env.update(service.env)
    missing: list[str] = []
    for target, source in service.env_refs.items():
        value = os.environ.get(source)
        if value is None:
            missing.append(source)
        else:
            env[target] = value
    if missing:
        raise RuntimeError(
            f"service {service.name} is missing environment reference(s): {', '.join(sorted(set(missing)))}"
        )
    return env


def _rotate_log(path: Path) -> None:
    if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
        backup = path.with_suffix(path.suffix + ".1"
        )
        backup.unlink(missing_ok=True)
        os.replace(path, backup)


def _start_service(project: DevProject, service: DevService, state: dict[str, Any]) -> dict[str, Any]:
    current = _service_status(project, service, state)
    if current["process_alive"]:
        return {**current, "changed": False}

    cwd = _relative_cwd(Path(project.root), service.cwd)
    log_dir = _logs_dir() / _key(project.name)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{_key(service.name)}.log"
    _rotate_log(log_path)
    log_handle = open(log_path, "ab", buffering=0)
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": _build_env(service),
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "shell": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(service.command, **kwargs)
    finally:
        log_handle.close()

    state.setdefault("services", {})[service.name] = {
        "pid": process.pid,
        "started_at": time.time(),
        "log": str(log_path),
    }
    _write_state(project, state)

    deadline = time.monotonic() + service.startup_timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            tail = tail_logs(project.name, service.name, lines=40)["text"]
            state.get("services", {}).pop(service.name, None)
            _write_state(project, state)
            raise RuntimeError(
                f"service {service.name} exited during startup with code {process.returncode}: {tail[-2000:]}"
            )
        if _service_ready(service):
            return {**_service_status(project, service, state), "changed": True}
        time.sleep(0.15)

    _stop_pid(process.pid)
    state.get("services", {}).pop(service.name, None)
    _write_state(project, state)
    raise TimeoutError(f"service {service.name} did not become ready within {service.startup_timeout_seconds:g}s")


def _stop_pid(pid: int) -> None:
    if not _pid_alive(pid):
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _git(project: DevProject, *args: str, timeout: float = 60.0) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=project.root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
        timeout=timeout,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stdout[-2000:]}")
    return completed.stdout.strip()


def sync_project(reference: str) -> dict[str, Any]:
    project, _ = _registered_project(reference)
    if not project.git.enabled:
        raise RuntimeError(f"git sync is not enabled in the manifest for {project.name}")
    dirty = _git(project, "status", "--porcelain")
    if dirty:
        raise RuntimeError("git sync refused because the project working tree has local changes")
    branch = _git(project, "branch", "--show-current")
    if branch != project.git.branch:
        raise RuntimeError(
            f"git sync refused: current branch is {branch or '(detached)'}, manifest requires {project.git.branch}"
        )
    before = _git(project, "rev-parse", "HEAD")
    output = _git(project, "pull", "--ff-only", project.git.remote, project.git.branch, timeout=120)
    after = _git(project, "rev-parse", "HEAD")
    return {
        "project": project.name,
        "changed": before != after,
        "before": before,
        "after": after,
        "remote": project.git.remote,
        "branch": project.git.branch,
        "output": output[-2000:],
    }


def start_project(reference: str, *, service_name: str | None = None, open_after: bool = True) -> dict[str, Any]:
    project, _ = _registered_project(reference)
    if project.git.enabled and project.git.sync_before_start:
        sync_project(project.name)
    state = _read_state(project)
    started: list[dict[str, Any]] = []
    newly_started: list[DevService] = []
    try:
        for service in project.start_order(service_name):
            result = _start_service(project, service, state)
            started.append(result)
            if result.get("changed"):
                newly_started.append(service)
    except Exception:
        for service in reversed(newly_started):
            record = state.get("services", {}).get(service.name) or {}
            _stop_pid(int(record.get("pid") or 0))
            state.get("services", {}).pop(service.name, None)
        _write_state(project, state)
        raise

    opened = None
    if open_after and service_name is None:
        opened = open_project(project.name).get("url")
    return {
        "project": project.name,
        "service": service_name,
        "started": started,
        "opened": opened,
        "status": project_status(project.name),
    }


def stop_project(reference: str, *, service_name: str | None = None) -> dict[str, Any]:
    project, _ = _registered_project(reference)
    state = _read_state(project)
    services = project.start_order(service_name)
    stopped: list[dict[str, Any]] = []
    for service in reversed(services):
        record = state.get("services", {}).get(service.name) or {}
        pid = int(record.get("pid") or 0)
        alive = _pid_alive(pid)
        if alive:
            _stop_pid(pid)
        state.get("services", {}).pop(service.name, None)
        stopped.append({"service": service.name, "pid": pid if pid else None, "changed": alive})
    _write_state(project, state)
    return {"project": project.name, "service": service_name, "stopped": stopped, "status": project_status(project.name)}


def restart_project(reference: str, *, service_name: str | None = None, open_after: bool = True) -> dict[str, Any]:
    stop_project(reference, service_name=service_name)
    return start_project(reference, service_name=service_name, open_after=open_after)


def ports_view(reference: str | None = None) -> list[dict[str, Any]]:
    projects = [reference] if reference else [row["name"] for row in _read_registry()["projects"] if isinstance(row, dict)]
    result: list[dict[str, Any]] = []
    for project_name in projects:
        project, _ = _registered_project(str(project_name))
        state = _read_state(project)
        for service in project.services:
            status = _service_status(project, service, state)
            for port in status["ports"]:
                result.append({
                    "project": project.name,
                    "service": service.name,
                    "port": port["port"],
                    "host": port["host"],
                    "open": port["open"],
                    "pid": status["pid"],
                    "process_alive": status["process_alive"],
                })
    return sorted(result, key=lambda row: (row["project"].lower(), row["service"].lower(), row["port"]))


def open_project(reference: str) -> dict[str, Any]:
    project, _ = _registered_project(reference)
    candidates = [project.open_url]
    candidates.extend(service.open_url for service in project.services)
    candidates.extend(service.health_url for service in project.services)
    url = next((value for value in candidates if value), None)
    if not url:
        raise RuntimeError(f"project {project.name} does not declare a local open_url")
    opened = bool(webbrowser.open(url, new=2))
    return {"project": project.name, "url": url, "opened": opened}


def tail_logs(reference: str, service_name: str, *, lines: int = 120) -> dict[str, Any]:
    if lines < 1 or lines > 5000:
        raise ValueError("lines must be between 1 and 5000")
    project, _ = _registered_project(reference)
    service = project.service(service_name)
    state = _read_state(project)
    record = state.get("services", {}).get(service.name) or {}
    path_text = str(record.get("log") or "")
    if not path_text:
        default = _logs_dir() / _key(project.name) / f"{_key(service.name)}.log"
        path = default
    else:
        path = Path(path_text)
    if not path.exists():
        return {"project": project.name, "service": service.name, "lines": 0, "text": ""}
    raw = path.read_bytes()[-MAX_LOG_BYTES:]
    text = raw.decode("utf-8", errors="replace")
    selected = "\n".join(text.splitlines()[-lines:])
    selected = _redact(selected, _secret_values(service))
    return {"project": project.name, "service": service.name, "lines": len(selected.splitlines()), "text": selected}


def unregister_project(reference: str, *, stop: bool = False) -> dict[str, Any]:
    project, _ = _registered_project(reference)
    status = project_status(project.name)
    if any(row["process_alive"] for row in status["services"]):
        if not stop:
            raise RuntimeError(f"project {project.name} still has running services; repeat with --stop")
        stop_project(project.name)
    rows = [
        row
        for row in _read_registry()["projects"]
        if isinstance(row, dict) and str(row.get("name")) != project.name
    ]
    _write_registry(rows)
    _state_path(project).unlink(missing_ok=True)
    return {"unregistered": project.name}


def validate_manifest(path: Path | str) -> dict[str, Any]:
    project = load_project_manifest(path)
    return {
        "format": project.format,
        "version": project.version,
        "name": project.name,
        "root": project.root,
        "open_url": project.open_url,
        "git": project.git.to_dict(),
        "start_order": [service.name for service in project.start_order()],
        "services": [service.to_dict() for service in project.services],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="megas dev",
        description="Local developer project orchestrator: start only the project/services you need, track readiness/ports/PIDs, and keep commands owner-declared.",
    )
    parser.add_argument("--json", action="store_true")
    actions = parser.add_subparsers(dest="dev_command", required=True)

    validate = actions.add_parser("validate", help="validate an owner-authored developer project manifest")
    validate.add_argument("manifest")

    register = actions.add_parser("register", help="register a developer project manifest")
    register.add_argument("manifest")
    register.add_argument("--replace", action="store_true")

    unregister = actions.add_parser("unregister", help="remove a project from the local developer registry")
    unregister.add_argument("project")
    unregister.add_argument("--stop", action="store_true")

    actions.add_parser("list", help="list registered developer projects")

    show = actions.add_parser("show", help="show one project manifest plus live status")
    show.add_argument("project")

    start = actions.add_parser("start", help="start one project, or one service with dependencies")
    start.add_argument("project")
    start.add_argument("--service")
    start.add_argument("--no-open", action="store_true")

    stop = actions.add_parser("stop", help="stop one project, or one service and its dependency slice")
    stop.add_argument("project")
    stop.add_argument("--service")

    restart = actions.add_parser("restart", help="restart one project, or one service with dependencies")
    restart.add_argument("project")
    restart.add_argument("--service")
    restart.add_argument("--no-open", action="store_true")

    status = actions.add_parser("status", help="show process/readiness state for one project")
    status.add_argument("project")

    ports = actions.add_parser("ports", help="show declared local ports with project/service/PID state")
    ports.add_argument("project", nargs="?")

    open_cmd = actions.add_parser("open", help="open the project's declared local URL in the browser")
    open_cmd.add_argument("project")

    sync = actions.add_parser("sync", help="perform owner-configured git ff-only sync for one clean project")
    sync.add_argument("project")

    logs = actions.add_parser("logs", help="tail one service's bounded, secret-redacted log")
    logs.add_argument("project")
    logs.add_argument("service")
    logs.add_argument("--lines", type=int, default=120)
    return parser


def run(args: argparse.Namespace) -> Any:
    command = args.dev_command
    if command == "validate":
        return validate_manifest(args.manifest)
    if command == "register":
        return register_project(args.manifest, replace=args.replace)
    if command == "unregister":
        return unregister_project(args.project, stop=args.stop)
    if command == "list":
        return list_projects()
    if command == "show":
        project, manifest = _registered_project(args.project)
        return {"manifest": str(manifest), "project": project.to_dict(), "status": project_status(project.name)}
    if command == "start":
        return start_project(args.project, service_name=args.service, open_after=not args.no_open)
    if command == "stop":
        return stop_project(args.project, service_name=args.service)
    if command == "restart":
        return restart_project(args.project, service_name=args.service, open_after=not args.no_open)
    if command == "status":
        return project_status(args.project)
    if command == "ports":
        return ports_view(args.project)
    if command == "open":
        return open_project(args.project)
    if command == "sync":
        return sync_project(args.project)
    if command == "logs":
        return tail_logs(args.project, args.service, lines=args.lines)
    raise ValueError(f"unknown developer command: {command}")


def _emit(value: Any, machine: bool) -> None:
    if machine:
        print(json.dumps(value, indent=2, sort_keys=True, default=str))
    elif isinstance(value, list):
        if not value:
            print("(none)")
        for item in value:
            print(json.dumps(item, sort_keys=True, default=str))
    elif isinstance(value, dict):
        for key, item in value.items():
            print(f"{key}: {json.dumps(item, sort_keys=True, default=str) if isinstance(item, (dict, list)) else item}")
    else:
        print(value)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        _emit(run(args), args.json)
    except (
        FileExistsError,
        FileNotFoundError,
        KeyError,
        ValueError,
        RuntimeError,
        TimeoutError,
        PermissionError,
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ) as exc:
        if args.json:
            _emit({"error": type(exc).__name__, "message": str(exc)}, True)
        else:
            print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


__all__ = [
    "DEV_PROJECT_FORMAT",
    "DEV_PROJECT_VERSION",
    "DevGit",
    "DevProject",
    "DevService",
    "build_parser",
    "list_projects",
    "load_project_manifest",
    "main",
    "open_project",
    "ports_view",
    "project_status",
    "register_project",
    "restart_project",
    "start_project",
    "stop_project",
    "sync_project",
    "tail_logs",
    "unregister_project",
    "validate_manifest",
]
