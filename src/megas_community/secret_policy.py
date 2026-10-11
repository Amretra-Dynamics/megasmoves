from __future__ import annotations

import re


_SECRET_KEY_PATTERNS = (
    re.compile(r"(^|_)(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)(_|$)", re.IGNORECASE),
    re.compile(r"(^|_)(API_?KEY|PRIVATE_?KEY|CLIENT_?SECRET|AUTH_?TOKEN)(_|$)", re.IGNORECASE),
    re.compile(r"(^|_)(ACCESS_?KEY(_ID)?|SECRET_?ACCESS_?KEY)(_|$)", re.IGNORECASE),
    re.compile(r"(^|_)(COOKIE|SESSION_?KEY|SIGNING_?KEY)(_|$)", re.IGNORECASE),
)


def is_sensitive_env_key(key: str) -> bool:
    """Conservative deterministic check for environment names likely to carry secrets.

    This is a safety gate, not a secret classifier. Values are never inspected or
    persisted by this policy. Users can always bind the key through SecretBinding.
    """

    normalized = key.strip()
    return bool(normalized) and any(pattern.search(normalized) for pattern in _SECRET_KEY_PATTERNS)


__all__ = ["is_sensitive_env_key"]
