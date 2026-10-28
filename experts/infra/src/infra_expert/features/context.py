"""Convert a shared CommitContext into the pre-execution Infra model fields."""

from __future__ import annotations

import re
from typing import Any

import numpy as np
import pandas as pd

from shared.contracts import CommitContext, FileTag

from .model_features import prepare_features

_DEFAULT_BRANCHES = {"main", "master"}
_USES_ACTION = re.compile(r"(?im)^\s*-?\s*uses\s*:\s*([^\s#]+)")
_SECRET_REFERENCE = re.compile(r"\bsecrets\.[A-Za-z_][A-Za-z0-9_]*", re.IGNORECASE)


def _branch_category(branch: str) -> str:
    normalised = branch.lower().strip()
    if normalised in _DEFAULT_BRANCHES:
        return "Default branch"
    if "dependabot" in normalised or "renovate" in normalised or "dependenc" in normalised:
        return "Dependency update"
    if any(part in normalised for part in ("release", "backport", "cherry-pick")):
        return "Release or backport"
    if any(part in normalised for part in ("hotfix", "fix/", "bugfix", "bug/")):
        return "Fix branch"
    if normalised.startswith(("feature/", "feat/")):
        return "Feature branch"
    return "Other branch"


def _workflow_text(context: CommitContext) -> str:
    return "\n".join(
        snapshot.head_content or ""
        for snapshot in context.snapshots
        if snapshot.tag == FileTag.WORKFLOW
    )


def _runner_labels(context: CommitContext) -> str:
    labels = dict.fromkeys(label for job in context.workflow_jobs for label in job.runs_on)
    return "; ".join(labels)


def _third_party_actions(workflow_text: str) -> int:
    actions = _USES_ACTION.findall(workflow_text)
    # Match the workbook collector: docker:// actions count as third-party actions too.
    return sum(
        not action.lower().startswith("actions/") and not action.startswith("./")
        for action in actions
    )


def workflow_signals(workflow_text: str) -> dict[str, Any]:
    """Match the historical workbook's workflowSignals extraction, including unknowns."""
    fields = (
        "Configured_Job_Count",
        "Runner_Labels",
        "Uses_Self_Hosted_Runner",
        "Permissions_Defined",
        "Docker_Used",
        "Services_Used",
        "Matrix_Used",
        "Cache_Action_Used",
        "Timeout_Defined",
        "Continue_On_Error_Used",
        "Secret_Reference_Count",
        "Third_Party_Action_Count",
        "Workflow_Line_Count",
    )
    if not workflow_text:
        return dict.fromkeys(fields, np.nan)
    runners = [
        label.strip().replace('"', "").replace("'", "")
        for label in re.findall(r"(?im)^\s*runs-on\s*:\s*([^#\r\n]+)", workflow_text)
    ]
    return {
        "Configured_Job_Count": len(runners) or np.nan,
        "Runner_Labels": "; ".join(dict.fromkeys(runners)) or np.nan,
        "Uses_Self_Hosted_Runner": any("self-hosted" in runner.lower() for runner in runners),
        "Permissions_Defined": bool(re.search(r"(?im)^\s*permissions\s*:", workflow_text)),
        "Docker_Used": bool(
            re.search(r"(?im)(^|\s)(docker|container\s*:|services\s*:|buildx)", workflow_text)
        ),
        "Services_Used": bool(re.search(r"(?im)^\s*services\s*:", workflow_text)),
        "Matrix_Used": bool(re.search(r"(?im)strategy\s*:[\s\S]{0,400}?matrix\s*:", workflow_text)),
        "Cache_Action_Used": bool(
            re.search(
                r"(?i)uses\s*:\s*actions/cache(?:/[^@\s]+)?@|cache-dependency-path\s*:",
                workflow_text,
            )
        ),
        "Timeout_Defined": bool(re.search(r"(?im)^\s*timeout-minutes\s*:", workflow_text)),
        "Continue_On_Error_Used": bool(re.search(r"(?im)^\s*continue-on-error\s*:", workflow_text)),
        "Secret_Reference_Count": len(_SECRET_REFERENCE.findall(workflow_text)),
        "Third_Party_Action_Count": _third_party_actions(workflow_text),
        # JS split counts a final newline as an empty line; str.splitlines() did not.
        "Workflow_Line_Count": len(re.split(r"\r?\n", workflow_text)),
    }


def raw_features_from_context(context: CommitContext) -> dict[str, Any]:
    """Create the exact raw fields expected by the saved no-history research model."""
    workflow_text = _workflow_text(context)
    runner_labels = _runner_labels(context)
    infra_tags = {FileTag.WORKFLOW, FileTag.DOCKERFILE, FileTag.DEPLOY_CONFIG}
    infra_files = [file for file in context.changed_files if infra_tags.intersection(file.tags)]

    event = "push" if context.event in {"pre_push", "replay"} else context.event
    dockerfile_changed = bool(context.files_with(FileTag.DOCKERFILE))

    result: dict[str, Any] = {
        "Trigger_Event": event,
        "Branch_Category": _branch_category(context.branch),
        "Is_Default_Branch": context.branch.lower() in _DEFAULT_BRANCHES,
        "Is_Pull_Request": context.event == "pull_request",
        "Is_Scheduled": False,
        "Workflow_Changed": bool(context.files_with(FileTag.WORKFLOW)),
        "Dockerfile_Changed": dockerfile_changed,
        "Deployment_Config_Changed": bool(context.files_with(FileTag.DEPLOY_CONFIG)),
        "Run_Attempt": 1,
        "Files_Changed_Count": len(context.changed_files),
        "Lines_Added": sum(file.additions for file in context.changed_files),
        "Lines_Deleted": sum(file.deletions for file in context.changed_files),
        "Infra_Files_Changed_Count": len(infra_files),
        # The serving model intentionally excludes history; these keep one canonical raw schema.
        "Repo_Prior_Runs_20": np.nan,
        "Repo_Prior_Failure_Rate_20": np.nan,
        "Workflow_Prior_Runs_10": np.nan,
        "Workflow_Prior_Failure_Rate_10": np.nan,
        "Hours_Since_Previous_Workflow_Run": np.nan,
    }
    result.update(workflow_signals(workflow_text))
    if not workflow_text and context.workflow_jobs:
        # Typed jobs still provide actual runner information when snapshots are unavailable.
        result["Runner_Labels"] = runner_labels or np.nan
    return result


def prepared_features_from_context(context: CommitContext) -> pd.DataFrame:
    """Return one model-ready row for a commit."""
    return prepare_features(pd.DataFrame([raw_features_from_context(context)]))
