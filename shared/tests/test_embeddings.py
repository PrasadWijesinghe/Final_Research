"""Tests for shared.embeddings (no model download; SQL path is integration-marked)."""

from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy import create_engine, text

from shared.embeddings import (
    EMBEDDING_DIM,
    Collection,
    DimensionMismatchError,
    FakeEmbedder,
    GraniteEmbedder,
    check_dimension,
    get_embedder,
    normalise_log,
    quote_identifier,
    reciprocal_rank_fusion,
)

NORMALISE_CASES = [
    ("\x1b[31mError\x1b[0m: boom", "Error: boom"),
    ("2026-10-21T10:11:12.345Z npm ERR!", "<TS> npm ERR!"),
    ("2026-10-21 10:11:12 failed", "<TS> failed"),
    ("at 10:11:12 done", "at <TS> done"),
    ("commit 3f9a1c2b7d4e done", "commit <HEX> done"),
    ("ptr 0xdeadBEEF", "ptr <HEX>"),
    ("id 123e4567-e89b-12d3-a456-426614174000", "id <ID>"),
    ("open /home/runner/work/app/src/a.js failed", "open <PATH> failed"),
    ("open C:\\Users\\me\\app\\a.js failed", "open <PATH> failed"),
    ("see ./src/lib/x.ts now", "see <PATH> now"),
    ("deadline decade", "deadline decade"),  # hex-looking words without digits stay
    ("a   b\n\n  c  ", "a b\nc"),
]


@pytest.mark.parametrize(("raw", "expected"), NORMALISE_CASES)
def test_normalise_log(raw: str, expected: str) -> None:
    assert normalise_log(raw) == expected


def test_normalise_same_error_different_run_is_equal() -> None:
    a = "2026-01-01T00:00:00Z /home/runner/work/a/b.js sha 1a2b3c4d5e6f"
    b = "2026-02-02T09:09:09Z /home/runner/work/x/y.js sha 9f8e7d6c5b4a"
    assert normalise_log(a) == normalise_log(b)


def test_rrf_scores_and_order() -> None:
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["b", "a", "d"]])
    assert [i for i, _ in fused[:2]] == ["a", "b"]  # equal score, 'a' seen first
    assert fused[0][1] == pytest.approx(1 / 61 + 1 / 62)
    assert {i for i, _ in fused} == {"a", "b", "c", "d"}


def test_rrf_item_in_both_lists_beats_single_list_leader() -> None:
    fused = reciprocal_rank_fusion([["x", "shared"], ["y", "shared"]])
    assert fused[0][0] == "shared"


def test_rrf_limit_empty_and_k() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []
    assert len(reciprocal_rank_fusion([["a", "b", "c"]], limit=2)) == 2
    assert reciprocal_rank_fusion([["a"]], k=1)[0][1] == pytest.approx(0.5)


def test_fake_embedder_deterministic_unit_and_dim() -> None:
    e = FakeEmbedder()
    v1, v2 = e.embed(["npm ERR! ERESOLVE", "npm ERR! ERESOLVE"])
    assert v1 == v2
    assert len(v1) == EMBEDDING_DIM == e.dim
    assert sum(x * x for x in v1) == pytest.approx(1.0)


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


FIXTURE = {
    "peer": "npm ERR! ERESOLVE unable to resolve dependency tree peer dependency conflict",
    "oom": "JavaScript heap out of memory during webpack build",
    "flaky": "mocha test timeout of 2000ms exceeded in integration suite",
    "docker": "docker pull failed rate limit exceeded for image",
}


def test_top_k_on_fixture_set_with_fake_embedder() -> None:
    e = FakeEmbedder()
    ids = list(FIXTURE)
    vectors = dict(zip(ids, e.embed([FIXTURE[i] for i in ids]), strict=True))
    (q,) = e.embed(["ERESOLVE peer dependency conflict"])
    ranked = sorted(ids, key=lambda i: -_cos(q, vectors[i]))
    assert ranked[0] == "peer"


def test_dimension_check() -> None:
    check_dimension([0.0] * EMBEDDING_DIM)
    with pytest.raises(DimensionMismatchError, match="3 dimensions.*expects 384"):
        check_dimension([0.0, 1.0, 2.0])
    with pytest.raises(DimensionMismatchError):
        check_dimension([0.0] * 10, 12)


@pytest.mark.parametrize("bad", ["a;drop", 'a"b', "A", "", "1a", "a b", "a" * 60, "x.y"])
def test_quote_identifier_rejects(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid SQL identifier"):
        quote_identifier(bad)


def test_quote_identifier_ok() -> None:
    assert quote_identifier("verified_cases") == '"verified_cases"'


@pytest.mark.parametrize("schema", ["raw", "platform", "public", 'testing"; DROP', ""])
def test_collection_rejects_non_expert_schema(schema: str) -> None:
    engine = create_engine("postgresql+psycopg://u:p@127.0.0.1:1/x")
    with pytest.raises(ValueError, match="not an expert schema"):
        Collection(engine, FakeEmbedder(), schema, "cases")


def test_collection_rejects_bad_name() -> None:
    engine = create_engine("postgresql+psycopg://u:p@127.0.0.1:1/x")
    with pytest.raises(ValueError, match="invalid SQL identifier"):
        Collection(engine, FakeEmbedder(), "testing", "cases; DROP TABLE x")


def test_get_embedder_fake_and_granite_is_lazy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    assert isinstance(get_embedder(), FakeEmbedder)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "granite")
    g = get_embedder()
    assert isinstance(g, GraniteEmbedder)
    assert g._model is None  # nothing loaded by construction


def test_granite_missing_extra_gives_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)  # forces ImportError
    with pytest.raises(RuntimeError, match="--extra embeddings"):
        GraniteEmbedder().embed(["x"])


# ---- SQL path: needs Postgres + pgvector (auto-skipped without DATABASE_URL) ----


@pytest.mark.integration
def test_collection_hybrid_search_sql() -> None:
    engine = create_engine(os.environ["DATABASE_URL"])
    coll = Collection(engine, FakeEmbedder(), "testing", "p25_cases")
    with engine.begin() as conn:
        conn.execute(text('DROP TABLE IF EXISTS "testing"."p25_cases"'))
    try:
        coll.ensure()
        coll.ensure()  # idempotent
        assert coll.table_dimension() == EMBEDDING_DIM
        for doc_id, content in FIXTURE.items():
            coll.add(doc_id, content, {"id": doc_id})
        hits = coll.search("ERESOLVE peer dependency conflict", k=2)
        assert hits[0].doc_id == "peer"
        assert hits[0].metadata == {"id": "peer"}
        assert len(hits) == 2
        assert coll.search("", k=1)  # empty text query still returns vector hits
    finally:
        with engine.begin() as conn:
            conn.execute(text('DROP TABLE IF EXISTS "testing"."p25_cases"'))


@pytest.mark.integration
def test_collection_dimension_mismatch_sql() -> None:
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.begin() as conn:
        conn.execute(text('DROP TABLE IF EXISTS "testing"."p25_dim"'))
    try:
        Collection(engine, FakeEmbedder(8), "testing", "p25_dim").ensure()
        wrong = Collection(engine, FakeEmbedder(), "testing", "p25_dim")
        with pytest.raises(DimensionMismatchError, match=r"vector\(8\)"):
            wrong.ensure()
        with pytest.raises(DimensionMismatchError):
            wrong.add("a", "text")
    finally:
        with engine.begin() as conn:
            conn.execute(text('DROP TABLE IF EXISTS "testing"."p25_dim"'))
