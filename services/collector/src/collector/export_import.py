"""Export and import of collected data (DATA.md 9): one tar.gz with a pg_dump, logs and a manifest.

The database work runs INSIDE the postgres container (`docker compose exec -T postgres ...`),
so no local Postgres client is needed. Every container command goes through an injectable
`Runner`, which lets the unit tests run without Docker.

Archive layout (fixed names, sorted entries, no timestamps, so two exports of the same data
differ only in the pg_dump bytes):

    manifest.json
    db.dump                      pg_dump custom format of the whole database
    files/logs/...               sanitised, gzipped job logs
    files/snapshots/...          lockfile blobs that raw.file_snapshots point at
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import tarfile
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from sqlalchemy.engine import make_url

from collector.repo_config import DEFAULT_CONFIG_DIR

logger = logging.getLogger(__name__)

REPO_ROOT = DEFAULT_CONFIG_DIR.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
FORMAT_VERSION = 1
DUMP_NAME = "db.dump"
MANIFEST_NAME = "manifest.json"
FILES_PREFIX = "files/"
# DEFAULT: only these two folders under data/ travel with an export; reports and exports do not.
DATA_FOLDERS = ("logs", "snapshots")
# DEFAULT: names are limited to what an unquoted Postgres identifier allows, so they are safe
# to put into SQL and into command arguments.
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_DEFAULT_USER = "pipelineiq"
_DEFAULT_DB = "pipelineiq"
_CHUNK = 1024 * 1024


class TransferError(Exception):
    """An export or import failed; the message says what to do."""


@dataclass(frozen=True)
class RunResult:
    """Outcome of one command in the container."""

    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class Runner(Protocol):
    """Runs a command inside the postgres container (args exclude the `docker compose` part)."""

    def __call__(
        self,
        args: Sequence[str],
        *,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | None = None,
    ) -> RunResult:
        """Run `args`; stream `stdin` in and `stdout` out when given, else capture stdout."""
        ...


@dataclass(frozen=True)
class PgTarget:
    """Which database (and role) the container commands act on."""

    user: str
    database: str


def compose_command(args: Sequence[str], compose_file: Path = COMPOSE_FILE) -> list[str]:
    """The full host command that runs `args` in the postgres container (no TTY, binary safe)."""
    return ["docker", "compose", "-f", str(compose_file), "exec", "-T", "postgres", *args]


def docker_runner(
    args: Sequence[str],
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> RunResult:
    """Run `args` via `docker compose exec -T postgres`; file objects are wired to the pipes."""
    try:
        proc = subprocess.run(
            compose_command(args),
            stdin=stdin,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=REPO_ROOT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise TransferError("`docker` was not found on PATH; install Docker Desktop") from exc
    return RunResult(proc.returncode, b"" if stdout is not None else proc.stdout, proc.stderr)


def check_identifier(name: str, what: str = "database name") -> str:
    """Return `name` if it is a plain identifier, else raise."""
    if not _IDENT.match(name):
        raise TransferError(
            f"invalid {what} {name!r}: use letters, digits and underscores only (max 63)"
        )
    return name


def resolve_target(database: str | None = None, env: Mapping[str, str] | None = None) -> PgTarget:
    """Role and database from DATABASE_URL (else POSTGRES_*); `database` overrides the name."""
    env = os.environ if env is None else env
    user = env.get("POSTGRES_USER") or _DEFAULT_USER
    db = env.get("POSTGRES_DB") or _DEFAULT_DB
    url = env.get("DATABASE_URL", "").strip()
    if url:
        parsed = make_url(url)
        user, db = parsed.username or user, parsed.database or db
    return PgTarget(check_identifier(user, "database user"), check_identifier(database or db))


# ------------------------------------------------------------------ container commands


def _fail(what: str, result: RunResult) -> TransferError:
    detail = result.stderr.decode("utf-8", errors="replace").strip()[-500:]
    return TransferError(f"{what} failed (exit {result.returncode}): {detail or 'no output'}")


def psql_command(target: PgTarget, sql: str, database: str | None = None) -> list[str]:
    """psql inside the container: unaligned, tuples only, stop at the first error."""
    return [
        "psql", "-U", target.user, "-d", database or target.database,
        "-X", "-v", "ON_ERROR_STOP=1", "-A", "-t", "-c", sql,
    ]  # fmt: skip


def pg_dump_command(target: PgTarget) -> list[str]:
    """Custom-format dump of the whole database, written to stdout."""
    return ["pg_dump", "-U", target.user, "-d", target.database, "-Fc", "--no-owner"]


def pg_restore_command(target: PgTarget) -> list[str]:
    """Restore from stdin, atomically; replaces objects that are in the dump."""
    # DEFAULT: --clean --if-exists + --single-transaction: works into an empty database and
    # over a migrated one (same tables replaced), and a failure leaves the database untouched.
    return [
        "pg_restore", "-U", target.user, "-d", target.database,
        "--no-owner", "--no-privileges", "--clean", "--if-exists",
        "--single-transaction", "--exit-on-error",
    ]  # fmt: skip


def _psql(
    runner: Runner, target: PgTarget, sql: str, what: str, database: str | None = None
) -> str:
    result = runner(psql_command(target, sql, database))
    if result.returncode != 0:
        raise _fail(what, result)
    return result.stdout.decode("utf-8")


def raw_row_counts(runner: Runner, target: PgTarget) -> dict[str, int]:
    """Row count of every base table in schema `raw` (empty dict if the schema has none)."""
    listing = _psql(
        runner,
        target,
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'raw' AND table_type = 'BASE TABLE' ORDER BY table_name",
        "listing raw tables",
    )
    names = sorted(line.strip() for line in listing.splitlines() if line.strip())
    if not names:
        return {}
    for name in names:
        check_identifier(name, "table name")
    sql = " UNION ALL ".join(f"SELECT '{n}', count(*) FROM raw.\"{n}\"" for n in names)
    out = _psql(runner, target, sql, "counting raw rows")
    counts: dict[str, int] = {}
    for line in out.splitlines():
        if line.strip():
            name, _, number = line.strip().partition("|")
            counts[f"raw.{name}"] = int(number)
    if sorted(counts) != [f"raw.{n}" for n in names]:
        raise TransferError(f"unexpected row count output from psql: {out!r}")
    return dict(sorted(counts.items()))


def ensure_database(runner: Runner, target: PgTarget) -> None:
    """Create the target database when it does not exist yet."""
    exists = _psql(
        runner,
        target,
        f"SELECT 1 FROM pg_database WHERE datname = '{target.database}'",
        "checking the target database",
        database="postgres",
    ).strip()
    if exists != "1":
        _psql(
            runner,
            target,
            f'CREATE DATABASE "{target.database}"',
            "creating the target database",
            database="postgres",
        )
        logger.info("created database", extra={"database": target.database})


# ------------------------------------------------------------------ checksums and manifest


def sha256_file(path: Path) -> str:
    """Hex sha256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def collect_data_files(data_dir: Path) -> dict[str, str]:
    """`{path relative to data_dir: sha256}` for every file under logs/ and snapshots/, sorted."""
    found: dict[str, str] = {}
    for folder in DATA_FOLDERS:
        root = data_dir / folder
        if not root.is_dir():
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
            found[path.relative_to(data_dir).as_posix()] = sha256_file(path)
    return dict(sorted(found.items()))


def build_manifest(
    *,
    member: str,
    day: date,
    git_commit: str,
    database: str,
    dump_sha256: str,
    dump_bytes: int,
    row_counts: Mapping[str, int],
    files: Mapping[str, str],
) -> dict[str, Any]:
    """The manifest dict; entries are sorted so equal data gives an equal manifest."""
    return {
        "format_version": FORMAT_VERSION,
        "member": member,
        "date": day.isoformat(),
        "git_commit": git_commit,
        "database": database,
        "dump": {"file": DUMP_NAME, "sha256": dump_sha256, "bytes": dump_bytes},
        "row_counts": dict(sorted(row_counts.items())),
        "files": dict(sorted(files.items())),
    }


def manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """Canonical JSON encoding of a manifest."""
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


def current_git_commit(cwd: Path = REPO_ROOT) -> str:
    """Current commit hash, or "unknown" outside a git checkout."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else "unknown"


# ------------------------------------------------------------------ export


@dataclass(frozen=True)
class ExportResult:
    """What an export produced."""

    archive: Path
    manifest: dict[str, Any]


def _tarinfo(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = 0
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def export_data(
    *,
    member: str,
    data_dir: Path,
    out_dir: Path,
    runner: Runner = docker_runner,
    target: PgTarget | None = None,
    today: date | None = None,
    commit: str | None = None,
) -> ExportResult:
    """Dump the database and bundle it with the logs, snapshots and a manifest."""
    target = target or resolve_target()
    day = today or datetime.now(UTC).date()
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"{member}-{day.isoformat()}.tar.gz"
    with tempfile.TemporaryDirectory(prefix=".export-", dir=out_dir) as tmp:
        dump_path = Path(tmp) / DUMP_NAME
        counts_before = raw_row_counts(runner, target)
        with dump_path.open("wb") as handle:  # binary file, never shell redirection
            result = runner(pg_dump_command(target), stdout=handle)
        if result.returncode != 0:
            raise _fail("pg_dump", result)
        size = dump_path.stat().st_size
        if size == 0:
            raise TransferError("pg_dump produced an empty dump; is the postgres container up?")
        counts = raw_row_counts(runner, target)
        if counts != counts_before:
            raise TransferError(
                "the database changed while exporting (a collector run is active?); "
                "stop it and export again"
            )
        files = collect_data_files(data_dir)
        manifest = build_manifest(
            member=member,
            day=day,
            git_commit=commit if commit is not None else current_git_commit(),
            database=target.database,
            dump_sha256=sha256_file(dump_path),
            dump_bytes=size,
            row_counts=counts,
            files=files,
        )
        body = manifest_bytes(manifest)
        partial = Path(tmp) / "archive.tar.gz"
        with partial.open("wb") as raw_out:
            # mtime=0 and an empty name keep the gzip header free of timestamps
            with (
                gzip.GzipFile(filename="", mode="wb", fileobj=raw_out, mtime=0) as gz,
                tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
            ):
                tar.addfile(_tarinfo(MANIFEST_NAME, len(body)), io.BytesIO(body))
                with dump_path.open("rb") as handle:
                    tar.addfile(_tarinfo(DUMP_NAME, size), handle)
                for rel in files:
                    source = data_dir / rel
                    with source.open("rb") as handle:
                        tar.addfile(_tarinfo(FILES_PREFIX + rel, source.stat().st_size), handle)
        os.replace(partial, archive)
    logger.info(
        "export written",
        extra={"archive": str(archive), "files": len(files), "tables": len(counts)},
    )
    return ExportResult(archive, manifest)


# ------------------------------------------------------------------ safe archive reading


def safe_member_name(name: str) -> str:
    """Return `name` if it is a relative, clean path inside the archive, else raise."""
    bad = (
        not name
        or "\\" in name
        or "\x00" in name
        or name.startswith("/")
        or re.match(r"^[A-Za-z]:", name) is not None
        or any(part in ("", ".", "..") for part in name.split("/"))
    )
    if bad:
        raise TransferError(f"unsafe path in archive: {name!r}")
    return name


def _check_data_path(rel: str) -> str:
    safe_member_name(rel)
    parts = rel.split("/")
    if parts[0] not in DATA_FOLDERS or len(parts) < 2:
        raise TransferError(f"unexpected file in archive: {rel!r} (only logs/ and snapshots/)")
    return rel


def _iter_regular_members(tar: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
    seen: set[str] = set()
    for member in tar.getmembers():
        name = member.name.rstrip("/") if member.isdir() else member.name
        safe_member_name(name)
        if member.isdir():
            continue
        if not member.isreg():  # symlinks, hard links, devices, fifos
            raise TransferError(f"archive contains a link or special file: {member.name!r}")
        if member.name in seen:
            raise TransferError(f"duplicate entry in archive: {member.name!r}")
        seen.add(member.name)
        yield member


def _extract_hashed(tar: tarfile.TarFile, member: tarfile.TarInfo, dest: Path) -> str:
    """Copy one archive member to `dest`, returning its sha256."""
    source = tar.extractfile(member)
    if source is None:
        raise TransferError(f"cannot read {member.name!r} from the archive")
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with source, dest.open("wb") as out:
        while chunk := source.read(_CHUNK):
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


def _parse_manifest(raw: bytes) -> dict[str, Any]:
    try:
        manifest = json.loads(raw)
        if (
            manifest["format_version"] != FORMAT_VERSION
            or not isinstance(manifest["dump"]["sha256"], str)
            or not isinstance(manifest["row_counts"], dict)
            or not isinstance(manifest["files"], dict)
        ):
            raise ValueError("unsupported or malformed manifest")
    except (ValueError, KeyError, TypeError) as exc:
        raise TransferError(f"manifest.json is missing or invalid: {exc}") from exc
    result: dict[str, Any] = manifest
    return result


def unpack_and_verify(archive: Path, dest: Path) -> tuple[dict[str, Any], Path, dict[str, Path]]:
    """Safely unpack into `dest`, check every checksum; returns (manifest, dump path, files)."""
    if not archive.is_file():
        raise TransferError(f"archive not found: {archive}")
    try:
        tar = tarfile.open(archive, "r:gz")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise TransferError(f"{archive} is not a readable .tar.gz: {exc}") from exc
    try:
        members = {m.name: m for m in _iter_regular_members(tar)}
        if MANIFEST_NAME not in members or DUMP_NAME not in members:
            raise TransferError(f"archive lacks {MANIFEST_NAME} or {DUMP_NAME}")
        manifest_file = tar.extractfile(members[MANIFEST_NAME])
        if manifest_file is None:
            raise TransferError("cannot read manifest.json")
        manifest = _parse_manifest(manifest_file.read())
        expected = {_check_data_path(rel): sha for rel, sha in manifest["files"].items()}
        in_archive = set(members) - {MANIFEST_NAME, DUMP_NAME}
        wanted = {FILES_PREFIX + rel for rel in expected}
        if in_archive != wanted:
            extra = sorted(in_archive - wanted)[:3]
            missing = sorted(wanted - in_archive)[:3]
            raise TransferError(
                f"archive and manifest disagree on files (not in manifest: {extra}, "
                f"not in archive: {missing})"
            )
        dump_path = dest / DUMP_NAME
        if _extract_hashed(tar, members[DUMP_NAME], dump_path) != manifest["dump"]["sha256"]:
            raise TransferError("checksum mismatch for the database dump (db.dump); do not import")
        files: dict[str, Path] = {}
        for rel, sha in sorted(expected.items()):
            out = dest / "files" / rel
            if _extract_hashed(tar, members[FILES_PREFIX + rel], out) != sha:
                raise TransferError(f"checksum mismatch for {rel}; the archive was modified")
            files[rel] = out
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise TransferError(f"{archive} could not be read: {exc}") from exc
    finally:
        tar.close()
    return manifest, dump_path, files


# ------------------------------------------------------------------ import


@dataclass(frozen=True)
class ImportResult:
    """What an import restored."""

    manifest: dict[str, Any]
    row_counts: dict[str, int]
    files: int


def import_data(
    archive: Path,
    *,
    data_dir: Path,
    runner: Runner = docker_runner,
    target: PgTarget | None = None,
    force: bool = False,
) -> ImportResult:
    """Verify the archive, restore the dump, copy logs and snapshots, verify again."""
    target = target or resolve_target()
    data_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".import-", dir=data_dir) as tmp:
        # 1. everything is checked before the database or data/ is touched
        manifest, dump_path, files = unpack_and_verify(archive, Path(tmp))
        # 2. refuse to overwrite existing data
        ensure_database(runner, target)
        total = sum(raw_row_counts(runner, target).values())
        if total > 0 and not force:
            raise TransferError(
                f"database {target.database!r} already holds {total} rows in raw.* "
                "(refusing to overwrite). Use FORCE=1 (make) or --force to replace them."
            )
        # 3. restore (one transaction: on failure the database is unchanged)
        with dump_path.open("rb") as handle:
            result = runner(pg_restore_command(target), stdin=handle)
        if result.returncode != 0:
            raise _fail("pg_restore", result)
        # 4. files (DEFAULT: an existing file at the same path is replaced; the paths are
        # keyed by GitHub ids and content hashes, so same path means same job or blob)
        for rel, source in files.items():
            dest = data_dir / rel
            if dest.is_symlink():
                raise TransferError(f"refusing to write through a symlink: {dest}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, dest)
    # 5. verify what is now in place
    for rel, sha in manifest["files"].items():
        if not (data_dir / rel).is_file() or sha256_file(data_dir / rel) != sha:
            raise TransferError(f"checksum mismatch after import for {rel}")
    after = raw_row_counts(runner, target)
    expected_counts: dict[str, int] = manifest["row_counts"]
    if after != expected_counts:
        diff = sorted(
            k for k in set(after) | set(expected_counts) if after.get(k) != expected_counts.get(k)
        )
        raise TransferError(
            "row counts differ after import: "
            + ", ".join(f"{k} expected {expected_counts.get(k)}, got {after.get(k)}" for k in diff)
        )
    logger.info(
        "import verified",
        extra={"archive": str(archive), "files": len(files), "tables": len(after)},
    )
    return ImportResult(manifest, after, len(files))
