"""SqlStore checked without a database: statements compile for Postgres and target primary keys.

This cannot prove the SQL runs (the integration tests do that) but it catches wrong column names,
conflict targets that are not the table's primary key, and use outside a transaction.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from collector.store import (
    CommitFileRecord,
    CommitRecord,
    JobRecord,
    LogRecord,
    RepoRecord,
    RunRecord,
    SnapshotRecord,
    SqlStore,
    StepRecord,
    SyncStateRecord,
    WorkflowRecord,
)
from recorded_github import NOW
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import ClauseElement

from shared.db import models as m


class _Result:
    def mappings(self) -> _Result:
        return self

    def first(self) -> None:
        return None

    def all(self) -> list[Any]:
        return []


class _RecordingConnection:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def execute(self, stmt: ClauseElement) -> _Result:
        self.sql.append(str(stmt.compile(dialect=postgresql.dialect())))
        return _Result()


class _FakeBegin:
    def __init__(self, conn: _RecordingConnection) -> None:
        self._conn = conn

    def __enter__(self) -> _RecordingConnection:
        return self._conn

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeEngine:
    def __init__(self) -> None:
        self.conn = _RecordingConnection()

    def begin(self) -> _FakeBegin:
        return _FakeBegin(self.conn)


def test_every_statement_compiles_and_conflict_targets_are_primary_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _FakeEngine()
    store = SqlStore(engine)  # type: ignore[arg-type]
    seen: list[tuple[type[m.Base], list[str], bool]] = []
    original = SqlStore._upsert

    def spy(
        self: SqlStore,
        model: type[m.Base],
        values: dict[str, Any],
        keys: Any,
        *,
        update: bool,
        keep: Any = (),
    ) -> None:
        columns = {c.name for c in model.__table__.columns}  # type: ignore[attr-defined]
        assert set(values) <= columns, f"{model.__name__}: unknown columns {set(values) - columns}"
        seen.append((model, list(keys), update))
        original(self, model, values, keys, update=update, keep=keep)

    monkeypatch.setattr(SqlStore, "_upsert", spy)
    with store.transaction():
        store.upsert_repo(RepoRecord(1, "a/b", "test", "MIT", "main"))
        store.ensure_sync_state(1)
        store.get_sync_state(1)
        store.set_cursor(1, "c")
        store.finish_backfill(1, NOW, NOW)
        store.get_workflows(1)
        store.upsert_workflow(WorkflowRecord(1, "p", "n", True, "s"))
        store.attempt_done(1, 2, 3)
        run = RunRecord(2, 3, 1, "p", "s", "b", "push", "completed", "success", NOW, NOW, NOW, "h")
        store.upsert_run(run)
        store.upsert_job(JobRecord(4, 2, 3, "n", "k", "completed", "success", NOW, NOW, "Linux"))
        store.upsert_steps([StepRecord(4, 1, "s", "completed", "success", NOW, NOW)])
        store.has_commit(1, "s")
        store.upsert_commit(
            CommitRecord(1, "s", [], "h", NOW, "m"),
            [CommitFileRecord(1, "s", "f", None, "added", 1, 0, "p")],
        )
        store.commit_files(1, "s")
        store.get_snapshot(1, "s", "p")
        store.upsert_snapshot(SnapshotRecord(1, "s", "p", "c", None, NOW))
        store.has_log(4)
        store.upsert_log(LogRecord(4, "x", 1, "0" * 64, 0, "ok", NOW))

    assert len(engine.conn.sql) >= 18
    assert {model for model, _, _ in seen} == {
        m.Repo, m.SyncState, m.Workflow, m.WorkflowRun, m.Job, m.Step, m.Commit,
        m.CommitFile, m.FileSnapshot, m.Log,
    }  # fmt: skip
    for model, keys, _ in seen:
        primary = [c.name for c in model.__table__.primary_key.columns]  # type: ignore[attr-defined]
        assert keys == primary, f"{model.__name__}: conflict target {keys} is not the primary key"
    upserts = [s for s in engine.conn.sql if s.startswith("INSERT")]
    assert all("ON CONFLICT" in s for s in upserts)
    assert any("DO UPDATE SET" in s for s in upserts)
    assert not any("DO UPDATE" in s for s in upserts if "raw.sync_state" in s and "INSERT" in s)


def test_record_fields_match_table_columns() -> None:
    pairs = [
        (RepoRecord, m.Repo), (SyncStateRecord, m.SyncState), (WorkflowRecord, m.Workflow),
        (RunRecord, m.WorkflowRun), (JobRecord, m.Job), (StepRecord, m.Step),
        (CommitRecord, m.Commit), (CommitFileRecord, m.CommitFile),
        (SnapshotRecord, m.FileSnapshot), (LogRecord, m.Log),
    ]  # fmt: skip
    for record, model in pairs:
        columns = {c.name for c in model.__table__.columns}  # type: ignore[attr-defined]
        assert {f.name for f in dataclasses.fields(record)} == columns, record.__name__


def test_store_refuses_use_outside_a_transaction() -> None:
    store = SqlStore(_FakeEngine())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="outside transaction"):
        store.has_log(1)


def test_run_upsert_never_overwrites_source_and_none_json_is_sql_null() -> None:
    engine = _FakeEngine()
    store = SqlStore(engine)  # type: ignore[arg-type]
    run = RunRecord(2, 3, 1, "p", "s", "b", "push", "completed", "success", NOW, NOW, NOW, "h")
    with store.transaction():
        store.upsert_run(run)
    sql = engine.conn.sql[0]
    update_part = sql.split("DO UPDATE SET")[1]
    assert "source" not in update_part and "status = excluded.status" in update_part
    assert "source" in sql.split("DO UPDATE SET")[0]  # still inserted on first write
    # raw=None is sent as SQL NULL, not the JSON value null
    assert "NULL" in sql.split("VALUES")[1].split("ON CONFLICT")[0]
