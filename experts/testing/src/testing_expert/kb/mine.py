"""Mine verified cases from labelled failures (P4.10, docs/experts/test.md section 9).

Two kinds of case, both stored as `VerifiedCase(origin="mined")`:

* **genuine**: a failed test job that never passed on its commit (label `genuine`) and a later
  commit within 7 days on the same branch that touched the failing test file made the job pass.
  The rule `fix_commit_touches_file` (P4.3) gives the fix commit. Rows labelled
  `persistent_failure_streak` (no fix commit known) get a second chance: the first commit in the
  window that touched a file in the failing stack trace and was followed by a passing run is the
  fix (`stack_file_fix`).
* **flaky**: a failed job whose same commit passed on another attempt (`rerun_pass_same_sha`). There
  is no fix commit, the "fix" is the re-run. The contract (`origin` is `live` or `mined`) has no
  flaky value, so the case carries the tag `kind:flaky` and `fix_commit_sha` is the commit that
  was re-run.

Everything in a case is extracted by rules, with no LLM: the failure signature, the root-cause and
fix tags, and an extractive fix summary (commit message headline, changed files and a few changed
lines). Text is sanitised with `shared.sanitise` before it is stored.

A case becomes known (`verified_at`) when its fix, or its re-run, passed. Retrieval never returns a
case that was not yet known at the query's failure time.
"""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from shared.contracts import Expert, RepoRef, VerifiedCase
from shared.sanitise import load_patterns, sanitise
from testing_expert.parsing import FailedTest, clean_file
from testing_expert.triage.evidence import FAMILY_TEXT, failure_text, path_matches
from testing_expert.triage.features import LogEvidence, read_log

from .settings import KbSettings, load_settings
from .tags import CaseTags, FixFacts, tag_case
from .text import failure_part

log = logging.getLogger(__name__)

_NAMESPACE = uuid.UUID("5d1c1d0e-7d2a-4a3e-9d54-6b0c1d6f0c10")  # DEFAULT: fixed, so ids repeat
_DOC = re.compile(r"\.(md|markdown|mdx|txt|rst)$|(^|/)(docs?|\.github)/", re.IGNORECASE)
_LOCK = re.compile(r"(^|/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|npm-shrinkwrap\.json)$")
_NOISE_LINE = re.compile(r"^\s*([{}()\[\];,]*|//.*|/\*.*|\*.*|import .*|\} from .*)\s*$")
_TRAILER = re.compile(r"^\s*(co-authored-by|signed-off-by|reviewed-by|cc):", re.IGNORECASE)
_EMAIL = re.compile(r"<[^<>\s]+@[^<>\s]+>|[\w.+-]+@[\w-]+\.[\w.-]+")

# DEFAULT: one plain sentence per root-cause tag for the "fix" of a flaky case (no diff exists).
FLAKY_HINTS = {
    "async_wait": "await the async work or wait for the condition instead of a fixed delay",
    "concurrency": "run the conflicting tests one at a time or remove the shared resource",
    "timeout": "raise the timeout only after checking the test is not stuck; prefer waiting for a condition",
    "test_order_dependency": "reset shared state (mocks, modules, globals) between tests",
    "network": "mock the network call or retry it",
    "time_date": "use fake timers or fix the clock and the timezone",
    "randomness": "seed or stub the random value",
    "resource_leak": "close servers, handles and temp files at the end of the test",
    "platform": "guard or skip the test on the platform that differs",
    "unspecified": "re-run once and, if it repeats, look at the shared state of the test",
}  # fmt: skip


@dataclass(frozen=True)
class Candidate:
    """A labelled failed job that may become a case."""

    kind: str  # genuine | flaky
    rule: str  # label rule
    job_id: int
    run_id: int
    attempt: int
    repo_id: int
    repo: str
    licence: str
    sha: str
    branch: str
    wpath: str
    jname: str
    failed_at: datetime
    log_path: str
    fix_sha: str | None
    fix_file: str | None


@dataclass(frozen=True)
class CaseRow:
    """A built case: the contract object plus the columns the contract has no field for."""

    case: VerifiedCase
    kind: str
    repo: str
    licence: str
    failed_job_id: int
    failed_run_id: int
    failed_sha: str
    failed_at: datetime
    verified_at: datetime
    fix_sha: str
    error_family: str
    failing_tests: tuple[str, ...]
    search_text: str
    diff_excerpt: str
    links: dict[str, str]
    mine_rule: str
    tags: CaseTags

    @property
    def text_hash(self) -> str:
        """sha1 of the indexed text; a changed text means the case is embedded again."""
        return hashlib.sha1(self.search_text.encode()).hexdigest()


_CAND_SQL = """
select distinct on (r.full_name, w.head_sha, coalesce(w.workflow_path, ''), l.payload->>'job_name',
                    coalesce(l.payload->>'fix_commit', ''))
       l.job_id, j.run_id, j.attempt, w.repo_id, r.full_name, coalesce(r.licence, 'NOASSERTION'),
       w.head_sha, coalesce(w.head_branch, ''), coalesce(w.workflow_path, ''),
       coalesce(l.payload->>'job_name', ''), j.completed_at, lg.storage_path,
       l.payload->>'fix_commit', l.payload->>'fix_file'
from testing.labels l
join raw.jobs j on j.job_id = l.job_id
join raw.workflow_runs w on w.run_id = j.run_id and w.attempt = j.attempt
join raw.repos r on r.id = w.repo_id
join raw.logs lg on lg.job_id = j.job_id and lg.status = 'ok' and lg.storage_path is not null
where l.rule_name = %s and l.is_final and l.labelled_by = 'rule' and j.completed_at is not null
order by r.full_name, w.head_sha, coalesce(w.workflow_path, ''), l.payload->>'job_name',
         coalesce(l.payload->>'fix_commit', ''), j.attempt, j.job_id"""

_RULES = (
    ("genuine", "fix_commit_touches_file"),
    ("genuine", "persistent_failure_streak"),
    ("flaky", "rerun_pass_same_sha"),
)


def fetch_candidates(conn: psycopg.Connection[Any], limit: int | None = None) -> list[Candidate]:
    """One candidate per (repo, commit, workflow, job name, fix), with a readable log."""
    out: list[Candidate] = []
    for kind, rule in _RULES:
        with conn.cursor() as cur:
            cur.execute(_CAND_SQL, (rule,))
            rows = cur.fetchall()
        out.extend(Candidate(kind, rule, *row) for row in rows[:limit])
    return out


# --- reading the failing log (worker processes) -----------------------------------------------


def _data_dir() -> Path:
    env = os.environ.get("DATA_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[5] / "data"


def read_job_log(args: tuple[str, str]) -> tuple[LogEvidence, str] | None:
    """Parse one stored log: (evidence, log tail). None when the file is missing or unreadable."""
    path, repo = args
    try:
        text = clean_file(_data_dir() / path).text()
    except (OSError, EOFError):
        return None
    return read_log(text, repo), text[-4000:]


# --- diffs -------------------------------------------------------------------------------------


def changed_lines(patch: str | None, limit: int) -> tuple[list[str], list[str]]:
    """(added, removed) lines of a unified diff, without the `+`/`-` marker."""
    if not patch or len(patch) > limit:
        return [], []
    added, removed = [], []
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
    return added, removed


def pick_files(
    files: dict[str, tuple[str | None, int, int]],
    test_files: Iterable[str],
    frame_paths: Iterable[str],
) -> list[str]:
    """Files of the fix that matter: the failing test files, then stack files, else the largest."""
    tests = [p for p in files if any(path_matches(t, p) for t in test_files if t)]
    stack = [p for p in files if p not in tests and any(path_matches(f, p) for f in frame_paths)]
    picked = [*sorted(tests), *sorted(stack)]
    if picked:
        return picked
    rest = [p for p in files if not _DOC.search(p) and not _LOCK.search(p)]
    return sorted(rest, key=lambda p: (-(files[p][1] + files[p][2]), p))[:3]


def excerpt(
    files: dict[str, tuple[str | None, int, int]], picked: Sequence[str], cfg: KbSettings
) -> str:
    """Changed lines of the picked files with their hunk headers, bounded in size."""
    parts: list[str] = []
    for path in picked[: cfg.excerpt_files]:
        patch = files[path][0]
        if not patch or len(patch) > cfg.max_patch_chars:
            continue
        lines = [f"--- {path}"]
        for ln in patch.splitlines():
            if ln.startswith("@@") or (ln[:1] in "+-" and not ln.startswith(("+++", "---"))):
                lines.append(ln[:200])
        parts.append("\n".join(lines))
    return "\n".join(parts)[: cfg.excerpt_chars]


def _salient(lines: Iterable[str], n: int, width: int) -> list[str]:
    out = []
    for ln in lines:
        if _NOISE_LINE.match(ln):
            continue
        out.append(" ".join(ln.split())[:width])
        if len(out) == n:
            break
    return out


def commit_headline(message: str | None, cfg: KbSettings) -> str:
    """First paragraph of a commit message, without trailers and emails, cut to size."""
    lines: list[str] = []
    for ln in (message or "").splitlines():
        if not ln.strip() and lines:
            break
        if ln.strip() and not _TRAILER.match(ln):
            lines.append(ln.strip())
    return _EMAIL.sub("", " ".join(lines))[: cfg.message_chars].strip()


def fix_summary(
    sha: str,
    message: str | None,
    files: dict[str, tuple[str | None, int, int]],
    picked: Sequence[str],
    cfg: KbSettings,
) -> str:
    """Extractive summary: commit headline, changed files, a few changed lines. No model."""
    head = commit_headline(message, cfg) or "(no commit message)"
    shown = [f"{p} (+{files[p][1]} -{files[p][2]})" for p in picked[:3]] or list(files)[:3]
    parts = [
        f'Fix commit {sha[:7]}: "{head}".',
        f"Changed {len(files)} file(s), e.g. {', '.join(shown)}.",
    ]
    removed: list[str] = []
    added: list[str] = []
    for p in picked[: cfg.excerpt_files]:
        a, r = changed_lines(files[p][0], cfg.max_patch_chars)
        removed += r
        added += a
    half = max(cfg.summary_lines // 2, 1)
    rem = _salient(removed, half, cfg.summary_line_chars)
    add = _salient(added, cfg.summary_lines - len(rem), cfg.summary_line_chars)
    if rem:
        parts.append("Removed: " + " ; ".join(f"`{x}`" for x in rem) + ".")
    if add:
        parts.append("Added: " + " ; ".join(f"`{x}`" for x in add) + ".")
    return " ".join(parts)


# --- database lookups for one candidate --------------------------------------------------------


def _commit(
    conn: psycopg.Connection[Any], repo_id: int, sha: str
) -> tuple[datetime | None, str | None]:
    with conn.cursor() as cur:
        cur.execute(
            "select committed_at, message from raw.commits where repo_id = %s and sha = %s",
            (repo_id, sha),
        )
        row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def _files(
    conn: psycopg.Connection[Any], repo_id: int, sha: str
) -> dict[str, tuple[str | None, int, int]]:
    with conn.cursor() as cur:
        cur.execute(
            "select path, patch, coalesce(additions, 0), coalesce(deletions, 0) "
            "from raw.commit_files where repo_id = %s and sha = %s order by path",
            (repo_id, sha),
        )
        return {p: (patch, int(a), int(d)) for p, patch, a, d in cur.fetchall()}


def _passing_run_after(
    conn: psycopg.Connection[Any], c: Candidate, after: datetime, until: datetime
) -> tuple[int, datetime] | None:
    """First passing run of the same job on the same branch created in [after, until]."""
    with conn.cursor() as cur:
        cur.execute(
            """
select w.run_id, coalesce(j.completed_at, w.created_at)
from raw.jobs j join raw.workflow_runs w on w.run_id = j.run_id and w.attempt = j.attempt
where w.repo_id = %s and w.head_branch = %s and coalesce(w.workflow_path, '') = %s
  and coalesce(j.name, j.job_key, '') = %s and j.conclusion = 'success'
  and w.created_at >= %s and w.created_at <= %s
order by w.created_at, j.job_id limit 1""",
            (c.repo_id, c.branch, c.wpath, c.jname, after, until),
        )
        row = cur.fetchone()
    return (int(row[0]), row[1]) if row else None


def _passing_same_sha(conn: psycopg.Connection[Any], c: Candidate) -> tuple[int, datetime] | None:
    """A passing attempt of the same job on the same commit (the re-run that shows it was flaky)."""
    with conn.cursor() as cur:
        cur.execute(
            """
select w.run_id, coalesce(j.completed_at, w.created_at)
from raw.jobs j join raw.workflow_runs w on w.run_id = j.run_id and w.attempt = j.attempt
where w.repo_id = %s and w.head_sha = %s and coalesce(w.workflow_path, '') = %s
  and coalesce(j.name, j.job_key, '') = %s and j.conclusion = 'success'
order by j.completed_at nulls last, j.job_id limit 1""",
            (c.repo_id, c.sha, c.wpath, c.jname),
        )
        row = cur.fetchone()
    return (int(row[0]), row[1]) if row else None


def _stack_fix(
    conn: psycopg.Connection[Any],
    c: Candidate,
    hts: datetime,
    wanted: Sequence[str],
    cfg: KbSettings,
) -> str | None:
    """First commit in the window on the branch that touched a stack or test file, then passed."""
    until = hts + timedelta(days=cfg.window_days)
    with conn.cursor() as cur:
        cur.execute(
            """
select c.sha, c.committed_at, cf.path
from raw.commits c join raw.commit_files cf on cf.repo_id = c.repo_id and cf.sha = c.sha
where c.repo_id = %s and c.committed_at > %s and c.committed_at <= %s
  and exists (select 1 from raw.workflow_runs x where x.repo_id = c.repo_id
              and x.head_sha = c.sha and x.head_branch = %s)
order by c.committed_at, c.sha""",
            (c.repo_id, hts, until, c.branch),
        )
        rows = cur.fetchall()
    for sha, at, path in rows:
        if any(path_matches(w, path) for w in wanted) and _passing_run_after(conn, c, at, until):
            return str(sha)
    return None


# --- building one case -------------------------------------------------------------------------


def _frame_paths(failures: Sequence[FailedTest]) -> list[str]:
    return [
        f.path
        for t in failures
        for f in t.frames
        if f.kind == "project" and f.path and "node_modules" not in f.path
    ]


def _order(failures: Sequence[FailedTest], fix_file: str | None) -> list[FailedTest]:
    """Failures whose file is the fix file first (they are what the fix addressed)."""
    if not fix_file:
        return list(failures)
    hit = [f for f in failures if f.file and path_matches(f.file, fix_file)]
    return hit + [f for f in failures if f not in hit]


def build_case(
    conn: psycopg.Connection[Any],
    c: Candidate,
    parsed: tuple[LogEvidence, str] | None,
    cfg: KbSettings,
    patterns: Any,
    skipped: Counter[str],
) -> CaseRow | None:
    """Build one case, or None (the reason is counted in `skipped`)."""
    if parsed is None:
        skipped["log_unreadable"] += 1
        return None
    ev, tail = parsed
    if not ev.is_test_failure:
        skipped["no_parsed_test_failure"] += 1
        return None
    failures = _order(ev.failures, c.fix_file)
    frames = _frame_paths(failures)
    test_files = [f.file for f in failures if f.file] + ([c.fix_file] if c.fix_file else [])
    commit_at, _ = _commit(conn, c.repo_id, c.sha)
    hts = commit_at or c.failed_at
    until = hts + timedelta(days=cfg.window_days)

    fix_sha = c.fix_sha
    verify: tuple[int, datetime] | None
    files: dict[str, tuple[str | None, int, int]] = {}
    message: str | None = None
    rule = c.rule
    if c.kind == "genuine":
        if fix_sha is None:
            fix_sha = _stack_fix(conn, c, hts, [*frames, *test_files], cfg)
            rule = "stack_file_fix"
            if fix_sha is None:
                skipped["no_stack_file_fix"] += 1
                return None
        fix_at, message = _commit(conn, c.repo_id, fix_sha)
        files = _files(conn, c.repo_id, fix_sha)
        if fix_at is None or not files:
            skipped["fix_commit_unknown"] += 1
            return None
        verify = _passing_run_after(conn, c, fix_at, until)
        if verify is None:
            skipped["no_verifying_run"] += 1
            return None
    else:
        fix_sha = c.sha
        verify = _passing_same_sha(conn, c)
        if verify is None:
            skipped["no_verifying_run"] += 1
            return None

    picked = pick_files(files, test_files, frames) if files else []
    added: list[str] = []
    removed: list[str] = []
    for p in picked:
        a, r = changed_lines(files[p][0], cfg.max_patch_chars)
        added += a
        removed += r
    fix_facts = FixFacts(tuple(added), tuple(removed), tuple(files), bool(added or removed))

    part, family = failure_part(failures, tail, cfg.max_signature_tests)
    tags = tag_case(
        c.kind, failure_text(failures), tail, fix_facts if c.kind == "genuine" else None, cfg
    )
    names = tuple(
        dict.fromkeys(f.full_name or (f.file or "") for f in failures if f.full_name or f.file)
    )
    names = names[: cfg.max_signature_tests]

    def clean(text: str) -> str:
        return sanitise(text, patterns=patterns).text

    where = (
        "the fix also changed source files"
        if "source_change" in tags.fixes
        else "the fix changed test code only"
    )
    rc = ", ".join(tags.root_causes)
    fam_text = FAMILY_TEXT.get(family, "an unclassified error")
    if c.kind == "genuine":
        root_cause = (
            f"Real bug (a later commit fixed it). Root-cause category by rule: {rc}. "
            f"The failure showed {fam_text}; {where}."
        )
        summary = fix_summary(fix_sha, message, files, picked, cfg)
    else:
        root_cause = (
            f"Flaky failure. Root-cause category by rule: {rc}. The failure showed {fam_text}; "
            "the same commit passed on another attempt, so the change did not cause it."
        )
        hint = FLAKY_HINTS.get(tags.root_causes[0], FLAKY_HINTS["unspecified"])
        summary = f"No code change: the same commit passed on a re-run (run {verify[0]}). Usual hardening for this category: {hint}."
    test_list = "; ".join(names) or "(file-level failure)"
    failure_summary = f"{c.kind.capitalize()} failure in job '{c.jname}' of {c.repo}: {test_list}. Error family: {family}."
    signature_text = clean(part)
    log_signature = signature_text[:2000]
    verified_at = max(verify[1], c.failed_at)
    owner, name = c.repo.split("/", 1)
    base = f"https://github.com/{c.repo}"
    links = {
        "repo": base,
        "failed_run": f"{base}/actions/runs/{c.run_id}",
        "failed_job": f"{base}/actions/runs/{c.run_id}/job/{c.job_id}",
        "fix_commit": f"{base}/commit/{fix_sha}",
        "verifying_run": f"{base}/actions/runs/{verify[0]}",
    }
    if c.kind == "flaky":
        links["fix_commit"] = f"{base}/commit/{c.sha}"
    case_id = str(
        uuid.uuid5(_NAMESPACE, f"{c.kind}/{c.repo}/{c.sha}/{c.wpath}/{c.jname}/{fix_sha}")
    )
    case = VerifiedCase(
        case_id=case_id,
        expert=Expert.TEST,
        origin="mined",
        repo=RepoRef(owner=owner, name=name),
        repo_licence=c.licence,
        failure_summary=clean(failure_summary)[:1000],
        log_signature=log_signature,
        root_cause=clean(root_cause)[:1000],
        fix_summary=clean(summary)[:1000],
        fix_commit_sha=fix_sha,
        verifying_run_id=verify[0],
        verifying_run_passed=True,
        tags=tags.tags,
        verified_at=verified_at,
    )
    search_text = "\n".join(
        [
            signature_text,
            f"root cause: {case.root_cause}",
            f"fix: {case.fix_summary}",
            "tags: " + " ".join(t.replace(":", " ").replace("_", " ") for t in case.tags),
        ]
    )
    return CaseRow(
        case=case,
        kind=c.kind,
        repo=c.repo,
        licence=c.licence,
        failed_job_id=c.job_id,
        failed_run_id=c.run_id,
        failed_sha=c.sha,
        failed_at=c.failed_at,
        verified_at=verified_at,
        fix_sha=fix_sha,
        error_family=family,
        failing_tests=names,
        search_text=search_text,
        diff_excerpt=clean(excerpt(files, picked, cfg)),
        links=links,
        mine_rule=rule,
        tags=tags,
    )


def mine(
    conn: psycopg.Connection[Any],
    limit: int | None = None,
    workers: int = 6,
    cfg: KbSettings | None = None,
) -> tuple[list[CaseRow], Counter[str]]:
    """Mine every case. Returns the cases and a count of why candidates were skipped."""
    cfg = cfg or load_settings()
    candidates = fetch_candidates(conn, limit)
    log.info("candidates", extra={"n": len(candidates)})
    with ProcessPoolExecutor(workers) as pool:
        parsed = list(
            pool.map(read_job_log, [(c.log_path, c.repo) for c in candidates], chunksize=16)
        )
    patterns = load_patterns()
    skipped: Counter[str] = Counter()
    rows: list[CaseRow] = []
    seen: set[tuple[str, str, str, str]] = set()
    for cand, p in zip(candidates, parsed, strict=True):
        row = build_case(conn, cand, p, cfg, patterns, skipped)
        if row is None:
            continue
        # The same failure in several matrix legs gives the same signature: keep the first.
        key = (row.kind, row.failed_sha, row.fix_sha, row.case.log_signature)
        if key in seen:
            skipped["duplicate_signature"] += 1
            continue
        seen.add(key)
        rows.append(row)
    return rows, skipped
