"""Feature group A: what the commit changed in source code (docs/experts/build.md section 4).

Counts come from the changed-file list; the "markers" come from the added and removed lines of
each source or test patch (a diff only shows the changed hunks, not whole files, so these are
line-level signals, not a full syntax tree).
"""

from __future__ import annotations

import re

from shared.contracts import CommitContext, FileTag

from .patches import (
    JS_EXTS,
    TS_EXTS,
    added_lines,
    count_matching,
    is_declaration_file,
    module_dirs,
    removed_lines,
    suffix,
)

_IMPORT = re.compile(r"^\s*(?:import\b|export\s+(?:\*|\{[^}]*\})\s+from\b)|\brequire\(\s*['\"]")
_EXPORT = re.compile(r"^\s*export\b")
_TYPE_DECL = re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?(?:interface|type|enum)\s+\w+")
_ANNOTATION = re.compile(
    r"\)\s*:\s*[\w<\[({]|:\s*(?:string|number|boolean|unknown|any|void|never|bigint|object|"
    r"Record|Promise|Array|readonly|[A-Z]\w*)\b"
)
_ANY = re.compile(r":\s*any\b|\bas\s+any\b|<any>|\bany\[\]")
_TS_SUPPRESS = re.compile(r"@ts-(?:ignore|expect-error|nocheck)\b")
_ESLINT_DISABLE = re.compile(r"eslint-disable")
_CONSOLE = re.compile(r"\bconsole\.\w+\(")
_NON_NULL = re.compile(r"[\w)\]]!(?:\.|\[|\))")

NAMES: tuple[str, ...] = (
    "n_files",
    "n_source_files",
    "n_ts_files",
    "n_js_files",
    "n_test_files",
    "n_declaration_files",
    "n_build_config_files",
    "n_lint_config_files",
    "n_bundler_config_files",
    "n_docs_files",
    "lines_added",
    "lines_deleted",
    "source_lines_added",
    "source_lines_deleted",
    "max_file_churn",
    "n_added_files",
    "n_removed_files",
    "n_renamed_files",
    "n_removed_or_renamed_source",
    "n_module_dirs",
    "docs_only",
    "tests_only",
    "patches_missing",
    "truncated",
    "imports_added",
    "imports_removed",
    "exports_added",
    "exports_removed",
    "type_decls_changed",
    "annotations_added",
    "any_added",
    "ts_suppressions_added",
    "ts_suppressions_removed",
    "eslint_disables_added",
    "eslint_disables_removed",
    "console_calls_added",
    "non_null_assertions_added",
)


def change_features(ctx: CommitContext) -> dict[str, int | bool]:
    """Group A features for one commit."""
    files = ctx.changed_files
    source = ctx.files_with(FileTag.SOURCE)
    tests = ctx.files_with(FileTag.TEST)
    code = [f for f in files if FileTag.SOURCE in f.tags or FileTag.TEST in f.tags]
    added = [ln for f in code for ln in added_lines(f.patch)]
    removed = [ln for f in code for ln in removed_lines(f.patch)]
    quiet = {FileTag.DOCS, FileTag.OTHER}
    return {
        "n_files": len(files),
        "n_source_files": len(source),
        "n_ts_files": sum(1 for f in source if suffix(f.path) in TS_EXTS),
        "n_js_files": sum(1 for f in source if suffix(f.path) in JS_EXTS),
        "n_test_files": len(tests),
        "n_declaration_files": sum(1 for f in files if is_declaration_file(f.path)),
        "n_build_config_files": len(ctx.files_with(FileTag.BUILD_CONFIG)),
        "n_lint_config_files": len(ctx.files_with(FileTag.LINT_CONFIG)),
        "n_bundler_config_files": len(ctx.files_with(FileTag.BUNDLER_CONFIG)),
        "n_docs_files": len(ctx.files_with(FileTag.DOCS)),
        "lines_added": sum(f.additions for f in files),
        "lines_deleted": sum(f.deletions for f in files),
        "source_lines_added": sum(f.additions for f in source),
        "source_lines_deleted": sum(f.deletions for f in source),
        "max_file_churn": max((f.additions + f.deletions for f in files), default=0),
        "n_added_files": sum(1 for f in files if f.status == "added"),
        "n_removed_files": sum(1 for f in files if f.status == "removed"),
        "n_renamed_files": sum(1 for f in files if f.status == "renamed"),
        "n_removed_or_renamed_source": sum(1 for f in source if f.status in ("removed", "renamed")),
        "n_module_dirs": len(module_dirs(files)),
        "docs_only": bool(files) and all(set(f.tags) <= quiet for f in files),
        "tests_only": bool(files)
        and all(FileTag.TEST in f.tags or set(f.tags) <= quiet for f in files)
        and bool(tests),
        "patches_missing": sum(1 for f in code if f.patch is None),
        "truncated": ctx.truncated,
        "imports_added": count_matching(added, _IMPORT),
        "imports_removed": count_matching(removed, _IMPORT),
        "exports_added": count_matching(added, _EXPORT),
        "exports_removed": count_matching(removed, _EXPORT),
        "type_decls_changed": count_matching(added + removed, _TYPE_DECL),
        "annotations_added": count_matching(added, _ANNOTATION),
        "any_added": count_matching(added, _ANY),
        "ts_suppressions_added": count_matching(added, _TS_SUPPRESS),
        "ts_suppressions_removed": count_matching(removed, _TS_SUPPRESS),
        "eslint_disables_added": count_matching(added, _ESLINT_DISABLE),
        "eslint_disables_removed": count_matching(removed, _ESLINT_DISABLE),
        "console_calls_added": count_matching(added, _CONSOLE),
        "non_null_assertions_added": count_matching(added, _NON_NULL),
    }
