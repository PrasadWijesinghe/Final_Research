"""Tests for the knowledge base (P4.10): tags, mining helpers, retrieval filters, wiring.

Unit tests need nothing. The `integration` tests use a scratch copy of `testing.kb_cases`
(`TEST_KB_TABLE`), created and dropped by the fixture, and the offline fake embedder: the real
model is never loaded here.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from testing_expert import diagnose as diag
from testing_expert.kb import retrieve, settings, store
from testing_expert.kb.evaluate import eligible, random_hit_rate, relevant
from testing_expert.kb.mine import (
    CaseRow,
    changed_lines,
    commit_headline,
    excerpt,
    fix_summary,
    pick_files,
)
from testing_expert.kb.retrieve import Hit, Query, or_query, search
from testing_expert.kb.settings import load_settings
from testing_expert.kb.tags import FixFacts, error_families, tag_case

from shared.contracts import Expert, RepoRef, VerifiedCase
from shared.embeddings import FakeEmbedder

CFG = load_settings()


# --- tags ---------------------------------------------------------------------------------------


def test_config_has_every_root_cause_of_the_task() -> None:
    names = {r.name for r in CFG.root_cause_rules}
    assert names == {
        "async_wait", "concurrency", "timeout", "test_order_dependency", "network",
        "time_date", "randomness", "resource_leak", "platform",
    }  # fmt: skip


def test_family_precedence_prefers_the_specific_family() -> None:
    fams, primary = error_families("Exceeded timeout of 5000 ms\nexpected 1 to equal 2")
    assert {"timeout", "assertion"} <= set(fams)
    assert primary == "timeout"
    assert error_families("nothing special")[1] == "other"


def test_flaky_timeout_case_tags() -> None:
    t = tag_case("flaky", "Error: Exceeded timeout of 5000 ms for a hook", "", None, CFG)
    assert t.primary_family == "timeout"
    assert {"kind:flaky", "fam:timeout", "rc:timeout", "fix:rerun"} <= set(t.tags)


def test_runner_noise_in_the_log_tail_does_not_tag_a_platform_cause() -> None:
    tail = "Post job cleanup on Windows runner\nmacOS 14 darwin"
    t = tag_case("flaky", "expected 1 to equal 2", tail, None, CFG)
    assert "rc:platform" not in t.tags and t.root_causes == ("unspecified",)


def test_oom_in_the_tail_still_implies_resource_leak() -> None:
    t = tag_case("flaky", "", "FATAL ERROR: JavaScript heap out of memory", None, CFG)
    assert "rc:resource_leak" in t.tags and "fam:oom" in t.tags


def test_fix_tags_from_a_small_diff() -> None:
    fix = FixFacts(
        added=(
            "    await expect(run()).resolves.toEqual(1);",
            "  afterEach(() => jest.restoreAllMocks());",
        ),
        removed=("    expect(run()).toBe(1);", "  // old"),
        paths=("test/a.test.ts",),
        has_diff=True,
    )
    t = tag_case("genuine", "AssertionError: boom", "", fix, CFG)
    assert {"fix:change_assertion", "fix:reset_variable", "fix:test_only"} <= set(t.tags)
    assert "rc:test_order_dependency" in t.tags


def test_await_is_only_a_fix_when_added_to_an_existing_statement() -> None:
    gained = FixFacts(("  await save(x);",), ("  save(x);",), ("test/a.test.ts",), True)
    new_test = FixFacts(("  await save(x);",), (), ("test/a.test.ts",), True)
    assert "fix:add_await" in tag_case("genuine", "x", "", gained, CFG).tags
    assert "rc:async_wait" in tag_case("genuine", "x", "", gained, CFG).tags
    assert "fix:add_await" not in tag_case("genuine", "x", "", new_test, CFG).tags


def test_big_diffs_only_get_scope_tags() -> None:
    lines = tuple(f"  await thing{i}.close();" for i in range(CFG.tag_diff_max_lines + 5))
    fix = FixFacts(lines, (), ("test/a.test.ts", "src/a.ts"), True)
    t = tag_case("genuine", "AssertionError", "", fix, CFG)
    assert t.fixes == ("source_change", "misc")
    assert t.root_causes == ("unspecified",)


def test_no_diff_genuine_case_is_misc() -> None:
    assert tag_case("genuine", "AssertionError", "", FixFacts(), CFG).fixes == ("misc",)


# --- mining helpers -----------------------------------------------------------------------------

PATCH = (
    "@@ -10,4 +10,5 @@ describe('a', () => {\n"
    " context line\n"
    "-    expect(x).toBe(1);\n"
    "+    expect(x).toEqual(1);\n"
    "+    await settle();\n"
    " tail\n"
)
FILES: dict[str, tuple[str | None, int, int]] = {
    "test/a.test.ts": (PATCH, 2, 1),
    "src/a.ts": ("@@ -1 +1 @@\n-a\n+b\n", 1, 1),
    "README.md": ("@@ -1 +1 @@\n-a\n+b\n", 1, 1),
}


def test_changed_lines_skips_headers_and_context() -> None:
    added, removed = changed_lines(PATCH, 10_000)
    assert added == ["    expect(x).toEqual(1);", "    await settle();"]
    assert removed == ["    expect(x).toBe(1);"]
    assert changed_lines(PATCH, 10) == ([], [])  # over the size limit: not scanned
    assert changed_lines(None, 10) == ([], [])


def test_pick_files_prefers_the_failing_test_then_stack_files() -> None:
    assert pick_files(FILES, ["a.test.ts"], []) == ["test/a.test.ts"]
    assert pick_files(FILES, [], ["a.ts"]) == ["src/a.ts"]
    assert pick_files(FILES, [], []) == [
        "test/a.test.ts",
        "src/a.ts",
    ]  # fallback: largest first, no docs


def test_commit_headline_drops_trailers_and_emails() -> None:
    msg = "fix: stop the race (#12)\nmore words\n\nbody para\n\nCo-authored-by: A <a@b.co>"
    assert commit_headline(msg, CFG) == "fix: stop the race (#12) more words"
    assert "@" not in commit_headline("thanks bob@example.com for this", CFG)
    assert commit_headline(None, CFG) == ""


def test_fix_summary_is_deterministic_and_bounded() -> None:
    args = ("a" * 40, "fix: x\n\nbody", FILES, ["test/a.test.ts"], CFG)
    s1, s2 = fix_summary(*args), fix_summary(*args)
    assert s1 == s2 and len(s1) <= 1000
    assert "Fix commit aaaaaaa" in s1 and "test/a.test.ts (+2 -1)" in s1
    assert "Removed: `expect(x).toBe(1);`" in s1 and "Added: `expect(x).toEqual(1);`" in s1


def test_excerpt_is_bounded_and_keeps_changed_lines_only() -> None:
    out = excerpt(FILES, ["test/a.test.ts", "src/a.ts"], CFG)
    assert "--- test/a.test.ts" in out and "+    await settle();" in out
    assert "context line" not in out
    assert (
        len(excerpt({"f": ("@@ -1 +1 @@\n" + "+x\n" * 5000, 1, 1)}, ["f"], CFG))
        <= CFG.excerpt_chars
    )


# --- retrieval pieces (no database) ------------------------------------------------------------


def test_or_query_drops_template_words_and_caps_length() -> None:
    q = or_query("error family: timeout\ntest: Foo bar <dur>", 40)
    assert q == "timeout | foo | bar | dur"
    assert len(or_query(" ".join(f"w{i}" for i in range(100)), 10).split(" | ")) == 10
    assert or_query("!!! ??", 10) == ""  # nothing searchable


def test_random_baseline_matches_hand_computation() -> None:
    assert random_hit_rate(10, 0) == 0.0
    assert random_hit_rate(10, 10) == 1.0
    assert random_hit_rate(10, 1, k=5) == pytest.approx(0.5)  # 5 of 10 picked
    assert random_hit_rate(3, 1, k=5) == 1.0  # fewer cases than k: all are picked


def _info(**kw: Any) -> Any:
    from testing_expert.kb.evaluate import CaseInfo

    base: dict[str, Any] = dict(
        case_id="c", kind="genuine", repo="o/r", family="timeout", tests=frozenset({"t1"}),
        fixes=frozenset({"fix:add_wait"}), failed_at=datetime(2026, 1, 10, tzinfo=UTC),
        verified_at=datetime(2026, 1, 11, tzinfo=UTC), run_id=1, sha="s1", text="",
    )  # fmt: skip
    return CaseInfo(**{**base, **kw})


def test_relevance_and_eligibility_rules() -> None:
    q = _info()
    assert relevant(q, _info(case_id="d", family="timeout"), "same_family")
    assert not relevant(q, _info(case_id="d", repo="o/x"), "same_repo")
    assert relevant(q, _info(case_id="d", tests=frozenset({"t1", "t2"})), "same_test")
    assert not relevant(q, _info(case_id="d", fixes=frozenset()), "same_fix")
    pool = [
        _info(case_id="self", run_id=1),  # same run
        _info(case_id="sha", run_id=2, sha="s1"),  # same commit
        _info(case_id="future", run_id=3, sha="s3", verified_at=datetime(2026, 2, 1, tzinfo=UTC)),
        _info(case_id="ok", run_id=4, sha="s4"),
    ]
    # `ok` was verified a day AFTER q failed (Jan 11 vs Jan 10), so it is not eligible either.
    assert eligible(q, pool) == []
    earlier = _info(
        case_id="early", run_id=5, sha="s5", verified_at=datetime(2026, 1, 9, tzinfo=UTC)
    )
    assert eligible(q, [*pool, earlier]) == [earlier]


# --- database tests (scratch table, fake embedder) --------------------------------------------

T0 = datetime(2026, 3, 1, tzinfo=UTC)
SCRATCH = "testing.kb_cases_p410_test"


@pytest.fixture
def scratch(monkeypatch: pytest.MonkeyPatch) -> Iterator[psycopg.Connection[Any]]:
    monkeypatch.setenv(settings.TABLE_ENV, SCRATCH)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    retrieve.query_embedder.cache_clear()
    url = os.environ["DATABASE_URL"].replace("+psycopg", "")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"drop table if exists {SCRATCH}")
        conn.execute(f"create table {SCRATCH} (like testing.kb_cases including all)")
        try:
            yield conn
        finally:
            conn.execute(f"drop table if exists {SCRATCH}")
    retrieve.query_embedder.cache_clear()


def _row(
    n: int, text: str, *, run: int, sha: str, verified: datetime, licence: str = "MIT",
    kind: str = "genuine",
) -> CaseRow:  # fmt: skip
    from testing_expert.kb.tags import CaseTags

    case = VerifiedCase(
        case_id=f"case-{n}", expert=Expert.TEST, origin="mined", repo=RepoRef(owner="o", name="r"),
        repo_licence=licence, failure_summary=f"failure {n}", log_signature=text, root_cause="rc",
        fix_summary=f"fix {n}", fix_commit_sha="a" * 40, verifying_run_id=99,
        verifying_run_passed=True, tags=[f"kind:{kind}"], verified_at=verified,
    )  # fmt: skip
    return CaseRow(
        case=case, kind=kind, repo="o/r", licence=licence, failed_job_id=n, failed_run_id=run,
        failed_sha=sha, failed_at=verified - timedelta(hours=1), verified_at=verified,
        fix_sha="a" * 40, error_family="timeout", failing_tests=("t",), search_text=text,
        diff_excerpt="", links={"fix_commit": f"https://x/{n}", "verifying_run": f"https://y/{n}"},
        mine_rule="fix_commit_touches_file",
        tags=CaseTags(kind, ("timeout",), "timeout", ("timeout",), ()),
    )  # fmt: skip


def _seed(conn: psycopg.Connection[Any]) -> None:
    rows = [
        _row(1, "timeout exceeded redis hook socket", run=1, sha="s1", verified=T0),
        _row(
            2,
            "timeout exceeded redis hook socket",
            run=2,
            sha="s2",
            verified=T0 + timedelta(days=1),
        ),
        _row(
            3,
            "snapshot mismatch in render output",
            run=3,
            sha="s3",
            verified=T0 + timedelta(days=2),
        ),
        _row(
            4, "timeout exceeded redis hook", run=4, sha="s4", verified=T0, licence="GPL-3.0-only"
        ),
        _row(5, "timeout exceeded redis hook", run=5, sha="s5", verified=T0 + timedelta(days=9)),
    ]
    store.upsert_cases(conn, rows)
    assert store.pending_count(conn) == 5
    done = store.embed_pending(conn, FakeEmbedder(), allow_fake=True)
    assert done["embedded"] == 5 and store.pending_count(conn) == 0


@pytest.mark.integration
def test_scratch_search_filters(scratch: psycopg.Connection[Any]) -> None:
    _seed(scratch)
    emb = FakeEmbedder()
    q = "timeout exceeded redis hook"

    def ids(**kw: Any) -> list[str]:
        query = Query(q, **kw)
        return [h.case_id for h in search(scratch, query, k=5, embedder=emb)]

    everything = ids()
    assert len(everything) == 5
    assert everything[0] in {"case-1", "case-2", "case-4", "case-5"}  # best words match first
    # no future leakage: cases verified after the cutoff never come back
    early = ids(cutoff=T0 + timedelta(hours=1))
    assert set(early) <= {"case-1", "case-4"} and "case-2" not in early and "case-5" not in early
    # no self-retrieval: the query's own run and commit are left out
    assert "case-1" not in ids(run_id=1) and "case-2" not in ids(head_sha="s2")
    # licence hook: GPL cases are excluded only when asked
    assert "case-4" in everything
    permissive = [
        h.case_id for h in search(scratch, Query(q), k=5, embedder=emb, permissive_only=True)
    ]
    assert "case-4" not in permissive and "case-1" in permissive


@pytest.mark.integration
def test_modes_and_hit_fields(scratch: psycopg.Connection[Any]) -> None:
    _seed(scratch)
    emb = FakeEmbedder()
    q = Query("snapshot mismatch render")
    fts = search(scratch, q, mode="fts", embedder=emb)
    vec = search(scratch, q, mode="vector", embedder=emb)
    hyb = search(scratch, q, mode="hybrid", embedder=emb)
    assert fts[0].case_id == vec[0].case_id == hyb[0].case_id == "case-3"
    assert fts[0].vector_rank is None and vec[0].fts_rank is None
    top = hyb[0]
    assert top.fts_rank == 1 and top.vector_rank == 1 and 0.0 < top.similarity <= 1.0
    assert top.source_url == "https://x/3" and top.fix_summary == "fix 3"
    assert search(scratch, Query("!!!"), mode="fts", embedder=emb) == []  # nothing searchable


@pytest.mark.integration
def test_build_is_idempotent_and_changed_text_is_embedded_again(
    scratch: psycopg.Connection[Any],
) -> None:
    _seed(scratch)
    before = scratch.execute(
        f"select embedded_at from {SCRATCH} where case_id = 'case-1'"
    ).fetchone()
    store.upsert_cases(
        scratch, [_row(1, "timeout exceeded redis hook socket", run=1, sha="s1", verified=T0)]
    )
    assert store.pending_count(scratch) == 0  # same text keeps its embedding
    assert (
        scratch.execute(f"select embedded_at from {SCRATCH} where case_id = 'case-1'").fetchone()
        == before
    )
    assert scratch.execute(f"select count(*) from {SCRATCH}").fetchone() == (5,)
    store.upsert_cases(scratch, [_row(1, "a different text now", run=1, sha="s1", verified=T0)])
    assert store.pending_count(scratch) == 1  # changed text loses its vector
    store.embed_pending(scratch, FakeEmbedder(), allow_fake=True)
    assert store.pending_count(scratch) == 0


@pytest.mark.integration
def test_embedding_is_resumable_and_never_silently_fake(
    scratch: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store.upsert_cases(
        scratch, [_row(i, f"text number {i}", run=i, sha=f"s{i}", verified=T0) for i in range(5)]
    )
    small = KbSmall(2)
    assert store.embed_pending(scratch, FakeEmbedder(), small, allow_fake=True)["embedded"] == 5
    store.upsert_cases(scratch, [_row(7, "brand new text", run=7, sha="s7", verified=T0)])
    assert (
        store.embed_pending(scratch, FakeEmbedder(), allow_fake=True)["embedded"] == 1
    )  # only the new one
    store.upsert_cases(scratch, [_row(8, "another new text", run=8, sha="s8", verified=T0)])
    with pytest.raises(RuntimeError, match="stand-in model"):
        store.embed_pending(scratch)  # EMBEDDING_PROVIDER=fake without allow_fake


def KbSmall(batch: int) -> Any:
    """Settings with a small batch size, to prove several commits happen."""
    from dataclasses import replace

    return replace(CFG, embed_batch_size=batch)


# --- diagnose wiring ----------------------------------------------------------------------------


def _hit() -> Hit:
    return Hit(
        case_id="case-1", score=0.03, similarity=0.9, kind="genuine", repo="o/r",
        failure_summary="failure", fix_summary="fix", source_url="https://x/1", tags=(),
        verified_at=T0, fts_rank=1, vector_rank=1, case={},
    )  # fmt: skip


def test_hits_become_retrieved_cases_on_the_diagnosis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from test_triage_runtime import failure, tiny_bundle, use_bundle  # type: ignore[import-not-found]

    use_bundle(tiny_bundle(tmp_path / "v1", (0.9, 0.5)), monkeypatch)
    monkeypatch.setattr(diag, "retrieve_for_failure", lambda f, log: [_hit()])
    d = diag.diagnose(failure())
    assert [c.case_id for c in d.retrieved_cases] == ["case-1"]
    assert (
        d.retrieved_cases[0].fix_summary == "fix"
        and d.retrieved_cases[0].source_url == "https://x/1"
    )
    assert d.remediation is not None and d.remediation.cites_case_ids == [
        "case-1"
    ]  # template cites it (P4.11)


def test_retrieval_errors_never_break_the_diagnosis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from test_triage_runtime import failure, tiny_bundle, use_bundle  # type: ignore[import-not-found]

    use_bundle(tiny_bundle(tmp_path / "v1", (0.9, 0.5)), monkeypatch)

    def boom(f: Any, log: Any) -> list[Hit]:
        raise RuntimeError("db exploded")

    monkeypatch.setattr(diag, "retrieve_for_failure", boom)
    assert diag.diagnose(failure()).retrieved_cases == []


def test_retrieve_for_failure_degrades_without_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_triage_runtime import failure  # type: ignore[import-not-found]
    from testing_expert.kb.retrieve import retrieve_for_failure
    from testing_expert.triage.features import read_log

    monkeypatch.delenv("DATABASE_URL", raising=False)
    f = failure()
    ev = read_log(f.log_excerpt, "o/r")
    assert retrieve_for_failure(f, ev) == []
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    assert retrieve_for_failure(f, ev) == []  # unreachable database


@pytest.mark.integration
def test_retrieve_for_failure_end_to_end_on_scratch_table(
    scratch: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_triage_runtime import failure  # type: ignore[import-not-found]
    from testing_expert.kb.retrieve import retrieve_for_failure
    from testing_expert.triage.features import read_log

    f = failure()
    ev = read_log(f.log_excerpt, f"{f.repo.owner}/{f.repo.name}")
    assert retrieve_for_failure(f, ev) == []  # empty table: no cases, no error
    text = "\n".join(["error family: assertion", *(f"test: {t.full_name}" for t in ev.failures)])
    store.upsert_cases(
        scratch,
        [
            _row(
                1,
                text,
                run=f.run_id + 1000,
                sha="b" * 40,
                verified=datetime(2000, 1, 1, tzinfo=UTC),
            )
        ],
    )
    store.embed_pending(scratch, FakeEmbedder(), allow_fake=True)
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    hits = retrieve_for_failure(f, ev)
    assert [h.case_id for h in hits] == ["case-1"]
    # a case from the failure's own run is never returned
    store.upsert_cases(
        scratch,
        [_row(2, text, run=f.run_id, sha="c" * 40, verified=datetime(2000, 1, 1, tzinfo=UTC))],
    )
    store.embed_pending(scratch, FakeEmbedder(), allow_fake=True)
    assert "case-2" not in [h.case_id for h in retrieve_for_failure(f, ev)]


def test_tag_counts_are_stable_for_a_known_set() -> None:
    kinds = Counter(
        tag_case("flaky", t, "", None, CFG).primary_family for t in ("timed out", "ECONNRESET", "x")
    )
    assert kinds == Counter({"timeout": 1, "conn": 1, "other": 1})


@pytest.mark.integration
def test_health_count_reads_the_table(
    scratch: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retrieve, "_count_cache", (-1e9, 0))
    assert retrieve.count_cases() == 0
    store.upsert_cases(scratch, [_row(1, "some text", run=1, sha="s1", verified=T0)])
    monkeypatch.setattr(retrieve, "_count_cache", (-1e9, 0))  # skip the 30 second cache
    assert retrieve.count_cases() == 1


def test_health_count_is_zero_without_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(retrieve, "_count_cache", (-1e9, 0))
    assert retrieve.count_cases() == 0


# --- diverse retrieval (P4.11) --------------------------------------------------------------------


def _hit_of(case_id: str, repo: str = "o/r", fix: str | None = None) -> Hit:
    h = _hit()
    from dataclasses import replace

    return replace(h, case_id=case_id, repo=repo, case={"fix_commit_sha": fix} if fix else {})


def test_dedupe_keeps_the_best_case_per_fix_commit() -> None:
    from testing_expert.kb.retrieve import dedupe_by_fix_commit

    hits = [
        _hit_of("a1", fix="F1"), _hit_of("a2", fix="F1"), _hit_of("b1", fix="F2"),
        _hit_of("a3", fix="F1"), _hit_of("c1"), _hit_of("c2"), _hit_of("d1", "o/other", fix="F1"),
    ]  # fmt: skip
    kept = [h.case_id for h in dedupe_by_fix_commit(hits, 1, 5)]
    assert kept == ["a1", "b1", "c1", "c2", "d1"]  # no fix commit: its own group; repos apart
    assert [h.case_id for h in dedupe_by_fix_commit(hits, 2, 3)] == ["a1", "a2", "b1"]


def test_diagnose_retrieval_fetches_a_wider_pool_then_dedupes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from testing_expert.kb import retrieve as rt

    seen: list[int | None] = []
    pool = [_hit_of(f"x{i}", fix="SAME") for i in range(6)] + [_hit_of("y", fix="OTHER")]

    def fake(*args: Any) -> list[Hit]:
        seen.append(args[-1])
        return pool if args[-1] else pool[:5]

    monkeypatch.setattr(rt, "_retrieve", fake)
    got = rt.retrieve_for_failure(None, None)  # type: ignore[arg-type]
    assert [h.case_id for h in got] == ["x0", "y"] and seen == [25]
    plain = rt.retrieve_for_failure(None, None, diverse=False)  # type: ignore[arg-type]
    assert len(plain) == 5 and seen == [25, None]
