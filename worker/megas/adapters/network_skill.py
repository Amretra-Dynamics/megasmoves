from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .base import subprocess_hide_window_kwargs
from .network_base import (
    NetworkAdapter,
    NetworkCapability,
    NetworkEvidence,
    NetworkFailure,
    NetworkProfile,
    NetworkRestoreState,
    NetworkSession,
    NetworkVerification,
)

logger = logging.getLogger("megas.network_skill")


def platform_is_windows() -> bool:
    return os.environ.get("MEGAS_MOCK_OS_NAME", os.name) == "nt"


class BasePlatformVpnAdapter(NetworkAdapter):
    def prepare(self, profile: NetworkProfile) -> None:
        pass

    def connect(self, profile: NetworkProfile) -> NetworkSession:
        return NetworkSession(
            session_id=f"session-{profile.provider_id}",
            provider_id=profile.provider_id,
            connected_at=datetime.now(timezone.utc).isoformat(),
            original_state=NetworkRestoreState(original_env=dict(os.environ)),
        )

    def verify(self, session: NetworkSession, request_id: str) -> NetworkVerification:
        evidence = NetworkSkill.collect_verification_evidence()
        return NetworkVerification(
            request_id=request_id,
            success=evidence["success"],
            public_ip=evidence["public_ip"],
            dns_ok=evidence["dns_ok"],
            connectivity_ok=evidence["connectivity_ok"],
            timestamp=evidence["checked_at"],
            failed_checks=[] if evidence["success"] else ["verification_failed"],
        )

    def run(self, session: NetworkSession) -> None:
        pass

    def disconnect(self, session: NetworkSession) -> None:
        pass

    def restore(self, restore_state: NetworkRestoreState) -> None:
        # Revert environment changes
        for k in list(os.environ.keys()):
            if k not in restore_state.original_env:
                os.environ.pop(k, None)
        for k, v in restore_state.original_env.items():
            os.environ[k] = v


class WindowsVpnAdapter(BasePlatformVpnAdapter):
    def prepare(self, profile: NetworkProfile) -> None:
        if not platform_is_windows():
            raise RuntimeError("Windows VPN adapter is only supported on Windows.")
        if not shutil.which("rasdial"):
            raise RuntimeError("rasdial executable not found.")

    def connect(self, profile: NetworkProfile) -> NetworkSession:
        session = super().connect(profile)
        profile_name = profile.profile_name
        # Build command: rasdial entryname [username [password]]
        cmd = ["rasdial", profile_name]
        # In a real environment, lookup credential reference if username/password are provided in profile
        username = profile.policy.get("username")
        password = profile.policy.get("password")
        if username and password:
            cmd.extend([username, password])
        
        logger.info("Executing rasdial connection for %s", profile_name)
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False, **subprocess_hide_window_kwargs())
        if res.returncode != 0:
            err = res.stderr or res.stdout
            raise RuntimeError(f"rasdial connection failed: {err}")
        return session

    def disconnect(self, session: NetworkSession) -> None:
        profile_name = session.details.get("profile_name") or session.provider_id
        cmd = ["rasdial", profile_name, "/disconnect"]
        subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False, **subprocess_hide_window_kwargs())
        self.restore(session.original_state)


class MacVpnAdapter(BasePlatformVpnAdapter):
    def prepare(self, profile: NetworkProfile) -> None:
        if platform_is_windows() or not shutil.which("scutil"):
            raise RuntimeError("macOS scutil VPN adapter is not supported on this platform.")

    def connect(self, profile: NetworkProfile) -> NetworkSession:
        session = super().connect(profile)
        profile_name = profile.profile_name
        cmd = ["scutil", "--nc", "start", profile_name]
        logger.info("Executing scutil connection for %s", profile_name)
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False, **subprocess_hide_window_kwargs())
        if res.returncode != 0:
            raise RuntimeError(f"scutil connection failed: {res.stderr or res.stdout}")
        return session

    def disconnect(self, session: NetworkSession) -> None:
        profile_name = session.details.get("profile_name") or session.provider_id
        cmd = ["scutil", "--nc", "stop", profile_name]
        subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False, **subprocess_hide_window_kwargs())
        self.restore(session.original_state)


class LinuxVpnAdapter(BasePlatformVpnAdapter):
    def prepare(self, profile: NetworkProfile) -> None:
        if not shutil.which("nmcli"):
            raise RuntimeError("Linux nmcli VPN adapter is not supported or NetworkManager is missing.")

    def connect(self, profile: NetworkProfile) -> NetworkSession:
        session = super().connect(profile)
        profile_name = profile.profile_name
        cmd = ["nmcli", "connection", "up", profile_name]
        logger.info("Executing nmcli connection up for %s", profile_name)
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False, **subprocess_hide_window_kwargs())
        if res.returncode != 0:
            raise RuntimeError(f"nmcli connection up failed: {res.stderr or res.stdout}")
        return session

    def disconnect(self, session: NetworkSession) -> None:
        profile_name = session.details.get("profile_name") or session.provider_id
        cmd = ["nmcli", "connection", "down", profile_name]
        subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False, **subprocess_hide_window_kwargs())
        self.restore(session.original_state)


class WireGuardGenericAdapter(BasePlatformVpnAdapter):
    def prepare(self, profile: NetworkProfile) -> None:
        wg_conf = profile.policy.get("wg_conf")
        if not wg_conf:
            raise ValueError("WireGuard config (wg_conf) missing in policy.")

    def connect(self, profile: NetworkProfile) -> NetworkSession:
        session = super().connect(profile)
        wg_conf = profile.policy.get("wg_conf", "")
        conf_path = "/etc/wireguard/wg_megas.conf"
        if platform_is_windows():
            conf_path = os.path.join(os.getenv("TEMP", "C:\\Temp"), "wg_megas.conf")

        os.makedirs(os.path.dirname(conf_path), exist_ok=True)
        with open(conf_path, "w", encoding="utf-8") as f:
            f.write(wg_conf)
        try:
            os.chmod(conf_path, 0o600)
        except OSError:
            pass

        cmd = ["sudo", "-n", "wg-quick", "up", "wg_megas"]
        if platform_is_windows():
            cmd = ["wireguard.exe", "/installtunnelservice", conf_path]
            
        logger.info("Executing WireGuard up")
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False, **subprocess_hide_window_kwargs())
        if res.returncode != 0:
            raise RuntimeError(f"WireGuard up failed: {res.stderr or res.stdout}")
        return session

    def disconnect(self, session: NetworkSession) -> None:
        cmd = ["sudo", "-n", "wg-quick", "down", "wg_megas"]
        if platform_is_windows():
            cmd = ["wireguard.exe", "/uninstalltunnelservice", "wg_megas"]
        subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False, **subprocess_hide_window_kwargs())
        self.restore(session.original_state)


class NordvpnAdapter(BasePlatformVpnAdapter):
    def prepare(self, profile: NetworkProfile) -> None:
        if not shutil.which("nordvpn"):
            raise RuntimeError("NordVPN CLI executable not found.")

    def connect(self, profile: NetworkProfile) -> NetworkSession:
        session = super().connect(profile)
        region = profile.policy.get("region", "us-west")
        if not isinstance(region, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", region):
            raise ValueError(f"Invalid NordVPN region specified: {region}")
            
        cmd = ["nordvpn", "connect", region]
        logger.info("Executing NordVPN connect %s", region)
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False, **subprocess_hide_window_kwargs())
        if res.returncode != 0:
            raise RuntimeError(f"NordVPN connect failed: {res.stderr or res.stdout}")
        return session

    def disconnect(self, session: NetworkSession) -> None:
        cmd = ["nordvpn", "disconnect"]
        subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False, **subprocess_hide_window_kwargs())
        self.restore(session.original_state)


class TailscaleAdapter(BasePlatformVpnAdapter):
    """Tailscale mesh VPN — network egress profile, not a terminal stream mode.

    Same product family as NordVPN/WireGuard: assign under agent Network profiles.
    Users may keep NordVPN for commercial egress and Tailscale for private mesh
    as separate profiles (OS-dependent whether both are active simultaneously).
    """

    def prepare(self, profile: NetworkProfile) -> None:
        if not shutil.which("tailscale"):
            raise RuntimeError("Tailscale CLI executable not found.")

    def connect(self, profile: NetworkProfile) -> NetworkSession:
        session = super().connect(profile)
        policy = profile.policy if isinstance(profile.policy, dict) else {}
        cmd: list[str] = ["tailscale", "up"]
        auth_key = str(policy.get("auth_key") or "").strip()
        if auth_key:
            if not re.fullmatch(r"[A-Za-z0-9._:-]{8,256}", auth_key):
                raise ValueError("Invalid Tailscale auth key format.")
            cmd.extend(["--authkey", auth_key])
        login_server = str(policy.get("login_server") or "").strip()
        if login_server:
            if not re.fullmatch(r"https?://[A-Za-z0-9._:/-]{3,200}", login_server):
                raise ValueError("Invalid Tailscale login server URL.")
            cmd.extend(["--login-server", login_server])
        exit_node = str(policy.get("exit_node") or "").strip()
        if exit_node:
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", exit_node):
                raise ValueError("Invalid Tailscale exit node hostname.")
            cmd.extend(["--exit-node", exit_node])
        logger.info("Executing Tailscale up")
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
            **subprocess_hide_window_kwargs(),
        )
        if res.returncode != 0:
            raise RuntimeError(f"Tailscale up failed: {res.stderr or res.stdout}")
        return session

    def disconnect(self, session: NetworkSession) -> None:
        # Do not `tailscale down` hard — other apps may share the mesh. Clear exit node only.
        exit_clear = ["tailscale", "set", "--exit-node="]
        subprocess.run(
            exit_clear,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            **subprocess_hide_window_kwargs(),
        )
        self.restore(session.original_state)


class ProxyAdapter(BasePlatformVpnAdapter):
    def connect(self, profile: NetworkProfile) -> NetworkSession:
        session = super().connect(profile)
        proxy_url = str(profile.policy.get("proxy_url") or "")
        if not proxy_url:
            raise ValueError("Proxy URL missing in policy.")
            
        parsed = urlparse(proxy_url)
        if parsed.scheme not in self.allowed_schemes() or not parsed.hostname:
            raise ValueError(f"Invalid proxy URL or scheme: {proxy_url}")
            
        os.environ["http_proxy"] = proxy_url
        os.environ["https_proxy"] = proxy_url
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        return session

    def disconnect(self, session: NetworkSession) -> None:
        self.restore(session.original_state)

    def allowed_schemes(self) -> set[str]:
        return set()


class HttpProxyAdapter(ProxyAdapter):
    def allowed_schemes(self) -> set[str]:
        return {"http", "https"}


class Socks5ProxyAdapter(ProxyAdapter):
    def allowed_schemes(self) -> set[str]:
        return {"socks5", "socks5h"}


class NetworkSkill:
    """
    Built-in network adapter manager handling WireGuard, NordVPN, and proxy configurations.
    Conforms to versioned multi-phase lifecycle interfaces.
    """
    _current_session: NetworkSession | None = None
    _pre_connect_ip: str = ""
    _verification_status: str = "unverified"
    _verification_details: Dict[str, Any] | None = None
    _current_adapter: NetworkAdapter | None = None

    @staticmethod
    def get_public_ip() -> str:
        """
        Retrieves the current public IP address of the worker with retries and timeout bounds.
        """
        endpoints = [
            "https://api.ipify.org",
            "https://ifconfig.me/ip",
            "https://httpbin.org/ip",
        ]
        for url in endpoints:
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0 (MegasMoves Worker)"}
                )
                with urllib.request.urlopen(req, timeout=5) as response:
                    # Bounded response read (max 4KB)
                    ip_bytes = response.read(4096)
                    ip = ip_bytes.decode("utf-8").strip()
                    if "httpbin" in url:
                        try:
                            data = json.loads(ip)
                            ip = data.get("origin", ip)
                        except Exception:
                            pass
                    # Parse first IP candidate in case of proxy headers
                    ip_candidate = ip.split(",", 1)[0].strip()
                    address = ipaddress.ip_address(ip_candidate)
                    if address.is_global:
                        return str(address)
            except Exception as exc:
                logger.warning(
                    "Public IP lookup failed for %s (%s)", url, type(exc).__name__
                )
        return ""

    @staticmethod
    def verify() -> bool:
        """
        Verifies that network connectivity is healthy by querying public endpoints.
        """
        try:
            req = urllib.request.Request(
                "https://megasmoves.io/healthz",
                headers={"User-Agent": "Mozilla/5.0 (MegasMoves Worker)"},
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status == 200
        except Exception as exc:
            logger.error("Network verification failed (%s)", type(exc).__name__)
            return False

    @staticmethod
    def dns_check(host: str = "megasmoves.io") -> bool:
        try:
            return bool(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except OSError as exc:
            logger.error("DNS verification failed (%s)", type(exc).__name__)
            return False

    @classmethod
    def collect_verification_evidence(cls) -> Dict[str, Any]:
        public_ip = cls.get_public_ip()
        dns_ok = cls.dns_check()
        connectivity_ok = cls.verify()
        
        try:
            valid_public_ip = ipaddress.ip_address(public_ip).is_global
        except ValueError:
            valid_public_ip = False

        ip_changed = True
        if cls._pre_connect_ip and public_ip:
            if public_ip == cls._pre_connect_ip:
                ip_changed = False
                logger.warning("IP verification failed: public IP did not change from pre-connect IP: %s", public_ip)

        # Commercial VPN/proxy profiles fail closed when public egress did not
        # change; otherwise a silently ineffective connection could run work on
        # the user's original network. The Worker message handler explicitly
        # relaxes this one check for Tailscale mesh profiles, which are not an
        # egress VPN and commonly retain the same public IP.
        success = bool(valid_public_ip and dns_ok and connectivity_ok and ip_changed)
        if success:
            cls._verification_status = "verified"
        else:
            cls._verification_status = "failed"

        cls._verification_details = {
            "success": success,
            "public_ip": public_ip,
            "dns_ok": dns_ok,
            "connectivity_ok": connectivity_ok,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "ip_changed": ip_changed,
        }
        return cls._verification_details

    @classmethod
    def _normalize_adapter_id(cls, provider_id: str) -> str:
        raw = str(provider_id or "").strip().lower()
        if raw.startswith("provider-"):
            key = raw
        else:
            key = f"provider-{raw.replace('_', '-')}"
        aliases = {
            "provider-http_proxy": "provider-http-proxy",
            "provider-httpproxy": "provider-http-proxy",
            "provider-windows-ras": "provider-rasdial",
            "provider-windows_ras": "provider-rasdial",
            "provider-ras": "provider-rasdial",
            "provider-wg": "provider-wireguard",
            "provider-ts": "provider-tailscale",
            "provider-custom": "provider-nmcli",  # best-effort host VPN CLI
        }
        return aliases.get(key, key)

    @classmethod
    def _select_adapter(cls, provider_id: str) -> NetworkAdapter:
        mapping = {
            "provider-wireguard": WireGuardGenericAdapter,
            "provider-nordvpn": NordvpnAdapter,
            "provider-tailscale": TailscaleAdapter,
            "provider-socks5": Socks5ProxyAdapter,
            "provider-http-proxy": HttpProxyAdapter,
            "provider-rasdial": WindowsVpnAdapter,
            "provider-scutil": MacVpnAdapter,
            "provider-nmcli": LinuxVpnAdapter,
        }
        key = cls._normalize_adapter_id(provider_id)
        adapter_cls = mapping.get(key)
        if not adapter_cls:
            raise ValueError(f"Unsupported network provider ID: {provider_id}")
        return adapter_cls()

    @classmethod
    def connect(cls, provider_id: str, policy: Dict[str, Any]) -> bool:
        """
        Connects the worker runtime to a specified VPN or proxy network provider.
        """
        logger.info("Connecting network provider %s", provider_id)
        cls._pre_connect_ip = cls.get_public_ip()
        cls._verification_status = "unverified"
        cls._verification_details = None

        try:
            adapter = cls._select_adapter(provider_id)
            profile = NetworkProfile(
                provider_id=provider_id,
                profile_name=policy.get("profile_name", provider_id),
                credential_reference=policy.get("credential_ref", ""),
                policy=policy,
            )
            adapter.prepare(profile)
            session = adapter.connect(profile)
            cls._current_session = session
            cls._current_adapter = adapter
            return True
        except Exception as exc:
            logger.error("Failed to connect network provider %s: %s", provider_id, exc)
            cls._verification_status = "failed"
            # Restore in case of partial setup failure
            if cls._current_session:
                try:
                    adapter.disconnect(cls._current_session)
                except Exception:
                    pass
                cls._current_session = None
                cls._current_adapter = None
            return False

    @classmethod
    def disconnect(cls, provider_id: str) -> bool:
        """
        Disconnects the active VPN or proxy network provider and restores previous state.
        """
        logger.info("Disconnecting provider: %s", provider_id)
        session = cls._current_session
        adapter = cls._current_adapter

        # Fallback to direct CLI calls if session state was lost
        if not session or not adapter or session.provider_id != provider_id:
            try:
                adapter = cls._select_adapter(provider_id)
                dummy_session = NetworkSession(
                    session_id=f"dummy-{provider_id}",
                    provider_id=provider_id,
                    connected_at="",
                )
                adapter.disconnect(dummy_session)
                cls._current_session = None
                cls._current_adapter = None
                cls._verification_status = "unverified"
                return True
            except Exception:
                return False

        try:
            adapter.disconnect(session)
            cls._current_session = None
            cls._current_adapter = None
            cls._verification_status = "unverified"
            return True
        except Exception as exc:
            logger.error("Failed to disconnect network provider %s: %s", provider_id, exc)
            return False

    @classmethod
    def is_execution_allowed(cls) -> bool:
        """
        Kill-switch constraint check: no execution allowed if verification failed on an active session.
        """
        if cls._current_session is None:
            return True
        return cls._verification_status == "verified"
