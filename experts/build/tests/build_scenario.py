"""A small repo history for dataset tests, described once and served two ways:
in memory (InMemoryHistory + InMemoryDatasetSource) and as rows for the raw Postgres tables.

Timeline on acme/acme-web (default branch main), all times UTC on 10-15 Oct 2026:

  C1  main        success                          (the first commit; no history)
  C2  main        tsc failure in the typecheck step (tsconfig turned strict on)
  C2B docs/typo   success, created while C2 is still running (C2 must not count yet)
  C3  main        success, fixes src/api.ts        (fix candidate for C2; passing lint log)
  C4  feature/x   ESLint failure on a pull request, fails again on re-run
  C5  feature/x   only a test fails                (not a build failure)
  C6  main        Vite failure, passes on re-run   (claimed, confidence lowered)
  C7  main        npm install fails (ERESOLVE)     (dependency, not build)
  C8  main        run still in progress            (skipped)
  C9  main        run cancelled                    (skipped)
  C10 main        lint step failed, log expired    (step-name rule, low confidence)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from build_expert.dataset import InMemoryDatasetSource, JobInfo, RepoInfo, RunInfo
from build_expert.labels import StepFacts
from features.replay import CommitFileRow, CommitRow, InMemoryHistory, RunRow, SnapshotRow

REPO = "acme/acme-web"
REPO_ID = 7
DEFAULT_BRANCH = "main"
LICENCE = "MIT"
CI = ".github/workflows/ci.yml"
LOGS = Path(__file__).parent / "fixtures" / "logs"
AUTHOR_A = "a" * 64
AUTHOR_B = "b" * 64

CI_YAML = """\
name: CI
on: [push, pull_request]
jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 22
      - run: pnpm install --frozen-lockfile
      - run: pnpm run lint
      - run: pnpm run typecheck
      - run: pnpm run build
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pnpm install --frozen-lockfile
      - run: pnpm exec vitest run
"""
TSCONFIG = '{ "compilerOptions": { "strict": true, "noUnusedLocals": true } }'


def sha(n: int) -> str:
    """A fake 40-character SHA for commit number n."""
    return f"{n:040x}"


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """A UTC time in October 2026."""
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


SETUP_STEPS = (
    StepFacts(1, "Set up job", "success"),
    StepFacts(2, "Run actions/checkout@v4", "success"),
    StepFacts(3, "Run actions/setup-node@v4", "success"),
    StepFacts(4, "Run pnpm install --frozen-lockfile", "success"),
)


def steps(*rest: tuple[str, str]) -> tuple[StepFacts, ...]:
    """Setup steps followed by the given (name, conclusion) steps."""
    tail = tuple(StepFacts(5 + i, name, c) for i, (name, c) in enumerate(rest))
    return SETUP_STEPS + tail


@dataclass
class CommitSpec:
    """One commit with its runs, jobs, files and logs."""

    n: int
    branch: str
    created: datetime
    author: str
    files: list[CommitFileRow]
    jobs: list[tuple[int, str, str, tuple[StepFacts, ...], str | None]]  # attempt,key,concl,..,log
    event: str = "push"
    pr: int | None = None
    run_status: str = "completed"
    run_conclusion: str | None = None
    attempts: int = 1
    message: str = "change"
    log_status: str = "ok"
    duration: timedelta = timedelta(minutes=10)
    snapshots: dict[str, str] = field(default_factory=dict)

    @property
    def run_id(self) -> int:
        return 5000 + self.n


def _file(path: str, add: int, rem: int, patch: str, status: str = "modified") -> CommitFileRow:
    return CommitFileRow(path=path, status=status, additions=add, deletions=rem, patch=patch)


API_PATCH = (
    "@@ -1,3 +1,4 @@\n+import { missingHelper } from './helpers';\n-const ok = 1;\n"
    "+const unused = 42;"
)
FIX_PATCH = "@@ -1,4 +1,3 @@\n-import { missingHelper } from './helpers';\n-const unused = 42;"
TS_PATCH = '@@ -1 +1 @@\n-{ "compilerOptions": {} }\n+{ "compilerOptions": { "strict": true } }'
LINT_PATCH = "@@ -1 +1,3 @@\n+export function check(value: any) {\n+  console.log(value);\n+}"

COMMITS: list[CommitSpec] = [
    CommitSpec(
        1,
        "main",
        at(10, 8),
        AUTHOR_A,
        [
            _file(
                "src/user.ts",
                2,
                0,
                "@@ -1 +1,2 @@\n+export const x = 1;\n+export type Id = string;",
            )
        ],
        [
            (1, "quality", "success", steps(("Run pnpm run lint", "success")), None),
            (1, "test", "success", steps(("Run pnpm exec vitest run", "success")), None),
        ],
    ),
    CommitSpec(
        2,
        "main",
        at(11, 9),
        AUTHOR_A,
        [_file("tsconfig.json", 1, 1, TS_PATCH), _file("src/api.ts", 2, 1, API_PATCH)],
        [
            (
                1,
                "quality",
                "failure",
                steps(("Run pnpm run lint:warn", "success"), ("Run pnpm run typecheck", "failure")),
                "tsc_typecheck_fail.log",
            ),
            (1, "test", "success", steps(("Run pnpm exec vitest run", "success")), None),
        ],
        snapshots={"tsconfig.json": TSCONFIG},
    ),
    CommitSpec(
        102,
        "docs/typo",
        at(11, 9, 5),
        AUTHOR_B,
        [_file("README.md", 1, 1, "@@ -1 +1 @@\n-teh\n+the")],
        [(1, "quality", "success", steps(("Run pnpm run lint", "success")), None)],
        duration=timedelta(minutes=3),
    ),
    CommitSpec(
        3,
        "main",
        at(11, 15),
        AUTHOR_A,
        [_file("src/api.ts", 0, 2, FIX_PATCH)],
        [
            (
                1,
                "quality",
                "success",
                steps(("Run npm run lint:warn", "success")),
                "lint_pass_with_warnings.log",
            ),
            (1, "test", "success", steps(("Run pnpm exec vitest run", "success")), None),
        ],
        message="fix: remove unused import",
    ),
    CommitSpec(
        4,
        "feature/x",
        at(12, 10),
        AUTHOR_B,
        [_file("src/lint.ts", 3, 0, LINT_PATCH, status="added")],
        [
            (1, "quality", "failure", steps(("Run pnpm run lint", "failure")), "eslint_fail.log"),
            (2, "quality", "failure", steps(("Run pnpm run lint", "failure")), "eslint_fail.log"),
        ],
        event="pull_request",
        pr=12,
        attempts=2,
    ),
    CommitSpec(
        5,
        "feature/x",
        at(12, 12),
        AUTHOR_B,
        [_file("test/user.test.ts", 4, 0, "@@ -0,0 +1,4 @@\n+it('x', () => {})", status="added")],
        [
            (1, "quality", "success", steps(("Run pnpm run lint", "success")), None),
            (
                1,
                "test",
                "failure",
                steps(("Run pnpm exec vitest run", "failure")),
                "vitest_test_fail.log",
            ),
        ],
        event="pull_request",
        pr=12,
    ),
    CommitSpec(
        6,
        "main",
        at(13, 9),
        AUTHOR_A,
        [
            _file("vite.config.mjs", 1, 1, "@@ -1 +1 @@\n-entry: 'a'\n+entry: 'src/main.js'"),
            _file(
                "src/main.js", 1, 0, "@@ -1 +1,2 @@\n+import { render } from './does-not-exist.js';"
            ),
        ],
        [
            (
                1,
                "quality",
                "failure",
                steps(("Run npm run build", "failure")),
                "vite_build_fail.log",
            ),
            (2, "quality", "success", steps(("Run npm run build", "success")), None),
        ],
        attempts=2,
        snapshots={"vite.config.mjs": "export default { build: {} }"},
    ),
    CommitSpec(
        7,
        "main",
        at(13, 11),
        AUTHOR_B,
        [_file("package.json", 1, 1, '@@ -1 +1 @@\n-"eslint": "^9"\n+"@eslint/js": "*"')],
        [
            (
                1,
                "quality",
                "failure",
                SETUP_STEPS[:3] + (StepFacts(4, "Run npm install", "failure"),),
                "dependency_eresolve.log",
            )
        ],
        snapshots={"package.json": '{"name": "acme-web"}'},
    ),
    CommitSpec(
        8,
        "main",
        at(14, 9),
        AUTHOR_A,
        [_file("src/a.ts", 1, 0, "@@ +1 @@\n+x")],
        [(1, "quality", "in_progress", steps(), None)],
        run_status="in_progress",
    ),
    CommitSpec(
        9,
        "main",
        at(14, 10),
        AUTHOR_A,
        [_file("src/b.ts", 1, 0, "@@ +1 @@\n+y")],
        [(1, "quality", "cancelled", steps(), None)],
        run_conclusion="cancelled",
    ),
    CommitSpec(
        10,
        "main",
        at(15, 9),
        AUTHOR_B,
        [_file("src/c.ts", 1, 0, "@@ +1 @@\n+z")],
        [(1, "quality", "failure", steps(("Run pnpm run lint", "failure")), "eslint_fail.log")],
        log_status="expired",
    ),
]


def _run_conclusion(spec: CommitSpec, attempt: int) -> str | None:
    if spec.run_status != "completed":
        return None
    if spec.run_conclusion:
        return spec.run_conclusion
    jobs = [c for a, _, c, _, _ in spec.jobs if a == attempt]
    return "failure" if "failure" in jobs else "success"


def runs() -> list[RunInfo]:
    """Every run attempt in the scenario."""
    out = []
    for c in COMMITS:
        for attempt in range(1, c.attempts + 1):
            created = c.created + timedelta(minutes=30 * (attempt - 1))
            out.append(
                RunInfo(
                    run_id=c.run_id,
                    attempt=attempt,
                    head_sha=sha(c.n),
                    created_at=created,
                    head_branch=c.branch,
                    event=c.event,
                    status=c.run_status,
                    conclusion=_run_conclusion(c, attempt),
                    updated_at=created + c.duration if c.run_status == "completed" else created,
                    workflow_path=CI,
                    pr_numbers=(c.pr,) if c.pr else (),
                )
            )
    return out


def job_id(spec: CommitSpec, attempt: int, index: int) -> int:
    """A stable job id."""
    return spec.run_id * 100 + attempt * 10 + index


def log_path(spec: CommitSpec, attempt: int, jid: int) -> str:
    """Where the collector would store this job's log."""
    return f"logs/acme/acme-web/{spec.run_id}/{attempt}/{jid}.log.gz"


def jobs() -> list[JobInfo]:
    """Every job in the scenario."""
    out = []
    for c in COMMITS:
        for i, (attempt, key, concl, st, log) in enumerate(c.jobs):
            jid = job_id(c, attempt, i)
            status = "completed" if c.run_status == "completed" else "in_progress"
            out.append(
                JobInfo(
                    job_id=jid,
                    run_id=c.run_id,
                    attempt=attempt,
                    name=key,
                    job_key=key,
                    status=status,
                    conclusion=concl if status == "completed" else None,
                    steps=st,
                    log_path=log_path(c, attempt, jid) if log else None,
                    log_status=c.log_status if log else None,
                )
            )
    return out


def log_texts() -> dict[str, str]:
    """log_path -> fixture text, for every job that has a stored log."""
    out = {}
    for c in COMMITS:
        for i, (attempt, _, _, _, log) in enumerate(c.jobs):
            if log:
                out[log_path(c, attempt, job_id(c, attempt, i))] = (LOGS / log).read_text()
    return out


def repo() -> RepoInfo:
    """The scenario's repo."""
    return RepoInfo(REPO_ID, REPO, DEFAULT_BRANCH, LICENCE)


def source() -> InMemoryDatasetSource:
    """The scenario as an in-memory DatasetSource."""
    return InMemoryDatasetSource(
        repo_rows=[("build", repo())],
        run_rows={REPO: runs()},
        job_rows=jobs(),
        logs=log_texts(),
        messages={(REPO, sha(c.n)): c.message for c in COMMITS},
    )


def history() -> InMemoryHistory:
    """The scenario as replay history (raw commits, files and snapshots)."""
    h = InMemoryHistory()
    for r in runs():
        h.runs.append(
            (
                REPO,
                RunRow(
                    run_id=r.run_id,
                    attempt=r.attempt,
                    head_sha=r.head_sha,
                    created_at=r.created_at,
                    workflow_path=r.workflow_path,
                    head_branch=r.head_branch,
                    actor_hash=None,
                    pr_numbers=r.pr_numbers,
                ),
            )
        )
    for i, c in enumerate(COMMITS):
        parent = sha(COMMITS[i - 1].n) if i else None
        h.commits[(REPO, sha(c.n))] = CommitRow(
            sha=sha(c.n),
            committed_at=c.created - timedelta(minutes=2),
            author_hash=c.author,
            parent_shas=(parent,) if parent else (),
        )
        h.files[(REPO, sha(c.n))] = list(c.files)
        h.snapshots[(REPO, sha(c.n), CI)] = SnapshotRow(path=CI, content=CI_YAML)
        for path, text in c.snapshots.items():
            h.snapshots[(REPO, sha(c.n), path)] = SnapshotRow(path=path, content=text)
    return h
