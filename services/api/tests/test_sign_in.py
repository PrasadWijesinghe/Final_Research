"""GitHub sign-in, session cookie, self-service keys and the failure list (P3.5)."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import pytest
import respx
from api.app import create_app
from api.auth import hash_key, sign_payload, verify_payload
from api.store import InMemoryStore
from api_helpers import error_diagnosis, routed_failure, settings

from shared.contracts import Expert, ReviewDecision

pytestmark = pytest.mark.anyio

SECRET = "fake-session-secret-for-tests"
CFG: dict[str, Any] = {
    "github_client_id": "fake-client-id",
    "github_client_secret": "fake-client-secret",
    "session_secret": SECRET,
    "session_cookie_secure": False,
}
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def salt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IDENTITY_SALT", "fake-salt-for-tests")


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def make(store: InMemoryStore, **kw: Any) -> httpx.AsyncClient:
    app = create_app(settings(**{**CFG, **kw}), store)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api.test")


@pytest.fixture
async def client(store: InMemoryStore, mock: respx.MockRouter) -> AsyncIterator[httpx.AsyncClient]:
    async with make(store) as c:
        yield c


async def sign_in(client: httpx.AsyncClient, mock: respx.MockRouter, login: str = "octo") -> None:
    """Run the whole login and callback dance; the client keeps the session cookie."""
    r = await client.get("/auth/github/login")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    mock.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "gho_fake"}))
    mock.get(USER_URL).mock(
        return_value=httpx.Response(
            200, json={"id": 42, "login": login, "avatar_url": "https://a.test/x.png"}
        )
    )
    cb = await client.get(f"/auth/github/callback?code=abc&state={state}")
    assert cb.status_code == 302


async def test_login_redirects_to_github_with_state_cookie(client: httpx.AsyncClient) -> None:
    r = await client.get("/auth/github/login")
    assert r.status_code == 302
    url = urlparse(r.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://github.com/login/oauth/authorize"
    q = parse_qs(url.query)
    assert q["client_id"] == ["fake-client-id"]
    assert q["redirect_uri"] == ["http://localhost:8000/auth/github/callback"]
    cookie = r.headers["set-cookie"]
    assert "piq_oauth_state=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Secure" not in cookie
    saved = verify_payload(client.cookies.get("piq_oauth_state"), SECRET)
    assert saved is not None and saved["state"] == q["state"][0]


async def test_callback_sets_session_and_me_works(
    client: httpx.AsyncClient, mock: respx.MockRouter
) -> None:
    await sign_in(client, mock)
    me = await client.get("/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["login"] == "octo" and body["avatar_url"] == "https://a.test/x.png"
    assert len(body["user_hash"]) == 64
    session = verify_payload(client.cookies.get("piq_session"), SECRET)
    assert session is not None and session["github_id"] == 42
    assert session["exp"] > time.time() + 6 * 86400
    # the GitHub user token is never put in the cookie
    assert "gho_fake" not in (client.cookies.get("piq_session") or "")


async def test_callback_redirects_to_dashboard_and_cookie_flags(
    store: InMemoryStore, mock: respx.MockRouter
) -> None:
    async with make(store, session_cookie_secure=True, dashboard_url="https://dash.test") as c:
        r = await c.get("/auth/github/login")
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        mock.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
        mock.get(USER_URL).mock(return_value=httpx.Response(200, json={"id": 1, "login": "a"}))
        # a Secure cookie is not sent back over plain http, so set it by hand
        c.cookies.set("piq_oauth_state", sign_payload({"typ": "state", "state": state}, SECRET))
        cb = await c.get(f"/auth/github/callback?code=x&state={state}")
    assert cb.headers["location"] == "https://dash.test"
    session_header = next(h for h in cb.headers.get_list("set-cookie") if "piq_session" in h)
    assert "HttpOnly" in session_header and "Secure" in session_header
    assert "SameSite=lax" in session_header


async def test_callback_bad_state_is_400(client: httpx.AsyncClient, mock: respx.MockRouter) -> None:
    await client.get("/auth/github/login")
    r = await client.get("/auth/github/callback?code=abc&state=wrong")
    assert r.status_code == 400
    assert not mock.calls  # GitHub is never called
    async with make(InMemoryStore()) as fresh:
        assert (await fresh.get("/auth/github/callback?code=a&state=b")).status_code == 400


async def test_callback_rejected_code_is_400(
    client: httpx.AsyncClient, mock: respx.MockRouter
) -> None:
    r = await client.get("/auth/github/login")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"error": "bad_verification_code"})
    )
    r = await client.get(f"/auth/github/callback?code=abc&state={state}")
    assert r.status_code == 400 and "piq_session" not in client.cookies


@pytest.mark.parametrize("missing", ["github_client_id", "github_client_secret", "session_secret"])
async def test_sign_in_is_503_without_config(store: InMemoryStore, missing: str) -> None:
    async with make(store, **{missing: ""}) as c:
        assert (await c.get("/auth/github/login")).status_code == 503
        assert (await c.get("/auth/github/callback?code=a&state=b")).status_code == 503


async def test_me_needs_a_valid_session(client: httpx.AsyncClient) -> None:
    assert (await client.get("/auth/me")).status_code == 401
    client.cookies.set("piq_session", "garbage")
    assert (await client.get("/auth/me")).status_code == 401
    forged = sign_payload({"typ": "session", "login": "x"}, "other-secret")
    client.cookies.set("piq_session", forged)
    assert (await client.get("/auth/me")).status_code == 401
    expired = sign_payload(
        {"typ": "session", "login": "x", "avatar_url": "", "user_hash": "0" * 64, "exp": 1},
        SECRET,
    )
    client.cookies.set("piq_session", expired)
    assert (await client.get("/auth/me")).status_code == 401


async def test_state_cookie_is_not_a_session(client: httpx.AsyncClient) -> None:
    await client.get("/auth/github/login")
    client.cookies.set("piq_session", client.cookies["piq_oauth_state"])
    assert (await client.get("/auth/me")).status_code == 401


async def test_logout_clears_cookie(client: httpx.AsyncClient, mock: respx.MockRouter) -> None:
    await sign_in(client, mock)
    r = await client.post("/auth/logout")
    assert r.status_code == 204
    assert "piq_session=" in r.headers["set-cookie"] and "Max-Age=0" in r.headers["set-cookie"]
    assert (await client.get("/auth/me")).status_code == 401


# ------------------------------------------------------------------ self-service keys


async def test_keys_need_a_session(client: httpx.AsyncClient) -> None:
    assert (await client.get("/me/api-keys")).status_code == 401
    assert (await client.post("/me/api-keys")).status_code == 401
    assert (await client.delete(f"/me/api-keys/{uuid4()}")).status_code == 401


async def test_issue_list_and_revoke_own_key(
    client: httpx.AsyncClient, store: InMemoryStore, mock: respx.MockRouter
) -> None:
    await sign_in(client, mock)
    user_hash = (await client.get("/auth/me")).json()["user_hash"]
    r = await client.post("/me/api-keys")
    assert r.status_code == 201
    issued = r.json()
    assert set(issued) == {"id", "key", "created_at"}
    rec = next(iter(store.keys.values()))
    assert rec.user_hash == user_hash and rec.key_hash == hash_key(issued["key"])

    listing = (await client.get("/me/api-keys")).json()
    assert [set(k) for k in listing] == [{"id", "created_at", "revoked_at"}]
    assert listing[0]["id"] == issued["id"] and listing[0]["revoked_at"] is None
    assert issued["key"] not in str(listing)

    assert (await client.delete(f"/me/api-keys/{issued['id']}")).status_code == 204
    assert (await client.get("/me/api-keys")).json()[0]["revoked_at"] is not None


async def test_other_users_key_is_404_and_not_listed(
    client: httpx.AsyncClient, store: InMemoryStore, mock: respx.MockRouter
) -> None:
    other = store.add_key("f" * 64, hash_key("piq_other"))
    await sign_in(client, mock)
    assert (await client.get("/me/api-keys")).json() == []
    assert (await client.delete(f"/me/api-keys/{other.id}")).status_code == 404
    assert (await client.delete(f"/me/api-keys/{uuid4()}")).status_code == 404
    assert store.keys[other.id].revoked_at is None


# ------------------------------------------------------------------------ failure list


async def test_failure_list_shape_order_and_filters(
    client: httpx.AsyncClient, store: InMemoryStore
) -> None:
    older = routed_failure(run_id=1)
    newer = routed_failure(run_id=2, sent_to=[Expert.INFRA])
    other_repo = routed_failure(run_id=3)
    object.__setattr__(older, "routed_at", newer.routed_at.replace(year=2020))
    from shared.contracts import RepoRef

    object.__setattr__(other_repo, "repo", RepoRef(owner="acme", name="other"))
    replayed = routed_failure(run_id=4, replay=True)
    for f in (older, newer, other_repo, replayed):
        store.save_routed_failure(f)
    diag = error_diagnosis(newer.failure_id, Expert.INFRA)
    store.save_diagnosis(diag)
    store.save_review(
        ReviewDecision(
            diagnosis_id=diag.diagnosis_id,
            decision="accept",
            reviewer_hash="1" * 64,
            cause_confirmed=True,
        )
    )

    items = (await client.get("/failures")).json()
    assert [i["run_id"] for i in items if i["repo"] == "acme/widgets"] == [2, 1]
    assert 4 not in [i["run_id"] for i in items]  # replay hidden
    top = next(i for i in items if i["run_id"] == 2)
    assert set(top) == {
        "failure_id", "repo", "head_sha", "run_id", "job_name", "expert",
        "created_at", "diagnosis_count", "latest_review",
    }  # fmt: skip
    assert top["expert"] == "infra" and top["diagnosis_count"] == 1
    assert top["latest_review"] == "accept"
    plain = next(i for i in items if i["run_id"] == 1)
    assert plain["diagnosis_count"] == 0 and plain["latest_review"] is None

    only = (await client.get("/failures?repo=acme/other")).json()
    assert [i["run_id"] for i in only] == [3]
    assert len((await client.get("/failures?limit=1")).json()) == 1
    assert len((await client.get("/failures?limit=0")).json()) == 1  # clamped to 1
