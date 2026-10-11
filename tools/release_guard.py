"""Conservative no-secret release gate for MegasMoves Community.

Never prints matched data. A finding blocks publication pending human inspection.
Scan active files AND Git history; removing a secret from HEAD is insufficient.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_SUFFIXES = {
    ".py", ".md", ".txt", ".html", ".css", ".yml", ".yaml", ".toml",
    ".json", ".js", ".ts", ".tsx", ".xml", ".sh", ".ps1", ".cs", ".kt",
}
ALLOWED_BASENAMES = {"LICENSE", ".gitignore"}
MAX_TEXT_BYTES = 2_000_000


def _pattern_rules() -> dict[str, re.Pattern[str]]:
    return {
        "private_key": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
        "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{25,}|github_pat_[A-Za-z0-9_]{20,})\b"),
        "cloud_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
        "cloud_api_credential": re.compile(r"\b(?:sk_(?:live|test)_[A-Za-z0-9]{20,}|sk-proj-[A-Za-z0-9_-]{28,}|AIza[0-9A-Za-z_-]{30,}|xox[baprs]-[A-Za-z0-9-]{20,})\b"),
        "tunnel_hostname": re.compile(r"\b[\w-]+\.(?:trycloudflare\.com|ngrok(?:-free)?\.(?:io|app)|loca\.lt|localtunnel\.me|tailscale\.net|cfargotunnel\.com|serveo\.net|duckdns\.org)\b", re.I),
        "personal_host_path": re.compile(r"(?:[A-Za-z]:\\Users\\[^\\\s\"']+\\|/(?:Users|home)/[A-Za-z0-9_.-]+/)"),
        "private_repository": re.compile(re.escape("abhinav" + "musrif/megasmoves"), re.I),
        "database_connection": re.compile(r"\b(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|amqp)://[^\s\"'<>]{8,}", re.I),
        "jwt_credential": re.compile(r"\beyJ[A-Za-z0-9_-]{18,}\.eyJ[A-Za-z0-9_-]{18,}\.[A-Za-z0-9_-]{12,}\b"),
        "user_email": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
        "tunnel_token": re.compile(r"\b(?:TUNNEL_TOKEN|CLOUDFLARE_TOKEN|NGROK_AUTHTOKEN|TAILSCALE_AUTHKEY)\s*[:=]\s*[\"'][A-Za-z0-9_=-]{10,}[\"']", re.I),
        "private_ip": re.compile(r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"),
        "url_embedded_credentials": re.compile(r"https?://[^\s/'\"@:]+:[^\s/'\"@]+@[^\s/'\"@]+", re.I),
        "phone_number": re.compile(r"(?<!\d)(?:\+91[- ]?)?[6-9]\d{9}(?!\d)"),
        "tunnel_id_assignment": re.compile(r"\b(?:tunnel[_-]?id|account[_-]?id|cloudflare[_-]?tunnel[_-]?id)\s*[:=]\s*['\"][0-9a-f-]{30,}['\"]", re.I),

    }


RULES = _pattern_rules()
URL_PATTERN = re.compile(r"https?://(?:[^\s/\"'@]+@)?([a-z0-9.-]+)", re.I)
PUBLIC_ENDPOINTS = frozenset({
    "github.com", "www.github.com", "megasmoves.io", "www.megasmoves.io",
    "t.me", "pypi.org", "www.python.org", "docs.python.org",
    "localhost", "127.0.0.1", "example.com", "example.org",
})

EXEMPT_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "invalid", "localhost", "github.com", "users.noreply.github.com", "noreply.github.com"}


def _run_git(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True
    ).stdout


def _inspect(name: str, data: bytes) -> list[str]:
    if len(data) > MAX_TEXT_BYTES or b"\0" in data[:8192]:
        return []
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return []
    labels = []
    for label, rule in RULES.items():
        matches = list(rule.finditer(content))
        if label == "user_email":
            matches = [m for m in matches if m.group().rsplit("@", 1)[-1].lower() not in EXEMPT_EMAIL_DOMAINS]
        if matches:
            labels.append(label)
    for hostname in URL_PATTERN.findall(content):
        if hostname.casefold() not in PUBLIC_ENDPOINTS:
            labels.append("unapproved_network_endpoint")
            break
    return labels


def _is_text_path(name: str) -> bool:
    candidate = Path(name)
    return candidate.suffix.lower() in ALLOWED_SUFFIXES or candidate.name in ALLOWED_BASENAMES


def scan_tree() -> list[tuple[str, str]]:
    findings = []
    names = _run_git("ls-files", "-z").split(b"\0")
    for raw in names:
        if not raw:
            continue
        name = raw.decode("utf-8", "surrogateescape")
        if not _is_text_path(name):
            continue
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            continue
        for category in _inspect(name, path.read_bytes()):
            findings.append((name, category))
    return findings


def scan_history() -> list[tuple[str, str]]:
    findings = []
    seen = set()
    # Every reachable blob, including current and old branches fetched by CI.
    for line in _run_git("rev-list", "--objects", "--all").splitlines():
        sha, _, raw_name = line.partition(b" ")
        if not raw_name:
            continue
        name = raw_name.decode("utf-8", "replace")
        if not _is_text_path(name) or sha in seen:
            continue
        seen.add(sha)
        if _run_git("cat-file", "-t", sha.decode()).strip() != b"blob":
            continue
        size = int(_run_git("cat-file", "-s", sha.decode()).strip())
        if size > MAX_TEXT_BYTES:
            continue
        for category in _inspect(name, _run_git("cat-file", "blob", sha.decode())):
            findings.append((name, category))
    return findings


def scan_commit_metadata() -> list[tuple[str, str]]:
    """Check addresses embedded in git commits, not just repository files."""
    records = _run_git("log", "--all", "--format=%ae%x00%ce%x00")
    findings = []
    for entry in records.decode("utf-8", "replace").split("\0"):
        address = entry.strip().lower()
        if not address:
            continue
        if address == "noreply@github.com" or address.endswith("@users.noreply.github.com") or address.endswith("@noreply.github.com"):
            continue
        findings.append(("[git commit metadata]", "non_public_email"))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=("tree", "history", "both"), default="both")
    args = parser.parse_args(argv)
    try:
        findings = (
            (scan_tree() if args.scope in ("tree", "both") else [])
            + (scan_history() if args.scope in ("history", "both") else [])
            + (scan_commit_metadata() if args.scope in ("history", "both") else [])
        )
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print("Release audit could not complete. Fail closed: " + type(exc).__name__, file=sys.stderr)
        return 2
    found = sorted(set(findings))
    if found:
        print("BLOCKED: potential private material detected. Do not publish.")
        for name, category in found[:120]:
            print(f"  {name}: {category}")
        if len(found) > 120:
            print(f"  ... plus {len(found) - 120} additional file/category flags")
        return 1
    print(f"PASS: no flagged private material in {args.scope} scan ({len(RULES)} detector categories).")
    print("This is heuristic coverage, not proof of zero secrets. Manual review remains mandatory.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
