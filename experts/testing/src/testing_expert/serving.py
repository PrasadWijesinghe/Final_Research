"""Serve the trained pre-run model (P4.12 partial): bundle loading, request-time features, scoring.

The features come from the same code that built the training rows (`features/compute.build_row`).
Everything that can go wrong (no bundle, bad bundle, no database, unknown repo) degrades quietly:
a missing bundle means the stub answers, a missing database or repo means cold-start history.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
import psycopg

from shared.contracts import CommitContext, RiskOutput, WorkflowJob
from shared.contracts.risk_output import RiskFactor

from .config import EXPERT
from .features import compute, describe, loader, test_changes
from .features._types import ChangedTestFile, FeatureDict, FileChange, JobTarget
from .features.history import EMPTY_SERIES
from .features.history_tests import EMPTY_TEST_HISTORY
from .features.repo import EMPTY_DURATIONS, EMPTY_TIMELINE
from .features.settings import get_settings
from .model import rule as still_failing
from .model.data import CLASSES, Encoder
from .model.metrics import apply_binary_calibrator, apply_calibrator, combine_any, p_fail
from .model.models import SEPARATE_KIND, SeparateBooster, TwoStageBooster
from .predict import is_test_job

logger = logging.getLogger(__name__)

BUNDLE_ENV = "TESTING_MODEL_BUNDLE"
_VERSION_DIR = re.compile(r"^v(\d+)$")
_REQUIRED = ("features.json", "threshold.json", "calibrator.joblib")
# v1-v3 bundles hold model.txt; v4 (separate genuine and flaky models) holds model_genuine.txt.
_MODEL_FILES = ("model.txt", "model_genuine.txt")
# DEFAULT: the interim reasons show at most the 5 allowed by the contract.
MAX_FACTORS = 5
# DEFAULT: a score is shown in factors only when its contribution is at least this (non-zero).
MIN_CONTRIB = 1e-6


@dataclass
class Bundle:
    """A loaded model bundle (models/testing/<version>/)."""

    version: str
    threshold: float
    encoder: Encoder
    booster: lgb.Booster | TwoStageBooster | SeparateBooster
    calibrator: dict[str, Any]
    path: Path
    # v6+: the "still failing" rule table (rule.json); None for older bundles
    rule: dict[str, Any] | None = None

    @property
    def history_mode(self) -> str:
        """Which per-test history the model reads: "none" (no ht_ column), "job" or "full"."""
        cols = self.encoder.columns
        if any(c.startswith("ht_touched_") for c in cols):
            return "full"
        return "job" if any(c.startswith("ht_") for c in cols) else "none"


def _is_bundle(path: Path) -> bool:
    return (
        path.is_dir()
        and all((path / name).is_file() for name in _REQUIRED)
        and any((path / name).is_file() for name in _MODEL_FILES)
    )


def resolve_bundle_path(raw: str | None = None) -> Path | None:
    """Bundle folder from `TESTING_MODEL_BUNDLE`; a folder of versions picks the newest v<N>.

    # DEFAULT: with no env var, the newest bundle under models/testing/ (searched from the working
    # directory and the parents of this file), so a new version is served as soon as it appears.
    """
    value = raw if raw is not None else os.environ.get(BUNDLE_ENV)
    candidates: list[Path] = []
    if value:
        candidates.append(Path(value))
    else:
        candidates.append(Path.cwd() / "models" / "testing")
        candidates.extend(p / "models" / "testing" for p in Path(__file__).resolve().parents)
    for base in candidates:
        if _is_bundle(base):
            return base
        if base.is_dir():
            versions = sorted(
                (
                    (int(m.group(1)), d)
                    for d in base.iterdir()
                    if (m := _VERSION_DIR.match(d.name)) and _is_bundle(d)
                ),
                key=lambda t: t[0],
            )
            if versions:
                return versions[-1][1]
    return None


def load_bundle(path: Path) -> Bundle:
    """Read a LightGBM bundle; raises on anything unexpected (the caller falls back to the stub)."""
    meta = json.loads((path / "features.json").read_text())
    if meta.get("kind") not in ("lightgbm", SEPARATE_KIND):
        # DEFAULT: only LightGBM bundles are served (cheap per-feature contributions for reasons).
        raise ValueError(f"unsupported model kind {meta.get('kind')!r}")
    if list(meta.get("classes", [])) != list(CLASSES):
        raise ValueError("bundle classes differ from the trainer's")
    threshold = float(json.loads((path / "threshold.json").read_text())["threshold"])
    return Bundle(
        version=f"lightgbm-{path.name}",
        threshold=threshold,
        encoder=Encoder(numeric=list(meta["numeric"]), categorical=dict(meta["categorical"])),
        # v3+ bundles are two-stage (fail-vs-pass booster + genuine-vs-flaky booster)
        # v4 is two separate binary models (genuine vs rest, flaky vs rest)
        booster=SeparateBooster.load(path)
        if meta["kind"] == SEPARATE_KIND
        else TwoStageBooster.load(path)
        if (path / "split_model.txt").exists()
        else lgb.Booster(model_file=str(path / "model.txt")),
        calibrator=joblib.load(path / "calibrator.joblib"),
        path=path,
        rule=json.loads((path / "rule.json").read_text())
        if (path / "rule.json").is_file()
        else None,
    )


@lru_cache(maxsize=1)
def get_bundle() -> Bundle | None:
    """The served bundle, loaded once; None (stub mode) when missing or unreadable."""
    path = resolve_bundle_path()
    if path is None:
        logger.warning("no model bundle found, serving the stub (set %s)", BUNDLE_ENV)
        return None
    try:
        bundle = load_bundle(path)
    except Exception:
        logger.warning("model bundle %s failed to load, serving the stub", path, exc_info=True)
        return None
    logger.info("model bundle loaded", extra={"version": bundle.version, "path": str(path)})
    return bundle


# --- request-time features -------------------------------------------------------------------


def _os_names(job: WorkflowJob | None) -> tuple[str | None, str]:
    """(runner_os as raw stores it, normalised OS) from the job's runs-on labels."""
    blob = " ".join(job.runs_on).lower() if job else ""
    if "windows" in blob:
        return "Windows", "windows"
    if "mac" in blob or "darwin" in blob:
        return "macOS", "macos"
    if "ubuntu" in blob or "linux" in blob:
        return "Linux", "linux"
    return None, "unknown"


def _file_changes(context: CommitContext) -> list[FileChange]:
    return [
        FileChange(
            path=f.path,
            additions=f.additions,
            deletions=f.deletions,
            tags=frozenset(t.value for t in f.tags),
            patch=f.patch if any(t.value == "package_manifest" for t in f.tags) else None,
        )
        for f in context.changed_files
    ]


def _test_files(context: CommitContext) -> list[ChangedTestFile]:
    out = []
    for f in context.changed_files:
        if f.status == "removed" or not test_changes.is_code_path(f.path):
            continue
        if not any(t.value == "test" for t in f.tags):
            continue
        patch = f.patch if f.patch and len(f.patch) <= test_changes.MAX_PATCH_CHARS else None
        out.append(ChangedTestFile(f.path, patch))
    return out


def _connect() -> psycopg.Connection[Any] | None:
    """Short-lived read connection, or None when no DATABASE_URL is set."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    return psycopg.connect(url.replace("+psycopg", ""), connect_timeout=3)


def _target(context: CommitContext, job: WorkflowJob | None, repo_id: int) -> JobTarget:
    runner_os, os_name = _os_names(job)
    return JobTarget(
        run_id=0,
        attempt=1,
        job_id=0,
        repo_id=repo_id,
        cutoff=context.history_cutoff.timestamp(),
        head_sha=context.head_sha,
        event=context.event,
        branch=context.branch,
        default_branch=None,  # DEFAULT: unknown before the run; rs_on_default_branch stays missing
        runner_os=runner_os,
        workflow_path=job.workflow_path if job else "",
        series_key=job.job_key if job else "",
        matrix_size=1,  # DEFAULT: no parsed matrix in the context, same proxy as training (1)
        jobs_in_run=max(len(context.workflow_jobs), 1),
        os_name=os_name,
    )


def build_features(
    context: CommitContext, jobs: list[WorkflowJob | None], history_mode: str = "full"
) -> tuple[list[FeatureDict], bool]:
    """One feature row per job; second value is True when the database was needed but failed.

    Change-size, config, run-setup and test-change groups come from the context itself. History
    comes from `raw` when DATABASE_URL works and the repo is known; otherwise it is cold start.
    `history_mode` says which per-test history (`ht_*`) to load: "none" skips it (the bundle has no
    `ht_` column, so the 5 s query is not needed), "job" loads only the job-level numbers (fast),
    "full" loads everything including the touched-file numbers.
    """
    settings = get_settings()
    files = _file_changes(context)
    test_files = _test_files(context)
    tw = test_changes.what_changed_in_tests(test_files)
    db_failed = False
    repo_id = 0
    series: dict[Any, Any] = {}
    os_series: dict[Any, Any] = {}
    durations: dict[Any, Any] = {}
    timelines: dict[Any, Any] = {}
    test_hist: dict[Any, Any] = {}
    cutoff = context.history_cutoff.timestamp()
    try:
        conn = _connect()
        if conn is not None:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "select id from raw.repos where full_name = %s",
                        (f"{context.repo.owner}/{context.repo.name}",),
                    )
                    row = cur.fetchone()
                if row is not None:  # unknown repo: cold start
                    repo_id = int(row[0])
                    only = sorted(
                        {(repo_id, j.workflow_path, j.job_key) for j in jobs if j is not None}
                    )
                    series = loader.load_series(conn, [repo_id], cutoff, only)
                    os_series = loader.load_series_by_os(conn, [repo_id], cutoff, only)
                    durations = loader.load_durations(conn, [repo_id], cutoff, settings)
                    timelines = loader.load_frameworks(conn, [repo_id], settings)
                    if history_mode == "job":
                        test_hist = loader.load_job_test_history(conn, only, cutoff)
                    elif history_mode == "full":
                        test_hist = loader.load_test_history(conn, [repo_id], cutoff, only)
    except Exception:
        logger.warning("history unavailable, scoring with cold-start history", exc_info=True)
        db_failed = True
        repo_id = 0
    rows: list[FeatureDict] = []
    for job in jobs:
        t = _target(context, job, repo_id)
        rows.append(
            compute.build_row(
                t,
                files,
                series.get(loader.series_key_of(t), EMPTY_SERIES),
                durations.get(repo_id, EMPTY_DURATIONS),
                timelines.get(repo_id, EMPTY_TIMELINE),
                settings,
                os_series.get(loader.os_series_key_of(t), EMPTY_SERIES),
                test_change_row=tw,
                touched_test_files=[f.path for f in test_files],
                test_history=test_hist.get(loader.series_key_of(t), EMPTY_TEST_HISTORY),
            )
        )
    return rows, db_failed


# --- scoring and reasons ---------------------------------------------------------------------

# Where a tw_ feature points: the first added line that matches (path:line evidence).
_TW_LINE = {
    "tw_set_timeout": re.compile(r"\bset(Timeout|Interval)\s*\("),
    "tw_math_random": re.compile(r"\bMath\.random\s*\("),
    "tw_date_now": re.compile(r"\bDate\.now\s*\(|\bnew Date\s*\("),
    "tw_unmocked_network": re.compile(r"\bfetch\s*\(|\baxios\b|\bgot\s*\(|https?\.(get|request)\("),
    "tw_only_added": re.compile(r"\.only\s*\(|\bf(it|describe)\s*\("),
    "tw_skip_added": re.compile(r"\.skip\s*\(|\bx(it|describe|test)\s*\("),
    "tw_real_timers": re.compile(r"useRealTimers"),
}
_HUNK_START = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")


def _evidence(feature: str, context: CommitContext) -> tuple[str | None, int | None]:
    """File and line of the first added line behind a tw_ feature (path only when no line)."""
    files = _test_files(context)
    pattern = _TW_LINE.get(feature)
    for f in files:
        if f.patch is None or pattern is None:
            continue
        line_no = 0
        for raw in f.patch.split("\n"):
            m = _HUNK_START.match(raw)
            if m:
                line_no = int(m.group(1)) - 1
            elif raw.startswith("+"):
                line_no += 1
                if pattern.search(raw):
                    return f.path, line_no
            elif raw.startswith(" "):
                line_no += 1
    if feature.startswith("tw_") and files:
        return files[0].path, None
    return None, None


def _frame(bundle: Bundle, rows: list[FeatureDict]) -> pd.DataFrame:
    df = pd.DataFrame(rows).reindex(columns=bundle.encoder.columns)
    return bundle.encoder.lgbm_frame(df)


_OS_LABEL = {"linux": "Linux", "windows": "Windows", "macos": "macOS"}


def still_failing_reason(count: int, os_name: str | None) -> str:
    """The plain-language reason of the "still failing" rule (at most 200 characters)."""
    times = "1 time" if count == 1 else f"{count} times"
    where = f" on {_OS_LABEL[os_name]}" if os_name in _OS_LABEL else ""
    return (
        f"This job already failed {times} in a row{where}; "
        "the failure is likely not caused by this push."
    )


def _factors(
    bundle: Bundle,
    frame: pd.DataFrame,
    i: int,
    row: FeatureDict,
    context: CommitContext,
    limit: int = MAX_FACTORS,
) -> list[RiskFactor]:
    # DEFAULT: interim reasons until P4.8 SHAP: LightGBM's per-feature contributions to the
    # genuine-fail and flaky-fail scores (pred_contrib), summed, largest absolute value first.
    contrib = np.asarray(bundle.booster.predict(frame.iloc[[i]], pred_contrib=True))[0]
    n = len(bundle.encoder.columns)
    contrib = contrib.reshape(len(CLASSES), n + 1)[:, :n]
    fail = contrib[1] + contrib[2]
    order = np.argsort(-np.abs(fail))
    out: list[RiskFactor] = []
    for idx in order:
        value = float(fail[idx])
        if abs(value) < MIN_CONTRIB:
            break
        name = bundle.encoder.columns[idx]
        raw = row.get(name)
        missing = raw is None or (isinstance(raw, float) and math.isnan(raw))
        has_diff = name.startswith("tw_") and not missing
        path, line = _evidence(name, context) if has_diff else (None, None)
        out.append(
            RiskFactor(
                feature=name,
                value="unknown" if missing else raw,  # type: ignore[arg-type]
                shap_value=value,
                direction="raises" if value >= 0 else "lowers",
                reason=describe(name, None if missing else raw)[:200],
                path=path,
                line=line,
            )
        )
        if len(out) == limit:
            break
    return out


def predict_with_model(
    bundle: Bundle, context: CommitContext, jobs: list[WorkflowJob]
) -> list[RiskOutput]:
    """One RiskOutput per test job (or one commit row when there is none): calibrated P(fail)."""
    started = time.perf_counter()
    targets: list[WorkflowJob | None] = list(jobs) or [None]
    rows, db_failed = build_features(context, targets, bundle.history_mode)
    frame = _frame(bundle, rows)
    if isinstance(bundle.booster, SeparateBooster):
        # v4: each class has its own calibrator; P(fail) = 1 - (1 - pg)(1 - pf)
        raw_g, raw_f = bundle.booster.predict_each(frame)
        pg = apply_binary_calibrator(bundle.calibrator["gen"], raw_g)
        pf = apply_binary_calibrator(bundle.calibrator["flaky"], raw_f)
        calibrated = np.column_stack([1 - combine_any(pg, pf), pg, pf])
        scores = combine_any(pg, pf)
    else:
        proba = np.asarray(bundle.booster.predict(frame))
        calibrated = apply_calibrator(bundle.calibrator, proba)
        scores = p_fail(calibrated)
    # v6: a job whose previous run failed is scored at least at the rule's empirical fail rate
    rule_p = (
        still_failing.apply_rule(bundle.rule, pd.DataFrame(rows))
        if bundle.rule is not None
        else None
    )
    if rule_p is not None:
        scores = still_failing.combine(scores, rule_p["any"])
        calibrated = np.column_stack(
            [
                calibrated[:, 0],
                still_failing.combine(calibrated[:, 1], rule_p["genuine"]),
                still_failing.combine(calibrated[:, 2], rule_p["flaky"]),
            ]
        )
    latency_ms = int((time.perf_counter() - started) * 1000)
    outputs = []
    for i, job in enumerate(targets):
        prob = float(min(max(scores[i], 0.0), 1.0))
        rule_on = rule_p is not None and bool(rule_p["active"][i])
        factors = _factors(bundle, frame, i, rows[i], context, MAX_FACTORS - int(rule_on))
        sub_risks = {
            "genuine_fail": float(min(calibrated[i][1], 1.0)),
            "flaky_fail": float(min(calibrated[i][2], 1.0)),
        }
        if rule_p is not None and rule_on:
            count = int(rule_p["count"][i])
            rule_value = float(min(rule_p["any"][i], 1.0))
            sub_risks["still_failing"] = rule_value
            os_name = _os_names(job)[1] if rule_p["same_os"][i] else None
            factors.insert(
                0,
                RiskFactor(
                    feature="still_failing_streak",
                    value=count,
                    shap_value=rule_value,
                    direction="raises",
                    reason=still_failing_reason(count, os_name),
                ),
            )
        alert = prob >= bundle.threshold
        if alert and not factors:
            factors = [
                RiskFactor(
                    feature="model_score",
                    value=round(prob, 4),
                    shap_value=prob,
                    direction="raises",
                    reason="The overall model score is above the alert threshold",
                )
            ]
        outputs.append(
            RiskOutput(
                context_id=context.context_id,
                expert=EXPERT,
                model_version=bundle.version,
                scope="job" if job else "commit",
                workflow_path=job.workflow_path if job else None,
                job_key=job.job_key if job else None,
                status="degraded" if db_failed else "ok",
                error_message="history database unreachable, scored with cold-start history"
                if db_failed
                else None,
                probability=prob,
                threshold=bundle.threshold,
                alert=alert,
                sub_risks=sub_risks,
                top_factors=factors,
                latency_ms=latency_ms,
            )
        )
    return outputs


def serve(context: CommitContext) -> list[RiskOutput] | None:
    """Model outputs for a commit, or None when no model is loaded (caller uses the stub).

    DEFAULT: one output per test-like job from the workflow files, each scored on its own. The
    commit's headline risk is the highest of them (the caller sorts); none found = one row.
    """
    bundle = get_bundle()
    if bundle is None:
        return None
    jobs = [j for j in context.workflow_jobs if is_test_job(j)]
    try:
        return predict_with_model(bundle, context, jobs)
    except Exception:
        logger.warning("model scoring failed, serving the stub", exc_info=True)
        return None
