"""CLI behaviour that does not need a database or the network."""

from __future__ import annotations

import pytest
from collector.cli import main


def _env(monkeypatch: pytest.MonkeyPatch, **values: str | None) -> None:
    for name in ("GITHUB_TOKEN", "IDENTITY_SALT", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        if value is not None:
            monkeypatch.setenv(name, value)


def test_missing_token_is_a_clear_error_with_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(monkeypatch, IDENTITY_SALT="fake-salt", DATABASE_URL="postgresql+psycopg://x@127.0.0.1/x")
    assert main(["backfill", "--member", "test"]) == 2
    err = capsys.readouterr().err
    assert "GITHUB_TOKEN" in err and "IDENTITY_SALT" not in err


def test_missing_salt_and_database_are_reported_together(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(monkeypatch, GITHUB_TOKEN="fake-token-for-tests-only")
    assert main(["backfill", "--member", "test"]) == 2
    err = capsys.readouterr().err
    assert "IDENTITY_SALT" in err and "DATABASE_URL" in err
    assert "fake-token-for-tests-only" not in err


def test_unknown_repo_is_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(
        monkeypatch,
        GITHUB_TOKEN="fake-token-for-tests-only",
        IDENTITY_SALT="fake-salt",
        DATABASE_URL="postgresql+psycopg://x@127.0.0.1/x",
    )
    assert main(["backfill", "--member", "test", "--repo", "nobody/nothing"]) == 2
    assert "nobody/nothing" in capsys.readouterr().err


def test_invalid_member_is_rejected_by_argparse() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["backfill", "--member", "nope"])
    assert exc.value.code == 2
