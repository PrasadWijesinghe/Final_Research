"""Backend API tests: experts are respx routes, the database is the in-memory store."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
import respx
from api.app import create_app
from api.auth import generate_key, hash_key, parse_bearer
from api.store import InMemoryStore
from api_helpers import (
    ADMIN_TOKEN,
    URLS,
    USER_HASH,
    error_diagnosis,
    mock_experts,
    payload_json,
    risk_for,
    routed_failure,
    settings,
)

from shared.contracts import Expert, ReviewDecision

pytestmark = pytest.mark.anyio

ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def anyio_backend() -> str:
    """Run async tests on asyncio only (anyio's pytest plugin ships with FastAPI)."""
    return "asyncio"


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    # respx only patches real network transports; the ASGI transport below is left alone.
    with respx.mock(assert_all_called=False) as router:
        yield router


def make_client(store: InMemoryStore, **kw: Any) -> httpx.AsyncClient:
    app = create_app(settings(**kw), store)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api.test")


@pytest.fixture
async def client(store: InMemoryStore, mock: respx.MockRouter) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client(store) as c:
        yield c


def issue_key(store: InMemoryStore) -> dict[str, str]:
    plain = generate_key()
    store.add_key(USER_HASH, hash_key(plain))
    return {"Authorization": f"Bearer {plain}"}


# ------------------------------------------------------------------------------ /predict


async def test_predict_returns_four_experts_within_budget(
    client: httpx.AsyncClient, store: InMemoryStore, mock: respx.MockRouter
) -> None:
    seen: list[httpx.Request] = []
    mock_experts(mock, seen=seen)
    started = time.monotonic()
    r = await client.post("/predict", json=payload_json(), headers=issue_key(store))
    assert r.status_code == 200, r.text
    body = r.json()
    assert time.monotonic() - started < 10
    assert body["budget_ms"] == 10_000 and body["elapsed_ms"] < 10_000
    assert [x["expert"] for x in body["results"]] == [e.value for e in Expert]
    assert all(x["state"] == "ready" and len(x["risks"]) == 1 for x in body["results"])
    # every expert got the context and the deadline header
    assert len(seen) == 4 and {s.headers["X-Deadline-Ms"] for s in seen} == {"10000"}
    # saved: one context, four predictions, built with the key owner's hash
    assert len(store.contexts) == 1 and len(store.predictions) == 4
    ctx = next(iter(store.contexts.values()))
    assert ctx.event == "pre_push" and ctx.author_hash == USER_HASH


async def test_slow_expert_is_not_ready_and_others_still_answer(
    store: InMemoryStore, mock: respx.MockRouter
) -> None:
    mock_experts(mock, slow={Expert.INFRA: 5.0})
    async with make_client(store, pre_push_budget_ms=300) as c:
        started = time.monotonic()
        r = await c.post("/predict", json=payload_json(), headers=issue_key(store))
    assert r.status_code == 200
    assert time.monotonic() - started < 3  # cut off at the budget, not after 5 s
    by_expert = {x["expert"]: x for x in r.json()["results"]}
    assert by_expert["infra"]["state"] == "not_ready" and by_expert["infra"]["reason"] == "timeout"
    assert by_expert["infra"]["risks"] == []
    assert all(by_expert[e]["state"] == "ready" for e in ("dependency", "build", "test"))
    assert len(store.predictions) == 3  # only ready results are saved


def _server_error(_req: httpx.Request) -> httpx.Response:
    return httpx.Response(500)


def _not_a_list(_req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"not": "a list"})


def _refused(_req: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("down")


@pytest.mark.parametrize(
    ("handler", "reason"),
    [
        (_server_error, "http_500"),
        (_not_a_list, "invalid_response"),
        (_refused, "unreachable"),
    ],
)
async def test_failing_expert_is_not_ready(
    client: httpx.AsyncClient,
    store: InMemoryStore,
    mock: respx.MockRouter,
    handler: Any,
    reason: str,
) -> None:
    mock_experts(mock, overrides={Expert.BUILD: handler})
    r = await client.post("/predict", json=payload_json(), headers=issue_key(store))
    build = next(x for x in r.json()["results"] if x["expert"] == "build")
    assert build["state"] == "not_ready" and build["reason"] == reason


async def test_expert_answering_for_another_context_is_rejected(
    client: httpx.AsyncClient, store: InMemoryStore, mock: respx.MockRouter
) -> None:
    def wrong_context(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[risk_for(Expert.TEST, {"context_id": str(uuid4())})])

    mock_experts(mock, overrides={Expert.TEST: wrong_context})
    r = await client.post("/predict", json=payload_json(), headers=issue_key(store))
    test = next(x for x in r.json()["results"] if x["expert"] == "test")
    assert test["reason"] == "invalid_response"


async def test_predict_rejects_bad_payload(client: httpx.AsyncClient, store: InMemoryStore) -> None:
    bad = {**payload_json(), "head_sha": "nope"}
    r = await client.post("/predict", json=bad, headers=issue_key(store))
    assert r.status_code == 422


async def test_fan_out_runs_in_parallel(store: InMemoryStore, mock: respx.MockRouter) -> None:
    mock_experts(mock, slow={e: 0.5 for e in Expert})
    async with make_client(store) as c:
        started = time.monotonic()
        r = await c.post("/predict", json=payload_json(), headers=issue_key(store))
    assert r.status_code == 200
    assert time.monotonic() - started < 1.8  # four 0.5 s calls in series would be 2 s


# ---------------------------------------------------------------------------------- auth


async def test_predict_needs_a_key(client: httpx.AsyncClient, store: InMemoryStore) -> None:
    assert (await client.post("/predict", json=payload_json())).status_code == 401
    wrong = {"Authorization": "Bearer piq_not-a-real-key"}
    assert (await client.post("/predict", json=payload_json(), headers=wrong)).status_code == 401
    basic = {"Authorization": "Basic abc"}
    assert (await client.post("/predict", json=payload_json(), headers=basic)).status_code == 401
    assert not store.contexts


async def test_issue_use_and_revoke_key(
    client: httpx.AsyncClient, store: InMemoryStore, mock: respx.MockRouter
) -> None:
    mock_experts(mock)
    r = await client.post("/api-keys", json={"user_hash": USER_HASH}, headers=ADMIN)
    assert r.status_code == 201
    issued = r.json()
    plain = issued["key"]
    # only the hash is stored, never the plain key
    rec = next(iter(store.keys.values()))
    assert rec.key_hash == hash_key(plain)
    assert plain not in repr(store.keys)

    headers = {"Authorization": f"Bearer {plain}"}
    assert (await client.post("/predict", json=payload_json(), headers=headers)).status_code == 200

    assert (await client.delete(f"/api-keys/{issued['id']}", headers=ADMIN)).status_code == 204
    assert (await client.post("/predict", json=payload_json(), headers=headers)).status_code == 401
    assert (await client.delete(f"/api-keys/{uuid4()}", headers=ADMIN)).status_code == 404


async def test_key_management_needs_the_admin_token(
    client: httpx.AsyncClient, store: InMemoryStore
) -> None:
    body = {"user_hash": USER_HASH}
    assert (await client.post("/api-keys", json=body)).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert (await client.post("/api-keys", json=body, headers=bad)).status_code == 401
    assert (await client.delete(f"/api-keys/{uuid4()}", headers=bad)).status_code == 401
    assert not store.keys


async def test_key_management_is_off_without_admin_token(store: InMemoryStore) -> None:
    async with make_client(store, admin_token="") as c:
        r = await c.post("/api-keys", json={"user_hash": USER_HASH}, headers=ADMIN)
    assert r.status_code == 503


def test_key_helpers() -> None:
    key = generate_key()
    assert key.startswith("piq_") and generate_key() != key
    assert hash_key(key) == hash_key(key) and len(hash_key(key)) == 64
    assert parse_bearer(f"Bearer {key}") == key and parse_bearer("bearer x") == "x"
    assert parse_bearer(None) is None and parse_bearer("Bearer ") is None
    assert parse_bearer("Token abc") is None


# --------------------------------------------------------- diagnoses, reviews, failures


async def test_diagnosis_shows_up_in_failure_view(
    client: httpx.AsyncClient, store: InMemoryStore
) -> None:
    failure = routed_failure()
    store.save_routed_failure(failure)
    diag = error_diagnosis(failure.failure_id)
    r = await client.post("/diagnoses", json=diag.model_dump(mode="json"))
    assert r.status_code == 201 and r.json()["diagnosis_id"] == str(diag.diagnosis_id)
    view = (await client.get(f"/failures/{failure.failure_id}")).json()
    assert view["failure"]["run_id"] == 1
    assert [d["diagnosis_id"] for d in view["diagnoses"]] == [str(diag.diagnosis_id)]
    assert (await client.get(f"/failures/{uuid4()}")).status_code == 404


async def test_diagnosis_with_invalid_contract_is_rejected(client: httpx.AsyncClient) -> None:
    r = await client.post("/diagnoses", json={"status": "diagnosed"})
    assert r.status_code == 422


async def test_review_is_saved_and_forwarded_to_the_diagnosing_expert(
    client: httpx.AsyncClient, store: InMemoryStore, mock: respx.MockRouter
) -> None:
    failure = routed_failure()
    store.save_routed_failure(failure)
    diag = error_diagnosis(failure.failure_id, Expert.TEST)
    store.save_diagnosis(diag)
    route = mock.post(f"{URLS[Expert.TEST]}/review").mock(return_value=httpx.Response(204))
    decision = ReviewDecision(
        diagnosis_id=diag.diagnosis_id,
        decision="accept",
        reviewer_hash=USER_HASH,
        cause_confirmed=True,
    )
    r = await client.post("/reviews", json=decision.model_dump(mode="json"))
    assert r.status_code == 201 and r.json()["forwarded"] is True
    assert route.called
    assert decision.decision_id in store.reviews
    view = (await client.get(f"/failures/{failure.failure_id}")).json()
    assert len(view["reviews"]) == 1


async def test_review_is_kept_when_expert_is_down(
    client: httpx.AsyncClient, store: InMemoryStore, mock: respx.MockRouter
) -> None:
    diag = error_diagnosis(uuid4(), Expert.BUILD)
    store.save_diagnosis(diag)
    mock.post(f"{URLS[Expert.BUILD]}/review").mock(side_effect=httpx.ConnectError("down"))
    decision = ReviewDecision(
        diagnosis_id=diag.diagnosis_id,
        decision="reject",
        reviewer_hash=USER_HASH,
        cause_confirmed=False,
    )
    r = await client.post("/reviews", json=decision.model_dump(mode="json"))
    assert r.status_code == 201 and r.json()["forwarded"] is False
    assert decision.decision_id in store.reviews


async def test_review_of_unknown_diagnosis_is_404(client: httpx.AsyncClient) -> None:
    decision = ReviewDecision(
        diagnosis_id=uuid4(), decision="accept", reviewer_hash=USER_HASH, cause_confirmed=True
    )
    assert (await client.post("/reviews", json=decision.model_dump(mode="json"))).status_code == 404


# ------------------------------------------------------------- risks, runs, health, 501s


async def test_commit_risks_and_runs_after_predict(
    client: httpx.AsyncClient, store: InMemoryStore, mock: respx.MockRouter
) -> None:
    mock_experts(mock)
    headers = issue_key(store)
    sha = payload_json()["head_sha"]
    await client.post("/predict", json=payload_json(), headers=headers)

    risks = (await client.get(f"/commits/{sha}/risks")).json()
    assert risks["head_sha"] == sha and len(risks["risks"]) == 4
    assert (await client.get(f"/commits/{'c' * 40}/risks")).json()["risks"] == []

    store.run_outcomes[("acme/widgets", sha)] = (99, "completed", "failure")
    runs = (await client.get("/runs", params={"repo": "acme/widgets"})).json()
    assert len(runs) == 1 and len(runs[0]["predictions"]) == 4
    assert runs[0]["run_id"] == 99 and runs[0]["run_conclusion"] == "failure"
    assert (await client.get("/runs", params={"repo": "other/repo"})).json() == []


async def test_replay_predictions_are_hidden_unless_asked(
    client: httpx.AsyncClient, store: InMemoryStore, mock: respx.MockRouter
) -> None:
    mock_experts(mock)
    await client.post("/predict", json=payload_json(), headers=issue_key(store))
    for pid, (risk, _) in list(store.predictions.items()):
        store.predictions[pid] = (risk, True)  # mark everything stored so far as replay
    sha = payload_json()["head_sha"]
    assert (await client.get(f"/commits/{sha}/risks")).json()["risks"] == []
    shown = await client.get(f"/commits/{sha}/risks", params={"include_replay": "true"})
    assert len(shown.json()["risks"]) == 4


async def test_health_reports_each_expert(
    client: httpx.AsyncClient, mock: respx.MockRouter
) -> None:
    for e in Expert:
        if e is Expert.INFRA:
            mock.get(f"{URLS[e]}/health").mock(side_effect=httpx.ConnectError("down"))
        else:
            mock.get(f"{URLS[e]}/health").mock(
                return_value=httpx.Response(
                    200, json={"status": "ok", "model_version": "stub", "kb_cases": 0}
                )
            )
    body = (await client.get("/health")).json()
    assert body["status"] == "degraded"
    assert body["experts"]["infra"]["status"] == "unreachable"
    assert body["experts"]["test"]["detail"]["model_version"] == "stub"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/webhooks/github"),
        ("GET", "/auth/github/login"),
        ("GET", "/auth/github/callback"),
    ],
)
async def test_github_endpoints_are_501(client: httpx.AsyncClient, method: str, path: str) -> None:
    assert (await client.request(method, path)).status_code == 501
