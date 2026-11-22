"""Open-source, local-only Sign in with ChatGPT client for MegasMoves.

This implementation is deliberately independent of the private desktop core.
It never reads browser cookies, Codex credentials or developer API keys.
OpenAI-issued scopes and account identity are mandatory for inference.
"""
from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid

import httpx

AUTH = "https://auth.openai.com"
API = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
MAX_TEXT = 24000
CLIENT_ID = re.compile(r"^oaiapp_[A-Za-z0-9_-]{6,200}$")
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
IDENTITY = "MegasMoves"


class ChatGPTConnectionError(RuntimeError):
    """Safe, non-credential-bearing OAuth and inference failure."""


def _root() -> Path:
    configured = os.getenv("MEGAS_SIWC_LOCAL_HOME", "")
    base = Path(configured).expanduser() if configured else (
        Path.home() / ".config" / "megasmoves" / "siwc")
    return base


def _safe_path(path: Path) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ChatGPTConnectionError("Unsafe ChatGPT credential storage path")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    _safe_path(path)
    if os.name == "posix":
        if path.parent.stat().st_uid != os.getuid():
            raise ChatGPTConnectionError("ChatGPT storage directory belongs to another user")
        path.parent.chmod(0o700)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(raw) > 65536:
        raise ChatGPTConnectionError("ChatGPT local record is too large")
    fd, temporary = tempfile.mkstemp(prefix=".siwc-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path: Path) -> dict[str, Any] | None:
    _safe_path(path)
    if not path.exists():
        return None
    meta = path.stat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_size > 65536:
        raise ChatGPTConnectionError("Invalid ChatGPT local record")
    if os.name == "posix" and (
            meta.st_uid != os.getuid() or meta.st_mode & 0o077):
        raise ChatGPTConnectionError("Unsafe ChatGPT record permissions")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError, OSError):
        raise ChatGPTConnectionError("ChatGPT local record is unreadable") from None
    if not isinstance(value, dict):
        raise ChatGPTConnectionError("ChatGPT local record is invalid")
    return value


def _b64_decode(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > 16000:
        raise ChatGPTConnectionError("Malformed OpenAI ID token")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def chrome_profile(profile_name: str) -> tuple[Path, str, str]:
    """Resolve one *existing* Chrome profile, never create or guess a profile."""
    if not isinstance(profile_name, str) or not 1 <= len(profile_name.strip()) <= 160:
        raise ChatGPTConnectionError("Choose the exact existing Chrome profile name")
    if sys.platform == "win32":
        root = Path(os.getenv("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
        browser = next((str(path) for path in (
            Path(os.getenv("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.getenv("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
            root.parent.parent / "Google/Chrome/Application/chrome.exe",
        ) if path.is_file()), "")
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support/Google/Chrome"
        browser = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    else:
        root = Path.home() / ".config" / "google-chrome"
        browser = (shutil.which("google-chrome-stable")
                   or shutil.which("google-chrome") or "")
    if not root.is_dir() or root.is_symlink():
        raise ChatGPTConnectionError("No installed local Google Chrome profile directory")
    metadata = root / "Local State"
    if not metadata.is_file() or metadata.is_symlink() or metadata.stat().st_size > 4_000_000:
        raise ChatGPTConnectionError("Chrome profile metadata is unavailable")
    try:
        local_state = json.loads(metadata.read_text(encoding="utf-8"))
        profiles = local_state.get("profile", {}).get("info_cache", {})
    except (ValueError, OSError, TypeError):
        raise ChatGPTConnectionError("Chrome profile metadata is invalid") from None
    if not isinstance(profiles, dict):
        raise ChatGPTConnectionError("Chrome profile inventory is invalid")
    matches = []
    for profile_id, data in profiles.items():
        if (not isinstance(profile_id, str)
                or not re.fullmatch(r"(Default|Profile [0-9]{1,6})", profile_id)
                or not isinstance(data, dict)):
            continue
        if (str(data.get("name", "")).casefold() == profile_name.strip().casefold()
                and (root / profile_id).is_dir()
                and not (root / profile_id).is_symlink()):
            matches.append(profile_id)
    if len(matches) != 1:
        raise ChatGPTConnectionError(
            "Requested existing Chrome profile is missing or ambiguous; no fallback")
    if not Path(browser).is_file() and not shutil.which(browser):
        raise ChatGPTConnectionError("Google Chrome executable is unavailable")
    return root, browser, matches[0]


def _launch_existing_profile(url: str, profile_name: str) -> str:
    root, browser, profile_id = chrome_profile(profile_name)
    # No cookies are accessed or exported. No --user-data-dir override, no
    # default-profile launch, no shell, no artificial auth browser.
    subprocess.Popen(
        [browser, f"--profile-directory={profile_id}", url],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True)
    return profile_id


class LocalRecords:
    """Local non-secret metadata; credentials in the user's OS-backed keyring."""
    SERVICE = "com.amretra.megasmoves.siwc"

    def __init__(self, home: Path | None = None, *, vault: Any = None):
        self.home = home or _root()
        self.host_file = self.home / "host.json"
        self.index_file = self.home / "accounts.json"
        self._vault = vault

    @property
    def vault(self) -> Any:
        if self._vault is not None:
            return self._vault
        try:
            import keyring
            backend = keyring.get_keyring()
            backend_type = (type(backend).__module__ + "." + type(backend).__name__).lower()
            os_backed = any(marker in backend_type for marker in (
                "keyring.backends.secretservice",
                "keyring.backends.kwallet",
                "keyring.backends.windows",
                "keyring.backends.macos",
            ))
            if getattr(backend, "priority", 0) > 0 and os_backed:
                return keyring
        except (ImportError, RuntimeError):
            pass
        if sys.platform.startswith("linux") and shutil.which("secret-tool"):
            return LinuxSecretToolVault()
        raise ChatGPTConnectionError(
            "No secure OS credential vault is available; install keyring "
            "or configure Linux Secret Service before ChatGPT sign-in")

    def host_id(self) -> str:
        known = _read_json(self.host_file)
        if known:
            value = known.get("ext_agent_host_id", "")
            try:
                if value.startswith("urn:uuid:"):
                    uuid.UUID(value.removeprefix("urn:uuid:"))
                    return value
            except (ValueError, AttributeError):
                pass
            raise ChatGPTConnectionError("Saved host identity is invalid")
        value = "urn:uuid:" + str(uuid.uuid4())
        _atomic_json(self.host_file, {"ext_agent_host_id": value})
        return value

    def _index(self) -> dict[str, Any]:
        data = _read_json(self.index_file) or {"accounts": [], "active": ""}
        if not isinstance(data.get("accounts"), list):
            raise ChatGPTConnectionError("Invalid ChatGPT account index")
        return data

    def summaries(self) -> list[dict[str, Any]]:
        data = self._index()
        return [{"email": a.get("email", ""), "client_id": a.get("client_id", ""),
                 "active": a.get("key") == data.get("active")}
                for a in data["accounts"] if isinstance(a, dict)]

    def selected(self, email: str | None = None) -> dict[str, Any] | None:
        index = self._index()
        if email:
            matches = [a for a in index["accounts"] if isinstance(a, dict)
                       and str(a.get("email", "")).casefold() == email.casefold()]
            if len(matches) > 1:
                raise ChatGPTConnectionError("Multiple registered ChatGPT workspaces; choose another account")
            return matches[0] if matches else None
        return next((a for a in index["accounts"] if isinstance(a, dict)
                     and a.get("key") == index.get("active")), None)

    def _vault_key(self, key: str) -> str:
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ChatGPTConnectionError("Invalid ChatGPT account reference")
        return key

    def get(self, entry: dict[str, Any]) -> dict[str, Any]:
        try:
            raw = self.vault.get_password(self.SERVICE, self._vault_key(entry["key"]))
        except Exception:
            raise ChatGPTConnectionError("Could not read the local OS credential vault") from None
        if not raw:
            raise ChatGPTConnectionError("ChatGPT credentials are not available")
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            raise ChatGPTConnectionError("Stored ChatGPT credential record is invalid") from None
        if not isinstance(value, dict):
            raise ChatGPTConnectionError("Stored ChatGPT credential record is invalid")
        return value

    def save(self, account: dict[str, Any]) -> None:
        client, subject = account.get("client_id"), account.get("subject")
        if not isinstance(client, str) or not CLIENT_ID.fullmatch(client):
            raise ChatGPTConnectionError("Invalid issued ChatGPT client ID")
        if not isinstance(subject, str) or not subject:
            raise ChatGPTConnectionError("Unverified ChatGPT subject")
        key = hashlib.sha256((client + ":" + subject).encode()).hexdigest()
        try:
            self.vault.set_password(
                self.SERVICE, self._vault_key(key),
                json.dumps(account, separators=(",", ":"), sort_keys=True))
        except Exception:
            raise ChatGPTConnectionError("Could not store ChatGPT tokens in OS credential vault") from None
        index = self._index()
        remaining = [a for a in index["accounts"] if a.get("key") != key]
        remaining.append({"key": key, "email": account.get("email", ""),
                          "subject": subject, "client_id": client})
        index["accounts"], index["active"] = remaining, key
        _atomic_json(self.index_file, index)

    def clear(self, entry: dict[str, Any]) -> None:
        try:
            self.vault.delete_password(self.SERVICE, self._vault_key(entry["key"]))
        except Exception:
            raise ChatGPTConnectionError(
                "Could not delete the local ChatGPT credential; account remains connected") from None
        index = self._index()
        index["accounts"] = [a for a in index["accounts"]
                             if a.get("key") != entry["key"]]
        index["active"] = index["accounts"][0]["key"] if index["accounts"] else ""
        _atomic_json(self.index_file, index)



def _verify_signed_identity(jwt: str, client: str, nonce: str,
                            http: httpx.Client) -> dict[str, str]:
    """Validate OpenAI RS256 signature and account-bound signed claims."""
    try:
        head, payload, sig = jwt.split(".")
        header = json.loads(_b64_decode(head))
        claims = json.loads(_b64_decode(payload))
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise ValueError("Untrusted JWT algorithm")
        metadata = http.get(AUTH + "/.well-known/openid-configuration")
        metadata.raise_for_status()
        meta = metadata.json()
        uri = meta.get("jwks_uri")
        if (meta.get("issuer") != AUTH or not isinstance(uri, str)
                or not uri.startswith(AUTH + "/")):
            raise ValueError("Unexpected OIDC issuer or JWKS host")
        response = http.get(uri)
        response.raise_for_status()
        jwk = next(k for k in response.json()["keys"]
                   if k.get("kid") == header["kid"] and k.get("kty") == "RSA"
                   and k.get("use", "sig") == "sig")
        from cryptography.hazmat.primitives.asymmetric import rsa, padding
        from cryptography.hazmat.primitives import hashes
        n = int.from_bytes(_b64_decode(jwk["n"]), "big")
        e = int.from_bytes(_b64_decode(jwk["e"]), "big")
        key = rsa.RSAPublicNumbers(e, n).public_key()
        key.verify(_b64_decode(sig), (head + "." + payload).encode(),
                   padding.PKCS1v15(), hashes.SHA256())
        now = time.time()
        audiences = claims.get("aud")
        audiences = [audiences] if isinstance(audiences, str) else audiences
        if (claims.get("iss") != AUTH or not isinstance(audiences, list)
                or client not in audiences or claims.get("nonce") != nonce
                or not isinstance(claims.get("sub"), str) or not claims["sub"]
                or not isinstance(claims.get("exp"), (int, float))
                or claims["exp"] <= now
                or (isinstance(claims.get("iat"), (int, float)) and claims["iat"] > now + 60)
                or (isinstance(claims.get("nbf"), (int, float)) and claims["nbf"] > now + 60)):
            raise ValueError("JWT claims failed validation")
        return {"issuer": AUTH, "subject": claims["sub"],
                "email": str(claims.get("email") or "")}
    except Exception:
        raise ChatGPTConnectionError("ChatGPT identity verification failed") from None


def _oauth_failure(response: httpx.Response, *, stage: str) -> ChatGPTConnectionError:
    code = ""
    try:
        body = response.json()
        err = body.get("error") if isinstance(body, dict) else ""
        code = err if isinstance(err, str) else (
            err.get("code", "") if isinstance(err, dict) else "")
    except (ValueError, TypeError):
        pass
    code = code if re.fullmatch(r"[a-z0-9_]{1,100}", code or "") else ""
    rid = response.headers.get("x-request-id", "")
    rid = rid if re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", rid) else ""
    details = ", ".join(filter(None, (str(response.status_code), code, rid)))
    return ChatGPTConnectionError(stage + " failed (" + details + "); credentials not exposed")


class LinuxSecretToolVault:
    """Linux Secret Service/libsecret credentials, never on argv or disk."""

    def __init__(self):
        if not sys.platform.startswith("linux") or not shutil.which("secret-tool"):
            raise ChatGPTConnectionError("Linux Secret Service is not available")

    @staticmethod
    def _args(service: str, key: str) -> list[str]:
        if (service != LocalRecords.SERVICE
                or not re.fullmatch(r"[a-f0-9]{64}", key)):
            raise ChatGPTConnectionError("Invalid Secret Service credential key")
        return ["service", service, "account", key]

    def get_password(self, service: str, key: str) -> str | None:
        result = subprocess.run(
            ["secret-tool", "lookup", *self._args(service, key)],
            capture_output=True, text=True, shell=False, timeout=12)
        if result.returncode not in (0, 1):
            raise ChatGPTConnectionError("Linux Secret Service lookup failed")
        return result.stdout.strip() or None if result.returncode == 0 else None

    def set_password(self, service: str, key: str, value: str) -> None:
        if not isinstance(value, str) or not 1 <= len(value) <= 65536:
            raise ChatGPTConnectionError("Invalid Secret Service credential size")
        result = subprocess.run(
            ["secret-tool", "store", "--label=MegasMoves ChatGPT session",
             *self._args(service, key)],
            input=value + "\n", capture_output=True, text=True,
            shell=False, timeout=12)
        if result.returncode != 0:
            raise ChatGPTConnectionError("Linux Secret Service store failed")

    def delete_password(self, service: str, key: str) -> None:
        result = subprocess.run(
            ["secret-tool", "clear", *self._args(service, key)],
            capture_output=True, text=True, shell=False, timeout=12)
        if result.returncode not in (0, 1):
            raise ChatGPTConnectionError("Linux Secret Service delete failed")


# The public client owns its own local host registration. No private Megas
# tokens, browser cookies or Codex credentials are read by this module.
from contextlib import contextmanager


@contextmanager
def _credential_lock(home: Path, timeout: float = 12.0):
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    _safe_path(home / ".siwc.lock")
    lockpath = home / ".siwc.lock"
    fd = os.open(lockpath, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    held = False
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or
                (os.name == "posix" and
                 (info.st_uid != os.getuid() or info.st_mode & 0o077))):
            raise ChatGPTConnectionError("Unsafe local OAuth lock")
        if os.name == "nt":
            import msvcrt
            if not info.st_size:
                os.write(fd, b"\0")
            def acquire():
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            def release():
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def acquire():
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release():
                fcntl.flock(fd, fcntl.LOCK_UN)
        deadline = time.monotonic() + timeout
        while True:
            try:
                acquire()
                held = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ChatGPTConnectionError("Another local ChatGPT session is busy") from None
                time.sleep(.04)
        yield
    finally:
        if held:
            release()
        os.close(fd)


class OSSRecords(LocalRecords):
    def issued_for(self, email: str) -> str:
        email = email.strip().casefold()
        pending = self._index().get("pending", [])
        for record in pending if isinstance(pending, list) else []:
            if isinstance(record, dict) and record.get("email") == email:
                value = record.get("client_id", "")
                if isinstance(value, str) and CLIENT_ID.fullmatch(value):
                    return value
        return ""

    def remember_issued(self, email: str, issued: str) -> None:
        if not isinstance(issued, str) or not CLIENT_ID.fullmatch(issued):
            raise ChatGPTConnectionError("Invalid returned dynamic client registration")
        email = email.strip().casefold()
        if not email or "@" not in email or len(email) > 254:
            raise ChatGPTConnectionError("Select an exact account email")
        index = self._index()
        existing = index.get("pending", [])
        if not isinstance(existing, list):
            raise ChatGPTConnectionError("Saved OAuth client registrations are invalid")
        remaining = [a for a in existing if isinstance(a, dict) and a.get("email") != email]
        remaining.append({"email": email, "client_id": issued})
        index["pending"] = remaining[:64]
        _atomic_json(self.index_file, index)

    def forget_pending(self, email: str) -> None:
        index = self._index()
        index["pending"] = [a for a in index.get("pending", [])
                            if isinstance(a, dict) and a.get("email") != email.casefold()]
        _atomic_json(self.index_file, index)
import hmac


class OSSChatGPTClient:
    """Standalone public-client OAuth and plan inference for a user-owned host."""

    def __init__(self, *, records: OSSRecords | None = None,
                 http: httpx.Client | None = None):
        self.records = records or OSSRecords()
        self.http = http or httpx.Client(timeout=20, follow_redirects=False)
        self._guard = threading.RLock()
        self._server = None
        self._phase = "idle"
        self._detail = ""

    def status(self, email: str | None = None) -> dict[str, Any]:
        with self._guard:
            entry = self.records.selected(email)
            record = self.records.get(entry) if entry else {}
            scopes = str(record.get("scope", "")).split()
            connected = bool(record.get("subject") and record.get("access_token"))
            granted = bool(connected and record.get("refresh_token")
                           and "chatgpt.tokens.use.direct" in scopes)
            return {"state": self._phase, "message": self._detail,
                    "connected": connected, "plan_enabled": granted,
                    "email": str(entry.get("email", "")) if entry else "",
                    "client_registered": bool(entry or (email and self.records.issued_for(email))),
                    "no_paid_fallback": True}

    def begin(self, *, chrome_profile_name: str, account_email: str,
              reconsent: bool = False) -> dict[str, str]:
        if not isinstance(account_email, str) or not re.fullmatch(
                r"[A-Za-z0-9.!#$%&'*+/=?^_\`{|}~-]+@[A-Za-z0-9.-]+",
                account_email.strip()) or len(account_email) > 254:
            raise ChatGPTConnectionError("Choose the exact ChatGPT account email")
        if type(reconsent) is not bool:
            raise ChatGPTConnectionError("Reconsent must be boolean")
        email = account_email.strip().casefold()
        # Fail before opening any account-consent screen if this host cannot
        # securely persist the eventual OAuth token set.
        _ = self.records.vault
        # This must succeed before any host ID or credential write.
        chrome_profile(chrome_profile_name)
        with self._guard:
            if self._server:
                raise ChatGPTConnectionError("ChatGPT authorization is already active")
            existing = self.records.selected(email)
            issued = existing.get("client_id", "") if existing else self.records.issued_for(email)
            if issued and not CLIENT_ID.fullmatch(issued):
                raise ChatGPTConnectionError("Invalid saved OAuth client registration")
            previous = self.records.get(existing) if existing else {}
            host_id = self.records.host_id()
            state = _b64_encode(os.urandom(32))
            nonce = _b64_encode(os.urandom(32))
            verifier = _b64_encode(os.urandom(48))
            challenge = _b64_encode(hashlib.sha256(verifier.encode()).digest())
            inbox: queue.Queue[dict[str, str]] = queue.Queue(maxsize=1)
            expected = state

            class Callback(BaseHTTPRequestHandler):
                def log_message(self, *_args: Any) -> None:
                    pass

                def do_GET(self) -> None:
                    parsed = urlsplit(self.path)
                    params = parse_qs(parsed.query)
                    exact = (parsed.path == "/auth/callback" and len(self.path) < 8192
                             and len(params.get("state", [])) == 1
                             and hmac.compare_digest(params["state"][0], expected)
                             and ((len(params.get("code", [])) == 1 and "error" not in params)
                                  or (len(params.get("error", [])) == 1 and "code" not in params)))
                    if not exact:
                        self.send_error(400, "Invalid OAuth callback")
                        return
                    values = {k: v[0] for k, v in params.items()
                              if k in ("code", "error", "state", "client_id", "scope")
                              and len(v) == 1}
                    try:
                        inbox.put_nowait(values)
                    except queue.Full:
                        self.send_error(409, "Callback already processed")
                        return
                    body = b"Authorization received. Return to MegasMoves."
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)


            server = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
            server.daemon_threads = True
            redirect = f"http://127.0.0.1:{server.server_address[1]}/auth/callback"
            params = {
                "client_id": issued or "dynamic_agent_client",
                "response_type": "code",
                "redirect_uri": redirect,
                "scope": SCOPES,
                "resource": API,
                "state": state,
                "nonce": nonce,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
                "ext_agent_host_id": host_id,
            }
            if not issued:
                params["agent_name_hint"] = IDENTITY
            elif reconsent:
                params["prompt"] = "consent"
            if previous and issued:
                if previous.get("id_token"):
                    params["id_token_hint"] = previous["id_token"]
                if previous.get("email"):
                    params["login_hint"] = previous["email"]
            url = AUTH + "/api/accounts/authorize?" + urlencode(params)
            self._server = server
            self._phase, self._detail = "waiting_for_browser", ""
            threading.Thread(target=server.serve_forever,
                             daemon=True, name="Megas-OSS-Loopback").start()
            try:
                profile_id = _launch_existing_profile(url, chrome_profile_name)
            except Exception:
                server.shutdown()
                server.server_close()
                self._server = None
                self._phase = "profile_not_ready"
                self._detail = "Could not open the specified existing Chrome profile"
                raise ChatGPTConnectionError(self._detail) from None
            threading.Thread(
                target=self._finish,
                args=(server, inbox, issued, previous, verifier, nonce, redirect, email, host_id),
                daemon=True, name="Megas-OSS-Exchange",
            ).start()
            return {"state": "waiting_for_browser", "profile_id": profile_id}

    def wait(self, *, timeout: float = 605.0) -> dict[str, Any]:
        deadline = time.monotonic() + max(.1, min(timeout, 610))
        while self._server is not None and time.monotonic() < deadline:
            time.sleep(.08)
        if self._server is not None:
            raise ChatGPTConnectionError("Sign-in remains active; inspect OpenAI browser")
        return self.status()

    def _finish(self, server: ThreadingHTTPServer, inbox: queue.Queue,
                previous_client: str, previous: dict[str, Any],
                verifier: str, nonce: str, redirect: str,
                expected_email: str, host_id: str) -> None:
        try:
            callback = inbox.get(timeout=600)
            with self._guard:
                self._phase = "exchanging"
            if callback.get("error"):
                with self._guard:
                    self._phase, self._detail = "declined", "ChatGPT authorization declined"
                return
            code = callback.get("code", "")
            if not isinstance(code, str) or not 1 <= len(code) <= 4096:
                raise ChatGPTConnectionError("Invalid OAuth authorization code")
            client = callback.get("client_id") or previous_client
            if not isinstance(client, str) or not CLIENT_ID.fullmatch(client):
                raise ChatGPTConnectionError("OpenAI did not return an issued client ID")
            if previous_client and client != previous_client:
                raise ChatGPTConnectionError("Selected registration client changed unexpectedly")
            with _credential_lock(self.records.home):
                if not previous_client:
                    self.records.remember_issued(expected_email, client)
            response = self.http.post(
                AUTH + "/api/accounts/oauth/token",
                data={"grant_type": "authorization_code", "client_id": client,
                      "code": code, "code_verifier": verifier,
                      "redirect_uri": redirect, "resource": API},
            )
            if response.status_code >= 400:
                raise _oauth_failure(response, stage="Token exchange")
            try:
                values = response.json()
            except ValueError:
                raise ChatGPTConnectionError("OAuth token exchange returned an invalid response") from None
            if not isinstance(values, dict) or not isinstance(values.get("id_token"), str):
                raise ChatGPTConnectionError("OpenAI did not return a signed identity")
            identity = _verify_signed_identity(values["id_token"], client, nonce, self.http)
            if identity["email"].strip().casefold() != expected_email:
                raise ChatGPTConnectionError("Authorized ChatGPT account does not match selection")
            if previous and previous.get("subject") != identity["subject"]:
                raise ChatGPTConnectionError("ChatGPT account identity changed")
            access, refresh = values.get("access_token"), values.get("refresh_token")
            scope, expiry = values.get("scope"), values.get("expires_in")
            if (str(values.get("token_type", "Bearer")).casefold() != "bearer"
                    or not isinstance(access, str) or not access
                    or not isinstance(refresh, str) or not refresh
                    or not isinstance(scope, str)
                    or not isinstance(expiry, (int, float)) or not 30 <= expiry <= 86400):
                raise ChatGPTConnectionError("Incomplete ChatGPT session authorization")
            record = {
                **identity, "client_id": client, "host_id": host_id,
                "id_token": values["id_token"], "access_token": access,
                "refresh_token": refresh, "scope": scope,
                "expires_at": time.time() + expiry,
            }
            with _credential_lock(self.records.home):
                self.records.save(record)
                self.records.forget_pending(expected_email)
            with self._guard:
                if "chatgpt.tokens.use.direct" in scope.split():
                    self._phase, self._detail = "connected", ""
                else:
                    self._phase = "identity_only"
                    self._detail = "ChatGPT plan use was not granted"

        except queue.Empty:
            with self._guard:
                self._phase = "provider_no_callback"
                self._detail = "OpenAI did not redirect to the local callback"
        except ChatGPTConnectionError as exc:
            with self._guard:
                self._phase = "failed"
                self._detail = str(exc)
        except httpx.HTTPError:
            with self._guard:
                self._phase = "failed"
                self._detail = "OAuth network or provider request failed"
        except Exception:
            with self._guard:
                self._phase = "failed"
                self._detail = "OAuth processing failed; no authorization confirmed"
        finally:
            server.shutdown()
            server.server_close()
            with self._guard:
                self._server = None

    def _access_token(self, email: str) -> str:
        """Refresh atomically per host, then read the newest token."""
        with _credential_lock(self.records.home):
            entry = self.records.selected(email)
            if not entry:
                raise ChatGPTConnectionError("Sign in with ChatGPT first")
            saved = self.records.get(entry)
            scopes = str(saved.get("scope", "")).split()
            if "chatgpt.tokens.use.direct" not in scopes:
                raise ChatGPTConnectionError("ChatGPT plan-use permission was not granted")
            client = saved.get("client_id", "")
            if not isinstance(client, str) or not CLIENT_ID.fullmatch(client):
                raise ChatGPTConnectionError("Saved ChatGPT client registration is invalid")
            if saved.get("expires_at", 0) > time.time() + 60 and saved.get("access_token"):
                return saved["access_token"]
            refresh = saved.get("refresh_token", "")
            if not isinstance(refresh, str) or not refresh:
                raise ChatGPTConnectionError("ChatGPT session requires sign-in again")
            try:
                response = self.http.post(
                    AUTH + "/api/accounts/oauth/token",
                    data={"grant_type": "refresh_token", "client_id": client,
                          "refresh_token": refresh, "resource": API},
                )
            except httpx.HTTPError:
                raise ChatGPTConnectionError("Could not renew ChatGPT authorization") from None
            if response.status_code >= 400:
                code = ""
                try:
                    obj = response.json()
                    code = obj.get("error", "") if isinstance(obj, dict) else ""
                except (ValueError, TypeError):
                    pass
                if code in (
                    "invalid_grant", "invalid_refresh_token", "token_expired",
                    "refresh_token_expired", "refresh_token_invalidated",
                    "refresh_token_reused",
                ):
                    saved.update(access_token="", refresh_token="",
                                 id_token="", scope="", expires_at=0)
                    self.records.save(saved)
                    raise ChatGPTConnectionError("ChatGPT authorization expired; sign in again")
                raise _oauth_failure(response, stage="Token refresh")
            try:
                values = response.json()
            except ValueError:
                raise ChatGPTConnectionError("ChatGPT refresh returned invalid JSON") from None
            if not isinstance(values, dict):
                raise ChatGPTConnectionError("ChatGPT refresh returned an invalid record")
            new_access, new_refresh = values.get("access_token"), values.get("refresh_token")
            scope, lifetime = values.get("scope", saved["scope"]), values.get("expires_in", 3600)
            if (not isinstance(new_access, str) or not new_access
                    or not isinstance(new_refresh, str) or not new_refresh
                    or not isinstance(scope, str)
                    or "chatgpt.tokens.use.direct" not in scope.split()
                    or not isinstance(lifetime, (int, float))
                    or not 30 <= lifetime <= 86400):
                raise ChatGPTConnectionError("ChatGPT refresh was incomplete; no tokens replaced")
            saved.update(access_token=new_access, refresh_token=new_refresh,
                         scope=scope, expires_at=time.time() + lifetime)
            self.records.save(saved)
            return new_access

    def models(self, *, email: str) -> list[dict[str, str]]:
        token = self._access_token(email)
        try:
            response = self.http.get(API + "/models",
                                     headers={"Authorization": "Bearer " + token})
        except httpx.HTTPError:
            raise ChatGPTConnectionError("ChatGPT model catalog unavailable") from None
        if response.status_code >= 400:
            raise _oauth_failure(response, stage="ChatGPT model access")
        try:
            payload = response.json()
        except ValueError:
            raise ChatGPTConnectionError("ChatGPT model catalog returned invalid JSON") from None
        items = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ChatGPTConnectionError("ChatGPT account model catalog is invalid")
        result = []
        for item in items:
            if not isinstance(item, dict) or item.get("visibility") != "list":
                continue
            slug = item.get("slug", "")
            if isinstance(slug, str) and MODEL.fullmatch(slug):
                result.append({"slug": slug,
                               "display_name": str(item.get("display_name") or slug)[:160]})
        return result[:100]


    def infer(self, *, email: str, model: str, prompt: str) -> dict[str, str]:
        """Plan-funded Responses stream; no external tools or paid API fallback."""
        if not isinstance(model, str) or not MODEL.fullmatch(model):
            raise ChatGPTConnectionError("Choose an account-supported model ID")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 16000:
            raise ChatGPTConnectionError("Enter an inference prompt under 16,000 characters")
        access = self._access_token(email)
        payload = {
            "model": model,
            "input": [{"role": "user", "content": prompt}],
            "store": False,
            "stream": True,
        }
        pieces: list[str] = []
        total = 0
        completed = False
        try:
            with self.http.stream("POST", API + "/responses",
                                  headers={"Authorization": "Bearer " + access},
                                  json=payload, timeout=90) as response:
                if response.status_code >= 400:
                    raise _oauth_failure(response, stage="ChatGPT inference")
                for line in response.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    message = line[6:]
                    if message == "[DONE]":
                        break
                    try:
                        event = json.loads(message)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    kind = event.get("type")
                    if kind == "response.output_text.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str):
                            total += len(delta)
                            if total > MAX_TEXT:
                                raise ChatGPTConnectionError("ChatGPT answer exceeded local size limit")
                            pieces.append(delta)
                    elif kind == "response.completed":
                        completed = True
                    elif kind in ("response.failed", "response.incomplete", "error"):
                        failure = event.get("error") or (
                            event.get("response", {}).get("error") if
                            isinstance(event.get("response"), dict) else {}) or {}
                        code = failure.get("code", "") if isinstance(failure, dict) else ""
                        if code in (
                            "subscription_sharing_usage_limit_exceeded",
                            "subscription_sharing_usage_unavailable",
                        ):
                            raise ChatGPTConnectionError(
                                "ChatGPT plan usage is unavailable or exhausted; no paid fallback")
                        raise ChatGPTConnectionError("ChatGPT did not complete inference")
        except httpx.HTTPError:
            raise ChatGPTConnectionError("ChatGPT inference transport failed; no paid fallback") from None
        if not completed:
            raise ChatGPTConnectionError("ChatGPT stream ended without response.completed")
        return {"provider": "chatgpt_plan", "model": model, "text": "".join(pieces)}

    def disconnect(self, *, email: str) -> dict[str, Any]:
        """Remove renewable session locally; report remote revocation honestly."""
        with _credential_lock(self.records.home):
            entry = self.records.selected(email)
            if not entry:
                return {"disconnected": True, "remote_revocation_confirmed": False}
            saved = self.records.get(entry)
            client = saved.get("client_id", "")
            refresh = saved.get("refresh_token", "")
            revoked = False
            if isinstance(refresh, str) and refresh and CLIENT_ID.fullmatch(client or ""):
                try:
                    metadata = self.http.get(AUTH + "/.well-known/openid-configuration")
                    metadata.raise_for_status()
                    url = metadata.json().get("revocation_endpoint", "")
                    if isinstance(url, str) and url.startswith(AUTH + "/"):
                        response = self.http.post(
                            url, data={"token": refresh, "client_id": client,
                                       "token_type_hint": "refresh_token"}, timeout=8)
                        revoked = response.status_code == 200
                except (httpx.HTTPError, KeyError, TypeError, ValueError):
                    pass
            # Retain the issued registration, but no access/refresh/ID token.
            saved.update(access_token="", refresh_token="", id_token="",
                         scope="", expires_at=0)
            self.records.save(saved)
        return {"disconnected": True, "remote_revocation_confirmed": revoked}



def main(argv: list[str] | None = None) -> int:
    """Local CLI; no OAuth tokens or private prompts are passed in argv."""
    import argparse
    parser = argparse.ArgumentParser(
        description="MegasMoves open-source, local Sign in with ChatGPT")
    command = parser.add_subparsers(dest="command", required=True)
    command.add_parser("status")
    signin = command.add_parser("sign-in")
    signin.add_argument("--profile", required=True,
                        help="exact name of an existing Chrome profile")
    signin.add_argument("--account", required=True,
                        help="expected ChatGPT account email")
    signin.add_argument("--reconsent", action="store_true")
    list_models = command.add_parser("models")
    list_models.add_argument("--account", required=True)
    ask = command.add_parser("ask")
    ask.add_argument("--account", required=True)
    ask.add_argument("--model", required=True)
    plan = command.add_parser("plan", help="Propose reviewed tasks for connected Megas agents")
    plan.add_argument("--account", required=True)
    plan.add_argument("--model", required=True)
    command.add_parser("agents", help="List connected, reviewable Megas agent targets")
    disconnect = command.add_parser("disconnect")
    disconnect.add_argument("--account", required=True)
    args = parser.parse_args(argv)
    client = OSSChatGPTClient()
    try:
        if args.command == "status":
            output = {"accounts": client.records.summaries(),
                      "state": client.status()}
        elif args.command == "sign-in":
            output = client.begin(chrome_profile_name=args.profile,
                                  account_email=args.account,
                                  reconsent=args.reconsent)
            print(json.dumps(output), flush=True)
            output = client.wait()
        elif args.command == "models":
            output = {"models": client.models(email=args.account)}
        elif args.command == "ask":
            # Reading stdin avoids exposing the prompt in process argv.
            prompt = sys.stdin.read(MAX_TEXT)
            output = client.infer(email=args.account, model=args.model,
                                  prompt=prompt)
        elif args.command == "agents":
            from .siwc_leader import connected_agents
            output = {"agents": connected_agents(), "read_only": True}
        elif args.command == "plan":
            from .siwc_leader import preview
            goal = sys.stdin.read(4001)
            output = preview(account_email=args.account,
                             model=args.model, goal=goal, client=client)
        else:
            output = client.disconnect(email=args.account)
        print(json.dumps(output, ensure_ascii=False))
        return 0
    except ChatGPTConnectionError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
