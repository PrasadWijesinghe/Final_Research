"""Triage of one live failure: RoutedFailure in, verdict and evidence out (P4.9).

Used by `diagnose.py`. Everything that can go wrong degrades quietly: no bundle, no database, no
full log. The result is None only when triage cannot say anything (no bundle, or the log shows no
Jest, Mocha or Vitest failure), and then the caller keeps the stub diagnosis.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import psycopg

from shared.contracts import RoutedFailure

from . import loader
from .features import (
    FEATURE_NAMES,
    LogEvidence,
    Reason,
    TriageEvidence,
    feature_row,
    read_log,
    reasons,
)
from .gather import FailureRef, gather
from .model import TriageModel, load_bundle

logger = logging.getLogger(__name__)

BUNDLE_ENV = "TESTING_TRIAGE_BUNDLE"
_VERSION_DIR = re.compile(r"^v(\d+)$")
_REQUIRED = ("features.json", "floors.json", "model.joblib", "calibrator.joblib")
# DEFAULT: triage is its own bundle family under models/testing/triage/v<N>, so the pre-push
# search for the newest `models/testing/v<N>` never sees it (the folder name is `triage`).
TRIAGE_DIR = Path("models") / "testing" / "triage"


@dataclass(frozen=True)
class TriageResult:
    """The triage outcome for one failure."""

    verdict: Literal["genuine", "flaky", "unclear"]
    confidence: float
    p_flaky: float
    suggest_rerun: bool
    evidence: TriageEvidence
    reasons: list[Reason]
    model_version: str

    @property
    def framework(self) -> Literal["jest", "mocha", "vitest", "other"]:
        """Test runner found in the log."""
        fw = self.evidence.log.framework
        return fw if fw in ("jest", "mocha", "vitest") else "other"  # type: ignore[return-value]


def _is_bundle(path: Path) -> bool:
    return path.is_dir() and all((path / n).is_file() for n in _REQUIRED)


def resolve_bundle_path(raw: str | None = None) -> Path | None:
    """Triage bundle folder: `TESTING_TRIAGE_BUNDLE` (a bundle, or a folder of v<N> bundles);
    otherwise the newest `models/testing/triage/v<N>` found from the working directory or above
    this file."""
    value = raw if raw is not None else os.environ.get(BUNDLE_ENV)
    bases = (
        [Path(value)]
        if value
        else [Path.cwd() / TRIAGE_DIR, *(p / TRIAGE_DIR for p in Path(__file__).resolve().parents)]
    )
    for base in bases:
        if _is_bundle(base):
            return base
        if base.is_dir():
            versions = sorted(
                (int(m.group(1)), d)
                for d in base.iterdir()
                if (m := _VERSION_DIR.match(d.name)) and _is_bundle(d)
            )
            if versions:
                return versions[-1][1]
    return None


@lru_cache(maxsize=1)
def get_model() -> tuple[TriageModel, str] | None:
    """The triage model and its version, loaded once; None when missing or unreadable."""
    path = resolve_bundle_path()
    if path is None:
        logger.info("no triage bundle found (set %s), triage is off", BUNDLE_ENV)
        return None
    try:
        return load_bundle(path), f"triage-{path.name}"
    except Exception:
        logger.warning("triage bundle %s failed to load, triage is off", path, exc_info=True)
        return None


def _connect() -> psycopg.Connection[Any] | None:
    url = os.environ.get("DATABASE_URL")
    return psycopg.connect(url.replace("+psycopg", ""), connect_timeout=3) if url else None


def log_text(failure: RoutedFailure) -> str:
    """The full sanitised log when it can be read from DATA_DIR, else the router's excerpt."""
    root = os.environ.get("DATA_DIR")
    if root:
        base = Path(root).resolve()
        path = (base / str(failure.log_ref)).resolve()
        if path.is_file() and base in path.parents:  # never read outside DATA_DIR
            try:
                from testing_expert.parsing import clean_file

                return clean_file(path).text()
            except Exception:
                logger.warning("full log unreadable, using the excerpt", exc_info=True)
    return failure.log_excerpt


def prerun_flaky_risk(conn: loader.Conn, failure: RoutedFailure) -> float | None:
    """The pre-run model's `flaky_fail` for this job, from the stored prediction (or None)."""
    if failure.context_id is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
select (payload->'sub_risks'->>'flaky_fail')::float from platform.predictions
where context_id = %s and expert = 'test'
order by (payload->>'job_key' is not distinct from %s) desc, probability desc nulls last
limit 1""",
            (failure.context_id, failure.job_key),
        )
        row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def collect(failure: RoutedFailure, conn: loader.Conn | None) -> TriageEvidence | None:
    """Evidence for a live failure; None when the log shows no test-runner failure."""
    repo = f"{failure.repo.owner}/{failure.repo.name}"
    log: LogEvidence = read_log(log_text(failure), repo)
    if not log.is_test_failure:
        return None
    cutoff: datetime = failure.routed_at
    job = None
    prerun: float | None = None
    if conn is not None:
        try:
            job = loader.job_info(conn, failure.job_id)
            prerun = prerun_flaky_risk(conn, failure)
        except Exception:
            logger.warning("job lookup failed, using routed_at as cutoff", exc_info=True)
            conn.rollback()
    if job is not None:
        cutoff = job.completed_at
    ref = FailureRef(
        repo=repo,
        head_sha=failure.head_sha,
        job_id=failure.job_id,
        run_attempt=failure.run_attempt,
        job_name=failure.job_name,
        workflow_path=failure.workflow_path,
        cutoff=cutoff,
    )
    try:
        return gather(conn, ref, log, prerun)
    except Exception:
        logger.warning("database evidence unavailable, triage uses the log only", exc_info=True)
        return gather(None, ref, log, prerun)


def triage(failure: RoutedFailure) -> TriageResult | None:
    """Verdict, confidence, rerun advice and reasons for a failed test job (None = not triaged)."""
    loaded = get_model()
    if loaded is None:
        return None
    model, version = loaded
    conn = None
    try:
        try:
            conn = _connect()
        except Exception:
            logger.warning("database unreachable, triage uses the log only", exc_info=True)
        ev = collect(failure, conn)
    finally:
        if conn is not None:
            conn.close()
    if ev is None:
        return None
    row = pd.DataFrame([feature_row(ev)], columns=list(FEATURE_NAMES))
    p_flaky = float(model.predict_flaky(row)[0])
    verdict, confidence = model.verdicts(np.array([p_flaky]))[0]
    return TriageResult(
        verdict=verdict,
        confidence=float(confidence),
        p_flaky=p_flaky,
        suggest_rerun=verdict == "flaky",  # model.verdicts only says flaky at or above the floor
        evidence=ev,
        reasons=order_reasons(reasons(ev), verdict),
        model_version=version,
    )


def order_reasons(items: list[Reason], verdict: str) -> list[Reason]:
    """Reasons that point to the verdict first, then neutral ones, then the ones against it."""
    rank = {verdict: 0, "neutral": 1}
    return sorted(items, key=lambda r: rank.get(r.leans, 2))  # stable: keeps the evidence order
