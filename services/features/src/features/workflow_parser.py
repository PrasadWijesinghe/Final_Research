"""Parse a GitHub Actions workflow file into contract `WorkflowJob`s (ARCHITECTURE.md 3).

Pure: text in, jobs out. No network, no database.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

import yaml

from shared.contracts import WorkflowJob
from shared.sanitise import sanitise

__all__ = ["WorkflowParseError", "parse_workflow"]

logger = logging.getLogger(__name__)

_MATRIX_REF = re.compile(r"^\$\{\{\s*matrix\.([\w-]+)\s*\}\}$")
_SETUP_NODE = re.compile(r"^actions/setup-node(@.*)?$")
# DEFAULT: with no setup-node step, matrix keys with these names still count as Node versions.
_NODE_MATRIX_KEYS = ("node", "node-version", "node_version")


class WorkflowParseError(ValueError):
    """The workflow text is not valid YAML or has no `jobs` mapping."""


def _scalar(value: Any) -> str:
    """Render a YAML scalar as the string a person would write (`20`, `20.x`, `lts/*`)."""
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _matrix_values(matrix: Any, key: str) -> list[str]:
    """Every value `matrix.<key>` can take: the listed values plus `include` entries."""
    if not isinstance(matrix, dict):
        return []
    values: list[str] = [_scalar(v) for v in _as_list(matrix.get(key)) if not isinstance(v, dict)]
    include = matrix.get("include")
    for entry in include if isinstance(include, list) else []:
        if isinstance(entry, dict) and key in entry:
            values.append(_scalar(entry[key]))
    return values


def _resolve(raw: Any, matrix: Any) -> list[str]:
    """Resolve a value that may be `${{ matrix.x }}` (or a list of values) to plain strings."""
    out: list[str] = []
    for item in _as_list(raw):
        if isinstance(item, dict):
            continue
        text = _scalar(item).strip()
        ref = _MATRIX_REF.match(text)
        if ref:
            out.extend(_matrix_values(matrix, ref.group(1)))
        elif text:
            out.append(text)
    return out


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _node_versions(spec: dict[str, Any], matrix: Any) -> list[str]:
    steps = spec.get("steps")
    versions: list[str] = []
    has_setup = False
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict):
            continue
        uses = step.get("uses")
        if isinstance(uses, str) and _SETUP_NODE.match(uses):
            has_setup = True
            with_ = step.get("with")
            if isinstance(with_, dict) and "node-version" in with_:
                versions.extend(_resolve(with_["node-version"], matrix))
    if not has_setup:
        for key in _NODE_MATRIX_KEYS:
            versions.extend(_matrix_values(matrix, key))
    return _dedupe(versions)


def _runs_on(spec: dict[str, Any], matrix: Any) -> list[str]:
    raw = spec.get("runs-on")
    if isinstance(raw, dict):  # {group: ..., labels: [...]}
        raw = raw.get("labels")
    return _dedupe(_resolve(raw, matrix))


def _permissions(job_value: Any, workflow_value: Any) -> dict[str, str]:
    """Job-level permissions replace workflow-level ones (as GitHub does)."""
    value = job_value if job_value is not None else workflow_value
    if isinstance(value, str):
        # DEFAULT: `read-all` / `write-all` become one entry under the key `*`.
        return {"*": value.split("-")[0]} if value in ("read-all", "write-all") else {}
    if isinstance(value, dict):
        return {str(k): _scalar(v) for k, v in value.items()}
    return {}


def _commands(spec: dict[str, Any], scrub: Callable[[str], str]) -> list[str]:
    commands: list[str] = []
    steps = spec.get("steps")
    for step in steps if isinstance(steps, list) else []:
        run = step.get("run") if isinstance(step, dict) else None
        if not isinstance(run, str):
            continue
        # DEFAULT: one entry per non-blank line of a multi-line `run:` block, whitespace trimmed.
        commands.extend(scrub(line.strip()) for line in run.splitlines() if line.strip())
    return commands


def _default_scrub(text: str) -> str:
    return sanitise(text).text


def parse_workflow(
    path: str, text: str, *, scrub: Callable[[str], str] = _default_scrub
) -> list[WorkflowJob]:
    """Return one `WorkflowJob` per job in the file, in YAML order.

    `run:` commands are passed through `scrub` (the secret sanitiser by default). Raises
    `WorkflowParseError` when the text is not a workflow.
    """
    try:
        workflow = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowParseError(f"{path}: invalid YAML") from exc
    jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
    if not isinstance(jobs, dict):
        raise WorkflowParseError(f"{path}: no `jobs` mapping")
    top_permissions = workflow.get("permissions")
    out: list[WorkflowJob] = []
    for key, raw in jobs.items():
        spec: dict[str, Any] = raw if isinstance(raw, dict) else {}
        strategy = spec.get("strategy")
        matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
        name = spec.get("name")
        out.append(
            WorkflowJob(
                workflow_path=path,
                job_key=str(key),
                job_name=name if isinstance(name, str) else None,
                runs_on=_runs_on(spec, matrix),
                node_versions=_node_versions(spec, matrix),
                permissions=_permissions(spec.get("permissions"), top_permissions),
                uses_container="container" in spec,
                commands=_commands(spec, scrub),
            )
        )
    return out
