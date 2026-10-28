"""Dataset builder (P4.5): one row per labelled test-job attempt -> rows.parquet + manifest.json.

    uv run --env-file .env python -m testing_expert.dataset --version v1

Rows = attempts whose rule label (testing.labels) is pass, flaky or genuine. Features come from
`compute_features_batch`, so training rows use exactly the code the live expert uses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from .features import compute_features_batch, feature_names
from .features.loader import SERIES_KEY_SQL

log = logging.getLogger(__name__)

# DEFAULT: built directly from raw + testing.labels with history_cutoff = the run's
# created_at, not through the P2.8 replay CLI. compute_features_batch applies that same cutoff
# per row, so the features equal what the replay path (expert over HTTP) would produce.
LABEL_MAP = {"pass": "pass", "genuine": "genuine_fail", "flaky": "flaky_fail"}
KEY_COLS = ("run_id", "attempt", "job_id", "repo", "created_at", "series_key")
TARGET_COLS = ("label", "label_confidence", "rule_name", "genuine_strict")
V1_CUTS = [
    "labels from rules only (pass, flaky same-SHA, flaky silent low, genuine); no manual labels",
    "history at job-key granularity, not per test; no tree-sitter group, no code vector",
    "logs parsed only for failed test-job attempts and their same-SHA siblings",
    "built directly from raw + testing.labels, history_cutoff = run created_at (no replay)",
    "genuine_strict = genuine with >= 2 failing (run, attempt) pairs of the same job on the SHA",
]
# DEFAULT: dataset versions are explicit. v1 keeps its original 41 features (so it stays
# reproducible after new feature groups land); v2 adds the groups below.
V2_ADDED_PREFIXES = ("tw_", "h_os_")
V2_FEATURE_GROUPS_ADDED = [
    "tw_*: tree-sitter test-change group (14 features; None = unknown, 0 = no test change)",
    "h_os_*: job-history features per runner OS (13 features)",
]
V2_CUTS = [
    "labels from rules only (P4.3 still v1 label scope); human kappa round not done yet",
    "logs parsed as in v1 (P4.2 still v1 scope: failed test-job attempts and same-SHA siblings)",
    "no daily collector: data is the one-off collection, no newer runs",
    "no new repos: same 16 repos as v1",
    "history at job-key granularity, not per test; no code vector",
    "built directly from raw + testing.labels, history_cutoff = run created_at (no replay)",
    "genuine_strict = genuine with >= 2 failing (run, attempt) pairs of the same job on the SHA",
]
V3_LABEL_CHANGES = [
    "P4.3 v3 rule persistent_failure_streak: failed test job, never passed on its commit, "
    "failing on "
    ">= 3 consecutive earlier attempts over >= 2 other commits, is genuine (low confidence); "
    "before, those attempts were `unclear` and left out",
    "same 68 features and the same pass / flaky / fix-commit-genuine rows as v2; only the extra "
    "genuine rows are new",
]
V2_FROZEN_NOTE = "labels pending the human kappa round (P4.3); not frozen until it completes"

# One row per labelled attempt. genuine_strict counts distinct failing (run, attempt) pairs of the
# same series on the same repo + head_sha + workflow (matrix legs of one attempt count once).
_ROWS_SQL = f"""
select j.run_id, j.attempt, j.job_id, r.full_name, w.created_at, {SERIES_KEY_SQL} as series_key,
       l.sub_label, l.confidence, l.rule_name,
       (select count(distinct (w2.run_id, w2.attempt)) from raw.workflow_runs w2
          join raw.jobs j2 on j2.run_id = w2.run_id and j2.attempt = w2.attempt
         where w2.repo_id = w.repo_id and w2.head_sha = w.head_sha
           and w2.workflow_path = w.workflow_path and j2.conclusion = 'failure'
           and coalesce(j2.job_key, regexp_replace(j2.name, '\\s*\\(.*$', ''))
               = {SERIES_KEY_SQL}) as n_fail
from testing.labels l
join raw.jobs j on j.job_id = l.job_id and j.run_id = l.run_id and j.attempt = l.attempt
join raw.workflow_runs w on w.run_id = j.run_id and w.attempt = j.attempt
join raw.repos r on r.id = w.repo_id
where l.labelled_by = 'rule' and l.sub_label in ('pass', 'flaky', 'genuine')
  and (%(repos)s::text[] is null or r.full_name = any(%(repos)s))
order by w.created_at, j.job_id
"""


def dataset_feature_names(version: str) -> list[str]:
    """Feature columns of a dataset version: v1 = the original 41, later = all current ones."""
    names = list(feature_names())
    if version == "v1":
        return [n for n in names if not n.startswith(V2_ADDED_PREFIXES)]
    return names


def _git_dirty() -> bool:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=True,
        )
        return bool(out.stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return False


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def sha256_file(path: Path) -> str:
    """Hex sha256 of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _column(values: list[Any]) -> pa.Array:
    """Arrow column; all-null columns become float64 so the schema is stable."""
    arr = pa.array(values)
    return pa.array(values, type=pa.float64()) if pa.types.is_null(arr.type) else arr


def build_table(
    conn: psycopg.Connection[Any],
    repos: Sequence[str] | None = None,
    names: Sequence[str] | None = None,
) -> pa.Table:
    """Query labelled attempts (optionally only `repos`), compute features, return the table.

    `names` picks the feature columns (default: all current ones, see dataset_feature_names).
    """
    meta = conn.execute(_ROWS_SQL, {"repos": list(repos) if repos else None}).fetchall()  # type: ignore[call-overload]
    log.info("labelled attempts", extra={"rows": len(meta)})
    feats = compute_features_batch(conn, [(m[0], m[1], m[2]) for m in meta])
    names = list(names) if names is not None else feature_names()
    cols: dict[str, list[Any]] = {c: [] for c in (*KEY_COLS, *TARGET_COLS, *names)}
    for run_id, attempt, job_id, repo, created, series, sub, conf, rule, n_fail in meta:
        f = feats[(run_id, attempt, job_id)]
        row: dict[str, Any] = {
            "run_id": run_id,
            "attempt": attempt,
            "job_id": job_id,
            "repo": repo,
            "created_at": created.astimezone(UTC),
            "series_key": series,
            "label": LABEL_MAP[sub],
            "label_confidence": conf,
            "rule_name": rule,
            "genuine_strict": sub == "genuine" and int(n_fail) >= 2,
        }
        row.update({n: f.get(n) for n in names})
        for k, v in row.items():
            cols[k].append(v)
    return pa.table({k: _column(v) for k, v in cols.items()})


def write_dataset(
    table: pa.Table, out_dir: Path, version: str, force: bool = False
) -> dict[str, Any]:
    """Write rows.parquet + manifest.json into out_dir; refuse to overwrite unless force."""
    if out_dir.exists() and any(out_dir.iterdir()) and not force:
        raise FileExistsError(f"{out_dir} exists; datasets are frozen, use --force to overwrite")
    out_dir.mkdir(parents=True, exist_ok=True)
    rows_path = out_dir / "rows.parquet"
    pq.write_table(table, rows_path, compression="zstd")
    d = table.to_pydict()
    manifest: dict[str, Any] = {
        "version": version,
        "built_at": datetime.now(UTC).isoformat(),
        "git_commit": _git_commit(),
        "history_cutoff": "per row: created_at of the run",
        "n_rows": table.num_rows,
        "repos": sorted(set(d["repo"])),
        "rows_per_label": dict(Counter(d["label"])),
        "rows_per_label_confidence": dict(
            Counter(f"{a}/{b}" for a, b in zip(d["label"], d["label_confidence"], strict=True))
        ),
        "rows_per_repo": dict(Counter(d["repo"])),
        "genuine_strict_rows": sum(1 for x in d["genuine_strict"] if x),
        "variants": {
            "all": table.num_rows,
            "high": sum(1 for x in d["label_confidence"] if x == "high"),
            "strict": sum(
                1
                for lab, s in zip(d["label"], d["genuine_strict"], strict=True)
                if lab != "genuine_fail" or s
            ),
        },
        "time_range": [min(d["created_at"]).isoformat(), max(d["created_at"]).isoformat()]
        if table.num_rows
        else None,
        "key_columns": list(KEY_COLS),
        "target_columns": list(TARGET_COLS),
        "feature_names": [c for c in table.column_names if c not in (*KEY_COLS, *TARGET_COLS)],
        "files": {
            "rows.parquet": {"sha256": sha256_file(rows_path), "bytes": rows_path.stat().st_size}
        },
    }
    if version == "v1":
        manifest["v1_cuts"] = V1_CUTS
    else:
        manifest.update(
            {
                "frozen": False,
                "frozen_note": V2_FROZEN_NOTE,
                "git_dirty": _git_dirty(),
                "feature_groups_added": V2_FEATURE_GROUPS_ADDED,
                "cuts": V2_CUTS,
            }
        )
        if version != "v2":
            manifest["label_changes"] = V3_LABEL_CHANGES
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n")
    return manifest


def verify_manifest(out_dir: Path) -> bool:
    """True when every file listed in manifest.json matches its recorded sha256."""
    m = json.loads((out_dir / "manifest.json").read_text())
    return all(sha256_file(out_dir / n) == info["sha256"] for n, info in m["files"].items())


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    ap = argparse.ArgumentParser(prog="testing_expert.dataset", description=__doc__)
    ap.add_argument("--version", required=True, help="e.g. v1")
    ap.add_argument("--out-root", type=Path, default=Path("data/datasets/testing"))
    ap.add_argument("--force", action="store_true", help="overwrite an existing version")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    out_dir = args.out_root / args.version
    if out_dir.exists() and any(out_dir.iterdir()) and not args.force:
        print(f"refusing: {out_dir} exists (use --force)", file=sys.stderr)
        return 1
    t0 = time.perf_counter()
    with psycopg.connect(os.environ["DATABASE_URL"].replace("+psycopg", "")) as conn:
        table = build_table(conn, names=dataset_feature_names(args.version))
    m = write_dataset(table, out_dir, args.version, force=args.force)
    print(f"{m['n_rows']} rows in {time.perf_counter() - t0:.0f}s -> {out_dir}")
    print(
        json.dumps(
            {
                k: m[k]
                for k in (
                    "rows_per_label",
                    "rows_per_label_confidence",
                    "variants",
                    "rows_per_repo",
                )
            },
            indent=1,
        )
    )
    print(f"rows.parquet: {m['files']['rows.parquet']['bytes'] / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
