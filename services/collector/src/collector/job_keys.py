"""Map a GitHub job name (`test (20)`) to its job key in the workflow YAML (`test`)."""

from __future__ import annotations

import re
from typing import Any

import yaml

_EXPR = re.compile(r"\$\{\{.*?\}\}")


def _template_regex(template: str) -> re.Pattern[str]:
    parts = _EXPR.split(template)
    body = ".+?".join(re.escape(p) for p in parts)
    return re.compile(body + r"(?: \(.+\))?")


def job_display_names(workflow_yaml: str) -> dict[str, str] | None:
    """Return {job key: display name template}, or None if the YAML cannot be read."""
    try:
        workflow = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return None
    jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
    if not isinstance(jobs, dict):
        return None
    names: dict[str, str] = {}
    for key, job in jobs.items():
        spec: Any = job if isinstance(job, dict) else {}
        name = spec.get("name")
        names[str(key)] = name if isinstance(name, str) and name.strip() else str(key)
    return names


def map_job_key(job_name: str, workflow_yaml: str | None) -> str | None:
    """Return the YAML job key for `job_name`, or None when unknown or ambiguous."""
    if not workflow_yaml:
        return None
    names = job_display_names(workflow_yaml)
    if not names:
        return None
    # Tier 1: the name is exactly a job key or exactly a job's `name:`.
    exact = {k for k, display in names.items() if job_name in (k, display)}
    if len(exact) == 1:
        return next(iter(exact))
    if exact:
        return None
    # Tier 2: matrix suffix `key (20)`, an expression in `name:`, or a reusable workflow
    # call `caller / callee`.
    loose: set[str] = set()
    for key, display in names.items():
        if _EXPR.search(display):
            if _template_regex(display).fullmatch(job_name):
                loose.add(key)
            continue
        for base in {key, display}:
            if re.fullmatch(re.escape(base) + r" \(.+\)", job_name) or job_name.startswith(
                base + " / "
            ):
                loose.add(key)
    return next(iter(loose)) if len(loose) == 1 else None
