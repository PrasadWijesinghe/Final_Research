"""API keys: generation, hashing and Bearer parsing. Plain keys are never stored or logged."""

from __future__ import annotations

import hashlib
import hmac
import secrets

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
