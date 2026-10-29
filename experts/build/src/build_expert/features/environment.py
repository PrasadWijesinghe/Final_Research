"""Feature group C: the CI environment the commit will build in (docs/experts/build.md section 4).

Comes from the workflow jobs parsed at this commit (`CommitContext.workflow_jobs`): Node versions,
runner operating systems, package manager and which build-type commands the CI runs.
"""

from __future__ import annotations

import re

from shared.contracts import CommitContext

_MAJOR = re.compile(r"^v?(\d{1,2})(?:\.|$|\.x)")
_PM = re.compile(r"(?<![\w./@-])(npm|pnpm|yarn|bun)\b")
_TYPECHECK = re.compile(
    r"(?<![\w-])(?:tsc|vue-tsc|svelte-check|typecheck|type-check|check-types|types)(?![\w-])"
)
_LINT = re.compile(r"(?<![\w-])(?:lint|eslint|oxlint|biome)(?![\w-])")
_FORMAT = re.compile(r"prettier\b.*(?:--check|-c\b)|format:check|fmt:check|biome (?:format|ci)")
_BUNDLE = re.compile(
    r"(?<![\w-])(?:build|webpack|vite build|rollup|esbuild|tsup|rspack|next build|nuxi build)"
    r"(?![\w-])"
)
_MONOREPO = re.compile(
    r"(?<![\w./@-])(?:turbo|nx|lerna)\b|\bpnpm\s+(?:-r|--recursive|--filter|-F)\b"
)
_BOT_BRANCH = re.compile(r"^(?:dependabot|renovate|snyk-|greenkeeper)", re.I)

NAMES: tuple[str, ...] = (
    "n_workflow_jobs",
    "n_node_versions",
    "node_major_min",
    "node_major_max",
    "node_version_unresolved",
    "runs_on_windows",
    "runs_on_macos",
    "package_manager",
    "uses_monorepo_runner",
    "ci_has_typecheck",
    "ci_has_lint",
    "ci_has_format_check",
    "ci_has_bundle",
    "is_pull_request",
    "is_default_branch",
    "is_bot_branch",
)


def _majors(versions: list[str]) -> list[int]:
    out = []
    for v in versions:
        m = _MAJOR.match(v.strip())
        if m:
            out.append(int(m.group(1)))
    return out


def environment_features(ctx: CommitContext, *, default_branch: str | None) -> dict[str, object]:
    """Group C features for one commit."""
    jobs = ctx.workflow_jobs
    versions = sorted({v for j in jobs for v in j.node_versions})
    majors = _majors(versions)
    commands = [c for j in jobs for c in j.commands]
    runners = " ".join(r for j in jobs for r in j.runs_on).lower()
    managers = [m.group(1) for c in commands for m in _PM.finditer(c)]
    manager = max(set(managers), key=managers.count) if managers else "unknown"
    return {
        "n_workflow_jobs": len(jobs),
        "n_node_versions": len(versions),
        "node_major_min": min(majors) if majors else None,
        "node_major_max": max(majors) if majors else None,
        "node_version_unresolved": sum(1 for v in versions if not _MAJOR.match(v.strip())),
        "runs_on_windows": "windows" in runners,
        "runs_on_macos": "macos" in runners,
        "package_manager": manager,
        "uses_monorepo_runner": any(_MONOREPO.search(c) for c in commands),
        "ci_has_typecheck": any(_TYPECHECK.search(c) for c in commands),
        "ci_has_lint": any(_LINT.search(c) for c in commands),
        "ci_has_format_check": any(_FORMAT.search(c) for c in commands),
        "ci_has_bundle": any(_BUNDLE.search(c) for c in commands),
        "is_pull_request": ctx.pr_number is not None,
        "is_default_branch": default_branch is not None and ctx.branch == default_branch,
        "is_bot_branch": bool(_BOT_BRANCH.match(ctx.branch)),
    }
