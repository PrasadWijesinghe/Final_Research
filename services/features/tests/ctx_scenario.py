"""One fake commit, expressed once and served two ways: as GitHub API JSON and as raw-table rows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from features.replay import CommitFileRow, CommitRow, InMemoryHistory, RunRow, SnapshotRow

from shared.sanitise import hash_identity

REPO = "acme/widgets"
HEAD = "a" * 40
PARENT = "b" * 40
LOGIN = "octocat"
COMMITTED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
RUN_CREATED = COMMITTED_AT + timedelta(minutes=5)
RUN_ID = 777
BRANCH = "feature/x"
CI = ".github/workflows/ci.yml"
RELEASE = ".github/workflows/release.yml"

CI_YAML = """\
name: CI
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        node: [18, 20]
    steps:
      - uses: actions/setup-node@v4
        with:
          node-version: ${{ matrix.node }}
      - run: npm ci
      - run: npm test
"""
RELEASE_YAML = """\
jobs:
  publish:
    runs-on: ubuntu-latest
    container: node:20
    steps:
      - run: npm publish
"""
PKG = '{"name": "widgets", "scripts": {"test": "jest"}}'
VITE = "export default { test: { globals: true } }"

# path -> (status, additions, deletions, patch, head content or None)
FILES: dict[str, tuple[str, int, int, str | None, str | None]] = {
    "package.json": ("modified", 1, 1, "@@ -1 +1 @@\n-a\n+b", PKG),
    "src/index.ts": ("modified", 3, 0, "@@ -1 +1,3 @@\n+x", "export {}"),
    "test/index.test.ts": ("added", 5, 0, "@@ -0,0 +1,5 @@\n+t", "it()"),
    "package-lock.json": ("modified", 900, 800, "@@ huge lock patch", "{}"),
    "vite.config.ts": ("modified", 1, 0, "@@ -1 +1 @@\n+test", VITE),
    "old.js": ("removed", 0, 4, "@@ -1,4 +0,0 @@\n-gone", None),
    CI: ("modified", 2, 0, "@@ -1 +1,2 @@\n+node", CI_YAML),
}
CONFIG_CONTENT = {
    p
    for p, v in FILES.items()
    if v[4] is not None and p not in ("src/index.ts", "test/index.test.ts")
}


def author_hash() -> str:
    """The hash both builders must produce (needs IDENTITY_SALT)."""
    return hash_identity(LOGIN)


def commit_json() -> dict[str, Any]:
    """Body of GET /repos/acme/widgets/commits/<HEAD>."""
    return {
        "sha": HEAD,
        "parents": [{"sha": PARENT}],
        "author": {"login": LOGIN},
        "commit": {
            "author": {"email": "x@example.test"},
            "committer": {"date": COMMITTED_AT.strftime("%Y-%m-%dT%H:%M:%SZ")},
            "message": "change things",
        },
        "files": [
            {
                "filename": path,
                "status": status,
                "additions": add,
                "deletions": dele,
                "patch": patch,
            }
            for path, (status, add, dele, patch, _c) in reversed(list(FILES.items()))
        ],
    }


def raw_contents() -> dict[str, str]:
    """Raw file contents at HEAD, served by raw.githubusercontent.com (workflows included)."""
    out = {p: v[4] for p, v in FILES.items() if v[4] is not None}
    out[RELEASE] = RELEASE_YAML
    return out


def history() -> InMemoryHistory:
    """The same commit as the collector would have stored it."""
    h = InMemoryHistory()
    h.runs.append((REPO, RunRow(RUN_ID, 1, HEAD, RUN_CREATED, CI, BRANCH, "c" * 64, ())))
    # A later run of the same commit (created after the cutoff) must never be read.
    h.runs.append(
        (REPO, RunRow(RUN_ID + 1, 1, HEAD, RUN_CREATED + timedelta(hours=1), RELEASE, BRANCH))
    )
    h.commits[(REPO, HEAD)] = CommitRow(HEAD, COMMITTED_AT, author_hash(), (PARENT,))
    h.files[(REPO, HEAD)] = [
        CommitFileRow(path, status, add, dele, patch)
        for path, (status, add, dele, patch, _c) in FILES.items()
    ]
    for path in CONFIG_CONTENT:
        if path == "package-lock.json":
            h.snapshots[(REPO, HEAD, path)] = SnapshotRow(path, None, "snapshots/blobs/aa/x.gz")
        else:
            h.snapshots[(REPO, HEAD, path)] = SnapshotRow(path, FILES[path][4])
    h.snapshots[(REPO, HEAD, RELEASE)] = SnapshotRow(RELEASE, RELEASE_YAML)
    return h
