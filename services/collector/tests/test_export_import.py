"""Export and import without Docker: a fake runner stands in for `docker compose exec`."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import BinaryIO

import pytest
from collector import cli
from collector.export_import import (
    COMPOSE_FILE,
    PgTarget,
    Runner,
    RunResult,
    TransferError,
    build_manifest,
    collect_data_files,
    compose_command,
    export_data,
    import_data,
    manifest_bytes,
    pg_dump_command,
    pg_restore_command,
    psql_command,
    resolve_target,
    safe_member_name,
)

TARGET = PgTarget("pipelineiq", "pipelineiq")
FRESH = PgTarget("pipelineiq", "fresh")
DAY = date(2026, 11, 2)
COUNTS = {"repos": 1, "workflow_runs": 4, "jobs": 8, "logs": 2}
LOG_ONE = "logs/o/r/1/1/10.log.gz"
LOG_TWO = "logs/o/r/1/1/11.log.gz"


class FakeServer:
    """A fake Postgres server: databases map to raw table counts; dumps are JSON of the counts."""

    def __init__(self, databases: dict[str, dict[str, int] | None]) -> None:
        self.databases = databases
        self.calls: list[list[str]] = []
        self.restore_fails = False
        self.after_restore: dict[str, int] | None = None

    def __call__(
        self,
        args: Sequence[str],
        *,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | None = None,
    ) -> RunResult:
        argv = list(args)
        self.calls.append(argv)
        db = argv[argv.index("-d") + 1]
        if argv[0] == "pg_dump":
            assert stdout is not None
            stdout.write(json.dumps(self.databases[db]).encode())
            return RunResult(0)
        if argv[0] == "pg_restore":
            assert stdin is not None
            if self.restore_fails:
                return RunResult(1, b"", b"boom: relation already exists")
            self.databases[db] = self.after_restore or json.loads(stdin.read())
            return RunResult(0)
        sql = argv[-1]
        if sql.startswith("SELECT table_name"):
            tables = self.databases.get(db) or {}
            return RunResult(0, "".join(f"{t}\n" for t in sorted(tables)).encode())
        if sql.startswith("SELECT 1 FROM pg_database"):
            name = re.search(r"datname = '(\w+)'", sql)
            assert name
            return RunResult(0, b"1\n" if name.group(1) in self.databases else b"\n")
        if sql.startswith("CREATE DATABASE"):
            self.databases[sql.split('"')[1]] = None
            return RunResult(0)
        assert sql.startswith("SELECT '"), sql
        tables = self.databases[db] or {}
        return RunResult(0, "".join(f"{t}|{n}\n" for t, n in sorted(tables.items())).encode())

    def count(self, program: str) -> int:
        return sum(1 for c in self.calls if c[0] == program)


def make_data(root: Path) -> None:
    (root / "logs/o/r/1/1").mkdir(parents=True)
    (root / LOG_ONE).write_bytes(gzip.compress(b"log one"))
    (root / LOG_TWO).write_bytes(gzip.compress(b"log two"))
    (root / "snapshots/blobs/ab").mkdir(parents=True)
    (root / "snapshots/blobs/ab/abcdef.gz").write_bytes(gzip.compress(b"lock"))
    (root / "reports").mkdir()
    (root / "reports/ignored.md").write_text("not exported")


def export(tmp_path: Path) -> tuple[Path, FakeServer, Path]:
    server = FakeServer({"pipelineiq": dict(COUNTS)})
    data = tmp_path / "src-data"
    make_data(data)
    result = export_data(
        member="test",
        data_dir=data,
        out_dir=tmp_path / "exports",
        runner=server,
        target=TARGET,
        today=DAY,
        commit="a" * 40,
    )
    return result.archive, server, data


def rewrite(
    archive: Path,
    out: Path,
    change: dict[str, bytes | None],
    extra: list[tarfile.TarInfo] | None = None,
) -> Path:
    """Copy an archive, replacing (bytes) or dropping (None) members, optionally adding more."""
    with tarfile.open(archive, "r:gz") as src, tarfile.open(out, "w:gz") as dst:
        for m in src.getmembers():
            if m.name in change:
                data = change[m.name]
                if data is None:
                    continue
                m.size = len(data)
                dst.addfile(m, io.BytesIO(data))
            else:
                dst.addfile(m, src.extractfile(m))
        for info in extra or []:
            dst.addfile(info, io.BytesIO(b"x") if info.isreg() else None)
    return out


def do_import(
    archive: Path, dest: Path, server: Runner, target: PgTarget = FRESH, **kw: bool
) -> None:
    import_data(archive, data_dir=dest, runner=server, target=target, **kw)


# ------------------------------------------------------------------ commands and manifest


def test_commands_are_built_for_the_container() -> None:
    assert compose_command(["psql", "-V"]) == [
        "docker", "compose", "-f", str(COMPOSE_FILE), "exec", "-T", "postgres", "psql", "-V",
    ]  # fmt: skip
    assert pg_dump_command(TARGET) == [
        "pg_dump", "-U", "pipelineiq", "-d", "pipelineiq", "-Fc", "--no-owner",
    ]  # fmt: skip
    restore = pg_restore_command(PgTarget("u", "other_db"))
    assert restore[:5] == ["pg_restore", "-U", "u", "-d", "other_db"]
    for flag in ("--clean", "--if-exists", "--no-owner", "--single-transaction"):
        assert flag in restore
    cmd = psql_command(TARGET, "SELECT 1", database="postgres")
    assert cmd[cmd.index("-d") + 1] == "postgres" and cmd[-2:] == ["-c", "SELECT 1"]


def test_target_comes_from_database_url_and_can_be_overridden() -> None:
    env = {"DATABASE_URL": "postgresql+psycopg://alice:pw@127.0.0.1:5432/mydb"}
    assert resolve_target(env=env) == PgTarget("alice", "mydb")
    assert resolve_target("other", env=env) == PgTarget("alice", "other")
    assert resolve_target(env={}) == PgTarget("pipelineiq", "pipelineiq")
    with pytest.raises(TransferError, match="invalid database name"):
        resolve_target('x"; DROP DATABASE y; --', env=env)


def test_manifest_is_deterministic() -> None:
    def build(counts: dict[str, int], files: dict[str, str]) -> bytes:
        return manifest_bytes(
            build_manifest(
                member="test",
                day=DAY,
                git_commit="abc",
                database="d",
                dump_sha256="0" * 64,
                dump_bytes=3,
                row_counts=counts,
                files=files,
            )
        )

    one = build({"raw.b": 2, "raw.a": 1}, {"logs/z": "1", "logs/a": "2"})
    two = build({"raw.a": 1, "raw.b": 2}, {"logs/a": "2", "logs/z": "1"})
    assert one == two
    assert list(json.loads(one)["files"]) == ["logs/a", "logs/z"]


def test_collect_data_files_only_logs_and_snapshots(tmp_path: Path) -> None:
    make_data(tmp_path)
    files = collect_data_files(tmp_path)
    assert sorted(files) == [LOG_ONE, LOG_TWO, "snapshots/blobs/ab/abcdef.gz"]
    assert files[LOG_ONE] == hashlib.sha256(gzip.compress(b"log one")).hexdigest()


# ------------------------------------------------------------------ export


def test_export_writes_archive_with_manifest(tmp_path: Path) -> None:
    archive, server, data = export(tmp_path)
    assert archive.name == "test-2026-11-02.tar.gz"
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        manifest_file = tar.extractfile("manifest.json")
        dump_file = tar.extractfile("db.dump")
        assert manifest_file is not None and dump_file is not None
        manifest = json.loads(manifest_file.read())
        dump = dump_file.read()
    assert names[:2] == ["manifest.json", "db.dump"]
    assert f"files/{LOG_ONE}" in names and "files/reports/ignored.md" not in names
    assert manifest["member"] == "test" and manifest["date"] == "2026-11-02"
    assert manifest["git_commit"] == "a" * 40
    assert manifest["row_counts"] == {f"raw.{k}": v for k, v in COUNTS.items()}
    assert manifest["dump"]["sha256"] == hashlib.sha256(dump).hexdigest()
    assert manifest["files"] == collect_data_files(data)
    assert server.count("pg_dump") == 1


def test_export_twice_gives_identical_archives(tmp_path: Path) -> None:
    one, _, _ = export(tmp_path / "a")
    two, _, _ = export(tmp_path / "b")
    assert one.read_bytes() == two.read_bytes()


def test_export_reports_pg_dump_failure(tmp_path: Path) -> None:
    server = FakeServer({"pipelineiq": dict(COUNTS)})

    def failing(
        args: Sequence[str], *, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None
    ) -> RunResult:
        if args[0] == "pg_dump":
            return RunResult(1, b"", b"pg_dump: error: connection failed")
        return server(args, stdin=stdin, stdout=stdout)

    with pytest.raises(TransferError, match=r"pg_dump failed.*connection failed"):
        export_data(
            member="test",
            data_dir=tmp_path,
            out_dir=tmp_path / "o",
            runner=failing,
            target=TARGET,
            today=DAY,
            commit="x",
        )
    assert not (tmp_path / "o/test-2026-11-02.tar.gz").exists()


# ------------------------------------------------------------------ import


def test_import_round_trip_into_new_database(tmp_path: Path) -> None:
    archive, server, data = export(tmp_path)
    dest = tmp_path / "dest-data"
    result = import_data(archive, data_dir=dest, runner=server, target=FRESH)
    assert server.databases["fresh"] == COUNTS  # database was created, then restored
    assert result.row_counts == {f"raw.{k}": v for k, v in COUNTS.items()}
    assert collect_data_files(dest) == collect_data_files(data)
    assert list(dest.glob(".import-*")) == []  # temp folder cleaned up


def test_import_refuses_non_empty_database_without_force(tmp_path: Path) -> None:
    archive, server, _ = export(tmp_path)
    server.databases["busy"] = {"repos": 3}
    busy = PgTarget("pipelineiq", "busy")
    dest = tmp_path / "dest"
    with pytest.raises(TransferError, match=r"already holds 3 rows.*FORCE=1"):
        do_import(archive, dest, server, busy)
    assert server.count("pg_restore") == 0
    assert not (dest / "logs").exists()
    do_import(archive, dest, server, busy, force=True)
    assert server.databases["busy"] == COUNTS


def test_import_into_migrated_but_empty_database_is_allowed(tmp_path: Path) -> None:
    archive, server, _ = export(tmp_path)
    server.databases["migrated"] = {k: 0 for k in COUNTS}
    do_import(archive, tmp_path / "d", server, PgTarget("pipelineiq", "migrated"))
    assert server.databases["migrated"] == COUNTS


def test_modified_log_file_is_detected_before_touching_anything(tmp_path: Path) -> None:
    archive, server, _ = export(tmp_path)
    bad = rewrite(archive, tmp_path / "bad.tar.gz", {f"files/{LOG_ONE}": b"tampered"})
    dest = tmp_path / "dest"
    with pytest.raises(TransferError, match=f"checksum mismatch for {LOG_ONE}"):
        do_import(bad, dest, server)
    assert server.count("pg_restore") == 0
    assert "fresh" not in server.databases
    assert not (dest / "logs").exists()


def test_modified_dump_is_detected(tmp_path: Path) -> None:
    archive, server, _ = export(tmp_path)
    bad = rewrite(archive, tmp_path / "bad.tar.gz", {"db.dump": b'{"repos": 999}'})
    with pytest.raises(TransferError, match="checksum mismatch for the database dump"):
        do_import(bad, tmp_path / "d", server)


def test_missing_and_extra_files_are_detected(tmp_path: Path) -> None:
    archive, server, _ = export(tmp_path)
    missing = rewrite(archive, tmp_path / "m.tar.gz", {f"files/{LOG_TWO}": None})
    with pytest.raises(TransferError, match="disagree on files"):
        do_import(missing, tmp_path / "d", server, force=True)
    extra = tarfile.TarInfo("files/logs/evil.log.gz")
    extra.size = 1
    padded = rewrite(archive, tmp_path / "e.tar.gz", {}, [extra])
    with pytest.raises(TransferError, match="disagree on files"):
        do_import(padded, tmp_path / "d", server, force=True)


def test_count_mismatch_after_restore_fails(tmp_path: Path) -> None:
    archive, server, _ = export(tmp_path)
    server.after_restore = {**COUNTS, "jobs": 7}
    with pytest.raises(TransferError, match=r"row counts differ.*jobs expected 8, got 7"):
        do_import(archive, tmp_path / "d", server)


def test_restore_failure_is_reported(tmp_path: Path) -> None:
    archive, server, _ = export(tmp_path)
    server.restore_fails = True
    with pytest.raises(TransferError, match=r"pg_restore failed.*already exists"):
        do_import(archive, tmp_path / "d", server)
    assert not (tmp_path / "d/logs").exists()


@pytest.mark.parametrize(
    "name",
    ["/etc/passwd", "../x", "files/../../x", "files//x", "a\\b", "C:/x", "files/./x", ""],
)
def test_unsafe_names_are_rejected(name: str) -> None:
    with pytest.raises(TransferError, match="unsafe path"):
        safe_member_name(name)


def _evil(tmp_path: Path, info: tarfile.TarInfo) -> Path:
    archive, _, _ = export(tmp_path / "base")
    return rewrite(archive, tmp_path / "evil.tar.gz", {}, [info])


@pytest.mark.parametrize("name", ["../escape.txt", "/abs.txt", "files/logs/../../escape"])
def test_path_traversal_and_absolute_paths_in_archive_are_rejected(
    tmp_path: Path, name: str
) -> None:
    info = tarfile.TarInfo(name)
    info.size = 1
    with pytest.raises(TransferError, match="unsafe path"):
        do_import(_evil(tmp_path, info), tmp_path / "d", FakeServer({}))
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_links_and_special_files_are_rejected(tmp_path: Path, kind: bytes) -> None:
    info = tarfile.TarInfo("files/logs/link")
    info.type = kind
    info.linkname = "/etc/passwd"
    with pytest.raises(TransferError, match="link or special file"):
        do_import(_evil(tmp_path, info), tmp_path / "d", FakeServer({}))


def test_non_archive_and_missing_files_fail_clearly(tmp_path: Path) -> None:
    junk = tmp_path / "junk.tar.gz"
    junk.write_bytes(b"not a tarball")
    with pytest.raises(TransferError, match="not a readable"):
        do_import(junk, tmp_path / "d", FakeServer({}))
    with pytest.raises(TransferError, match="archive not found"):
        do_import(tmp_path / "none.tar.gz", tmp_path / "d", FakeServer({}))


# ------------------------------------------------------------------ CLI


def test_cli_import_returns_nonzero_with_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["import", str(tmp_path / "missing.tar.gz"), "--data-dir", str(tmp_path)])
    assert code == cli.EXIT_FAILED
    assert "import failed: archive not found" in capsys.readouterr().err


def test_cli_parses_export_and_import_flags() -> None:
    parser = cli._build_parser()
    a = parser.parse_args(["export", "--member", "test", "--database", "db2"])
    assert (a.member, a.database) == ("test", "db2")
    b = parser.parse_args(["import", "x.tar.gz", "--force", "--database", "db3"])
    assert b.force and b.database == "db3" and b.file == Path("x.tar.gz")
