"""API keys: generation, hashing and Bearer parsing. Plain keys are never stored or logged."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

# DEFAULT: visible prefix so a leaked key is recognisable in secret scanners.
KEY_PREFIX = "piq_"


def generate_key() -> str:
    """Return a new random plain key (shown to the developer once)."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(plain: str) -> str:
    """Return the SHA-256 hex of a key.

    DEFAULT: unsalted SHA-256, because keys are 256-bit random values (not passwords), which
    also lets the lookup be a plain indexed equality.
    """
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def parse_bearer(header: str | None) -> str | None:
    """Return the token of an `Authorization: Bearer <token>` header, else None."""
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def tokens_equal(a: str, b: str) -> bool:
    """Constant-time string comparison."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ------------------------------------------------------------------ session cookies (P3.5)


def sign_payload(payload: dict[str, Any], secret: str) -> str:
    """Return `<b64 json>.<b64 hmac-sha256>` for a small JSON payload.

    DEFAULT: stdlib HMAC cookie instead of a new dependency (itsdangerous is not in the project).
    """
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(
        b"="
    )
    sig = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return body.decode() + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


def verify_payload(token: str | None, secret: str) -> dict[str, Any] | None:
    """Return the payload when the signature is valid and `exp` (if present) has not passed."""
    if not token or "." not in token or not secret:
        return None
    body, _, sig = token.partition(".")
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    try:
        given = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(given, expected) or not isinstance(data, dict):
        return None
    exp = data.get("exp")
    if exp is not None and (not isinstance(exp, int | float) or exp < time.time()):
        return None
    return data
