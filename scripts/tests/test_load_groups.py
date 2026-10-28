"""Tests for adding Dependency research groups to the member repo config."""

from pathlib import Path

import yaml
from load_groups import merge_group_into_config


def _write_groups(path: Path) -> None:
    path.write_text(
        "groups:\n"
        "  first:\n"
        "    - owner/one\n"
        "    - owner/shared\n"
        "  second:\n"
        "    - owner/two\n"
        "    - owner/shared\n",
        encoding="utf-8",
    )


def _read_config(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as config_file:
        data = yaml.safe_load(config_file)
    assert isinstance(data, dict)
    return data


def test_selected_group_adds_only_its_repos_disabled(tmp_path: Path) -> None:
    groups_path = tmp_path / "groups.yaml"
    config_path = tmp_path / "repos" / "dependency.yaml"
    _write_groups(groups_path)

    assert merge_group_into_config(groups_path, config_path, group="first") == 2
    entries = _read_config(config_path)["repos"]
    assert isinstance(entries, list)
    assert [entry["full_name"] for entry in entries] == ["owner/one", "owner/shared"]
    assert all(entry["enabled"] is False and entry["licence"] == "TODO" for entry in entries)


def test_all_groups_adds_unique_candidates(tmp_path: Path) -> None:
    groups_path = tmp_path / "groups.yaml"
    config_path = tmp_path / "dependency.yaml"
    _write_groups(groups_path)

    assert merge_group_into_config(groups_path, config_path, all_groups=True) == 3
    entries = _read_config(config_path)["repos"]
    assert isinstance(entries, list)
    assert len(entries) == 3


def test_existing_repo_settings_are_preserved_and_not_duplicated(tmp_path: Path) -> None:
    groups_path = tmp_path / "groups.yaml"
    config_path = tmp_path / "dependency.yaml"
    _write_groups(groups_path)
    config_path.write_text(
        "member: dependency\nrepos:\n"
        "  - full_name: owner/one\n"
        "    licence: Apache-2.0\n"
        "    workflows: [.github/workflows/ci.yml]\n"
        "    enabled: true\n",
        encoding="utf-8",
    )

    assert merge_group_into_config(groups_path, config_path, group="first") == 1
    entries = _read_config(config_path)["repos"]
    assert isinstance(entries, list)
    assert entries[0] == {
        "full_name": "owner/one",
        "licence": "Apache-2.0",
        "workflows": [".github/workflows/ci.yml"],
        "enabled": True,
    }
    assert [entry["full_name"] for entry in entries] == ["owner/one", "owner/shared"]


def test_requires_one_selection(tmp_path: Path) -> None:
    groups_path = tmp_path / "groups.yaml"
    config_path = tmp_path / "dependency.yaml"
    _write_groups(groups_path)

    try:
        merge_group_into_config(groups_path, config_path)
    except ValueError as error:
        assert "choose exactly one" in str(error)
    else:
        raise AssertionError("missing selection was accepted")
