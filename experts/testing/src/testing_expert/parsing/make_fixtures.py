"""Builds the log fixtures for the parser tests from the local collector database (read-only).

Run from the repository root, with the database settings loaded from `.env`:

    uv run --env-file .env python -m testing_expert.parsing.make_fixtures --suggest
    uv run --env-file .env python -m testing_expert.parsing.make_fixtures

`--suggest` samples logs per category (see `suggest:` in the manifest) and prints candidate jobs
as ready-to-paste manifest lines. Without it, every job listed under `fixtures:` in
`tests/fixtures/logs/manifest.yaml` is read from `data/logs/`, trimmed to the size cap, scrubbed
and written as `<framework>/<owner>__<repo>__<job_id>.log` with a stub `.expected.json`.

The database is only read: every query runs in a `SET TRANSACTION READ ONLY` transaction. The
database URL comes from DATABASE_URL (never printed). Tests never run this tool; they only read
the committed fixtures.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from shared.sanitise import sanitise
from testing_expert.parsing.clean import clean_file, clean_line
from testing_expert.parsing.settings import FixtureSettings, load_config

ANY_REPO = "*"  # a suggest category with this repo searches every repo
EXIT_OK = 0
EXIT_USAGE = 2
FRAMEWORKS = ("jest", "mocha", "vitest", "other")

# Any email-looking text. Used to scrub fixtures and by the cleanliness test.
# DEFAULT: the local part may hold `[bot]` (GitHub's bot addresses); the first domain label must
# start with a letter, so package specs such as `pkg@1.2.3-rc.next` are not mistaken for emails.
EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-\[\]])[A-Za-z0-9][A-Za-z0-9._%+\-\[\]]*"
    r"@(?:[A-Za-z][A-Za-z0-9\-]*\.)+[A-Za-z]{2,}"
)

# DEFAULT: a step is "the test step" when any of its output lines looks like a runner's report.
# Only used to decide what to keep when a log is over the size cap.
TEST_STEP_SIGNATURE = re.compile(
    r"^(?:\s*(?:PASS|FAIL)\s+\S"  # jest
    r"|\s*Tests?:\s+\d"  # jest, vitest
    r"|\s*Test Suites:\s"  # jest
    r"|\s*\d+ (?:passing|failing)"  # mocha
    r"|\s*Test Files\s"  # vitest
    r"|\s*[✓✔×✗✘]\s"  # tick and cross marks
    r"|\s*⎯+\s"  # vitest section rules
    r"|\s*(?:TAP version|# tests |ℹ tests ))",  # node:test, TAP
    re.MULTILINE,
)
_RUN_GROUP = re.compile(r"^##\[group\]Run ")
_TS_PREFIX = re.compile(r"^﻿?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ")
# DEFAULT: lines that report totals, and lines that start a failure; windows around them are kept
# when a log has to be trimmed (summary windows first, then failure windows, then more context).
SUMMARY_ANCHOR = re.compile(
    r"^\s*(?:\d+ (?:passing|failing|pending)\b|Tests?:\s+\d|Test Suites:|Test Files\s"
    r"|Summary of all failing tests|Failed (?:Tests|Suites)|Unhandled|# (?:tests|pass|fail) \d"
    r"|\u2139 (?:tests|pass|fail) \d)"
)
FAILURE_ANCHOR = re.compile(
    r"^\s*(?:FAIL\s|\u25cf .+ \u203a |\d+\) \S|\u00d7 |\u2717 |\u2716 |\w*Error: |not ok )"
)
# DEFAULT: 3 lines before and 60 after a summary anchor are kept (the whole summary block and the
# failure list that follows it).
SUMMARY_WINDOW = (3, 60)
# DEFAULT: 2 lines before and 25 after a failure anchor are kept (the error and its stack).
FAILURE_WINDOW = (2, 25)
# DEFAULT: the first 12 lines of the test step (its command and env) are always kept.
STEP_HEAD_LINES = 12
# DEFAULT: the first 40 lines of the log (runner setup) are always kept.
LOG_HEAD_LINES = 40
# DEFAULT: the last lines of a log (the failing-step annotation) are always kept.
KEEP_LAST_LINES = 6
# DEFAULT: 120 extra lines at the end of the test step are added last, if they still fit.
STEP_TAIL_LINES = 120
_EXIT_ANNOTATION = re.compile(r"Process completed with exit code")


@dataclass(frozen=True)
class FixtureEntry:
    """One job listed in the manifest."""

    job_id: int
    framework: str
    repo: str
    reason: str


@dataclass(frozen=True)
class Category:
    """A search rule for `--suggest`: which jobs to sample and what their log must contain."""

    id: str
    framework: str
    repo: str
    conclusion: str
    patterns: tuple[re.Pattern[str], ...] = ()
    excludes: tuple[re.Pattern[str], ...] = ()
    runner_os: str | None = None
    max_gz_bytes: int = 20000
    want: int = 1
    note: str = ""


@dataclass(frozen=True)
class Manifest:
    """The parsed manifest.yaml."""

    fixtures: tuple[FixtureEntry, ...]
    categories: tuple[Category, ...]


@dataclass(frozen=True)
class JobMeta:
    """Database facts about one job with a stored log."""

    job_id: int
    repo: str
    conclusion: str | None
    runner_os: str | None
    storage_path: str
    gz_bytes: int | None = None


@dataclass
class TrimReport:
    """What trimming and scrubbing did to one fixture, for the console summary."""

    long_lines_dropped: int = 0
    lines_omitted: int = 0
    emails_replaced: int = 0
    redactions: int = 0
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- pure helpers (unit tested)


def fixture_name(repo: str, job_id: int) -> str:
    """`owner/repo` + job id -> `owner__repo__<job_id>` (no extension)."""
    return f"{repo.replace('/', '__')}__{job_id}"


def _line_bytes(line: str) -> int:
    return len(line.encode("utf-8", errors="replace")) + 1


def _steps(cleaned: list[str]) -> list[tuple[int, int]]:
    """Index ranges [start, end) of the workflow steps, found from `##[group]Run` markers."""
    starts = [i for i, line in enumerate(cleaned) if _RUN_GROUP.match(line)]
    if not starts:
        return [(0, len(cleaned))]
    bounds = [0, *starts, len(cleaned)]
    return [(a, b) for a, b in zip(bounds, bounds[1:], strict=False) if b > a]


def _marker(prev_raw: str, omitted: int) -> str:
    m = _TS_PREFIX.match(prev_raw)
    prefix = m.group(0) if m else ""
    return f"{prefix}[fixture trimmed: {omitted} lines omitted]"


def _select(lines: list[str], keep: set[int]) -> tuple[list[str], int]:
    """Keep the marked lines, replacing each removed run with one marker line."""
    out: list[str] = []
    omitted_total = 0
    gap = 0
    for i, line in enumerate(lines):
        if i in keep:
            if gap:
                out.append(_marker(lines[i - gap - 1] if i - gap - 1 >= 0 else "", gap))
                gap = 0
            out.append(line)
        else:
            gap += 1
            omitted_total += 1
    if gap:
        out.append(_marker(lines[-gap - 1] if len(lines) > gap else "", gap))
    return out, omitted_total


def _window(index: int, size: tuple[int, int], n: int) -> set[int]:
    return set(range(max(index - size[0], 0), min(index + size[1] + 1, n)))


def trim_lines(
    raw_lines: Sequence[str], settings: FixtureSettings, report: TrimReport | None = None
) -> list[str]:
    """Shrink a log (lines without their newline) to `settings.max_bytes`.

    Lines over `max_line_bytes` are always dropped. If the rest is over the cap, the log start,
    the last lines and the start of the test step are kept, then windows around the runner's
    summary lines and failure lines are added, latest summary first, while they fit. Each cut
    region becomes one `[fixture trimmed: N lines omitted]` line.
    """
    report = report if report is not None else TrimReport()
    lines = [
        x for x in raw_lines if len(x.encode("utf-8", errors="replace")) <= settings.max_line_bytes
    ]
    report.long_lines_dropped += len(raw_lines) - len(lines)

    def size_of(kept: Sequence[str]) -> int:
        return sum(len(x.encode("utf-8", errors="replace")) + 1 for x in kept)

    if size_of(lines) <= settings.max_bytes:
        return lines

    n = len(lines)
    cleaned = [clean_line(x) for x in lines]
    steps = _steps(cleaned)
    chosen = [(a, b) for a, b in steps if TEST_STEP_SIGNATURE.search("\n".join(cleaned[a:b]))]
    if not chosen:  # no runner output: the step that printed the exit-code annotation
        marks = [i for i, x in enumerate(cleaned) if _EXIT_ANNOTATION.search(x)]
        chosen = [(a, b) for a, b in steps if marks and a <= marks[-1] < b] or [steps[-1]]
    mandatory = set(range(min(LOG_HEAD_LINES, n))) | set(range(max(n - KEEP_LAST_LINES, 0), n))
    summary_groups: list[set[int]] = []
    failure_groups: list[set[int]] = []
    tail_groups: list[set[int]] = []
    for a, b in chosen:
        mandatory |= set(range(a, min(a + STEP_HEAD_LINES, b)))
        for i in range(a, b):
            if SUMMARY_ANCHOR.match(cleaned[i]):
                summary_groups.append(_window(i, SUMMARY_WINDOW, b))
            elif FAILURE_ANCHOR.match(cleaned[i]):
                failure_groups.append(_window(i, FAILURE_WINDOW, b))
        tail_groups.append(set(range(max(b - STEP_TAIL_LINES, a), b)))
    extra_head = [set(range(min(settings.keep_head_lines, n)))]
    ordered = [*reversed(summary_groups), *failure_groups, *tail_groups, *extra_head]

    keep = set(mandatory)
    kept, omitted = _select(lines, keep)
    for group in ordered:
        if group <= keep:
            continue
        trial_kept, trial_omitted = _select(lines, keep | group)
        if size_of(trial_kept) <= settings.max_bytes:
            keep |= group
            kept, omitted = trial_kept, trial_omitted
    if size_of(kept) > settings.max_bytes:
        report.notes.append("still over the size cap after the smallest trim")
    report.lines_omitted += omitted
    return kept


def scrub_text(text: str, settings: FixtureSettings, report: TrimReport | None = None) -> str:
    """Re-run the sanitiser, then map every email address to the reserved placeholder."""
    report = report if report is not None else TrimReport()
    result = sanitise(text)
    report.redactions += result.total
    text, emails = EMAIL_RE.subn(settings.email_replacement, result.text)
    report.emails_replaced += emails
    again = sanitise(text)
    if again.total:
        report.notes.append(f"sanitiser still finds {again.total} secret(s) after scrubbing")
    return text


def build_fixture_text(
    raw: bytes, settings: FixtureSettings, report: TrimReport | None = None
) -> str:
    """Raw log bytes -> trimmed and scrubbed fixture text (timestamps and colours kept)."""
    report = report if report is not None else TrimReport()
    decoded = raw.decode("utf-8", errors="replace")
    lines = decoded.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    trimmed = trim_lines(lines, settings, report)
    text = scrub_text("\n".join(trimmed), settings, report)
    return text + "\n" if text else text


def stub_expected(entry: FixtureEntry, meta: JobMeta) -> dict[str, Any]:
    """The `.expected.json` fields that are known before the parsers exist (step 2)."""
    return {
        "framework": entry.framework,
        "repo": meta.repo,
        "job_id": entry.job_id,
        "conclusion": meta.conclusion,
    }


def merge_expected(existing: dict[str, Any] | None, stub: dict[str, Any]) -> dict[str, Any]:
    """Update the stub fields and keep every other key (hand-checked parser expectations)."""
    merged = dict(existing or {})
    merged.update(stub)
    return merged


# ---------------------------------------------------------------- manifest


def default_fixture_dir() -> Path:
    """`experts/testing/tests/fixtures/logs` next to this package's source tree."""
    return Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "logs"


def load_manifest(path: Path) -> Manifest:
    """Read manifest.yaml: the chosen jobs and the `--suggest` categories."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries: list[FixtureEntry] = []
    for framework, items in (raw.get("fixtures") or {}).items():
        if framework not in FRAMEWORKS:
            raise ValueError(f"{path}: unknown framework '{framework}'")
        for item in items or []:
            entries.append(
                FixtureEntry(
                    job_id=int(item["job_id"]),
                    framework=framework,
                    repo=str(item["repo"]),
                    reason=str(item.get("reason", "")),
                )
            )
    ids = [e.job_id for e in entries]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: a job_id is listed twice")
    cats = tuple(
        Category(
            id=str(c["id"]),
            framework=str(c["framework"]),
            repo=str(c["repo"]),
            conclusion=str(c.get("conclusion", "failure")),
            patterns=tuple(re.compile(p, re.MULTILINE) for p in c.get("patterns", [])),
            excludes=tuple(re.compile(p, re.MULTILINE) for p in c.get("excludes", [])),
            runner_os=c.get("runner_os"),
            max_gz_bytes=int(c.get("max_gz_bytes", 20000)),
            want=int(c.get("want", 1)),
            note=str(c.get("note", "")),
        )
        for c in raw.get("suggest", [])
    )
    return Manifest(fixtures=tuple(entries), categories=cats)


# ---------------------------------------------------------------- database (read-only)

_META_SQL = """
select j.job_id, r.full_name, j.conclusion, j.runner_os, l.storage_path, l.bytes
from raw.logs l
join raw.jobs j on j.job_id = l.job_id
join raw.workflow_runs w on w.run_id = j.run_id and w.attempt = j.attempt
join raw.repos r on r.id = w.repo_id
where l.status = 'ok'
"""


def _connect_read_only() -> Any:
    from sqlalchemy import text

    from shared.db.session import get_engine

    conn = get_engine().connect()
    conn.execute(text("SET TRANSACTION READ ONLY"))
    return conn


def fetch_meta(job_ids: Sequence[int]) -> dict[int, JobMeta]:
    """Repo, conclusion and log path for each job id (jobs without a stored log are absent)."""
    from sqlalchemy import bindparam, text

    stmt = text(_META_SQL + " and j.job_id in :ids").bindparams(bindparam("ids", expanding=True))
    with _connect_read_only() as conn:
        rows = conn.execute(stmt, {"ids": list(job_ids)}).all()
    return {int(r[0]): JobMeta(int(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rows}


def fetch_candidates(cat: Category, seed: int, sample: int) -> list[JobMeta]:
    """A seeded pseudo-random sample of small logs for one suggest category."""
    from sqlalchemy import text

    sql = (
        _META_SQL
        + (" and r.full_name = :repo" if cat.repo != ANY_REPO else "")
        + " and j.conclusion = :conclusion and l.bytes <= :max_gz"
        + (" and j.runner_os = :os" if cat.runner_os else "")
        + " order by md5(j.job_id::text || :seed) limit :sample"
    )
    params: dict[str, Any] = {
        "conclusion": cat.conclusion,
        "max_gz": cat.max_gz_bytes,
        "seed": str(seed),
        "sample": sample,
    }
    if cat.repo != ANY_REPO:
        params["repo"] = cat.repo
    if cat.runner_os:
        params["os"] = cat.runner_os
    with _connect_read_only() as conn:
        rows = conn.execute(text(sql), params).all()
    return [JobMeta(int(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rows]


def default_data_dir() -> Path:
    """DATA_DIR if set, else `<checkout>/data` (found from the config folder)."""
    env = os.environ.get("DATA_DIR")
    if env:
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        if (parent / "config" / "test_log_parsing.yaml").is_file():
            return parent / "data"
    return Path("data")


# ---------------------------------------------------------------- commands


def run_build(manifest: Manifest, out_dir: Path, data_dir: Path) -> int:
    """Write every fixture in the manifest. Returns a process exit code."""
    settings = load_config().fixtures
    meta = fetch_meta([e.job_id for e in manifest.fixtures])
    missing = [e.job_id for e in manifest.fixtures if e.job_id not in meta]
    if missing:
        sys.stderr.write(f"make_fixtures: no stored log for job(s) {missing}\n")
        return 1
    for entry in manifest.fixtures:
        job = meta[entry.job_id]
        source = data_dir / job.storage_path
        raw = _read_log_bytes(source)
        report = TrimReport()
        text = build_fixture_text(raw, settings, report)
        folder = out_dir / entry.framework
        folder.mkdir(parents=True, exist_ok=True)
        stem = fixture_name(job.repo, entry.job_id)
        (folder / f"{stem}.log").write_text(text, encoding="utf-8", newline="")
        expected_path = folder / f"{stem}.expected.json"
        existing = (
            json.loads(expected_path.read_text(encoding="utf-8"))
            if expected_path.exists()
            else None
        )
        merged = merge_expected(existing, stub_expected(entry, job))
        expected_path.write_text(
            json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        size = len(text.encode("utf-8"))
        notes = f" ({'; '.join(report.notes)})" if report.notes else ""
        sys.stdout.write(
            f"{entry.framework}/{stem}.log {size} bytes; long lines dropped "
            f"{report.long_lines_dropped}, lines omitted {report.lines_omitted}, emails "
            f"{report.emails_replaced}, redactions {report.redactions}{notes}\n"
        )
    return EXIT_OK


def _read_log_bytes(path: Path) -> bytes:
    import gzip

    data = path.read_bytes()
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


_SUMMARY_LINE = re.compile(
    r"^\s*(?:Tests?:\s+\d.*|Test Files\s.*|\d+ (?:passing|failing|pending).*"
    r"|# (?:tests|pass|fail) \d+.*)$",
    re.MULTILINE,
)


def profile(text: str) -> str:
    """A short fingerprint of a cleaned log for `--suggest`: its runner summary lines."""
    found = list(
        dict.fromkeys(
            re.sub(r"\s+", " ", m.group(0)).strip()[:60] for m in _SUMMARY_LINE.finditer(text)
        )
    )
    return "; ".join(found[-3:]) or "no summary line"


def run_suggest(
    manifest: Manifest, data_dir: Path, seed: int, sample: int, top: int, only: Sequence[str] = ()
) -> int:
    """Print candidate jobs for every category (or the `only` ones), as manifest lines."""
    listed = {e.job_id for e in manifest.fixtures}
    categories = [c for c in manifest.categories if not only or c.id in only]
    if not categories:
        sys.stderr.write("make_fixtures: no matching `suggest:` categories in the manifest\n")
        return EXIT_USAGE
    for cat in categories:
        found: list[tuple[int, int, JobMeta, str]] = []
        seen_sizes: set[int] = set()
        for job in fetch_candidates(cat, seed, sample):
            path = data_dir / job.storage_path
            if job.job_id in listed or not path.is_file():
                continue
            cleaned = clean_file(path)
            text = cleaned.text()
            if not all(p.search(text) for p in cat.patterns):
                continue
            if any(p.search(text) for p in cat.excludes) or len(text) in seen_sizes:
                continue  # an identical-size log is almost always a re-run of the same commit
            seen_sizes.add(len(text))
            found.append((cleaned.bytes_read, len(text), job, profile(text)))
        found.sort(key=lambda item: item[0])
        sys.stdout.write(f"# {cat.id} ({cat.framework}, {cat.repo}, {cat.conclusion}): ")
        sys.stdout.write(f"{len(found)} of {sample} sampled match; want {cat.want}\n")
        for raw_size, _clean_size, job, prof in found[:top]:
            sys.stdout.write(
                f'  - {{job_id: {job.job_id}, repo: {job.repo}, reason: "{cat.id}"}}'
                f"  # raw {raw_size}, {job.runner_os}: {prof}\n"
            )
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m testing_expert.parsing.make_fixtures",
        description="Build the log-parser fixtures from the local database (read-only).",
    )
    p.add_argument("--suggest", action="store_true", help="print candidate jobs, write nothing")
    p.add_argument("--manifest", type=Path, help="manifest.yaml (default: next to the fixtures)")
    p.add_argument("--out-dir", type=Path, help="fixtures/logs folder (default: the committed one)")
    p.add_argument(
        "--data-dir", type=Path, help="collector data folder (default: DATA_DIR or ./data)"
    )
    p.add_argument("--sample", type=int, default=200, help="--suggest: jobs sampled per category")
    p.add_argument("--top", type=int, default=6, help="--suggest: candidates shown per category")
    p.add_argument("--seed", type=int, help="--suggest: sampling seed (default from config)")
    p.add_argument("--only", action="append", default=[], help="--suggest: only this category id")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    args = _parser().parse_args(argv)
    out_dir = args.out_dir or default_fixture_dir()
    manifest_path = args.manifest or out_dir / "manifest.yaml"
    data_dir = args.data_dir or default_data_dir()
    if not os.environ.get("DATABASE_URL", "").strip():
        sys.stderr.write(
            "make_fixtures: DATABASE_URL is not set. Run with `uv run --env-file .env python -m "
            "testing_expert.parsing.make_fixtures`.\n"
        )
        return EXIT_USAGE
    manifest = load_manifest(manifest_path)
    if args.suggest:
        seed = args.seed if args.seed is not None else load_config().fixtures.seed
        return run_suggest(manifest, data_dir, seed, args.sample, args.top, args.only)
    return run_build(manifest, out_dir, data_dir)


if __name__ == "__main__":
    sys.exit(main())
