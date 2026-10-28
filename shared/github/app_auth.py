"""GitHub App authentication: RS256 App JWT, installation access tokens (cached) and webhook
signature verification. Keys, tokens and secrets are never logged."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from shared.github.client import API_URL, GitHubError

logger = logging.getLogger(__name__)

# GitHub allows an App JWT to live at most 10 minutes; clock drift is covered by backdating `iat`.
# DEFAULT: 9 minutes of life, issued 60 s in the past.
JWT_TTL_S = 540
JWT_BACKDATE_S = 60
# DEFAULT: an installation token is renewed 120 s before GitHub says it expires.
TOKEN_MARGIN_S = 120


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def create_app_jwt(app_id: str, private_key_pem: bytes, *, now: float | None = None) -> str:
    """Return an RS256 JWT that authenticates as the App itself."""
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("the GitHub App private key must be an RSA key")
    issued = int(time.time() if now is None else now)
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {"iat": issued - JWT_BACKDATE_S, "exp": issued + JWT_TTL_S, "iss": str(app_id)}
    signing_input = ".".join(
        _b64(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims)
    )
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64(signature)}"


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Check an `X-Hub-Signature-256` header (`sha256=<hex>`) in constant time."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode("ascii"), header.encode("utf-8", "replace"))


class TokenProvider(Protocol):
    """Gives an installation access token for an installation id."""

    def get_token(self, installation_id: int) -> str:
        """Return a valid token (cached until shortly before it expires)."""
        ...


class InstallationTokenCache:
    """Mints installation tokens with the App JWT and caches each until it nearly expires."""

    def __init__(
        self,
        app_id: str,
        key_path: str | Path,
        *,
        base_url: str = API_URL,
        clock: Callable[[], float] = time.time,
        timeout: float = 15.0,
    ) -> None:
        self._app_id = app_id
        self._key_path = Path(key_path)
        self._base_url = base_url.rstrip("/")
        self._clock = clock
        self._timeout = timeout
        self._tokens: dict[int, tuple[str, float]] = {}  # id -> (token, expiry epoch)
        self._lock = threading.Lock()

    def get_token(self, installation_id: int) -> str:
        """Return a cached token, or ask GitHub for a new one."""
        with self._lock:
            cached = self._tokens.get(installation_id)
            if cached and cached[1] - TOKEN_MARGIN_S > self._clock():
                return cached[0]
            token, expires = self._mint(installation_id)
            self._tokens[installation_id] = (token, expires)
            return token

    def _mint(self, installation_id: int) -> tuple[str, float]:
        jwt = create_app_jwt(self._app_id, self._key_path.read_bytes(), now=self._clock())
        try:
            response = httpx.post(
                f"{self._base_url}/app/installations/{installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {jwt}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "pipelineiq-app",
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise GitHubError(f"installation token request failed: {type(exc).__name__}") from exc
        if response.status_code != 201:
            raise GitHubError(
                f"installation token request failed: HTTP {response.status_code}",
                response.status_code,
            )
        body = response.json()
        expires = datetime.fromisoformat(str(body["expires_at"])).astimezone(UTC).timestamp()
        logger.info("installation token minted", extra={"installation_id": installation_id})
        return str(body["token"]), expires
