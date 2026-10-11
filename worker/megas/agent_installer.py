from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile


MAX_RELEASE_METADATA_BYTES = 2 * 1024 * 1024
MAX_AGENT_ASSET_BYTES = 512 * 1024 * 1024
MAX_ZIP_MEMBERS = 128
MAX_EXTRACTED_BYTES = 768 * 1024 * 1024
USER_AGENT = "MegasMoves-AgentInstaller/1"


# Downloadable agents are an owner-reviewed catalog. The desktop IPC never
# accepts an arbitrary repository, asset URL, command, or installer argv.
AGENT_CATALOG: dict[str, dict[str, Any]] = {
    "codex": {
        "display_name": "Codex",
        "repository": "openai/codex",
        "release_api": "https://api.github.com/repos/openai/codex/releases/latest",
        "asset_name": "codex-x86_64-pc-windows-msvc.exe.zip",
        "primary_member": "codex-x86_64-pc-windows-msvc.exe",
        "installed_name": "codex.exe",
        # These helpers are part of the reviewed Windows release shape and may
        # be required by sandbox/code-mode paths. Only these fixed names are
        # copied from the ZIP. Unknown release members are ignored.
        "bundle_helpers": {
            "codex-command-runner.exe": "codex-command-runner.exe",
            "codex-windows-sandbox-setup.exe": "codex-windows-sandbox-setup.exe",
        },
        # OpenAI currently publishes code-mode-host as a separate release
        # asset. It is optional for compatibility with releases that can use
        # the in-process fallback, but when present Megas verifies and installs
        # it next to codex.exe under the canonical helper name.
        "optional_assets": {
            "codex-code-mode-host-x86_64-pc-windows-msvc.exe": "codex-code-mode-host.exe",
        },
        "homepage": "https://github.com/openai/codex",
        "platform": "windows-x64",
    },
}

_ALLOWED_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        parsed = urlparse(str(newurl))
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _ALLOWED_DOWNLOAD_HOSTS:
            raise PermissionError("agent download redirect left the approved GitHub release boundary")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _megas_home() -> Path:
    configured = os.environ.get("MEGAS_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".megas"


def _agent_root(client: str) -> Path:
    if client not in AGENT_CATALOG:
        raise ValueError("unsupported managed agent")
    return _megas_home() / "agents" / client


def _manifest_path(client: str) -> Path:
    return _agent_root(client) / "installed.json"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
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


def _read_response_bounded(response: Any, limit: int) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            declared_size = int(declared)
        except ValueError:
            declared_size = 0
        if declared_size > limit:
            raise ValueError("agent download exceeds the configured size limit")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(1024 * 1024, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValueError("agent download exceeds the configured size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _json_get(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "api.github.com":
        raise PermissionError("agent release metadata must come from api.github.com over HTTPS")
    request = Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
        method="GET",
    )
    opener = build_opener(_SafeRedirectHandler())
    try:
        with opener.open(request, timeout=20) as response:
            raw = _read_response_bounded(response, MAX_RELEASE_METADATA_BYTES)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError("unable to read approved agent release metadata") from exc
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("agent release metadata must be a JSON object")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _release_asset(assets: list[Any], name: str, *, required: bool) -> dict[str, Any] | None:
    selected = next(
        (row for row in assets if isinstance(row, dict) and str(row.get("name") or "") == name),
        None,
    )
    if selected is None:
        if required:
            raise ValueError(f"approved release asset is unavailable: {name}")
        return None
    digest = str(selected.get("digest") or "")
    if not digest.startswith("sha256:") or len(digest.removeprefix("sha256:")) != 64:
        raise ValueError(f"GitHub release asset is missing a SHA-256 digest: {name}")
    size = int(selected.get("size") or 0)
    if size <= 0 or size > MAX_AGENT_ASSET_BYTES:
        raise ValueError(f"GitHub release asset size is outside the allowed range: {name}")
    url = str(selected.get("browser_download_url") or "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _ALLOWED_DOWNLOAD_HOSTS:
        raise PermissionError("agent release asset URL is outside the approved GitHub boundary")
    return selected


def _download_asset(asset: dict[str, Any], destination: Path) -> str:
    expected_sha256 = str(asset["digest"]).removeprefix("sha256:").lower()
    expected_size = int(asset["size"])
    url = str(asset["browser_download_url"])
    request = Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    opener = build_opener(_SafeRedirectHandler())
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    try:
        with opener.open(request, timeout=90) as response, destination.open("wb") as out:
            final = urlparse(str(response.geturl()))
            if final.scheme != "https" or (final.hostname or "").lower() not in _ALLOWED_DOWNLOAD_HOSTS:
                raise PermissionError("agent download resolved outside the approved GitHub release boundary")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_AGENT_ASSET_BYTES or total > expected_size + 1024:
                    raise ValueError("agent download exceeded the declared release asset size")
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except (HTTPError, URLError, TimeoutError) as exc:
        destination.unlink(missing_ok=True)
        raise RuntimeError("approved agent release download failed") from exc
    if total != expected_size:
        destination.unlink(missing_ok=True)
        raise ValueError("agent release asset size did not match GitHub metadata")
    actual = digest.hexdigest().lower()
    if actual != expected_sha256:
        destination.unlink(missing_ok=True)
        raise ValueError("agent release asset SHA-256 did not match GitHub metadata")
    return actual


def _verify_amd64_pe(path: Path) -> None:
    with path.open("rb") as handle:
        prefix = handle.read(0x40)
        if len(prefix) < 0x40 or prefix[:2] != b"MZ":
            raise ValueError("managed agent executable is not a Windows PE file")
        pe_offset = int.from_bytes(prefix[0x3C:0x40], "little")
        if pe_offset <= 0 or pe_offset > 32 * 1024 * 1024:
            raise ValueError("managed agent executable has an invalid PE offset")
        handle.seek(pe_offset)
        pe = handle.read(32)
    if len(pe) < 26 or pe[:4] != b"PE\0\0":
        raise ValueError("managed agent executable has an invalid PE signature")
    machine = int.from_bytes(pe[4:6], "little")
    optional_magic = int.from_bytes(pe[24:26], "little")
    if machine != 0x8664 or optional_magic != 0x20B:
        raise ValueError("managed agent executable must be AMD64 PE32+")


def _safe_zip_member_name(member: zipfile.ZipInfo) -> str:
    name = member.filename.replace("\\", "/")
    if name.startswith("/") or name.startswith("../") or "/../" in f"/{name}":
        raise ValueError("agent release archive contains an unsafe path")
    return name.rsplit("/", 1)[-1]


def _extract_reviewed_bundle(archive: Path, catalog: dict[str, Any], destination: Path) -> dict[str, str]:
    expected: dict[str, str] = {
        str(catalog["primary_member"]): str(catalog["installed_name"]),
        **{str(k): str(v) for k, v in dict(catalog.get("bundle_helpers") or {}).items()},
    }
    found: dict[str, zipfile.ZipInfo] = {}
    with zipfile.ZipFile(archive, "r") as bundle:
        members = bundle.infolist()
        if len(members) > MAX_ZIP_MEMBERS:
            raise ValueError("agent release archive contains too many files")
        total = sum(max(0, int(member.file_size)) for member in members)
        if total > MAX_EXTRACTED_BYTES:
            raise ValueError("agent release archive expands beyond the safety limit")
        for member in members:
            base = _safe_zip_member_name(member)
            if not member.is_dir() and base in expected:
                if base in found:
                    raise ValueError(f"agent release archive contains duplicate reviewed member: {base}")
                found[base] = member
        primary = str(catalog["primary_member"])
        if primary not in found:
            raise ValueError("agent release archive did not contain the expected Windows x64 executable")
        destination.mkdir(parents=True, exist_ok=True)
        hashes: dict[str, str] = {}
        for source_name, install_name in expected.items():
            member = found.get(source_name)
            if member is None:
                continue
            target = destination / install_name
            with bundle.open(member, "r") as source, target.open("wb") as out:
                shutil.copyfileobj(source, out, length=1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            _verify_amd64_pe(target)
            hashes[install_name] = _sha256_file(target)
    return hashes


def catalog_status() -> list[dict[str, Any]]:
    return [managed_status(client) for client in sorted(AGENT_CATALOG)]


def managed_executable(client: str) -> Path | None:
    status = managed_status(client)
    path = str(status.get("executable") or "")
    if not path:
        return None
    candidate = Path(path)
    return candidate if candidate.is_file() else None


def managed_status(client: str) -> dict[str, Any]:
    if client not in AGENT_CATALOG:
        raise ValueError("unsupported managed agent")
    catalog = AGENT_CATALOG[client]
    manifest = _manifest_path(client)
    installed: dict[str, Any] | None = None
    if manifest.is_file():
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                installed = raw
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            installed = None
    executable: Path | None = None
    verified = False
    helpers_verified = True
    if installed:
        raw_path = str(installed.get("executable") or "")
        if raw_path:
            candidate = Path(raw_path)
            try:
                candidate.resolve().relative_to(_agent_root(client).resolve())
                if candidate.is_file():
                    executable = candidate
                    expected = str(installed.get("executable_sha256") or "")
                    verified = bool(expected) and _sha256_file(candidate) == expected
                    helper_hashes = installed.get("helper_sha256") or {}
                    if not isinstance(helper_hashes, dict):
                        helpers_verified = False
                    else:
                        for name, digest in helper_hashes.items():
                            helper = candidate.parent / str(name)
                            if not helper.is_file() or _sha256_file(helper) != str(digest):
                                helpers_verified = False
                                break
            except (OSError, ValueError):
                executable = None
    installed_ok = executable is not None and verified and helpers_verified
    return {
        "client": client,
        "display_name": catalog["display_name"],
        "repository": catalog["repository"],
        "homepage": catalog["homepage"],
        "platform": catalog["platform"],
        "managed": True,
        "installed": installed_ok,
        "verified": bool(installed_ok),
        "version": str(installed.get("version") or "") if installed else None,
        "release_tag": str(installed.get("release_tag") or "") if installed else None,
        "executable": str(executable) if installed_ok and executable else None,
        "source": "github-release",
    }


def install(client: str, *, confirm: bool) -> dict[str, Any]:
    if client not in AGENT_CATALOG:
        raise ValueError("unsupported managed agent")
    if not confirm:
        raise PermissionError("installing an agent requires explicit human confirmation")
    catalog = AGENT_CATALOG[client]
    release = _json_get(str(catalog["release_api"]))
    if release.get("draft") is True or release.get("prerelease") is True:
        raise ValueError("latest approved agent release must be stable")
    tag = str(release.get("tag_name") or "").strip()
    version = str(release.get("name") or tag).strip()
    if not tag:
        raise ValueError("agent release metadata is missing a tag")
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ValueError("agent release metadata is missing assets")
    primary_asset = _release_asset(assets, str(catalog["asset_name"]), required=True)
    assert primary_asset is not None

    root = _agent_root(client)
    version_dir = root / "versions" / hashlib.sha256(tag.encode("utf-8")).hexdigest()[:20]
    staging_parent = root / ".staging"
    staging_parent.mkdir(parents=True, exist_ok=True)
    primary_asset_sha = str(primary_asset["digest"]).removeprefix("sha256:").lower()
    helper_hashes: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="megas-agent-download-", dir=str(staging_parent)) as directory:
        stage = Path(directory)
        archive = stage / str(catalog["asset_name"])
        _download_asset(primary_asset, archive)
        extracted = stage / "bundle"
        helper_hashes.update(_extract_reviewed_bundle(archive, catalog, extracted))

        for asset_name, install_name in dict(catalog.get("optional_assets") or {}).items():
            asset = _release_asset(assets, str(asset_name), required=False)
            if asset is None:
                continue
            target = extracted / str(install_name)
            _download_asset(asset, target)
            _verify_amd64_pe(target)
            helper_hashes[str(install_name)] = _sha256_file(target)

        installed_name = str(catalog["installed_name"])
        executable_sha256 = helper_hashes.pop(installed_name)
        new_dir = root / f".version-{hashlib.sha256((tag + executable_sha256).encode()).hexdigest()[:20]}.new"
        if new_dir.exists():
            shutil.rmtree(new_dir)
        shutil.copytree(extracted, new_dir)
        if version_dir.exists():
            shutil.rmtree(version_dir)
        version_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(new_dir, version_dir)

    final_executable = version_dir / str(catalog["installed_name"])
    record = {
        "format_version": 2,
        "client": client,
        "repository": catalog["repository"],
        "release_tag": tag,
        "version": version,
        "asset": catalog["asset_name"],
        "asset_sha256": primary_asset_sha,
        "executable": str(final_executable),
        "executable_sha256": executable_sha256,
        "helper_sha256": helper_hashes,
        "verified_source": "github-release-digest",
        "platform": catalog["platform"],
    }
    _atomic_json(_manifest_path(client), record)
    return managed_status(client)


def uninstall(client: str, *, confirm: bool) -> dict[str, Any]:
    if client not in AGENT_CATALOG:
        raise ValueError("unsupported managed agent")
    if not confirm:
        raise PermissionError("uninstalling an agent requires explicit human confirmation")
    root = _agent_root(client)
    existed = root.exists()
    if existed:
        shutil.rmtree(root)
    return {"client": client, "removed": existed, "installed": False}


__all__ = [
    "AGENT_CATALOG",
    "catalog_status",
    "install",
    "managed_executable",
    "managed_status",
    "uninstall",
]
