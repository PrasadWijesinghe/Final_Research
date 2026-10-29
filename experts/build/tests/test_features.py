"""Feature groups A-D on hand-made commit contexts (docs/experts/build.md section 4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from build_expert.features import (
    FEATURE_GROUPS,
    FEATURE_NAMES,
    CommitOutcome,
    HistoryIndex,
    commit_features,
    history_for,
    module_dirs,
)
from build_expert.features.patches import module_dir

from shared.contracts import ChangedFile, CommitContext, FileTag, RepoRef, WorkflowJob

T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
AUTHOR = "c" * 64
DESCRIPTIONS = (
    Path(__file__).resolve().parents[1] / "src" / "build_expert" / "features" / "descriptions.yaml"
)


def _f(
    path: str, tags: list[FileTag], patch: str | None, add: int = 1, rem: int = 0, **kw: Any
) -> ChangedFile:
    return ChangedFile(
        path=path,
        status=kw.get("status", "modified"),
        additions=add,
        deletions=rem,
        patch=patch,
        tags=tags,
        previous_path=kw.get("previous_path"),
    )


SOURCE_PATCH = """@@ -1,6 +1,9 @@
+import { z } from "zod";
-import { old } from "./old";
+export interface Payload { id: string }
-export function legacy() {}
+export const parse = (v: any): Payload => v as any;
+// @ts-expect-error wrong on purpose
+const n = maybe!.value;
+console.log(n);
+/* eslint-disable no-console */"""
TSCONFIG_PATCH = """@@ -1,4 +1,6 @@
-    "strict": false,
+    "strict": true,
+    "noUncheckedIndexedAccess": true,
-    "target": "ES2020",
+    "target": "ES2022",
+    "paths": { "@/*": ["src/*"] },"""
ESLINT_PATCH = """@@ -1,3 +1,4 @@
+import tseslint from "typescript-eslint";
-    "no-console": "warn",
+    "no-console": "error",
+    eqeqeq: ["error", "always"],"""
PKG_PATCH = """@@ -3,6 +3,6 @@
-    "build": "tsc -b",
+    "build": "tsup src/index.ts",
-    "typescript": "^5.4.0",
+    "typescript": "^5.6.0",
+    "@types/node": "^22.0.0",
+    "type": "module","""
WORKFLOW_PATCH = "@@ -1 +1 @@\n-          node-version: 18\n+          node-version: 22"


def _ctx(files: list[ChangedFile], **kw: Any) -> CommitContext:
    jobs = kw.pop("jobs", [])
    return CommitContext(
        repo=RepoRef(owner="acme", name="web"),
        head_sha="d" * 40,
        event="replay",
        branch=str(kw.pop("branch", "main")),
        pr_number=kw.pop("pr", None),
        author_hash=AUTHOR,
        committed_at=T0,
        history_cutoff=T0,
        changed_files=files,
        workflow_jobs=jobs,
        truncated=bool(kw.pop("truncated", False)),
    )


def _full_ctx() -> CommitContext:
    files = [
        _f("packages/core/src/api.ts", [FileTag.SOURCE], SOURCE_PATCH, add=7, rem=2),
        _f("packages/core/src/old.ts", [FileTag.SOURCE], None, add=0, rem=10, status="removed"),
        _f("packages/core/types.d.ts", [FileTag.SOURCE], "@@ +1 @@\n+declare const x: 1;"),
        _f("packages/core/test/api.test.ts", [FileTag.TEST], "@@ +1 @@\n+it('x')"),
        _f("tsconfig.json", [FileTag.BUILD_CONFIG], TSCONFIG_PATCH, add=4, rem=2),
        _f("eslint.config.mjs", [FileTag.LINT_CONFIG], ESLINT_PATCH, add=3, rem=1),
        _f("package.json", [FileTag.PACKAGE_MANIFEST], PKG_PATCH, add=4, rem=2),
        _f("pnpm-lock.yaml", [FileTag.LOCKFILE], None, add=40, rem=30),
        _f(".github/workflows/ci.yml", [FileTag.WORKFLOW], WORKFLOW_PATCH),
        _f(".nvmrc", [FileTag.OTHER], "@@ -1 +1 @@\n-18\n+22"),
        _f("docs/intro.md", [FileTag.DOCS], "@@ +1 @@\n+hi"),
    ]
    jobs = [
        WorkflowJob(
            workflow_path=".github/workflows/ci.yml",
            job_key="quality",
            runs_on=["ubuntu-latest", "windows-latest"],
            node_versions=["18", "20.x", "lts/*"],
            commands=[
                "pnpm install",
                "pnpm turbo run typecheck",
                "pnpm run lint",
                "npx prettier --check .",
                "pnpm run build",
            ],
        )
    ]
    return _ctx(files, jobs=jobs, branch="renovate/typescript-5.x", pr=7)


def test_group_a_change_features() -> None:
    f = commit_features(_full_ctx(), default_branch="main")
    assert f["n_files"] == 11
    assert (f["n_source_files"], f["n_ts_files"], f["n_test_files"]) == (3, 3, 1)
    assert f["n_declaration_files"] == 1
    assert (f["n_removed_files"], f["n_removed_or_renamed_source"]) == (1, 1)
    assert (f["imports_added"], f["imports_removed"]) == (1, 1)
    assert (f["exports_added"], f["exports_removed"]) == (2, 1)
    assert f["type_decls_changed"] == 1
    assert f["any_added"] == 1  # one line with two `any`s counts once
    assert f["ts_suppressions_added"] == 1
    assert f["eslint_disables_added"] == 1
    assert f["console_calls_added"] == 1
    assert f["non_null_assertions_added"] == 1
    assert f["patches_missing"] == 1
    assert f["n_module_dirs"] == 4  # packages/core, "." (root files), .github, docs
    assert f["docs_only"] is False and f["tests_only"] is False


def test_group_b_config_features() -> None:
    f = commit_features(_full_ctx(), default_branch="main")
    assert f["tsconfig_changed"] == 1
    assert f["tsconfig_strictness_lines"] == 3
    assert f["tsconfig_target_module_lines"] == 2
    assert f["tsconfig_paths_lines"] == 1
    assert f["eslint_config_changed"] == 1
    assert f["eslint_severity_lines"] == 3
    assert f["eslint_rules_set_to_error"] == 2
    assert f["eslint_extends_plugins_lines"] == 1
    assert f["build_script_lines"] == 2
    assert f["build_tool_dep_lines"] == 2
    assert f["typescript_version_changed"] == 1
    assert f["types_dep_lines"] == 1
    assert f["module_field_lines"] == 1
    assert f["lockfile_changed"] == 1
    assert (f["workflow_changed"], f["workflow_node_version_lines"]) == (1, 2)
    assert f["node_version_file_changed"] == 1


def test_group_c_environment_features() -> None:
    f = commit_features(_full_ctx(), default_branch="main")
    assert (f["n_node_versions"], f["node_major_min"], f["node_major_max"]) == (3, 18, 20)
    assert f["node_version_unresolved"] == 1
    assert f["runs_on_windows"] is True and f["runs_on_macos"] is False
    assert f["package_manager"] == "pnpm"
    assert f["uses_monorepo_runner"] is True
    assert f["ci_has_typecheck"] and f["ci_has_lint"] and f["ci_has_format_check"]
    assert f["ci_has_bundle"] is True
    assert f["is_pull_request"] is True and f["is_default_branch"] is False
    assert f["is_bot_branch"] is True


def test_docs_only_and_tests_only() -> None:
    docs = commit_features(
        _ctx([_f("README.md", [FileTag.DOCS], "@@ +1 @@\n+x")]), default_branch="main"
    )
    assert docs["docs_only"] is True and docs["tests_only"] is False
    assert docs["is_default_branch"] is True
    tests = commit_features(
        _ctx([_f("src/a.test.ts", [FileTag.TEST], "@@ +1 @@\n+x")]), default_branch="main"
    )
    assert tests["tests_only"] is True and tests["docs_only"] is False


def test_groups_cover_exactly_the_declared_names() -> None:
    f = commit_features(_full_ctx(), default_branch="main")
    abc = FEATURE_GROUPS["change"] + FEATURE_GROUPS["config"] + FEATURE_GROUPS["environment"]
    assert set(f) == set(abc)
    h = history_for(
        HistoryIndex([]),
        repo="r",
        cutoff=T0,
        branch="b",
        author_hash=None,
        dirs=frozenset(),
        default_branch=None,
    )
    assert set(h) == set(FEATURE_GROUPS["history"])
    assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES))


def test_every_feature_has_a_plain_language_description() -> None:
    text = yaml.safe_load(DESCRIPTIONS.read_text(encoding="utf-8"))
    assert set(text) == set(FEATURE_NAMES)
    assert all(0 < len(v) <= 200 for v in text.values())


def _o(hours_before: float, *, failed: bool, branch: str = "main", **kw: Any) -> CommitOutcome:
    return CommitOutcome(
        repo="r",
        sha=f"{int(hours_before * 10):040x}",
        branch=branch,
        author_hash=kw.get("author"),
        completed_at=T0 - timedelta(hours=hours_before),
        is_default_branch=branch == "main",
        build_failed=failed,
        sub_labels=frozenset(kw.get("subs", {"tsc"} if failed else set())),
        any_failed=failed or bool(kw.get("other_fail")),
        dirs=frozenset(kw.get("dirs", {"packages/core"})),
        lint_warnings=kw.get("lint"),
        type_errors=kw.get("types"),
    )


def _hist(outcomes: list[CommitOutcome], **kw: Any) -> dict[str, object]:
    return history_for(
        HistoryIndex(outcomes),
        repo="r",
        cutoff=T0,
        branch=str(kw.get("branch", "main")),
        author_hash=kw.get("author"),
        dirs=frozenset(kw.get("dirs", {"packages/core"})),
        default_branch="main",
    )


def test_history_uses_only_outcomes_finished_before_the_cutoff() -> None:
    outcomes = [
        _o(48, failed=False, lint=3),
        _o(30, failed=True, lint=5, types=2),
        _o(2, failed=True, lint=9, types=4),
        _o(0, failed=True),  # finished exactly at the cut-off: not known yet
        _o(-1, failed=True),  # finished after the cut-off
        _o(24 * 40, failed=True),  # outside 30 days, inside 90
    ]
    h = _hist(outcomes)
    assert h["repo_build_fail_rate_7d"] == 2 / 3
    assert h["repo_build_fail_rate_30d"] == 2 / 3
    assert h["repo_build_fail_rate_90d"] == 3 / 4
    assert h["repo_commits_30d"] == 3
    assert h["repo_tsc_fail_rate_30d"] == 2 / 3
    assert h["branch_last_build_failed"] is True
    assert h["branch_consecutive_build_fails"] == 2
    assert h["hours_since_branch_build_fail"] == 2.0
    assert (h["last_lint_warnings"], h["last_type_errors"], h["lint_warnings_trend"]) == (9, 4, 4)


def test_new_branch_falls_back_to_the_default_branch() -> None:
    h = _hist([_o(5, failed=True, lint=1)], branch="feature/new")
    assert h["branch_has_history"] is False
    assert h["branch_last_build_failed"] is True
    assert h["last_lint_warnings"] == 1


def test_author_and_package_history() -> None:
    a, b = "a" * 64, "b" * 64
    outcomes = [
        _o(10, failed=True, author=a, dirs={"packages/ui"}),
        _o(9, failed=False, author=a, dirs={"packages/core"}),
        _o(8, failed=True, author=b, dirs={"packages/core"}),
    ]
    h = _hist(outcomes, author=a, dirs={"packages/core"})
    assert (h["author_commits_90d"], h["author_build_fail_rate_90d"]) == (2, 0.5)
    assert h["author_last_build_failed"] is False
    assert (h["dirs_commits_30d"], h["dirs_build_fail_rate_30d"]) == (2, 0.5)
    first_time = _hist(outcomes, author="e" * 64)
    assert (
        first_time["author_commits_90d"] == 0 and first_time["author_build_fail_rate_90d"] is None
    )


def test_empty_history_is_missing_not_zero() -> None:
    h = _hist([])
    assert h["repo_build_fail_rate_30d"] is None
    assert h["branch_last_build_failed"] is None
    assert h["last_lint_warnings"] is None
    assert h["repo_commits_30d"] == 0


def test_module_dirs() -> None:
    assert module_dir("packages/core/src/a.ts") == "packages/core"
    assert module_dir("src/a.ts") == "src"
    assert module_dir("package.json") == "."
    assert module_dir("packages/readme.md") == "packages"
    moved = _f("apps/web/x.ts", [FileTag.SOURCE], None, previous_path="apps/old/x.ts")
    assert module_dirs([moved]) == frozenset({"apps/web", "apps/old"})
