"""Structured indicators derived from sanitized failed-job logs."""

from __future__ import annotations

import re

LOG_FEATURE_COLUMNS = (
    "has_install_error",
    "has_missing_module",
    "has_test_failure",
    "has_coverage_error",
    "has_lint_type_error",
    "has_test_coverage_lint_error",
    "has_ci_permission_error",
    "has_external_network_error",
    "has_peer_dependency_conflict",
    "has_unsupported_engine",
    "has_missing_native_binding",
    "has_package_manager_cache_miss",
    "has_package_integrity_error",
    "has_build_or_lifecycle_error",
)

_INSTALL_CONTEXT = re.compile(
    r"\b(?:npm\s+(?:ci|install|i)|pnpm\s+(?:install|i)|yarn\s+install|"
    r"bun\s+install|pip\s+install|corepack\s+prepare)\b|"
    r"\b(?:npm\s+(?:error|err!)|pnpm\s+(?:error|err!)|yarn\s+error|bun\s+error)\b|"
    r"\b(?:ERR_PNPM_[A-Z0-9_]+|ERESOLVE|ETARGET|E404|ENOVERSIONS|ELOCKVERIFY|"
    r"YN00(?:02|35|46|48|49|60|86))\b",
    re.IGNORECASE,
)
_INSTALL_FAILURE = re.compile(
    r"\b(?:ERR_PNPM_[A-Z0-9_]+|ERESOLVE|ELOCKVERIFY|YN00(?:02|35|46|48|49|60|86)|"
    r"ETARGET|E404|lockfile.{0,60}(?:out of sync|outdated|mismatch|incompatible)|"
    r"(?:out of sync|outdated|mismatch).{0,60}lockfile|"
    r"could not resolve (?:dependency|package)|unable to resolve dependency tree|"
    r"conflicting peer dependency|peer dependency conflict|conflicting dependencies|"
    r"no matching version found|couldn't find (?:any versions|package)|"
    r"failed to install dependencies)\b",
    re.IGNORECASE,
)
_MISSING_MODULE = re.compile(
    r"\b(?:MODULE_NOT_FOUND|ERR_MODULE_NOT_FOUND|TS2307|TS2305|"
    r"Cannot find (?:module|package)|Could not find module|No module named|"
    r"has no exported member)\b",
    re.IGNORECASE,
)
_TEST_FAILURE = re.compile(
    r"\b(?:AssertionError|Test Suites?:\s*\d+ failed|Tests?:\s*\d+ failed|"
    r"\d+ failing|\d+ tests? failed|FAIL\s+[^\r\n]*\.(?:test|spec)\.|"
    r"not ok\s+\d+|snapshot[s]?(?:\s+\d+)?\s+(?:failed|mismatched)|"
    r"snapshot.{0,100}mismatch|mismatch.{0,100}snapshot)\b",
    re.IGNORECASE,
)
_COVERAGE_FAILURE = re.compile(
    r"(?:coverage.{0,100}(?:below|under|does not meet|failed|threshold)|"
    r"(?:below|under|does not meet).{0,100}coverage threshold)",
    re.IGNORECASE,
)
_LINT_TYPE_FAILURE = re.compile(
    r"\b(?:error TS\d{3,5}|TS\d{3,5}:|ESLint found \d+ problems?|"
    r"\d+ problems? \(\d+ errors?[,)]|oxlint.{0,80}(?:error|failed)|"
    r"prettier.{0,80}(?:error|failed|code style issues)|"
    r"\d+:\d+\s+error\s+[^\r\n]+)",
    re.IGNORECASE,
)
_CI_PERMISSION_FAILURE = re.compile(
    r"\b(?:Resource not accessible by integration|Not authorized|Unauthorized|"
    r"permission denied|access denied|requires? (?:write|read) access|"
    r"HTTP 401|HTTP 403|status(?: code)? 401|status(?: code)? 403)\b",
    re.IGNORECASE,
)
_EXTERNAL_NETWORK_FAILURE = re.compile(
    r"\b(?:ECONNREFUSED|ECONNRESET|ENOTFOUND|EAI_AGAIN|ETIMEDOUT|"
    r"ERR_SOCKET_TIMEOUT|socket hang up|connection timed out|connection refused|"
    r"network is unreachable|network connection.{0,50}(?:blocked|failed|timed out)|"
    r"DNS lookup failed|could not resolve host|request timed out|fetch failed)\b",
    re.IGNORECASE,
)
_PEER_DEPENDENCY_CONFLICT = re.compile(
    r"\b(?:ERESOLVE|ERR_PNPM_PEER_DEP_ISSUES|YN0002|YN0060|YN0086|"
    r"peer (?:dep(?:endency)?|requirement)(?: conflict)?|"
    r"conflicting peer dependency|peer dependency conflict|conflicting dependencies|"
    r"conflicting-dependencies|unable to resolve dependency tree)\b",
    re.IGNORECASE,
)
_UNSUPPORTED_ENGINE = re.compile(
    r"\b(?:EBADENGINE|unsupported engine|engine.{0,80}(?:incompatible|unsupported)|"
    r"(?:requires|expected).{0,40}(?:node(?:\.js)?|npm).{0,30}(?:version|[<>=^~]))\b",
    re.IGNORECASE,
)
_MISSING_NATIVE_BINDING = re.compile(
    r"\b(?:gyp ERR!|ERR_DLOPEN_FAILED|NODE_MODULE_VERSION|"
    r"compiled against a different Node|wrong ELF|invalid ELF)\b|"
    r"(?:node-gyp|node-pre-gyp|prebuild-install).{0,120}(?:ERR!|error|failed|not found)|"
    r"(?:failed|unable|could not) to find package.{0,160}(?:binding|native)|"
    r"(?:native binding|native module).{0,80}(?:not found|missing|failed to load)|"
    r"could not load.{0,80}(?:binding|binary)",
    re.IGNORECASE,
)
_PACKAGE_MANAGER_CACHE_MISS = re.compile(
    r"\b(?:pnpm|npm|yarn) cache (?:is )?not found\b|"
    r"\bcache (?:directory|store).{0,80}not found\b",
    re.IGNORECASE,
)
_PACKAGE_INTEGRITY_ERROR = re.compile(
    r"\b(?:EINTEGRITY|digest-mismatch|integrity check failed|"
    r"checksum mismatch|integrity checksum failed)\b",
    re.IGNORECASE,
)
_BUILD_OR_LIFECYCLE_FAILURE = re.compile(
    r"\b(?:failed to execute|failed to run|error running)\s+"
    r"(?:precompile|postinstall|preinstall|install|build|prepare)\b|"
    r"\b(?:precompile|postinstall|preinstall|prepare)\b.{0,120}"
    r"(?:script execution failed|failed|error|exit code [1-9])|"
    r"custom script execution failed|"
    r"TypeError:\s*Cannot read properties of undefined\s*"
    r"\(reading ['\"]PropertyDeclaration['\"]\)",
    re.IGNORECASE,
)


def extract_sanitized_log_features(log_text: str) -> dict[str, bool]:
    """Extract boolean error indicators; callers must sanitize log text first."""
    install_error = bool(_INSTALL_CONTEXT.search(log_text) and _INSTALL_FAILURE.search(log_text))
    missing_module = bool(_MISSING_MODULE.search(log_text))
    test_failure = bool(_TEST_FAILURE.search(log_text))
    coverage_error = bool(_COVERAGE_FAILURE.search(log_text))
    lint_type_error = bool(_LINT_TYPE_FAILURE.search(log_text))
    return {
        "has_install_error": install_error,
        "has_missing_module": missing_module,
        "has_test_failure": test_failure,
        "has_coverage_error": coverage_error,
        "has_lint_type_error": lint_type_error,
        "has_test_coverage_lint_error": test_failure or coverage_error or lint_type_error,
        "has_ci_permission_error": bool(_CI_PERMISSION_FAILURE.search(log_text)),
        "has_external_network_error": bool(_EXTERNAL_NETWORK_FAILURE.search(log_text)),
        "has_peer_dependency_conflict": bool(_PEER_DEPENDENCY_CONFLICT.search(log_text)),
        "has_unsupported_engine": bool(_UNSUPPORTED_ENGINE.search(log_text)),
        "has_missing_native_binding": bool(_MISSING_NATIVE_BINDING.search(log_text)),
        "has_package_manager_cache_miss": bool(_PACKAGE_MANAGER_CACHE_MISS.search(log_text)),
        "has_package_integrity_error": bool(_PACKAGE_INTEGRITY_ERROR.search(log_text)),
        "has_build_or_lifecycle_error": bool(_BUILD_OR_LIFECYCLE_FAILURE.search(log_text)),
    }
