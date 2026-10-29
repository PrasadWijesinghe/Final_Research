from __future__ import annotations

from pathlib import Path

import pytest
from collector.repo_config import (
    MEMBERS,
    ConfigError,
    load_all_repo_configs,
    load_member_config,
    load_repo_config,
)

REAL_CONFIG = Path(__file__).resolve().parents[3] / "config"


def write(tmp_path: Path, text: str, name: str = "test.yaml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_all_four_real_files_load() -> None:
    configs = load_all_repo_configs(REAL_CONFIG)
    assert set(configs) == set(MEMBERS)
    for member, cfg in configs.items():
        assert cfg.member == member
    assert configs["dependency"].repos == []


def test_real_build_config_content() -> None:
    cfg = load_member_config("build", REAL_CONFIG)
    assert len(cfg.repos) == 34  # 11 selected + 19 reserves + 4 licence-risky
    assert len(cfg.enabled_repos()) == 11
    risky = [r for r in cfg.repos if r.extras.get("commercial_kb") is False]
    assert risky and all(not r.enabled for r in risky)


def test_real_test_config_content_and_extras_preserved() -> None:
    cfg = load_member_config("test", REAL_CONFIG)
    assert len(cfg.repos) == 34  # 25 selected + 6 reserves + 3 licence-risky
    mongoose = next(r for r in cfg.repos if r.full_name == "Automattic/mongoose")
    assert mongoose.licence == "MIT"
    assert mongoose.enabled is True
    assert mongoose.workflows == []
    assert mongoose.extras["framework"] == "mocha"
    assert mongoose.extras["flaky"] == 14
    assert (mongoose.owner, mongoose.name) == ("Automattic", "mongoose")
    n8n = next(r for r in cfg.repos if r.full_name == "n8n-io/n8n")
    assert n8n.enabled is False
    assert n8n.extras["commercial_kb"] is False
    assert len(cfg.enabled_repos()) == 25


def test_defaults(tmp_path: Path) -> None:
    p = write(tmp_path, "member: test\nrepos:\n  - {full_name: a/b, licence: MIT}\n")
    r = load_repo_config(p).repos[0]
    assert r.workflows == [] and r.enabled is True and r.extras == {}


def test_explicit_workflows(tmp_path: Path) -> None:
    p = write(
        tmp_path,
        "member: test\nrepos:\n  - full_name: a/b\n    licence: MIT\n"
        "    workflows: ['.github/workflows/test.yml']\n",
    )
    assert load_repo_config(p).repos[0].workflows == [".github/workflows/test.yml"]


@pytest.mark.parametrize(
    ("text", "needles"),
    [
        ("member: test\nrepos:\n  - {full_name: a/b}\n", ["test.yaml", "a/b", "licence"]),
        ("member: test\nrepos:\n  - {full_name: ab, licence: MIT}\n", ["'ab'", "full_name"]),
        ("member: test\nrepos:\n  - {full_name: a/b/c, licence: MIT}\n", ["full_name"]),
        (
            "member: test\nrepos:\n  - {full_name: a/b, licence: MIT, enabled: 'yes'}\n",
            ["a/b", "enabled"],
        ),
        (
            "member: test\nrepos:\n  - {full_name: a/b, licence: MIT, workflows: x.yml}\n",
            ["a/b", "workflows"],
        ),
        ("member: test\nrepos:\n  - {full_name: a/b, licence: 1.5}\n", ["licence"]),
        ("member: nope\nrepos: []\n", ["member", "nope"]),
        ("member: build\nrepos: []\n", ["member", "'build'", "'test'"]),
        ("repos: []\n", ["member", "required"]),
        ("member: test\n", ["repos", "required"]),
        ("member: test\nrepos: {}\n", ["repos", "list"]),
        ("member: test\nrepos:\n  - just-a-string\n", ["mapping"]),
        ("- a\n- b\n", ["mapping"]),
        ("member: [unclosed\n", ["invalid YAML"]),
        ("", ["mapping"]),
    ],
)
def test_invalid_configs_have_clear_messages(tmp_path: Path, text: str, needles: list[str]) -> None:
    p = write(tmp_path, text)
    with pytest.raises(ConfigError) as exc:
        load_repo_config(p)
    message = str(exc.value)
    assert str(p) in message
    for needle in needles:
        assert needle in message, message


def test_duplicate_repos_error_case_insensitive(tmp_path: Path) -> None:
    p = write(
        tmp_path,
        "member: test\nrepos:\n  - {full_name: Foo/Bar, licence: MIT}\n"
        "  - {full_name: foo/bar, licence: MIT}\n",
    )
    with pytest.raises(ConfigError, match=r"foo/bar.*duplicate of entry #1"):
        load_repo_config(p)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_repo_config(tmp_path / "missing.yaml")


def test_unknown_member_name() -> None:
    with pytest.raises(ConfigError, match="unknown member"):
        load_member_config("ops", REAL_CONFIG)
