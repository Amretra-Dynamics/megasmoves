from __future__ import annotations

import hashlib
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .model import MCPServer


CHALLENGE_PATH = "/.well-known/openai-apps-challenge"
MAX_CHALLENGE_BYTES = 64 * 1024


def _origin(url: str) -> tuple[str, str, int, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("domain verification requires an HTTPS origin")
    host = parsed.hostname.lower().rstrip(".")
    port = parsed.port or 443
    netloc = host if port == 443 else f"{host}:{port}"
    return parsed.scheme, host, port, f"https://{netloc}"


def _allowed_challenge_host(mcp_host: str, candidate_host: str) -> bool:
    return mcp_host == candidate_host or mcp_host.endswith("." + candidate_host)


def challenge_url(
    server: MCPServer,
    *,
    challenge_base_url: str | None = None,
) -> dict[str, str]:
    if not server.url:
        raise ValueError("domain verification requires a configured MCP URL")
    _scheme, mcp_host, _mcp_port, mcp_origin = _origin(server.url)
    requested_base = challenge_base_url or mcp_origin
    _base_scheme, base_host, _base_port, base_origin = _origin(requested_base)
    if not _allowed_challenge_host(mcp_host, base_host):
        raise ValueError(
            "challenge base host must be the MCP host name or a parent host name"
        )
    return {
        "mcp_origin": mcp_origin,
        "challenge_base_origin": base_origin,
        "challenge_url": base_origin + CHALLENGE_PATH,
    }


def verify_domain_challenge(
    server: MCPServer,
    challenge_token: str,
    *,
    challenge_base_url: str | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Verify the public challenge endpoint without persisting or returning its token."""

    if not challenge_token:
        raise ValueError("domain verification challenge token must be non-empty")
    if timeout <= 0 or timeout > 60:
        raise ValueError("domain verification timeout must be between 0 and 60 seconds")

    locations = challenge_url(server, challenge_base_url=challenge_base_url)
    request = urllib.request.Request(
        locations["challenge_url"],
        headers={"Accept": "text/plain, */*;q=0.1"},
        method="GET",
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return {
            **locations,
            "verified": False,
            "status": int(exc.code),
            "error": f"challenge endpoint returned HTTP {exc.code}",
            "token_returned": False,
        }
    except urllib.error.URLError as exc:
        return {
            **locations,
            "verified": False,
            "status": None,
            "error": f"challenge endpoint connection failed: {exc.reason}",
            "token_returned": False,
        }

    with response:
        status = int(getattr(response, "status", 200) or 200)
        body = response.read(MAX_CHALLENGE_BYTES + 1)
        content_type = str(response.headers.get("Content-Type") or "")
    if len(body) > MAX_CHALLENGE_BYTES:
        return {
            **locations,
            "verified": False,
            "status": status,
            "content_type": content_type,
            "response_bytes": len(body),
            "error": "challenge endpoint response exceeds 64 KiB",
            "token_returned": False,
        }

    expected = challenge_token.encode("utf-8")
    verified = status == 200 and body == expected
    result: dict[str, Any] = {
        **locations,
        "verified": verified,
        "status": status,
        "content_type": content_type,
        "response_bytes": len(body),
        "response_sha256": hashlib.sha256(body).hexdigest(),
        "token_returned": False,
    }
    if not verified:
        result["error"] = (
            "challenge endpoint must return HTTP 200 with only the exact verification token"
        )
    return result


__all__ = ["CHALLENGE_PATH", "challenge_url", "verify_domain_challenge"]
