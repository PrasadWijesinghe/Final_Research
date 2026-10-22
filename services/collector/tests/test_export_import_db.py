"""Export from one database and import into a fresh one, through real `docker compose exec`.

Integration: skipped when Postgres is unreachable (root conftest) or when `docker compose`
cannot reach the postgres container (for example a Postgres that is not the compose one).
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from collector.export_import import (
    PgTarget,
    TransferError,
    collect_data_files,
    docker_runner,
    export_data,
    import_data,
    psql_command,
    raw_row_counts,
)
from collector.store import SqlStore
from recorded_github import FakeGitHub
from scenario import ENTRY, make_backfiller
from sqlalchemy import Engine
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration


def _compose_postgres_usable(user: str) -> bool:
    try:
        result = docker_runner(psql_command(PgTarget(user, "postgres"), "SELECT 1"))
    except (TransferError, OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


@pytest.fixture
def target_names(collector_database_url: str) -> Iterator[tuple[PgTarget, PgTarget]]:
    """(source, fresh target); skips when the compose postgres container is not reachable."""
    url = make_url(collector_database_url)
    assert url.username and url.database
    if not _compose_postgres_usable(url.username):
        pytest.skip("`docker compose exec` cannot reach the postgres service")
    source = PgTarget(url.username, url.database)
    # DEFAULT: the import target is `<main db>_import_test`, dropped before and after the test.
    main = url.database.removesuffix("_collector_test")
    fresh = PgTarget(url.username, f"{main}_import_test")
    drop = f'DROP DATABASE IF EXISTS "{fresh.database}" WITH (FORCE)'
    admin = PgTarget(url.username, "postgres")
    docker_runner(psql_command(admin, drop))
    yield source, fresh
    docker_runner(psql_command(admin, drop))


def test_export_then_import_into_a_fresh_database(
    engine: Engine,
    fake_github: FakeGitHub,
    tmp_path: Path,
    target_names: tuple[PgTarget, PgTarget],
) -> None:
    source, fresh = target_names
    src_data = tmp_path / "src-data"
    make_backfiller(SqlStore(engine), src_data).backfill_repo(ENTRY)
    expected_counts = raw_row_counts(docker_runner, source)
    assert expected_counts["raw.logs"] > 0 and expected_counts["raw.jobs"] > 0
    expected_files = collect_data_files(src_data)
    assert expected_files

    exported = export_data(
        member="test", data_dir=src_data, out_dir=tmp_path / "exports", target=source
    )

    dst_data = tmp_path / "dst-data"
    result = import_data(exported.archive, data_dir=dst_data, target=fresh)

    assert raw_row_counts(docker_runner, fresh) == expected_counts == result.row_counts
    assert collect_data_files(dst_data) == expected_files

    # a second import into the now non-empty database is refused unless forced
    with pytest.raises(TransferError, match="refusing to overwrite"):
        import_data(exported.archive, data_dir=dst_data, target=fresh)
    import_data(exported.archive, data_dir=dst_data, target=fresh, force=True)
    assert raw_row_counts(docker_runner, fresh) == expected_counts
