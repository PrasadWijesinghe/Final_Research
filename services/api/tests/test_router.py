"""Router: 20 fixture failures, margin, dedupe, unknown, and enqueueing on the right queues."""

from __future__ import annotations

import json
import re
from typing import Any

import fakeredis
import pytest
from api.failure_queue import JOB_FUNCTIONS, enqueue_failure, queue_name, redis_from_env
from api.router import (
    DEFAULT_RULES_PATH,
    ExpertRules,
    FailedJob,
    RouterConfigError,
    RouterRules,
    Rule,
    decide,
    load_rules,
    route_failed_job,
    route_job,
)
from api_helpers import FIXTURES, routed_failure
from rq import Queue

from shared.contracts import Expert, FailureCategory, RepoRef, RoutedFailure
from shared.contracts.routed_failure import FailedStep

CASES: list[dict[str, Any]] = json.loads((FIXTURES / "router_failures.json").read_text("utf-8"))[
    "cases"
]
RULES = load_rules()


def job_from(case: dict[str, Any], **kw: Any) -> FailedJob:
    """A FailedJob built from a fixture case."""
    base: dict[str, Any] = {
        "repo": RepoRef(owner="acme", name="app"),
        "head_sha": "a" * 40,
        "run_id": 1,
        "job_id": 2,
        "job_name": "ci",
        "workflow_path": ".github/workflows/ci.yml",
        "failed_steps": [
            FailedStep(number=i + 1, name=n, conclusion="failure")
            for i, n in enumerate(case["steps"])
        ],
        "log_ref": "logs/acme-app/1/2.log.gz",
        "log_excerpt": case["log"],
    }
    return FailedJob(**{**base, **kw})


def test_fixture_set_has_five_per_category() -> None:
    sizes = {c: 0 for c in ("dependency", "build", "test", "infra")}
    for case in CASES:
        if case["id"].split("-")[0] in ("dep", "build", "test", "infra"):
            sizes[case["expected_category"]] += 1
    assert sizes == {"dependency": 5, "build": 5, "test": 5, "infra": 5}


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_fixture_failures_route_correctly(case: dict[str, Any]) -> None:
    result = route_job(RULES, job_from(case))
    assert result.decision.category.value == case["expected_category"]
    assert [e.value for e in result.decision.experts] == case["expected_experts"]
    if case["expected_experts"]:
        assert result.failure is not None
        assert [e.value for e in result.failure.sent_to] == case["expected_experts"]
        assert result.failure.predicted_category.value == case["expected_category"]
        assert 0.0 < result.failure.routing_confidence <= 1.0
    else:
        assert result.failure is None


def test_single_category_cases_go_to_exactly_one_expert() -> None:
    for case in CASES:
        if case["id"].split("-")[0] in ("dep", "build", "test", "infra"):
            assert len(route_job(RULES, job_from(case)).decision.experts) == 1, case["id"]


# ------------------------------------------------------------------ margin, dedupe, unknown


def rules_with(margin: float = 0.25, min_score: float = 1.0) -> RouterRules:
    """Tiny rule set: a=TEST scores 4 on 'alpha'; b=BUILD scores 3 on 'beta'; INFRA 1 on 'gamma'."""

    def one(word: str, weight: float) -> ExpertRules:
        return ExpertRules(log=(Rule(re.compile(word), weight),))

    return RouterRules(
        two_expert_margin=margin,
        min_score=min_score,
        experts={
            Expert.TEST: one("alpha", 4.0),
            Expert.BUILD: one("beta", 3.0),
            Expert.INFRA: one("gamma", 1.0),
        },
    )


def test_margin_is_relative_to_the_winner() -> None:
    log = "alpha\nbeta"  # 4 vs 3: (4-3)/4 = 0.25
    assert decide(rules_with(margin=0.25), [], log).experts == [Expert.TEST, Expert.BUILD]
    assert decide(rules_with(margin=0.24), [], log).experts == [Expert.TEST]


def test_never_more_than_two_experts_and_never_the_same_twice() -> None:
    d = decide(rules_with(margin=1.0), [], "alpha\nbeta\ngamma")  # all three within margin
    assert d.experts == [Expert.TEST, Expert.BUILD]
    assert len(set(d.experts)) == len(d.experts)


def test_runner_up_below_min_score_is_not_added() -> None:
    d = decide(rules_with(margin=1.0, min_score=2.0), [], "alpha\ngamma")  # gamma = 1 < 2
    assert d.experts == [Expert.TEST]


def test_each_rule_counts_once_however_many_lines_match() -> None:
    d = decide(rules_with(), [], "alpha\nalpha\nalpha")
    assert d.scores[Expert.TEST] == 4.0


def test_step_name_rules_use_the_failed_step_names() -> None:
    rules = RouterRules(
        0.25, 1.0, {Expert.TEST: ExpertRules(step_name=(Rule(re.compile("(?i)tests"), 2.0),))}
    )
    assert decide(rules, ["Run tests"], "").experts == [Expert.TEST]
    assert decide(rules, ["Lint"], "tests in the log do not count").experts == []


def test_below_min_score_is_unknown_and_sent_nowhere() -> None:
    d = decide(rules_with(min_score=5.0), [], "alpha")  # 4 < 5
    assert d.category is FailureCategory.UNKNOWN
    assert d.experts == []
    assert d.confidence == 0.0


def test_shipped_rules_load_with_expected_settings() -> None:
    assert DEFAULT_RULES_PATH.is_file()
    assert RULES.two_expert_margin == 0.25
    assert set(RULES.experts) == set(Expert)


def test_bad_rules_file_raises_clear_error(tmp_path: Any) -> None:
    bad = tmp_path / "r.yaml"
    bad.write_text(
        "two_expert_margin: 0.2\nmin_score: 1\nexperts:\n  test: {log: [{pattern: '('}]}\n"
    )
    with pytest.raises(RouterConfigError):
        load_rules(bad)
    with pytest.raises(RouterConfigError):
        load_rules(tmp_path / "missing.yaml")


# ---------------------------------------------------------------------------------- queue


def queued(connection: Any, expert: Expert) -> list[Any]:
    """Jobs waiting on an expert's queue."""
    return Queue(queue_name(expert), connection=connection).jobs


def test_queue_names_match_expert_workers() -> None:
    assert queue_name(Expert.TEST) == "diagnose:test"
    assert {queue_name(e) for e in Expert} == {
        "diagnose:dependency",
        "diagnose:build",
        "diagnose:test",
        "diagnose:infra",
    }


def test_enqueue_goes_to_the_right_queue_with_a_valid_payload() -> None:
    conn = fakeredis.FakeRedis()
    failure = routed_failure(sent_to=[Expert.TEST])
    assert enqueue_failure(conn, failure) == ["diagnose:test"]
    jobs = queued(conn, Expert.TEST)
    assert len(jobs) == 1
    assert jobs[0].func_name == JOB_FUNCTIONS[Expert.TEST]
    assert JOB_FUNCTIONS[Expert.TEST] == "testing_expert.worker.process_failure"
    assert RoutedFailure.model_validate(jobs[0].args[0]) == failure
    for other in (Expert.DEPENDENCY, Expert.BUILD, Expert.INFRA):
        assert queued(conn, other) == []


def test_two_experts_get_one_job_each_and_a_repeat_adds_nothing() -> None:
    conn = fakeredis.FakeRedis()
    failure = routed_failure(sent_to=[Expert.BUILD, Expert.DEPENDENCY])
    assert enqueue_failure(conn, failure) == ["diagnose:build", "diagnose:dependency"]
    enqueue_failure(conn, failure)  # same failure again
    assert len(queued(conn, Expert.BUILD)) == 1
    assert len(queued(conn, Expert.DEPENDENCY)) == 1


def test_worker_job_functions_exist_in_expert_folders() -> None:
    """The dotted paths point at real worker modules (checked as files, no import)."""
    from pathlib import Path

    experts_dir = Path(__file__).resolve().parents[3] / "experts"
    for path in JOB_FUNCTIONS.values():
        module = path.rsplit(".", 1)[0].replace(".", "/")
        assert any(experts_dir.glob(f"*/src/{module}.py")), path


def test_redis_from_env_uses_redis_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://redis.test:6380/3")
    kwargs = redis_from_env().connection_pool.connection_kwargs
    assert (kwargs["host"], kwargs["port"], kwargs["db"]) == ("redis.test", 6380, 3)


def test_route_failed_job_stores_then_enqueues() -> None:
    from api.store import InMemoryStore

    store, conn = InMemoryStore(), fakeredis.FakeRedis()
    case = next(c for c in CASES if c["id"] == "close-1-build-vs-dependency")
    result = route_failed_job(job_from(case, replay=True), store, conn, RULES)
    assert result.failure is not None
    assert store.failures[result.failure.failure_id].replay is True
    assert len(queued(conn, Expert.BUILD)) == 1
    assert len(queued(conn, Expert.DEPENDENCY)) == 1


def test_unknown_failure_is_neither_stored_nor_enqueued() -> None:
    from api.store import InMemoryStore

    store, conn = InMemoryStore(), fakeredis.FakeRedis()
    case = next(c for c in CASES if c["id"] == "unknown-1")
    result = route_failed_job(job_from(case), store, conn, RULES)
    assert result.failure is None
    assert store.failures == {}
    assert all(queued(conn, e) == [] for e in Expert)
