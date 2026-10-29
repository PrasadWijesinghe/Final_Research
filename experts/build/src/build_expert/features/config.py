"""Feature group B: changes to build-tool configuration (docs/experts/build.md section 4).

Reads the patches of tsconfig, ESLint, Prettier, bundler, Babel and package.json files. A patch
shows which settings lines were added or removed, which is enough to tell "strict mode was turned
on" or "an ESLint rule was set to error" without parsing whole files.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from shared.contracts import ChangedFile, CommitContext, FileTag

from .patches import added_lines, changed_lines, count_matching

_STRICTNESS = re.compile(
    r'"(?:strict|noImplicitAny|strictNullChecks|strictFunctionTypes|strictPropertyInitialization|'
    r"noImplicitReturns|noImplicitOverride|noImplicitThis|noUnusedLocals|noUnusedParameters|"
    r"noUncheckedIndexedAccess|exactOptionalPropertyTypes|noFallthroughCasesInSwitch|"
    r'noPropertyAccessFromIndexSignature|useUnknownInCatchVariables|isolatedModules|verbatimModuleSyntax)"\s*:'
)
_TARGET_MODULE = re.compile(r'"(?:target|module|moduleResolution|lib|jsx|types|typeRoots)"\s*:')
_PATHS = re.compile(r'"(?:paths|baseUrl|rootDirs?|outDir|declarationDir|composite)"\s*:')
_INCLUDES = re.compile(r'"(?:include|exclude|files|references|extends)"\s*:')
_SEVERITY = re.compile(r"""(['"])(?:error|warn|off)\1|:\s*\[?\s*[012]\s*[,\]]""")
_TO_ERROR = re.compile(r"""(['"])error\1|:\s*\[?\s*2\s*[,\]]""")
_EXTENDS_PLUGINS = re.compile(
    r"\b(?:extends|plugins|configs\.|parserOptions|languageOptions|parser)\b|import\s.+eslint"
)
_BUNDLER_KEYS = re.compile(
    r"\b(?:plugins?|loaders?|rules|resolve|alias|entry|external|output|define|optimizeDeps|"
    r"rollupOptions|build|target|format)\b"
)
_BUILD_SCRIPT = re.compile(
    r'"(?:build|lint|typecheck|type-check|check-types|tsc|compile|check|prepare|prepublish\w*|'
    r'prepack|format)(?:[:\w-]*)"\s*:'
)
_BUILD_TOOL_DEP = re.compile(
    r'"(?:typescript|eslint(?:-[\w-]+)?|@eslint/[\w-]+|@typescript-eslint/[\w-]+|typescript-eslint|'
    r"prettier(?:-[\w-]+)?|webpack(?:-[\w-]+)?|vite|@vitejs/[\w-]+|rollup|@rollup/[\w-]+|esbuild|"
    r"tsup|tsdown|@babel/[\w-]+|babel-[\w-]+|next|nuxt|@swc/[\w-]+|ts-loader|vue-tsc|"
    r'svelte-check|@biomejs/biome|oxlint|rolldown|@rspack/[\w-]+)"\s*:'
)
_TS_DEP = re.compile(r'"typescript"\s*:')
_TYPES_DEP = re.compile(r'"@types/[\w.-]+"\s*:')
_MODULE_FIELDS = re.compile(r'"(?:type|exports|main|module|types|typings|imports)"\s*:')
_ENGINES = re.compile(r'"(?:engines|packageManager)"\s*:|"node"\s*:\s*"')
_NODE_VERSION_FILES = frozenset({".nvmrc", ".node-version", ".tool-versions"})
_WORKFLOW_NODE = re.compile(r"\bnode-version\b|\bnode\s*:\s*\[|\bnode_version\b")

NAMES: tuple[str, ...] = (
    "tsconfig_changed",
    "tsconfig_added",
    "tsconfig_strictness_lines",
    "tsconfig_target_module_lines",
    "tsconfig_paths_lines",
    "tsconfig_include_lines",
    "babel_config_changed",
    "eslint_config_changed",
    "eslint_severity_lines",
    "eslint_rules_set_to_error",
    "eslint_extends_plugins_lines",
    "prettier_config_changed",
    "bundler_config_changed",
    "bundler_option_lines",
    "package_json_changed",
    "build_script_lines",
    "build_tool_dep_lines",
    "typescript_version_changed",
    "types_dep_lines",
    "module_field_lines",
    "engines_lines",
    "lockfile_changed",
    "workflow_changed",
    "workflow_node_version_lines",
    "node_version_file_changed",
    "config_patches_missing",
)


def _name(f: ChangedFile) -> str:
    return PurePosixPath(f.path).name


def _lines(files: list[ChangedFile]) -> list[str]:
    return [ln for f in files for ln in changed_lines(f.patch)]


def config_change_features(ctx: CommitContext) -> dict[str, int]:
    """Group B features for one commit."""
    build_cfg = ctx.files_with(FileTag.BUILD_CONFIG)
    tsconfigs = [f for f in build_cfg if _name(f).startswith("tsconfig")]
    babel = [f for f in build_cfg if not _name(f).startswith("tsconfig")]
    lint_cfg = ctx.files_with(FileTag.LINT_CONFIG)
    eslint = [f for f in lint_cfg if "eslint" in _name(f)]
    prettier = [f for f in lint_cfg if "prettier" in _name(f)]
    bundler = ctx.files_with(FileTag.BUNDLER_CONFIG)
    manifests = ctx.files_with(FileTag.PACKAGE_MANIFEST)
    workflows = ctx.files_with(FileTag.WORKFLOW)
    ts_lines, es_lines, bundle_lines = _lines(tsconfigs), _lines(eslint), _lines(bundler)
    pkg_lines = _lines(manifests)
    config_files = tsconfigs + babel + eslint + prettier + bundler + manifests
    return {
        "tsconfig_changed": len(tsconfigs),
        "tsconfig_added": sum(1 for f in tsconfigs if f.status == "added"),
        "tsconfig_strictness_lines": count_matching(ts_lines, _STRICTNESS),
        "tsconfig_target_module_lines": count_matching(ts_lines, _TARGET_MODULE),
        "tsconfig_paths_lines": count_matching(ts_lines, _PATHS),
        "tsconfig_include_lines": count_matching(ts_lines, _INCLUDES),
        "babel_config_changed": len(babel),
        "eslint_config_changed": len(eslint),
        "eslint_severity_lines": count_matching(es_lines, _SEVERITY),
        "eslint_rules_set_to_error": count_matching(
            [ln for f in eslint for ln in added_lines(f.patch)], _TO_ERROR
        ),
        "eslint_extends_plugins_lines": count_matching(es_lines, _EXTENDS_PLUGINS),
        "prettier_config_changed": len(prettier),
        "bundler_config_changed": len(bundler),
        "bundler_option_lines": count_matching(bundle_lines, _BUNDLER_KEYS),
        "package_json_changed": len(manifests),
        "build_script_lines": count_matching(pkg_lines, _BUILD_SCRIPT),
        "build_tool_dep_lines": count_matching(pkg_lines, _BUILD_TOOL_DEP),
        "typescript_version_changed": int(count_matching(pkg_lines, _TS_DEP) > 0),
        "types_dep_lines": count_matching(pkg_lines, _TYPES_DEP),
        "module_field_lines": count_matching(pkg_lines, _MODULE_FIELDS),
        "engines_lines": count_matching(pkg_lines, _ENGINES),
        "lockfile_changed": len(ctx.files_with(FileTag.LOCKFILE)),
        "workflow_changed": len(workflows),
        "workflow_node_version_lines": count_matching(_lines(workflows), _WORKFLOW_NODE),
        "node_version_file_changed": sum(
            1 for f in ctx.changed_files if _name(f) in _NODE_VERSION_FILES
        ),
        "config_patches_missing": sum(1 for f in config_files if f.patch is None),
    }
