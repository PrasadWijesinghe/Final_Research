"""Decides which test frameworks printed output in a cleaned log.

Each framework has signatures: regexes for its summary line, its failure headers and its
per-file lines, each worth a few points. The scores say how sure we are the framework ran; a
parser is run when its score reaches `parse.detect_min_score` in config/test_log_parsing.yaml.
The workflow step's command (`yarn jest`, `vitest run`, `mocha`) only breaks ties, and the repo
config's framework is a weaker tie-break still: the config is wrong for some repos
(brianc/node-postgres says vitest but runs mocha).

Node's built-in runner (`node --test`) is `node-test`: its TAP reporter is told apart from other
TAP (tape, mocha's tap reporter, test262) by `# Subtest:` lines, the `failureType:` and
`duration_ms:` fields of its YAML blocks and the `# suites N` total; its spec reporter by the
`ℹ tests N` summary and the `✖ failing tests:` section. Generic TAP and Playwright are
recognised as "other": they are not parsed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from testing_expert.parsing._common import prepare_lines
from testing_expert.parsing.models import Framework
from testing_expert.parsing.settings import ParsingConfig, load_config


@dataclass(frozen=True, slots=True)
class Signature:
    """One thing to look for: a regex (matched per line), its points, and a cap on repeats.

    A signature with no `pattern` is checked by `_file_line` instead (it needs a check a regex
    cannot do in linear time).
    """

    name: str
    pattern: re.Pattern[str] | None
    points: int
    max_hits: int = 1


@dataclass(frozen=True, slots=True)
class Detection:
    """How sure the detector is that one framework ran: a score and the signatures that hit."""

    framework: Framework
    score: int
    markers: tuple[str, ...]
    command_match: bool = False


def _sig(name: str, pattern: str | None, points: int, max_hits: int = 1) -> Signature:
    return Signature(name, re.compile(pattern) if pattern else None, points, max_hits)


# Lines reach the signatures right-stripped and capped (see `prepare_lines`), and every pattern is
# written to run in linear time on one long line.


# DEFAULT: the weights below are a judgement: a summary line is near-certain (5), a failure
# header is strong (3-4), a lone PASS/FAIL or tick line is weak (1-2).
SIGNATURES: dict[Framework, tuple[Signature, ...]] = {
    "jest": (
        _sig("Test Suites summary", r"^Test Suites:\s+\d", 5),
        _sig("Tests summary", r"^Tests:\s+\d", 2),
        _sig("Summary of all failing tests", r"^Summary of all failing tests$", 3),
        _sig("failure block", r"^  ● \S.* › ", 2),
        _sig("PASS/FAIL file line", None, 1, 2),
    ),
    "mocha": (
        _sig("passing/failing summary", r"^  \d+ (?:passing|failing|pending)\b", 5),
        _sig("tick line", r"^\s+[✔√]\s+\S", 1, 2),
    ),
    "vitest": (
        _sig("Test Files summary", r"^ {1,2}Test Files\s+\d", 5),
        _sig("Tests summary", r"^ {4,8}Tests\s+\d", 3),
        _sig("Failed Tests section", r"^⎯{3,}\s*Failed (?:Tests|Suites) \d+\s*⎯{3,}$", 4),
        _sig("RUN banner", r"^\s+RUN\s+v\d", 3),
        _sig("FAIL test line", None, 2),
        _sig("file line", r"^ [✓❯×↓]\s+\S.*\(\d+ tests?", 1, 2),
    ),
    "node-test": (
        _sig("node:test summary", r"^ℹ tests \d+", 5),
        _sig("failing tests section", r"^✖ failing tests:$", 4),
        _sig("TAP Subtest", r"^ *# Subtest: \S", 3, 2),
        _sig("TAP failureType", r"^ +failureType: '", 4),
        _sig("TAP suites total", r"^# suites \d+$", 4),
        _sig("TAP duration_ms", r"^ +duration_ms: \d", 2),
        _sig("spec suite line", r"^ *▶ \S", 1, 2),
    ),
    "other": (
        _sig("TAP version", r"^TAP version \d", 5),
        _sig("TAP plan", r"^# tests \d+", 5),
        _sig("playwright summary", r"^\s+\d+ (?:passed|failed|flaky|skipped)\b.*\(\d", 3),
        _sig("TAP not ok", r"^not ok \d+", 2),
    ),
}
_COMMAND_WORDS: dict[Framework, re.Pattern[str]] = {
    "jest": re.compile(r"\bjest\b"),
    "mocha": re.compile(r"\bmocha\b"),
    "vitest": re.compile(r"\bvitest\b"),
    "node-test": re.compile(r"(?:^|\s)--test\b|node:test"),
}
_ORDER: tuple[Framework, ...] = ("jest", "mocha", "vitest", "node-test", "other")
# Markers any TAP producer prints; they say nothing about which runner it was.
_GENERIC_TAP = frozenset({"TAP version", "TAP plan", "TAP not ok"})
# One regex with a named group per signature: a single C-speed match per line, which keeps a
# multi-million-line log fast. Signatures are tried in order, so the first one that matches wins.
_BY_GROUP: dict[str, tuple[Framework, Signature]] = {}
_ALTERNATIVES: list[str] = []
for _fw, _sigs in SIGNATURES.items():
    for _sig_item in _sigs:
        _group = f"s{len(_BY_GROUP)}"
        _BY_GROUP[_group] = (_fw, _sig_item)
        if _sig_item.pattern is not None:
            _ALTERNATIVES.append(f"(?P<{_group}>{_sig_item.pattern.pattern})")
_COMBINED = re.compile("|".join(_ALTERNATIVES))
_FILE_LINE_SIGNATURES: dict[str, tuple[Framework, Signature]] = {
    sig.name: (fw, sig) for fw, sigs in SIGNATURES.items() for sig in sigs if sig.pattern is None
}


def _file_line(line: str) -> tuple[Framework, Signature] | None:
    """Classify a `PASS`/`FAIL` line: a jest file line, or vitest's ` FAIL  file > name`.

    Done in code, not in a regex: a jest line has no ` > ` and its first word is a path, which
    needs a look-ahead that is quadratic when written as a pattern.
    """
    if line.startswith(" FAIL ") and " > " in line:
        return _FILE_LINE_SIGNATURES["FAIL test line"]
    body = line[1:] if line.startswith(" ") else line
    if body.startswith(("PASS ", "FAIL ")) and " > " not in line:
        words = body[5:].split(None, 1)
        if words and any(c in words[0] for c in "./\\"):
            return _FILE_LINE_SIGNATURES["PASS/FAIL file line"]
    return None


def detect(
    lines: Sequence[str],
    commands: Iterable[str] = (),
    hint: str | None = None,
    config: ParsingConfig | None = None,
) -> list[Detection]:
    """Score every framework that left a trace in `lines`, best first.

    `commands` are the workflow steps' commands (tie-break), `hint` is the repo config's framework
    (weakest tie-break). Frameworks with no signature hit are left out.
    """
    hits: dict[Framework, dict[str, int]] = {fw: {} for fw in _ORDER}
    match = _COMBINED.match
    limit = (config or load_config()).parse.max_line_chars
    for line in prepare_lines(lines, limit):
        if not line:
            continue
        m = match(line)
        if m is not None and m.lastgroup is not None:
            fw, sig = _BY_GROUP[m.lastgroup]
        else:
            classified = _file_line(line) if line[0] in " PF" else None
            if classified is None:
                continue
            fw, sig = classified
        seen = hits[fw]
        if seen.get(sig.name, 0) < sig.max_hits:
            seen[sig.name] = seen.get(sig.name, 0) + 1
    text = "\n".join(commands).lower()
    found: list[Detection] = []
    for fw in _ORDER:
        seen = hits[fw]
        if not seen:
            continue
        score = sum(sig.points * seen.get(sig.name, 0) for sig in SIGNATURES[fw])
        word = _COMMAND_WORDS.get(fw)
        found.append(
            Detection(
                framework=fw,
                score=score,
                markers=tuple(name for name in seen),
                command_match=bool(word and word.search(text)),
            )
        )
    # Node's runner prints plain TAP too: once its own markers reach the threshold, the generic TAP
    # hits are its own and must not outrank it as "other".
    min_score = (config or load_config()).parse.detect_min_score
    if any(d.framework == "node-test" and d.score >= min_score for d in found):
        found = [
            d for d in found if not (d.framework == "other" and set(d.markers) <= _GENERIC_TAP)
        ]
    found.sort(key=lambda d: (-d.score, not d.command_match, d.framework != hint))
    return found
