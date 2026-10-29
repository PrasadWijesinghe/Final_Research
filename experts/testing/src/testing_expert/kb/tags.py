"""Rule-based tags for knowledge-base cases (P4.10). No model, no network: regexes from config.

Four tag families share one `tags` array, told apart by prefix:

| prefix | meaning | read from |
|---|---|---|
| `kind:` | `genuine` (fixed by a later commit) or `flaky` (a re-run of the same commit passed) | the label |
| `fam:` | error family of the failure (`timeout`, `conn`, `assertion` ...) from `triage/evidence.py` | the failure text |
| `rc:` | root-cause category from the flaky-test literature: async_wait, concurrency, timeout, test_order_dependency, network, time_date, randomness, resource_leak, platform; `unspecified` when nothing fits | the failure text, and for genuine cases the lines the fix changed |
| `fix:` | fix category: FlakyFix categories that map to JavaScript (change_assertion, change_condition, reset_variable, reorder_data, handle_exception, handle_timeout, change_timezone, misc) plus common JavaScript fixes (add_await, add_wait, mock_dependency, fake_timers, close_resource, dynamic_port, retry, skip_test, update_snapshot, update_expected_value) and the scope of the change (test_only, source_change, dependency_update); flaky cases get `fix:rerun` | the fix diff |

The patterns live in `config/test_kb.yaml`. They are heuristics: a tag says "this text looks
like", never "this is the cause". For genuine cases the `rc:` tag comes from the failure text
and the fix lines together, so a real bug that fails with a timeout is still tagged `rc:timeout`.
"""
# ruff: noqa: E501

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from testing_expert.triage.evidence import ERROR_FAMILIES, is_test_path

from .settings import KbSettings, TagRule, load_settings

# DEFAULT: when several error families match, the most specific one is the "primary" family:
# environment-looking ones before the generic assertion mismatch.
FAMILY_PRECEDENCE = (
    "unhandled", "oom", "killed", "addrinuse", "conn", "network", "timeout", "fs_resource",
    "module_missing", "compile", "code_error", "snapshot", "assertion",
)  # fmt: skip

# DEFAULT: an error family also implies a root-cause category. The family may come from the log tail
# (an OOM kill prints after the report), which the `log` patterns do not read: the tail holds noise
# such as the runner's OS name in clean-up steps, and made `platform` fire on a third of all cases.
FAMILY_TO_ROOT_CAUSE = {
    "oom": "resource_leak", "fs_resource": "resource_leak", "conn": "network",
    "network": "network", "timeout": "timeout", "addrinuse": "concurrency",
    "unhandled": "async_wait",
}  # fmt: skip
_ASSERT_LINE = re.compile(r"\b(expect|assert|should|chai)\b|\.to[A-Z]\w+\(|\.(equal|deepEqual)\(")
_MATCHER = re.compile(
    r"\.(to[A-Z]\w*|equal|deepEqual|deepStrictEqual|strictEqual|notEqual|ok|throws|rejects|"
    r"include|match|eql|eq)\b"
)
_DOC_PATH = re.compile(r"\.(md|markdown|mdx|txt|rst)$|(^|/)(docs?|\.github)/", re.IGNORECASE)
_LOCK_PATH = re.compile(r"(^|/)(package\.json|package-lock\.json|pnpm-lock\.yaml|yarn\.lock)$")


@dataclass(frozen=True)
class FixFacts:
    """What the fix commit changed: lines of the failing test and stack files, plus file lists."""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()  # every file of the fix commit
    has_diff: bool = False


@dataclass(frozen=True)
class CaseTags:
    """The tags of one case, by family."""

    kind: str
    families: tuple[str, ...]
    primary_family: str
    root_causes: tuple[str, ...]
    fixes: tuple[str, ...]
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def tags(self) -> list[str]:
        """The flat, sorted tag list stored with the case."""
        return sorted(
            {
                f"kind:{self.kind}",
                *(f"fam:{x}" for x in self.families),
                *(f"rc:{x}" for x in self.root_causes),
                *(f"fix:{x}" for x in self.fixes),
            }
        )


def error_families(failure_text: str, tail: str = "") -> tuple[tuple[str, ...], str]:
    """(families found in the failure text, primary family). Falls back to the log tail."""
    found = [n for n, rx in ERROR_FAMILIES.items() if rx.search(failure_text)]
    if not found:
        found = [n for n, rx in ERROR_FAMILIES.items() if rx.search(tail)]
    primary = next((n for n in FAMILY_PRECEDENCE if n in found), "other")
    return tuple(found), primary


def _any(patterns: Iterable[re.Pattern[str]], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def _await_added(fix: FixFacts) -> bool:
    """True when an existing statement gained `await` (a removed line equals an added one minus it)."""
    removed = {" ".join(ln.split()) for ln in fix.removed}
    for ln in fix.added:
        if "await " in ln and " ".join(ln.replace("await ", "", 1).split()) in removed:
            return True
    return False


def _readable(fix: FixFacts, cfg: KbSettings) -> bool:
    """Diff patterns are only trusted on small diffs (see `tag_diff_max_lines` in the config)."""
    return fix.has_diff and len(fix.added) + len(fix.removed) <= cfg.tag_diff_max_lines


def _root_causes(
    rules: Iterable[TagRule], log_text: str, fix: FixFacts, cfg: KbSettings
) -> tuple[str, ...]:
    use = _readable(fix, cfg)
    added = "\n".join(fix.added) if use else ""
    both = "\n".join((*fix.added, *fix.removed)) if use else ""
    out = [
        r.name
        for r in rules
        if _any(r.log, log_text)
        or _any(r.diff, both)
        or _any(r.diff_added, added)
        or (r.name == "async_wait" and use and _await_added(fix))
    ]
    return tuple(out) or ("unspecified",)


def _assertion_change(fix: FixFacts) -> tuple[bool, bool]:
    """(matcher changed, expected value changed) between removed and added assertion lines."""
    rem = [ln for ln in fix.removed if _ASSERT_LINE.search(ln)]
    add = [ln for ln in fix.added if _ASSERT_LINE.search(ln)]
    if not rem or not add:
        return False, False
    rem_m = {m.group(1) for ln in rem for m in _MATCHER.finditer(ln)}
    add_m = {m.group(1) for ln in add for m in _MATCHER.finditer(ln)}
    if rem_m and add_m and rem_m != add_m:
        return True, False
    return False, {ln.strip() for ln in rem} != {ln.strip() for ln in add}


def _fixes(rules: Iterable[TagRule], fix: FixFacts, cfg: KbSettings) -> tuple[str, ...]:
    use = _readable(fix, cfg)
    added = "\n".join(fix.added) if use else ""
    both = "\n".join((*fix.added, *fix.removed)) if use else ""
    out = [r.name for r in rules if _any(r.diff, both) or _any(r.diff_added, added)]
    if use:
        changed, value = _assertion_change(fix)
        if changed:
            out.append("change_assertion")
        elif value:
            out.append("update_expected_value")
        if _await_added(fix):
            out.append("add_await")
    content = [p for p in fix.paths if not _DOC_PATH.search(p)]
    if any(_LOCK_PATH.search(p) for p in content):
        out.append("dependency_update")
    code = [p for p in content if not _LOCK_PATH.search(p)]
    if code and all(is_test_path(p) for p in code):
        out.append("test_only")
    elif any(not is_test_path(p) for p in code):
        out.append("source_change")
    if not any(o not in {"test_only", "source_change", "dependency_update"} for o in out):
        out.append("misc")  # FlakyFix "Miscellaneous"
    return tuple(dict.fromkeys(out))


def tag_case(
    kind: str,
    failure_text: str,
    tail: str = "",
    fix: FixFacts | None = None,
    settings: KbSettings | None = None,
) -> CaseTags:
    """Tags for one case. `kind` is `genuine` or `flaky`; `fix` is None for flaky cases."""
    cfg = settings or load_settings()
    fams, primary = error_families(failure_text, tail)
    rc = _root_causes(cfg.root_cause_rules, failure_text, fix or FixFacts(), cfg)
    implied = tuple(FAMILY_TO_ROOT_CAUSE[f] for f in fams if f in FAMILY_TO_ROOT_CAUSE)
    rc = tuple(dict.fromkeys((*(x for x in rc if x != "unspecified"), *implied))) or (
        "unspecified",
    )
    if kind == "flaky":
        fixes: tuple[str, ...] = ("rerun",)
    else:
        fixes = _fixes(cfg.fix_rules, fix, cfg) if fix and fix.has_diff else ("misc",)
    return CaseTags(kind, fams, primary, rc, fixes)
