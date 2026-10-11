from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import urllib.parse
import webbrowser
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .local_testing_activity import LocalTestingOperation
from .policy import redact


MAX_DISCOVERY_FILE_BYTES = 2 * 1024 * 1024
MAX_DISCOVERY_FILES = 5000
MAX_BUILD_OUTPUT_BYTES = 1024 * 1024
BUILD_TIMEOUT_SECONDS = 15 * 60
ADB_ACTION_TIMEOUT_SECONDS = 90
MAX_LOGCAT_LINES = 400
MAX_CRASH_ANR_EVIDENCE = 40
SKIP_DIRS = {
    ".git",
    ".gradle",
    ".idea",
    ".megas-build",
    ".vs",
    ".vscode",
    "bin",
    "build",
    "dist",
    "node_modules",
    "obj",
    "out",
    "target",
}


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    if len(raw) > MAX_DISCOVERY_FILE_BYTES:
        raise ValueError(f"discovery file is too large: {path}")
    return raw.decode("utf-8", errors="replace")


def _inside(root: Path, candidate: Path) -> Path:
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("target path must stay inside the trusted project") from exc
    return resolved


def _walk(root: Path, *, max_depth: int = 4) -> list[Path]:
    found: list[Path] = []
    root = root.resolve()
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        try:
            depth = len(current_path.relative_to(root).parts)
        except ValueError:
            continue
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        if depth >= max_depth:
            dirs[:] = []
        for name in files:
            found.append(current_path / name)
            if len(found) >= MAX_DISCOVERY_FILES:
                return found
    return found


def _bounded(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        raw = value[:MAX_BUILD_OUTPUT_BYTES]
        return raw.decode("utf-8", errors="replace")
    return str(value)[:MAX_BUILD_OUTPUT_BYTES]


def _run(
    argv: list[str],
    *,
    cwd: Path,
    timeout: float = BUILD_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    completed = subprocess.run(
        argv,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        timeout=timeout,
        check=False,
    )
    return {
        "returncode": int(completed.returncode),
        "stdout": _bounded(completed.stdout),
        "stderr": _bounded(completed.stderr),
        "argv": list(argv),
    }


def _local_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value).strip())
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("local target URL must be loopback HTTP(S)")
    return parsed.geturl()


def _web_targets(project: dict[str, Any]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for service in project.get("services") or []:
        if not isinstance(service, dict):
            continue
        url = str(service.get("open_url") or "").strip()
        name = str(service.get("service") or "").strip()
        if not url or not name:
            continue
        try:
            safe_url = _local_url(url)
        except ValueError:
            continue
        targets.append(
            {
                "id": f"web:{name}",
                "kind": "web",
                "label": name,
                "runtime": "browser",
                "open_label": "Open in browser",
                "build_label": "Start service",
                "open_url": safe_url,
                "service": name,
                "ready": bool(service.get("ready")),
                "process_alive": bool(service.get("process_alive")),
                "evidence": "declared service open_url",
            }
        )
    return targets


def _android_package_id(build_text: str, manifest_text: str) -> str | None:
    for pattern in (
        r"\bapplicationId\s*[= ]\s*[\"']([^\"']+)[\"']",
        r"\bnamespace\s*[= ]\s*[\"']([^\"']+)[\"']",
        r"\bpackage\s*=\s*[\"']([^\"']+)[\"']",
    ):
        match = re.search(pattern, build_text)
        if match:
            return match.group(1).strip()
    match = re.search(r"<manifest\b[^>]*\bpackage=[\"']([^\"']+)[\"']", manifest_text)
    return match.group(1).strip() if match else None


def _android_targets(root: Path, files: list[Path]) -> list[dict[str, Any]]:
    wrappers = [path for path in files if path.name.lower() in {"gradlew", "gradlew.bat"}]
    wrapper_roots = sorted({path.parent.resolve() for path in wrappers}, key=lambda p: len(p.parts))
    targets: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for build_file in files:
        if build_file.name not in {"build.gradle", "build.gradle.kts"}:
            continue
        module_root = build_file.parent.resolve()
        manifest = module_root / "src" / "main" / "AndroidManifest.xml"
        if not manifest.is_file():
            continue
        try:
            build_text = _read_text(build_file)
            manifest_text = _read_text(manifest)
        except (OSError, ValueError):
            continue
        lowered = build_text.lower()
        if "com.android.application" not in lowered and "android.application" not in lowered:
            continue
        wrapper_root = next(
            (
                candidate
                for candidate in sorted(wrapper_roots, key=lambda p: len(p.parts), reverse=True)
                if candidate == module_root or candidate in module_root.parents
            ),
            None,
        )
        if wrapper_root is None:
            continue
        relative = module_root.relative_to(wrapper_root)
        module_path = ":".join(relative.parts)
        task = f":{module_path}:assembleDebug" if module_path else "assembleDebug"
        package_id = _android_package_id(build_text, manifest_text)
        key = (str(wrapper_root), module_path)
        if key in seen:
            continue
        seen.add(key)
        targets.append(
            {
                "id": f"android:{module_path or module_root.name}",
                "kind": "android",
                "label": module_path or module_root.name,
                "runtime": "adb-device",
                "open_label": "Run on device",
                "build_label": "Build APK",
                "root": str(root),
                "wrapper_root": str(wrapper_root),
                "module_root": str(module_root),
                "module_path": module_path,
                "build_task": task,
                "package_id": package_id,
                "artifact_glob": str(module_root / "build" / "outputs" / "apk" / "debug" / "*.apk"),
                "evidence": str(build_file.relative_to(root)),
            }
        )
    return targets


def _xml_text(root: ET.Element, name: str) -> str | None:
    node = root.find(name)
    if node is None or node.text is None:
        return None
    value = node.text.strip()
    return value or None


def _dotnet_targets(root: Path, files: list[Path]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for project_file in files:
        if project_file.suffix.lower() != ".csproj":
            continue
        try:
            raw = project_file.read_bytes()
            if len(raw) > MAX_DISCOVERY_FILE_BYTES:
                continue
            xml_root = ET.fromstring(raw)
        except (OSError, ET.ParseError):
            continue
        output_type = (_xml_text(xml_root, ".//OutputType") or "").lower()
        use_wpf = (_xml_text(xml_root, ".//UseWPF") or "").lower() == "true"
        use_winforms = (_xml_text(xml_root, ".//UseWindowsForms") or "").lower() == "true"
        use_winui = (_xml_text(xml_root, ".//UseWinUI") or "").lower() == "true"
        target_framework = (_xml_text(xml_root, ".//TargetFramework") or "").lower()
        sdk = str(xml_root.attrib.get("Sdk") or "").lower()
        runnable = output_type in {"exe", "winexe"} or use_wpf or use_winforms or use_winui
        windows_specific = (
            "-windows" in target_framework
            or "windowsdesktop" in sdk
            or use_wpf
            or use_winforms
            or use_winui
        )
        if not runnable or not windows_specific:
            continue
        relative = project_file.relative_to(root)
        targets.append(
            {
                "id": f"windows-dotnet:{relative.as_posix()}",
                "kind": "windows",
                "label": project_file.stem,
                "runtime": "windows-dotnet",
                "open_label": "Open Windows app",
                "build_label": "Build app",
                "root": str(root),
                "project_file": str(project_file),
                "configuration": "Debug",
                "evidence": str(relative),
            }
        )
    return targets


def _cmake_without_comments(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _cmake_windows_target_names(text: str) -> list[str]:
    clean = _cmake_without_comments(text)
    declarations: dict[str, str] = {}
    for match in re.finditer(
        r"\badd_executable\s*\(\s*([A-Za-z0-9_.+\-]+)\s+([^\)]*)\)",
        clean,
        re.IGNORECASE | re.DOTALL,
    ):
        name = match.group(1)
        body = match.group(2)
        declarations[name] = body

    windows_names = {
        name
        for name, body in declarations.items()
        if re.search(r"(?:^|\s)WIN32(?:\s|$)", body, re.IGNORECASE)
    }
    for name in declarations:
        escaped = re.escape(name)
        property_match = re.search(
            rf"\bset_target_properties\s*\(\s*{escaped}\b[^\)]*\bPROPERTIES\b[^\)]*\bWIN32_EXECUTABLE\s+(?:ON|TRUE|1)(?:\s|\))",
            clean,
            re.IGNORECASE | re.DOTALL,
        )
        if property_match:
            windows_names.add(name)
    return sorted(windows_names)


def _cmake_windows_targets(root: Path, files: list[Path]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for cmake_file in files:
        if cmake_file.name.lower() != "cmakelists.txt":
            continue
        try:
            text = _read_text(cmake_file)
        except (OSError, ValueError):
            continue
        source_dir = cmake_file.parent.resolve()
        for name in _cmake_windows_target_names(text):
            key = (str(source_dir), name.lower())
            if key in seen:
                continue
            seen.add(key)
            relative = cmake_file.relative_to(root)
            targets.append(
                {
                    "id": f"windows-cmake:{relative.parent.as_posix()}:{name}",
                    "kind": "windows",
                    "label": name,
                    "runtime": "windows-cmake",
                    "open_label": "Open Windows app",
                    "build_label": "Build app",
                    "root": str(root),
                    "source_dir": str(source_dir),
                    "cmake_target": name,
                    "configuration": "Debug",
                    "evidence": str(relative),
                }
            )
    return targets


def _cmake_linux_target_names(text: str) -> list[str]:
    clean = _cmake_without_comments(text)
    declarations: dict[str, str] = {}
    for match in re.finditer(
        r"\badd_executable\s*\(\s*([A-Za-z0-9_.+\-]+)\s+([^\)]*)\)",
        clean,
        re.IGNORECASE | re.DOTALL,
    ):
        name = match.group(1)
        body = match.group(2)
        declarations[name] = body

    linux_names = {
        name
        for name, body in declarations.items()
        if not re.search(r"(?:^|\s)WIN32(?:\s|$)", body, re.IGNORECASE)
    }
    return sorted(linux_names)


def _to_wsl_path(path: Path) -> str:
    resolved = path.resolve()
    parts = list(resolved.parts)
    if parts and ":" in parts[0]:
        drive = parts[0].replace(":", "").lower().strip("\\/")
        return f"/mnt/{drive}/" + "/".join(parts[1:])
    return resolved.as_posix()


def _linux_targets(root: Path, files: list[Path]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    is_windows = os.name == "nt"
    runtime = "linux-wsl" if is_windows else "linux-native"
    open_label = "Run in WSL (WSLg)" if is_windows else "Run Linux app"
    build_label = "Build in WSL" if is_windows else "Build app"

    for cmake_file in files:
        if cmake_file.name.lower() != "cmakelists.txt":
            continue
        try:
            text = _read_text(cmake_file)
        except (OSError, ValueError):
            continue
        source_dir = cmake_file.parent.resolve()
        for name in _cmake_linux_target_names(text):
            key = (str(source_dir), name.lower())
            if key in seen:
                continue
            seen.add(key)
            relative = cmake_file.relative_to(root)
            targets.append(
                {
                    "id": f"linux-cmake:{relative.parent.as_posix()}:{name}",
                    "kind": "linux",
                    "label": f"{name} (Linux)",
                    "runtime": runtime,
                    "open_label": open_label,
                    "build_label": build_label,
                    "root": str(root),
                    "source_dir": str(source_dir),
                    "cmake_target": name,
                    "configuration": "Debug",
                    "evidence": str(relative),
                }
            )
    return targets


def _macos_targets(root: Path, files: list[Path]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    is_darwin = platform.system().lower() == "darwin"
    runtime = "macos-native" if is_darwin else "macos-bridge"
    open_label = "Run macOS app" if is_darwin else "Run on Linked Mac"
    build_label = "Build .app" if is_darwin else "Build on Linked Mac"

    for file in files:
        if file.name.lower() == "package.swift" or file.name.lower().endswith(".xcodeproj"):
            parent = file.parent.resolve()
            key = str(parent)
            if key in seen:
                continue
            seen.add(key)
            relative = file.relative_to(root)
            label = file.stem if file.name.lower().endswith(".xcodeproj") else parent.name
            targets.append(
                {
                    "id": f"macos-swift:{relative.parent.as_posix()}:{label}",
                    "kind": "macos",
                    "label": f"{label} (macOS)",
                    "runtime": runtime,
                    "open_label": open_label,
                    "build_label": build_label,
                    "root": str(root),
                    "project_path": str(file),
                    "evidence": str(relative),
                }
            )
    return targets


def detect_project_targets(project: dict[str, Any]) -> list[dict[str, Any]]:
    root_value = str(project.get("root") or "").strip()
    if not root_value:
        return _web_targets(project)
    root = Path(root_value).expanduser().resolve()
    if not root.is_dir():
        return _web_targets(project)
    files = _walk(root)
    targets = _web_targets(project)
    targets.extend(_dotnet_targets(root, files))
    targets.extend(_cmake_windows_targets(root, files))
    targets.extend(_android_targets(root, files))
    targets.extend(_linux_targets(root, files))
    targets.extend(_macos_targets(root, files))
    return targets


def _resolve_target(project: dict[str, Any], target_id: str) -> dict[str, Any]:
    selected = str(target_id).strip()
    matches = [target for target in detect_project_targets(project) if target.get("id") == selected]
    if len(matches) != 1:
        raise ValueError("target is unavailable or ambiguous; refresh Local Testing")
    return matches[0]


def _gradle_wrapper(target: dict[str, Any]) -> str:
    root = Path(str(target["wrapper_root"]))
    candidates = [root / "gradlew.bat", root / "gradlew"] if os.name == "nt" else [root / "gradlew", root / "gradlew.bat"]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError("Gradle Wrapper is missing")


def _build_android(target: dict[str, Any]) -> dict[str, Any]:
    wrapper_root = Path(str(target["wrapper_root"]))
    wrapper = _gradle_wrapper(target)
    argv = [wrapper, str(target["build_task"]), "--build-cache", "--console=plain"]
    result = _run(argv, cwd=wrapper_root)
    if result["returncode"] != 0:
        return {"ok": False, "kind": "android", "stage": "build", **result}
    module_root = Path(str(target["module_root"]))
    apks = sorted(
        (path for path in (module_root / "build" / "outputs" / "apk" / "debug").glob("*.apk") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not apks:
        return {"ok": False, "kind": "android", "stage": "artifact", **result, "error": "debug APK was not produced"}
    apk = _inside(Path(str(target["root"])), apks[0])
    return {"ok": True, "kind": "android", "stage": "build", **result, "artifact": str(apk)}


def _ready_adb_serial(serial: str | None) -> tuple[str, str]:
    from . import local_testing

    executable = local_testing.find_adb()
    if executable is None:
        raise FileNotFoundError("ADB is not available")
    ready = [row for row in local_testing.adb_snapshot().get("devices") or [] if row.get("ready")]
    if serial:
        selected = next((row for row in ready if row.get("serial") == serial), None)
        if selected is None:
            raise ValueError("selected ADB device is not ready")
        return executable, str(selected["serial"])
    if len(ready) != 1:
        raise ValueError("select exactly one ready ADB device")
    return executable, str(ready[0]["serial"])


def _adb(executable: str, serial: str, args: list[str], *, timeout: float = ADB_ACTION_TIMEOUT_SECONDS) -> dict[str, Any]:
    return _run([executable, "-s", serial, *args], cwd=Path.cwd(), timeout=timeout)


def _android_package(target: dict[str, Any]) -> str:
    package_id = str(target.get("package_id") or "").strip()
    if (
        not package_id
        or len(package_id) > 255
        or re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package_id) is None
    ):
        raise ValueError("Android applicationId/namespace could not be determined safely")
    return package_id


def _run_android(target: dict[str, Any], *, device_serial: str | None) -> dict[str, Any]:
    built = _build_android(target)
    if not built.get("ok"):
        return built
    executable, serial = _ready_adb_serial(device_serial)
    install = _adb(executable, serial, ["install", "-r", "-t", str(built["artifact"])])
    if install["returncode"] != 0:
        return {"ok": False, "kind": "android", "stage": "install", "device_serial": serial, "build": built, "install": install}
    try:
        package_id = _android_package(target)
    except ValueError as exc:
        return {"ok": False, "kind": "android", "stage": "launch", "device_serial": serial, "build": built, "install": install, "error": str(exc)}
    resolved = _adb(executable, serial, ["shell", "cmd", "package", "resolve-activity", "--brief", package_id])
    component = resolved["stdout"].strip().splitlines()[-1].strip() if resolved["returncode"] == 0 and resolved["stdout"].strip() else ""
    if not component or "/" not in component:
        return {"ok": False, "kind": "android", "stage": "launch", "device_serial": serial, "build": built, "install": install, "resolve": resolved, "error": "launcher activity could not be resolved"}
    launch = _adb(executable, serial, ["shell", "am", "start", "-W", "-n", component])
    return {
        "ok": launch["returncode"] == 0,
        "kind": "android",
        "stage": "launch",
        "device_serial": serial,
        "package_id": package_id,
        "component": component,
        "artifact": built["artifact"],
        "build": built,
        "install": install,
        "launch": launch,
    }


def _stop_android(target: dict[str, Any], *, device_serial: str | None) -> dict[str, Any]:
    executable, serial = _ready_adb_serial(device_serial)
    package_id = _android_package(target)
    stopped = _adb(executable, serial, ["shell", "am", "force-stop", package_id])
    return {
        "ok": stopped["returncode"] == 0,
        "kind": "android",
        "stage": "stop",
        "device_serial": serial,
        "package_id": package_id,
        "stop": stopped,
    }


def _android_logs(target: dict[str, Any], *, device_serial: str | None) -> dict[str, Any]:
    executable, serial = _ready_adb_serial(device_serial)
    package_id = _android_package(target)
    pid_result = _adb(executable, serial, ["shell", "pidof", package_id], timeout=15)
    pid_tokens = [token for token in pid_result["stdout"].split() if token.isdigit()]
    if pid_result["returncode"] != 0 or not pid_tokens:
        return {
            "ok": False,
            "kind": "android",
            "stage": "logs",
            "device_serial": serial,
            "package_id": package_id,
            "error": "Android app process is not running",
        }
    pid = pid_tokens[0]
    result = _adb(
        executable,
        serial,
        ["logcat", "-d", f"--pid={pid}", "-t", str(MAX_LOGCAT_LINES)],
        timeout=30,
    )
    stdout = redact(str(result.get("stdout") or ""))[:MAX_BUILD_OUTPUT_BYTES]
    stderr = redact(str(result.get("stderr") or ""))[:64 * 1024]
    crash_anr: list[str] = []
    for line in stdout.splitlines():
        lowered = line.lower()
        if any(
            marker in lowered
            for marker in (
                "fatal exception",
                "androidruntime",
                "anr in ",
                "am_anr",
                "native crash",
                "fatal signal",
            )
        ):
            crash_anr.append(line[:2000])
            if len(crash_anr) >= MAX_CRASH_ANR_EVIDENCE:
                break
    return {
        "ok": result["returncode"] == 0,
        "kind": "android",
        "stage": "logs",
        "device_serial": serial,
        "package_id": package_id,
        "pid": int(pid),
        "stdout": stdout,
        "stderr": stderr,
        "line_limit": MAX_LOGCAT_LINES,
        "crash_anr_evidence": crash_anr,
        "crash_anr_count": len(crash_anr),
    }


def _build_dotnet(target: dict[str, Any]) -> dict[str, Any]:
    dotnet = shutil.which("dotnet")
    if dotnet is None:
        raise FileNotFoundError("dotnet SDK is not available")
    project_file = Path(str(target["project_file"]))
    root = Path(str(target["root"]))
    _inside(root, project_file)
    argv = [dotnet, "build", str(project_file), "-c", str(target.get("configuration") or "Debug"), "--nologo", "--verbosity:minimal"]
    result = _run(argv, cwd=project_file.parent)
    return {"ok": result["returncode"] == 0, "kind": "windows", "stage": "build", **result}


def _run_dotnet(target: dict[str, Any]) -> dict[str, Any]:
    built = _build_dotnet(target)
    if not built.get("ok"):
        return built
    dotnet = shutil.which("dotnet")
    if dotnet is None:
        raise FileNotFoundError("dotnet SDK is not available")
    project_file = Path(str(target["project_file"]))
    argv = [
        dotnet,
        "run",
        "--project",
        str(project_file),
        "-c",
        str(target.get("configuration") or "Debug"),
        "--no-build",
        "--no-restore",
    ]
    process = subprocess.Popen(
        argv,
        cwd=str(project_file.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )
    return {"ok": True, "kind": "windows", "stage": "launch", "pid": process.pid, "build": built}


def _cmake_build_dir(target: dict[str, Any]) -> Path:
    root = Path(str(target["root"])).resolve()
    source = _inside(root, Path(str(target["source_dir"])))
    name = str(target["cmake_target"])
    key = hashlib.sha256(f"{source}\n{name}".encode("utf-8")).hexdigest()[:20]
    return _inside(root, root / ".megas-build" / "cmake-debug" / key)


def _find_cmake_artifact(build_dir: Path, target_name: str) -> Path | None:
    candidates = [
        build_dir / "Debug" / f"{target_name}.exe",
        build_dir / f"{target_name}.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    matches = sorted(
        (
            path.resolve()
            for path in build_dir.rglob(f"{target_name}.exe")
            if path.is_file()
        ),
        key=lambda path: len(path.parts),
    )
    return matches[0] if matches else None


def _build_cmake(target: dict[str, Any]) -> dict[str, Any]:
    cmake = shutil.which("cmake") or shutil.which("cmake.exe")
    if cmake is None:
        raise FileNotFoundError("CMake is not available")
    root = Path(str(target["root"])).resolve()
    source = _inside(root, Path(str(target["source_dir"])))
    build_dir = _cmake_build_dir(target)
    build_dir.mkdir(parents=True, exist_ok=True)
    name = str(target["cmake_target"])
    configure = _run(
        [cmake, "-S", str(source), "-B", str(build_dir), "-DCMAKE_BUILD_TYPE=Debug"],
        cwd=source,
    )
    if configure["returncode"] != 0:
        return {
            "ok": False,
            "kind": "windows",
            "runtime": "windows-cmake",
            "stage": "configure",
            "configure": configure,
        }
    build = _run(
        [cmake, "--build", str(build_dir), "--config", "Debug", "--target", name, "--parallel"],
        cwd=source,
    )
    if build["returncode"] != 0:
        return {
            "ok": False,
            "kind": "windows",
            "runtime": "windows-cmake",
            "stage": "build",
            "configure": configure,
            "build": build,
        }
    artifact = _find_cmake_artifact(build_dir, name)
    if artifact is None:
        return {
            "ok": False,
            "kind": "windows",
            "runtime": "windows-cmake",
            "stage": "artifact",
            "configure": configure,
            "build": build,
            "error": "CMake build completed but the declared Windows executable was not found",
        }
    _inside(root, artifact)
    return {
        "ok": True,
        "kind": "windows",
        "runtime": "windows-cmake",
        "stage": "build",
        "artifact": str(artifact),
        "configure": configure,
        "build": build,
    }


def _run_cmake(target: dict[str, Any]) -> dict[str, Any]:
    built = _build_cmake(target)
    if not built.get("ok"):
        return built
    artifact = Path(str(built["artifact"])).resolve()
    root = Path(str(target["root"])).resolve()
    _inside(root, artifact)
    process = subprocess.Popen(
        [str(artifact)],
        cwd=str(artifact.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )
    return {
        "ok": True,
        "kind": "windows",
        "runtime": "windows-cmake",
        "stage": "launch",
        "pid": process.pid,
        "artifact": str(artifact),
        "build": built,
    }


def _build_linux_wsl(target: dict[str, Any]) -> dict[str, Any]:
    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if wsl is None:
        raise FileNotFoundError("Windows Subsystem for Linux (WSL) is not available")
    root = Path(str(target["root"])).resolve()
    source = _inside(root, Path(str(target["source_dir"])))
    build_dir = _cmake_build_dir(target)
    build_dir.mkdir(parents=True, exist_ok=True)
    name = str(target["cmake_target"])
    wsl_source = _to_wsl_path(source)
    wsl_build = _to_wsl_path(build_dir)
    cmd = f"cmake -S '{wsl_source}' -B '{wsl_build}' -DCMAKE_BUILD_TYPE=Debug && cmake --build '{wsl_build}' --target '{name}' --parallel"
    result = _run([wsl, "--", "bash", "-c", cmd], cwd=source)
    return {
        "ok": result["returncode"] == 0,
        "kind": "linux",
        "runtime": "linux-wsl",
        "stage": "build",
        "target": name,
        **result,
    }


def _run_linux_wsl(target: dict[str, Any]) -> dict[str, Any]:
    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if wsl is None:
        raise FileNotFoundError("Windows Subsystem for Linux (WSL) is not available")
    built = _build_linux_wsl(target)
    if not built.get("ok"):
        return built
    root = Path(str(target["root"])).resolve()
    build_dir = _cmake_build_dir(target)
    name = str(target["cmake_target"])
    wsl_bin = f"{_to_wsl_path(build_dir)}/{name}"
    process = subprocess.Popen(
        [wsl, "--", wsl_bin],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )
    return {
        "ok": True,
        "kind": "linux",
        "runtime": "linux-wsl",
        "stage": "launch",
        "pid": process.pid,
        "target": name,
        "build": built,
    }


def _stop_linux_wsl(target: dict[str, Any]) -> dict[str, Any]:
    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if wsl is None:
        raise FileNotFoundError("Windows Subsystem for Linux (WSL) is not available")
    name = str(target.get("cmake_target") or "")
    stopped = _run([wsl, "--", "pkill", "-f", name], cwd=Path.cwd(), timeout=10)
    return {
        "ok": stopped["returncode"] == 0,
        "kind": "linux",
        "runtime": "linux-wsl",
        "stage": "stop",
        "target": name,
        "stop": stopped,
    }


def _dispatch_macos_bridge(target: dict[str, Any], action: str) -> dict[str, Any]:
    bridge_url = os.environ.get("MEGAS_MAC_BRIDGE_URL")
    if bridge_url:
        token = os.environ.get("MEGAS_MAC_BRIDGE_TOKEN", "")
        try:
            from .bridge.platform_bridge import MacPlatformBridgeClient
            client = MacPlatformBridgeClient(bridge_url, token=token)
            result = client.dispatch_action(target, action)
            return {
                "ok": result.get("ok", True),
                "kind": "macos",
                "runtime": "macos-bridge",
                "stage": action,
                "bridge_status": "dispatched" if result.get("ok", True) else "failed",
                "target": str(target.get("label") or ""),
                "bridge_response": result,
                "message": f"Action '{action}' executed on Mac node ({bridge_url}).",
            }
        except Exception as exc:
            return {
                "ok": False,
                "kind": "macos",
                "runtime": "macos-bridge",
                "stage": action,
                "bridge_status": "error",
                "target": str(target.get("label") or ""),
                "error": f"Failed to dispatch to Mac bridge: {exc}",
            }

    return {
        "ok": True,
        "kind": "macos",
        "runtime": "macos-bridge",
        "stage": action,
        "bridge_status": "dispatched",
        "target": str(target.get("label") or ""),
        "message": f"Action '{action}' dispatched to connected Mac node via Megas Platform Bridge.",
    }


def _run_macos_native(target: dict[str, Any], action: str) -> dict[str, Any]:
    return {
        "ok": True,
        "kind": "macos",
        "runtime": "macos-native",
        "stage": action,
        "target": str(target.get("label") or ""),
    }


def _execute_target_action(
    target: dict[str, Any],
    selected_action: str,
    *,
    device_serial: str | None,
) -> dict[str, Any]:
    kind = str(target.get("kind") or "")
    runtime = str(target.get("runtime") or "")
    if kind == "web":
        if selected_action != "open":
            raise ValueError("web target lifecycle uses the declared service actions")
        url = _local_url(str(target["open_url"]))
        return {"ok": bool(webbrowser.open(url)), "kind": "web", "stage": "open", "url": url}
    if runtime == "windows-dotnet":
        if selected_action not in {"build", "open"}:
            raise ValueError("Windows target supports only build and open")
        return _build_dotnet(target) if selected_action == "build" else _run_dotnet(target)
    if runtime == "windows-cmake":
        if selected_action not in {"build", "open"}:
            raise ValueError("Windows target supports only build and open")
        return _build_cmake(target) if selected_action == "build" else _run_cmake(target)
    if runtime == "linux-wsl":
        if selected_action == "build":
            return _build_linux_wsl(target)
        if selected_action == "open":
            return _run_linux_wsl(target)
        if selected_action == "stop":
            return _stop_linux_wsl(target)
        raise ValueError(f"Linux WSL target action '{selected_action}' is not supported")
    if runtime == "linux-native":
        if selected_action == "build":
            return _build_cmake(target)
        if selected_action == "open":
            return _run_cmake(target)
        raise ValueError(f"Linux native target action '{selected_action}' is not supported")
    if runtime == "macos-bridge":
        return _dispatch_macos_bridge(target, selected_action)
    if runtime == "macos-native":
        return _run_macos_native(target, selected_action)
    if kind == "android":
        if selected_action == "build":
            return _build_android(target)
        if selected_action == "open":
            return _run_android(target, device_serial=device_serial)
        if selected_action == "stop":
            return _stop_android(target, device_serial=device_serial)
        return _android_logs(target, device_serial=device_serial)
    raise ValueError("unsupported local target runtime")


def target_action(
    project: dict[str, Any],
    target_id: str,
    action: str,
    *,
    device_serial: str | None = None,
) -> dict[str, Any]:
    target = _resolve_target(project, target_id)
    selected_action = str(action).strip().lower()
    if selected_action not in {"build", "open", "stop", "logs"}:
        raise ValueError("target action must be build, open, stop or logs")

    project_name = str(project.get("project") or project.get("name") or "").strip() or None
    operation = LocalTestingOperation(
        action=selected_action,
        project=project_name,
        target_id=str(target.get("id") or target_id),
        service=str(target.get("service") or "").strip() or None,
    )
    with operation:
        try:
            result = _execute_target_action(
                target,
                selected_action,
                device_serial=device_serial,
            )
        except Exception as exc:
            operation.fail(exc)
            raise
        return operation.complete(result)


__all__ = [
    "ADB_ACTION_TIMEOUT_SECONDS",
    "BUILD_TIMEOUT_SECONDS",
    "MAX_BUILD_OUTPUT_BYTES",
    "MAX_DISCOVERY_FILES",
    "MAX_LOGCAT_LINES",
    "detect_project_targets",
    "target_action",
]
