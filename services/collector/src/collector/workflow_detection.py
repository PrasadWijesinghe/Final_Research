"""Decide whether a GitHub Actions workflow file is relevant to a member (docs/DATA.md section 2).

Pure functions: no network, no database. Storing the result per file version
(`raw.workflows.detected_at_sha`) is done by the backfill (P1.5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from collector.repo_config import DEFAULT_CONFIG_DIR, ConfigError


class MemberRules(BaseModel):
    """Detection patterns for one member (regexes applied to each line of a `run:` step)."""

    model_config = ConfigDict(extra="forbid")

    run_patterns: list[str] = Field(min_length=1)
    exclude_patterns: list[str] = Field(default_factory=list)


class DetectionRules(BaseModel):
    """Contents of `config/workflow_detection.yaml`."""

    model_config = ConfigDict(extra="forbid")

    members: dict[str, MemberRules] = Field(default_factory=dict)


@dataclass(frozen=True)
class DetectionResult:
    """Outcome of detection for one workflow file."""

    relevant: bool
    reason: str  # "matched", "no match", "no rule for member", "unparseable"
    matches: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # (job key, command line)


def load_detection_rules(path: Path | str | None = None) -> DetectionRules:
    """Load `config/workflow_detection.yaml` and check every regex compiles."""
    path = Path(path) if path else DEFAULT_CONFIG_DIR / "workflow_detection.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: cannot load: {exc}") from exc
    try:
        rules = DetectionRules.model_validate(data or {})
    except ValidationError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    for member, mr in rules.members.items():
        for pat in [*mr.run_patterns, *mr.exclude_patterns]:
            try:
                re.compile(pat)
            except re.error as exc:
                raise ConfigError(f"{path}: member {member!r}: bad regex {pat!r}: {exc}") from exc
    return rules


def _run_lines(workflow: dict[Any, Any]) -> list[tuple[str, str]]:
    """Every (job key, line) from `run:` steps. Tolerates odd shapes."""
    out: list[tuple[str, str]] = []
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return out
    for key, job in jobs.items():
        steps = job.get("steps") if isinstance(job, dict) else None
        if not isinstance(steps, list):
            continue
        for step in steps:
            run = step.get("run") if isinstance(step, dict) else None
            if isinstance(run, str):
                out.extend((str(key), ln.strip()) for ln in run.splitlines() if ln.strip())
    return out


def detect_relevant_workflow(
    workflow_yaml: str, member: str, rules: DetectionRules | None = None
) -> DetectionResult:
    """Say whether a workflow has a job running the member's category (tests for 'test')."""
    rules = rules or load_detection_rules()
    member_rules = rules.members.get(member)
    if member_rules is None:
        # DEFAULT: members without a detection rule (dependency, build, infra) keep every
        # workflow until their owner adds a rule to config/workflow_detection.yaml.
        return DetectionResult(True, "no rule for member")
    try:
        # safe_load never builds arbitrary objects. The key `on:` parses as boolean True; that
        # is harmless because only `jobs` is read.
        workflow = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return DetectionResult(False, "unparseable")
    if not isinstance(workflow, dict):
        return DetectionResult(False, "unparseable")
    include = [re.compile(p) for p in member_rules.run_patterns]
    exclude = [re.compile(p) for p in member_rules.exclude_patterns]
    matches = tuple(
        (job, line)
        for job, line in _run_lines(workflow)
        if any(p.search(line) for p in include) and not any(p.search(line) for p in exclude)
    )
    if matches:
        return DetectionResult(True, "matched", matches)
    return DetectionResult(False, "no match")
