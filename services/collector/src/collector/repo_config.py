"""Load and validate `config/repos/<member>.yaml` (docs/DATA.md section 2)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, ValidationError

Member = Literal["dependency", "build", "test", "infra"]
MEMBERS: tuple[str, ...] = get_args(Member)

# DEFAULT: derived from this file's location (repo/services/collector/src/collector/); callers
# can pass any directory, this only serves the CLI when run from a source checkout.
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[4] / "config"

_FULL_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ConfigError(ValueError):
    """A config file is invalid; the message names the file, repo and field."""


class RepoEntry(BaseModel):
    """One repo in a member's list. Extra keys (framework, flaky, note, ...) are preserved."""

    model_config = ConfigDict(extra="allow")

    full_name: StrictStr
    licence: StrictStr
    workflows: list[StrictStr] = Field(default_factory=list)  # empty = auto-detect
    enabled: StrictBool = True

    @property
    def owner(self) -> str:
        """The GitHub owner part of full_name."""
        return self.full_name.split("/", 1)[0]

    @property
    def name(self) -> str:
        """The repository name part of full_name."""
        return self.full_name.split("/", 1)[1]

    @property
    def extras(self) -> dict[str, Any]:
        """Keys beyond the documented ones, as written in the file."""
        return dict(self.model_extra or {})


class RepoConfig(BaseModel):
    """A validated `config/repos/<member>.yaml`."""

    model_config = ConfigDict(extra="allow")

    member: Member
    repos: list[RepoEntry]

    def enabled_repos(self) -> list[RepoEntry]:
        """Repos with `enabled: true`, in file order."""
        return [r for r in self.repos if r.enabled]


def _fmt_errors(path: Path, repo: str, err: ValidationError) -> str:
    return "\n".join(
        f"{path}: repo {repo!r}, field {'.'.join(str(p) for p in e['loc'])!r}: {e['msg']}"
        for e in err.errors()
    )


def load_repo_config(path: Path | str, *, member: str | None = None) -> RepoConfig:
    """Parse and validate one repo config file; `member` (default: file stem) must match."""
    path = Path(path)
    expected = member or path.stem
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read file: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping with 'member' and 'repos'")
    if "member" not in data:
        raise ConfigError(f"{path}: field 'member': is required")
    if data["member"] not in MEMBERS:
        raise ConfigError(
            f"{path}: field 'member': {data['member']!r} is not one of {', '.join(MEMBERS)}"
        )
    if data["member"] != expected:
        raise ConfigError(
            f"{path}: field 'member': is {data['member']!r} but the file is for {expected!r}"
        )
    repos = data.get("repos")
    if repos is None:
        raise ConfigError(f"{path}: field 'repos': is required (use [] for an empty list)")
    if not isinstance(repos, list):
        raise ConfigError(f"{path}: field 'repos': must be a list")

    entries: list[RepoEntry] = []
    seen: dict[str, int] = {}
    for i, raw in enumerate(repos, start=1):
        name = raw.get("full_name") if isinstance(raw, dict) else None
        label = name if isinstance(name, str) else f"<entry #{i}>"
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: repo {label!r}: entry must be a mapping")
        try:
            entry = RepoEntry.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(_fmt_errors(path, label, exc)) from exc
        if not _FULL_NAME.match(entry.full_name):
            raise ConfigError(
                f"{path}: repo {label!r}: field 'full_name': must look like 'owner/repo'"
            )
        key = entry.full_name.lower()  # GitHub names are case-insensitive
        if key in seen:
            raise ConfigError(
                f"{path}: repo {label!r}: duplicate of entry #{seen[key]} (entry #{i})"
            )
        seen[key] = i
        entries.append(entry)
    extras = {k: v for k, v in data.items() if k not in ("member", "repos")}
    return RepoConfig(member=data["member"], repos=entries, **extras)


def load_member_config(member: str, config_dir: Path | str | None = None) -> RepoConfig:
    """Load `<config_dir>/repos/<member>.yaml` (default: the checkout's `config/`)."""
    if member not in MEMBERS:
        raise ConfigError(f"unknown member {member!r}; expected one of {', '.join(MEMBERS)}")
    base = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    return load_repo_config(base / "repos" / f"{member}.yaml", member=member)


def load_all_repo_configs(config_dir: Path | str | None = None) -> dict[str, RepoConfig]:
    """Load all four members' configs, keyed by member."""
    return {m: load_member_config(m, config_dir) for m in MEMBERS}
