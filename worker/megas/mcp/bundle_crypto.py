from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


ENCRYPTED_BUNDLE_FORMAT = "megas-mcp-encrypted-bundle"
ENCRYPTED_BUNDLE_VERSION = 1
CIPHER = "AES-256-GCM"
KDF = "scrypt"
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32
MIN_PASSPHRASE_LENGTH = 12
MAX_PLAINTEXT_BYTES = 16 * 1024 * 1024
MAX_CIPHERTEXT_BYTES = MAX_PLAINTEXT_BYTES + 1024


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64decode(value: Any, *, field: str, expected_bytes: int | None = None) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError(f"encrypted bundle is missing {field}")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError(f"encrypted bundle contains invalid base64 in {field}") from exc
    if expected_bytes is not None and len(decoded) != expected_bytes:
        raise ValueError(
            f"encrypted bundle {field} must decode to exactly {expected_bytes} bytes"
        )
    return decoded


def _passphrase_bytes(passphrase: str) -> bytes:
    if not isinstance(passphrase, str) or len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise ValueError(
            f"bundle passphrase must contain at least {MIN_PASSPHRASE_LENGTH} characters"
        )
    return passphrase.encode("utf-8")


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(
        salt=salt,
        length=KEY_BYTES,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
    ).derive(_passphrase_bytes(passphrase))


def _header(*, salt: str, nonce: str) -> dict[str, Any]:
    return {
        "format": ENCRYPTED_BUNDLE_FORMAT,
        "version": ENCRYPTED_BUNDLE_VERSION,
        "cipher": CIPHER,
        "kdf": {
            "name": KDF,
            "n": SCRYPT_N,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
            "salt": salt,
        },
        "nonce": nonce,
    }


def _validated_header(envelope: dict[str, Any]) -> tuple[dict[str, Any], bytes, bytes]:
    if envelope.get("format") != ENCRYPTED_BUNDLE_FORMAT:
        raise ValueError("not an encrypted Megas MCP bundle")
    if int(envelope.get("version") or 0) != ENCRYPTED_BUNDLE_VERSION:
        raise ValueError("unsupported encrypted Megas MCP bundle version")
    if envelope.get("cipher") != CIPHER:
        raise ValueError("unsupported encrypted Megas MCP bundle cipher")
    kdf = envelope.get("kdf")
    if not isinstance(kdf, dict):
        raise ValueError("encrypted bundle is missing KDF metadata")
    if kdf.get("name") != KDF:
        raise ValueError("unsupported encrypted Megas MCP bundle KDF")
    if (
        int(kdf.get("n") or 0) != SCRYPT_N
        or int(kdf.get("r") or 0) != SCRYPT_R
        or int(kdf.get("p") or 0) != SCRYPT_P
    ):
        raise ValueError("encrypted bundle uses unsupported scrypt parameters")
    salt_text = kdf.get("salt")
    nonce_text = envelope.get("nonce")
    salt = _b64decode(salt_text, field="kdf.salt", expected_bytes=SALT_BYTES)
    nonce = _b64decode(nonce_text, field="nonce", expected_bytes=NONCE_BYTES)
    return _header(salt=str(salt_text), nonce=str(nonce_text)), salt, nonce


def encrypt_bundle(bundle: dict[str, Any], passphrase: str) -> dict[str, Any]:
    """Encrypt a portable bundle with scrypt-derived AES-256-GCM authenticated encryption."""

    plaintext = _canonical_bytes(bundle)
    if len(plaintext) > MAX_PLAINTEXT_BYTES:
        raise ValueError("portable bundle exceeds the 16 MiB encrypted-bundle limit")
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    header = _header(salt=_b64encode(salt), nonce=_b64encode(nonce))
    key = _derive_key(passphrase, salt)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _canonical_bytes(header))
    if len(ciphertext) > MAX_CIPHERTEXT_BYTES:
        raise ValueError("encrypted bundle ciphertext exceeds the supported size limit")
    return {**header, "ciphertext": _b64encode(ciphertext)}


def decrypt_bundle(envelope: dict[str, Any], passphrase: str) -> dict[str, Any]:
    """Authenticate then decrypt an encrypted Megas bundle without persisting the passphrase."""

    header, salt, nonce = _validated_header(envelope)
    ciphertext = _b64decode(envelope.get("ciphertext"), field="ciphertext")
    if len(ciphertext) > MAX_CIPHERTEXT_BYTES:
        raise ValueError("encrypted bundle ciphertext exceeds the supported size limit")
    key = _derive_key(passphrase, salt)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _canonical_bytes(header))
    except InvalidTag as exc:
        raise ValueError(
            "encrypted bundle authentication failed; the passphrase is incorrect or the bundle was modified"
        ) from exc
    if len(plaintext) > MAX_PLAINTEXT_BYTES:
        raise ValueError("decrypted portable bundle exceeds the supported size limit")
    try:
        value = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("decrypted bundle payload is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("decrypted bundle payload must be a JSON object")
    return value


def is_encrypted_bundle(value: Any) -> bool:
    return isinstance(value, dict) and value.get("format") == ENCRYPTED_BUNDLE_FORMAT


__all__ = [
    "CIPHER",
    "ENCRYPTED_BUNDLE_FORMAT",
    "ENCRYPTED_BUNDLE_VERSION",
    "KDF",
    "MIN_PASSPHRASE_LENGTH",
    "decrypt_bundle",
    "encrypt_bundle",
    "is_encrypted_bundle",
]
