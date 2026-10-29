"""The knowledge base: cases from fix candidates, and hybrid search on Postgres (integration)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any

import pytest
from build_expert.rag.kb import KnowledgeBase, case_text, cases_from_fix_candidates, open_kb
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from shared.db.session import get_database_url
from shared.embeddings import FakeEmbedder


def _sha(n: int) -> str:
    return hashlib.sha1(str(n).encode()).hexdigest()


def _fix(n: int, code: str, message: str, path: str, overlap: bool = True) -> dict[str, Any]:
    return {
        "repo": "acme/web" if n % 2 else "beta/ui",
        "branch": "main",
        "failed_sha": _sha(n),
        "fix_sha": _sha(n + 100),
        "hours_to_fix": 2.0,
        "sub_labels": "tsc" if code.startswith("TS") else "eslint",
        "error_codes": code,
        "error_locations": json.dumps(
            [{"tool": "tsc", "path": path, "line": 3, "code": code, "message": message}]
        ),
        "fix_message": f"fix {code} in {path}",
        "fix_changed_paths": json.dumps([path]),
        "overlap_paths": json.dumps([path] if overlap else []),
        "licence": "MIT",
        "origin": "mined",
    }


FIXES = [
    _fix(1, "TS2322", "Type 'string' is not assignable to type 'number'.", "src/api.ts"),
    _fix(2, "no-unused-vars", "'helper' is defined but never used.", "src/util.ts"),
    _fix(3, "TS2307", "Cannot find module './helpers' or its type declarations.", "src/a.ts"),
    _fix(4, "TS2304", "Cannot find name 'foo'.", "src/b.ts", overlap=False),
]


def test_cases_keep_strong_fixes_and_respect_licences() -> None:
    cases = cases_from_fix_candidates(FIXES)
    assert len(cases) == 3  # the 4th fix touched none of the error files
    assert len(cases_from_fix_candidates(FIXES, strong_only=False)) == 4
    assert len(cases_from_fix_candidates(FIXES, excluded_repos=frozenset({"acme/web"}))) == 1
    c = cases[0]
    assert c.doc_id == f"acme/web@{_sha(1)[:12]}..{_sha(101)[:12]}"
    assert c.metadata["source_url"] == f"https://github.com/acme/web/commit/{_sha(101)}"
    assert c.metadata["reviewed"] is False
    assert "TS2322" in c.content and c.content.startswith("tsc")


def test_case_text_normalises_messages() -> None:
    loc = {"tool": "tsc", "code": "TS2307", "message": "x in /home/runner/work/a.ts"}
    t = case_text("tsc", "TS2307", [loc])
    assert "TS2307" in t and "/home/runner" not in t


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """A throwaway `<dbname>_build_kb_test` database with pgvector and the build schema."""
    try:
        main = make_url(get_database_url())
    except RuntimeError:
        pytest.skip("DATABASE_URL is not set")
    name = f"{main.database}_build_kb_test"
    admin = create_engine(main.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError:
        admin.dispose()
        pytest.skip("database in DATABASE_URL is unreachable (run `make up`)")
    eng = create_engine(main.set(database=name))
    with eng.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS build"))
    yield eng
    eng.dispose()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.mark.integration
def test_kb_load_search_and_count(engine: Engine) -> None:
    assert open_kb(engine, FakeEmbedder()) is None  # no table yet
    kb = KnowledgeBase(engine, FakeEmbedder())
    assert kb.count() == 0
    assert kb.load(cases_from_fix_candidates(FIXES)) == 3
    assert kb.load(cases_from_fix_candidates(FIXES)) == 3  # same doc ids: updated, not doubled
    assert kb.count() == 3
    hits = kb.search("tsc TS2307 Cannot find module './util'", k=3)
    assert hits and hits[0].case_id.startswith("acme/web@")
    assert hits[0].summary.startswith("acme/web: tsc failure (TS2307)")
    assert 0.5 <= hits[0].similarity <= 1.0
    assert hits[0].source_url and "fix TS2307" in hits[0].fix_summary
    assert open_kb(engine, FakeEmbedder()) is not None
    # Only cases of the same kind: the ESLint case is never offered for a type error.
    tsc_only = kb.search("tsc no-unused-vars 'helper' is defined but never used", k=3, kind="tsc")
    assert all("eslint" not in c.summary for c in tsc_only)
    lint = kb.search("eslint no-unused-vars 'helper' is defined but never used", k=3, kind="eslint")
    assert [c.summary.split(":")[0] for c in lint] == ["beta/ui"]


def test_overlap_ignores_common_words() -> None:
    from build_expert.rag.kb import overlap

    assert overlap("tsc TS2307 Cannot find module", "tsc TS2307 cannot find module x") == 1.0
    assert overlap("build_other failed error", "build_other failed error build") == 0.0
    assert overlap("eslint no-unused-vars helper", "tsc TS2322 string number") == 0.0


def test_only_the_fix_subject_line_is_kept() -> None:
    row = {**FIXES[0], "fix_message": "fix types in api.ts\n\nlong review chatter"}
    (case,) = cases_from_fix_candidates([row])
    assert case.metadata["fix_message"] == "fix types in api.ts"
