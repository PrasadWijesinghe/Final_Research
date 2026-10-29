"""`uv run --env-file .env build-demo --dataset v1 --model models/build/m1`: real examples.

Evidence that the whole expert works on collected data:
1. Predictions: commits from the newest (test) part of the dataset, rebuilt by replay and scored
   by the loaded model exactly as POST /predict does (with sub-risks and top factors).
2. Diagnoses: failed jobs from `job_decisions.parquet` (a few per kind, plus declined ones), turned
   into RoutedFailures from the stored tables and logs and diagnosed as the worker does.
Writes `<out>/build_demo.md` (readable) and `<out>/build_demo.json` (the contract objects).
Never calls GitHub.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
from features.replay import SqlHistory, build_replay_context
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from shared.contracts import Diagnosis, FailedStep, RepoRef, RiskOutput, RoutedFailure
from shared.contracts.common import Expert, FailureCategory
from shared.db import models as m
from shared.db.session import get_engine
from shared.file_tags import FileTagger

from .diagnose import diagnose
from .rag.kb import default_kb
from .serving import BuildPredictor

logger = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parents[4]
EXCERPT_CHARS = 20_000  # RoutedFailure.log_excerpt limit


def pick_commits(rows: list[dict[str, Any]], n: int, seed: int = 1) -> list[dict[str, Any]]:
    """Newest 15% of commits: up to n build failures and n/2 passes, at random."""
    rows = sorted(rows, key=lambda r: r["history_cutoff"])
    test = rows[int(len(rows) * 0.85) :]
    rng = np.random.default_rng(seed)
    fails = [r for r in test if r["y_build"]]
    passes = [r for r in test if not r["y_any_failure"]]
    out: list[dict[str, Any]] = []
    for pool, k in ((fails, n), (passes, max(1, n // 2))):
        if pool:
            out += [
                pool[i] for i in sorted(rng.choice(len(pool), min(k, len(pool)), replace=False))
            ]
    return out


def pick_jobs(decisions: list[dict[str, Any]], n: int, seed: int = 1) -> list[dict[str, Any]]:
    """First-attempt failed jobs with a stored log: one per sub-label in turn, plus 2 declined."""
    rng = np.random.default_rng(seed)
    usable = [d for d in decisions if d["attempt"] == 1 and d["log_status"] in (None, "ok")]
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for d in usable:
        by_kind.setdefault(d["sub_label"] if d["claimed"] else "declined", []).append(d)
    out: list[dict[str, Any]] = []
    kinds = sorted(k for k in by_kind if k != "declined")
    while len(out) < n and any(by_kind.get(k) for k in kinds):
        for k in kinds:
            pool = by_kind[k]
            if pool and len(out) < n:
                out.append(pool.pop(int(rng.integers(len(pool)))))
    declined = by_kind.get("declined", [])
    for _ in range(min(2, len(declined))):
        out.append(declined.pop(int(rng.integers(len(declined)))))
    return out


def routed_failure(
    engine: Engine, data_dir: Path, decision: dict[str, Any]
) -> RoutedFailure | None:
    """A RoutedFailure for a stored failed job, as the router would send it in replay mode."""
    with Session(engine) as s:
        job = s.get(m.Job, decision["job_id"])
        log = s.get(m.Log, decision["job_id"])
        run = s.get(m.WorkflowRun, (decision["run_id"], decision["attempt"]))
        steps = s.scalars(
            select(m.Step).where(m.Step.job_id == decision["job_id"]).order_by(m.Step.number)
        ).all()
    if job is None or run is None or log is None or not log.storage_path:
        return None
    failed = [
        FailedStep(number=st.number, name=st.name or f"step {st.number}", conclusion="failure")
        for st in steps
        if st.conclusion == "failure"
    ] or [FailedStep(number=1, name=job.name or "job", conclusion="failure")]
    try:
        text = gzip.decompress((data_dir / log.storage_path).read_bytes()).decode(
            "utf-8", "replace"
        )
    except OSError:
        return None
    owner, name = decision["repo"].split("/", 1)
    return RoutedFailure(
        repo=RepoRef(owner=owner, name=name),
        head_sha=run.head_sha,
        run_id=run.run_id,
        run_attempt=run.attempt,
        job_id=job.job_id,
        job_name=job.name or "job",
        workflow_path=run.workflow_path or "",
        job_key=job.job_key,
        failed_steps=failed,
        log_ref=log.storage_path,
        log_excerpt=text[-EXCERPT_CHARS:],
        predicted_category=FailureCategory.BUILD,
        routing_confidence=1.0,
        sent_to=[Expert.BUILD],
        replay=True,
    )


def _md_prediction(row: dict[str, Any], out: RiskOutput) -> list[str]:
    actual = "build failure" if row["y_build"] else "passed"
    lines = [
        f"### {row['repo']} `{row['head_sha'][:10]}` (actually: {actual})",
        "",
        f"Probability {out.probability:.3f} against threshold {out.threshold:.3f}: "
        f"**{'ALERT' if out.alert else 'no alert'}**. Sub-risks: "
        + ", ".join(f"{k} {v:.2f}" for k, v in out.sub_risks.items())
        + f". Status {out.status}, {out.latency_ms} ms.",
        "",
    ]
    lines += [f"- {f.reason} (SHAP {f.shap_value:+.3f})" for f in out.top_factors]
    return [*lines, ""]


def _md_diagnosis(decision: dict[str, Any], d: Diagnosis) -> list[str]:
    lines = [
        f"### {decision['repo']}: {decision['job_name']} ([job]({decision['url']}))",
        "",
        f"Rules said: {decision['rule_name']} ({decision['sub_label'] or 'not build'}). "
        f"Expert answer: **{d.status}**"
        + (f", suggests {d.suggested_category.value}" if d.suggested_category else "")
        + f" ({d.latency_ms} ms).",
        "",
    ]
    if d.root_cause:
        lines += [f"**Root cause.** {d.root_cause}", ""]
    lines += ["**Evidence:**", *[f"- `{e.text[:200]}`" for e in d.evidence], ""]
    if d.retrieved_cases:
        lines += ["**Similar past fixes:**"]
        lines += [
            f"- {c.case_id} (similarity {c.similarity:.2f}): {c.fix_summary}"
            for c in d.retrieved_cases
        ]
        lines.append("")
    if d.remediation:
        lines += [
            f"**Fix.** {d.remediation.summary}",
            *[f"1. {s}" for s in d.remediation.steps],
            "",
        ]
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for `build-demo`."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="build-demo", description=__doc__)
    p.add_argument("--dataset", required=True, help="dataset version (v1) or a folder path")
    p.add_argument("--model", type=Path, required=True, help="model bundle folder")
    p.add_argument("--commits", type=int, default=6, help="build failures to predict (default 6)")
    p.add_argument("--failures", type=int, default=6, help="failed jobs to diagnose (default 6)")
    p.add_argument("--data-dir", type=Path, help="data folder (default: DATA_DIR or ./data)")
    p.add_argument("--out", type=Path, help="folder for build_demo.md/json (default: the model)")
    args = p.parse_args(argv)
    data_dir = args.data_dir or Path(os.environ.get("DATA_DIR") or REPO_ROOT / "data")
    os.environ["DATA_DIR"] = str(data_dir)  # the diagnosis reads full logs from here
    folder = Path(args.dataset)
    if not folder.is_dir():
        folder = data_dir / "datasets" / "build" / args.dataset
    try:
        engine = get_engine()
        predictor = BuildPredictor.load(args.model, data_dir)
    except (RuntimeError, OSError, KeyError) as exc:
        sys.stderr.write(f"build-demo: {exc}\n")
        return 2
    tagger = FileTagger.load(REPO_ROOT / "config" / "file_tags.yaml")
    history = SqlHistory(engine)
    md = [
        f"# Build expert: real examples ({datetime.now(UTC):%Y-%m-%d %H:%M} UTC)",
        "",
        f"Model `{predictor.version}`, dataset `{folder.name}`. Predictions come from the newest "
        "commits (the test part), rebuilt from stored data exactly as `/predict` would see them. "
        "Diagnoses are for stored failed jobs, run as the worker runs them.",
        "",
        "## Predictions before CI",
        "",
    ]
    dump: dict[str, Any] = {"predictions": [], "diagnoses": []}
    for row in pick_commits(pq.read_table(folder / "rows.parquet").to_pylist(), args.commits):
        run_id = int(str(row["run_ids"]).split(",")[0])
        try:
            ctx = build_replay_context(
                history, repo=row["repo"], run_id=run_id, attempt=1, tagger=tagger
            )
        except Exception as exc:  # noqa: BLE001 - one bad commit must not stop the demo
            logger.warning("replay failed", extra={"sha": row["head_sha"], "error": str(exc)})
            continue
        out = predictor.predict(ctx)
        md += _md_prediction(row, out)
        dump["predictions"].append(
            {
                "repo": row["repo"],
                "head_sha": row["head_sha"],
                "y_build": row["y_build"],
                "risk_output": out.model_dump(mode="json"),
            }
        )
    md += ["## Diagnoses after a failure", ""]
    kb = default_kb()
    decisions = pq.read_table(folder / "job_decisions.parquet").to_pylist()
    for decision in pick_jobs(decisions, args.failures):
        failure = routed_failure(engine, data_dir, decision)
        if failure is None:
            continue
        d = diagnose(failure, kb=kb)
        md += _md_diagnosis(decision, d)
        dump["diagnoses"].append(
            {
                "job": decision["url"],
                "rule": decision["rule_name"],
                "routed_failure_id": str(failure.failure_id),
                "diagnosis": d.model_dump(mode="json"),
            }
        )
    engine.dispose()
    out_dir = args.out or args.model
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "build_demo.md").write_text("\n".join(md).rstrip() + "\n", encoding="utf-8")
    (out_dir / "build_demo.json").write_text(json.dumps(dump, indent=1), encoding="utf-8")
    sys.stdout.write(
        f"{len(dump['predictions'])} predictions, {len(dump['diagnoses'])} diagnoses "
        f"(knowledge base: {kb.count() if kb else 0} cases)\n{out_dir / 'build_demo.md'}\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
