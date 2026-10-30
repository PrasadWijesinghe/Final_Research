"""Tests for P4.3 labelling: kappa (unit), and the SQL rules and sample export (DB, rolled back)."""

from __future__ import annotations

import csv
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Connection, create_engine, text
from testing_expert.labels.kappa import cohen_kappa, kappa_from_files
from testing_expert.labels.rules import run_rules
from testing_expert.labels.sample import export_sample

from shared.contracts import FailureLabel


def test_kappa_perfect_chance_and_known_value() -> None:
    assert cohen_kappa(["a", "b", "a"], ["a", "b", "a"]) == 1.0
    assert cohen_kappa(["a", "a"], ["a", "a"]) == 1.0
    # classic example: 20 agree-yes, 5 yes/no, 10 no/yes, 15 agree-no -> kappa 0.4
    a = ["y"] * 25 + ["n"] * 25
    b = ["y"] * 20 + ["n"] * 5 + ["y"] * 10 + ["n"] * 15
    assert cohen_kappa(a, b) == pytest.approx(0.4)
    assert cohen_kappa(["a", "b"], ["b", "a"]) == pytest.approx(-1.0)


def test_kappa_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        cohen_kappa(["a"], [])
    with pytest.raises(ValueError):
        cohen_kappa([], [])


def test_kappa_from_files_skips_blank_rows(tmp_path: Path) -> None:
    key = tmp_path / "k.csv"
    key.write_text(
        "job_id,rule_label,rule_name,confidence\n1,flaky,r,high\n2,genuine,r,high\n3,pass,r,high\n"
    )
    filled = tmp_path / "s.csv"
    filled.write_text("job_id,human_label,ai_prelabel\n1,flaky,\n2,flaky,\n3,,\n")
    out = kappa_from_files(filled, key)
    assert out["n"] == 2
    assert out["skipped_blank"] == 1
    assert out["agreement"] == 0.5


T0 = datetime(2026, 1, 1, tzinfo=UTC)
REPO_ID = 9_300_001
MEMBER = "p43_test_member"
REPO = "o/p43"


def _seed(conn: Connection) -> None:
    def run(sql: str, **p: object) -> None:
        conn.execute(text(sql), p)

    run(
        "insert into raw.repos (id, full_name, member) values (:i, :n, :m)",
        i=REPO_ID, n=REPO, m=MEMBER,
    )  # fmt: skip

    def commit(sha: str, parents: list[str], days: float, files: list[str]) -> None:
        run(
            "insert into raw.commits (repo_id, sha, parent_shas, committed_at) "
            "values (:r, :s, :p, :t)",
            r=REPO_ID, s=sha, p=parents, t=T0 + timedelta(days=days),
        )  # fmt: skip
        for f in files:
            run(
                "insert into raw.commit_files (repo_id, sha, path) values (:r, :s, :f)",
                r=REPO_ID, s=sha, f=f,
            )  # fmt: skip

    n = {"job": 9_300_100}

    def job(sha: str, run_id: int, attempt: int, concl: str, days: float, test_log: bool | None,
            failed_file: str | None = None) -> int:  # fmt: skip
        n["job"] += 1
        jid = n["job"]
        run(
            "insert into raw.workflow_runs (run_id, attempt, repo_id, workflow_path, head_sha, "
            "head_branch, created_at) values (:ri, :a, :r, 'ci.yml', :s, 'feat', :t) "
            "on conflict do nothing",
            ri=run_id, a=attempt, r=REPO_ID, s=sha, t=T0 + timedelta(days=days),
        )  # fmt: skip
        run(
            "insert into raw.jobs (job_id, run_id, attempt, name, job_key, conclusion, status) "
            "values (:j, :ri, :a, 'test', 'test', :c, 'completed')",
            j=jid, ri=run_id, a=attempt, c=concl,
        )  # fmt: skip
        run(
            "insert into raw.logs (job_id, storage_path, status) values (:j, :p, 'ok')",
            j=jid, p=f"logs/o/p43/{jid}.log.gz",
        )  # fmt: skip
        if test_log is not None:
            run(
                "insert into testing.log_parses (job_id, run_id, attempt, repo, head_sha, "
                "job_name, reason, framework, parsed_ok, is_test_job) values "
                "(:j, :ri, :a, :repo, :s, 'test', 'seed_failed', 'jest', true, :t)",
                j=jid, ri=run_id, a=attempt, repo=REPO, s=sha, t=test_log,
            )  # fmt: skip
        if failed_file:
            run(
                "insert into testing.test_results (job_id, test_id, attempt, file, full_name, "
                "outcome, granularity) values (:j, :t, :a, :f, 'x', 'failed', 'test')",
                j=jid, t=f"t{jid}", a=attempt, f=failed_file,
            )  # fmt: skip
        return jid

    ids: dict[str, int] = {}
    # flaky: failed then passed on the same SHA (rerun attempt 2)
    commit("A1", [], 0, ["src/a.js"])
    ids["flaky"] = job("A1", 9_301, 1, "failure", 0, True, "test/a.test.js")
    ids["rerun_pass"] = job("A1", 9_301, 2, "success", 0.1, None)
    # genuine: kept failing, later commit within 7 days touched the failing file, then passed
    commit("B1", [], 1, ["src/b.js"])
    ids["genuine"] = job("B1", 9_302, 1, "failure", 1, True, "test/b.test.js")
    commit("B2", ["B1"], 2, ["packages/x/test/b.test.js"])
    job("B2", 9_303, 1, "success", 2, None)
    # silent flaky: child commit touched only docs and passed
    commit("C1", [], 3, ["src/c.js"])
    ids["silent"] = job("C1", 9_304, 1, "failure", 3, True, "test/c.test.js")
    commit("C2", ["C1"], 3.5, ["README.md", "docs/guide.rst"])
    job("C2", 9_305, 1, "success", 3.5, None)
    # unclear: nothing follows
    commit("D1", [], 20, ["src/d.js"])
    ids["unclear"] = job("D1", 9_306, 1, "failure", 20, True, "test/d.test.js")
    # pass: first attempt passed
    commit("E1", [], 21, ["src/e.js"])
    ids["pass"] = job("E1", 9_307, 1, "success", 21, None)
    # not_test: log shows no tests (install failed), and a cancelled job
    commit("F1", [], 22, ["src/f.js"])
    ids["non_test_log"] = job("F1", 9_308, 1, "failure", 22, False)
    ids["cancelled"] = job("F1", 9_309, 1, "cancelled", 22, None)
    # failure streak on four different commits: the 4th and 5th fail after >= 3 test failures on
    # >= 2 other commits (persistent_failure_streak); the first three do not have the history yet
    for i in range(1, 6):
        commit(f"G{i}", [], 29 + i, [f"src/g{i}.js"])
        ids[f"streak{i}"] = job(
            f"G{i}", 9_310 + i, 1, "failure", 29 + i, True, f"test/g{i}.test.js"
        )
    # a pass resets the streak; four failed attempts of ONE commit are reruns, not a streak
    commit("H0", [], 40, ["src/h0.js"])
    job("H0", 9_320, 1, "success", 40, None)
    commit("H1", [], 41, ["src/h1.js"])
    for att in range(1, 5):
        ids[f"same_sha{att}"] = job("H1", 9_321, att, "failure", 41 + att / 100, True,
                                    "test/h1.test.js")  # fmt: skip
    conn.info["ids"] = ids


@pytest.mark.integration
def test_rules_and_sample_on_seeded_db(tmp_path: Path) -> None:
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed(conn)
            ids = conn.info["ids"]
            cover = run_rules(conn, MEMBER)
            got = {
                r[0]: (r[1], r[2], r[3])
                for r in conn.execute(
                    text(
                        "select job_id, sub_label, rule_name, confidence from testing.labels "
                        "where job_id between 9300101 and 9300199"
                    )
                )
            }
            assert got[ids["flaky"]] == ("flaky", "rerun_pass_same_sha", "high")
            assert got[ids["genuine"]] == ("genuine", "fix_commit_touches_file", "high")
            assert got[ids["silent"]] == ("flaky", "silent_docs_only_next", "low")
            assert got[ids["unclear"]][0] == "unclear"
            assert got[ids["pass"]] == ("pass", "first_attempt_pass", "high")
            assert got[ids["non_test_log"]][0] == "not_test"
            assert got[ids["cancelled"]][0] == "not_test"
            assert ids["rerun_pass"] not in got  # a rerun pass gets no label
            for i in (1, 2, 3):
                assert got[ids[f"streak{i}"]][0] == "unclear"
            for i in (4, 5):
                assert got[ids[f"streak{i}"]] == ("genuine", "persistent_failure_streak", "low")
            assert got[ids["same_sha4"]][0] == "unclear"  # only same-commit reruns before it
            assert cover["series"] == 1

            # mirrored rows validate against the shared contract; pass / not_test are not mirrored
            mirrored = conn.execute(
                text(
                    "select job_id, payload, is_final from platform.failure_labels "
                    "where job_id between 9300101 and 9300199"
                )
            ).all()
            failed = {ids["flaky"], ids["genuine"], ids["silent"], ids["unclear"]}
            failed |= {v for k, v in ids.items() if k.startswith(("streak", "same_sha"))}
            assert {m[0] for m in mirrored} == failed
            for _, payload, _ in mirrored:
                assert FailureLabel.model_validate(payload).category.value == "test"
            final = {m[0]: m[2] for m in mirrored}
            assert final[ids["unclear"]] is False
            assert final[ids["genuine"]] is True

            # idempotent: a second run gives the same rows
            run_rules(conn, MEMBER)
            n = conn.execute(
                text("select count(*) from testing.labels where job_id between 9300101 and 9300199")
            ).scalar_one()
            assert n == len(got)

            # another category claims the genuine job: our row becomes non-final
            conn.execute(
                text(
                    "insert into platform.failure_labels (run_id, attempt, job_id, category, "
                    "labelled_by, payload) values (9302, 1, :j, 'build', 'rule', '{}'::jsonb)"
                ),
                {"j": ids["genuine"]},
            )
            run_rules(conn, MEMBER)
            fin = conn.execute(
                text(
                    "select is_final from platform.failure_labels "
                    "where job_id = :j and category = 'test'"
                ),
                {"j": ids["genuine"]},
            ).scalar_one()
            assert fin is False

            sp, kp, count = export_sample(conn, MEMBER, tmp_path, per_stratum=1, name="s")
            assert count == 7  # pass, flaky hi/lo, genuine hi/lo, unclear, not_test
            with sp.open(newline="") as fh:
                rows = list(csv.DictReader(fh))
            assert all(r["human_label"] == "" and r["ai_prelabel"] == "" for r in rows)
            assert "rule_label" not in rows[0]
            assert all(r["log_path"].startswith("data/logs/") for r in rows)
            with kp.open(newline="") as fh:
                assert {r["rule_label"] for r in csv.DictReader(fh)} >= {"pass", "genuine"}
            _, _, count2 = export_sample(conn, MEMBER, tmp_path, per_stratum=1, name="s2")
            assert (tmp_path / "s.csv").read_text() == (tmp_path / "s2.csv").read_text()
            assert count2 == count
        finally:
            trans.rollback()
