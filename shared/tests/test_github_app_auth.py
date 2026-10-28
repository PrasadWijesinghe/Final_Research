"""App JWT, installation token cache and webhook signature checks (no network)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from shared.github import GitHubError, InstallationTokenCache, create_app_jwt, verify_signature

SECRET = "fake-webhook-secret-for-tests"


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem_of(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def b64d(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_app_jwt_is_rs256_with_short_lifetime(key: rsa.RSAPrivateKey) -> None:
    token = create_app_jwt("12345", pem_of(key), now=1_000_000)
    head, claims, sig = token.split(".")
    assert json.loads(b64d(head)) == {"alg": "RS256", "typ": "JWT"}
    body = json.loads(b64d(claims))
    assert body["iss"] == "12345" and body["exp"] - body["iat"] <= 600
    key.public_key().verify(
        b64d(sig), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )  # raises if the signature is wrong


def test_signature_verification() -> None:
    body = b'{"zen": "fake"}'
    good = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(SECRET, body, good)
    assert not verify_signature(SECRET, body + b" ", good)
    assert not verify_signature("other-secret", body, good)
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, good.removeprefix("sha256="))
    assert not verify_signature("", body, good)


def test_installation_token_is_cached_until_near_expiry(
    key: rsa.RSAPrivateKey, tmp_path: Path
) -> None:
    pem = tmp_path / "app.pem"
    pem.write_bytes(pem_of(key))
    now = [1_700_000_000.0]
    cache = InstallationTokenCache("12345", pem, clock=lambda: now[0])
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("https://api.github.com/app/installations/7/access_tokens").mock(
            side_effect=[
                httpx.Response(
                    201, json={"token": "ghs_fake1", "expires_at": "2023-11-14T23:13:20Z"}
                ),
                httpx.Response(
                    201, json={"token": "ghs_fake2", "expires_at": "2023-11-15T00:13:20Z"}
                ),
            ]
        )
        assert cache.get_token(7) == "ghs_fake1"
        assert cache.get_token(7) == "ghs_fake1"  # cached
        assert route.call_count == 1
        auth = route.calls[0].request.headers["Authorization"]
        assert auth.startswith("Bearer ") and auth.count(".") == 2  # the App JWT
        now[0] += 3600  # past expiry (+3600 s) minus margin
        assert cache.get_token(7) == "ghs_fake2"
        assert route.call_count == 2


def test_token_failure_is_a_github_error(key: rsa.RSAPrivateKey, tmp_path: Path) -> None:
    pem = tmp_path / "app.pem"
    pem.write_bytes(pem_of(key))
    cache = InstallationTokenCache("12345", pem)
    with respx.mock() as mock:
        mock.post("https://api.github.com/app/installations/7/access_tokens").respond(401)
        with pytest.raises(GitHubError) as err:
            cache.get_token(7)
    assert err.value.status_code == 401
