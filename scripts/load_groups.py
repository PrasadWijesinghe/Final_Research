"""Merge selected Dependency repository candidates into the member repo config."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml
from collector.repo_config import load_repo_config
from dependency_expert.prototype_dataset import load_repository_group

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GROUPS = REPO_ROOT / "config" / "dependency_repository_groups.yaml"
DEFAULT_CONFIG = REPO_ROOT / "config" / "repos" / "dependency.yaml"


def load_all_groups(groups_path: Path) -> list[str]:
    """Load unique repository candidates from all configured groups."""
    with groups_path.open(encoding="utf-8") as groups_file:
        data = yaml.safe_load(groups_file)
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, dict):
        raise ValueError(f"{groups_path} must contain a groups mapping")

    repositories: list[str] = []
    for group_name in groups:
        repositories.extend(load_repository_group(groups_path, group_name))
    return list(dict.fromkeys(repositories))


def merge_group_into_config(
    groups_path: Path,
    config_path: Path,
    *,
    group: str | None = None,
    all_groups: bool = False,
) -> int:
    """Merge candidates into config without overwriting existing repo settings."""
    if (group is None) == (not all_groups):
        raise ValueError("choose exactly one group or all_groups=True")
    candidates = (
        load_all_groups(groups_path)
        if all_groups
        else load_repository_group(groups_path, group or "")
    )

    if config_path.exists():
        existing_config = load_repo_config(config_path, member="dependency")
        config: dict[str, Any] = {
            "member": "dependency",
            "repos": [
                {
                    "full_name": repo.full_name,
                    "licence": repo.licence,
                    "workflows": repo.workflows,
                    "enabled": repo.enabled,
                    **repo.extras,
                }
                for repo in existing_config.repos
            ],
        }
    else:
        config = {"member": "dependency", "repos": []}

    entries: list[dict[str, Any]] = config["repos"]
    existing_names = {entry["full_name"].casefold() for entry in entries}
    added = 0
    for repository in candidates:
        if repository.casefold() in existing_names:
            continue
        entries.append(
            {
                "full_name": repository,
                # DEFAULT: unknown licences must be reviewed before a candidate is enabled.
                "licence": "TODO",
                "enabled": False,
            }
        )
        existing_names.add(repository.casefold())
        added += 1

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8", newline="\n") as output_file:
        yaml.safe_dump(config, output_file, sort_keys=False, allow_unicode=True)
    return added


def main() -> None:
    """Parse selection options and update the Dependency repo configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--group", help="add one named candidate group")
    selection.add_argument(
        "--all-groups",
        action="store_true",
        help="add candidates from every group",
    )
    parser.add_argument("--groups-file", type=Path, default=DEFAULT_GROUPS)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()

    added = merge_group_into_config(
        args.groups_file,
        args.config,
        group=args.group,
        all_groups=args.all_groups,
    )
    print(f"Added {added} new candidates to {args.config}.")
    print("New entries remain disabled until reviewed.")
    print("Check each licence, then enable chosen repos before collecting.")


if __name__ == "__main__":
    main()
