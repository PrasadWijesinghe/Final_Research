"""Markdown metrics report and model card."""

# ruff: noqa: E501  (long lines are prose templates and table cells)

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from .data import CLASSES

if TYPE_CHECKING:
    from .train import VariantResult

KEYS = [
    ("pr_auc_any_fail", "PR-AUC any-fail"),
    ("pr_auc_genuine_fail", "PR-AUC genuine"),
    ("pr_auc_flaky_fail", "PR-AUC flaky"),
    ("pr_auc_pass", "PR-AUC pass"),
    ("roc_auc_macro", "ROC-AUC"),
    ("macro_f1", "macro F1"),
    ("flaky_f1", "flaky F1"),
    ("false_flaky_rate", "false-flaky"),
]


def _f(v: Any) -> str:
    return "n/a" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:.3f}"


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def _metrics_table(rows: dict[str, dict[str, float]]) -> str:
    return _table(
        ["model", *[h for _, h in KEYS]],
        [[n, *[_f(m.get(k)) for k, _ in KEYS]] for n, m in rows.items()],
    )


def _class_table(rows: dict[str, dict[str, float]]) -> str:
    out = []
    for n, m in rows.items():
        for c in CLASSES:
            out.append([n, c, _f(m[f"precision_{c}"]), _f(m[f"recall_{c}"]), _f(m[f"f1_{c}"])])
    return _table(["model", "class", "precision", "recall", "F1"], out)


def _loro_macro(res: VariantResult) -> dict[str, dict[str, float]]:
    from .train import macro_mean

    return {n: {k: macro_mean(per, k) for k, _ in KEYS} for n, per in res.loro.items() if per}


def _calibration_table(res: VariantResult) -> str:
    return _table(
        ["model", "Brier before", "Brier after", "Brier P(fail) before", "Brier P(fail) after"],
        [
            [
                n,
                _f(c["brier_before"]),
                _f(c["brier_after"]),
                _f(c["brier_fail_before"]),
                _f(c["brier_fail_after"]),
            ]
            for n, c in res.calibration.items()
        ],
    )


def _threshold_table(res: VariantResult) -> str:
    rows = []
    for n, t in res.threshold.items():
        tt = t["test"]
        rows.append(
            [
                n,
                _f(t["threshold"]),
                t["rule"],
                _f(t["precision"]),
                _f(t["recall"]),
                _f(tt["precision"]),
                _f(tt["recall"]),
                _f(tt["flagged_share"]),
            ]
        )
    return _table(
        [
            "model",
            "threshold",
            "rule",
            "val precision",
            "val recall",
            "test precision",
            "test recall",
            "test flagged",
        ],
        rows,
    )


def _verdict(results: dict[str, VariantResult], winner: str) -> str:
    t = results["all"].temporal
    best_base = max(
        ("majority", "last_outcome", "rerun_heuristic"),
        key=lambda b: np.nan_to_num(t[b]["pr_auc_any_fail"], nan=-1),
    )
    w, b = t[winner]["pr_auc_any_fail"], t[best_base]["pr_auc_any_fail"]
    word = "beats" if w > b else "does NOT beat"
    return (
        f"On the temporal test (variant all) the winner `{winner}` {word} the best baseline "
        f"`{best_base}` on any-fail PR-AUC ({_f(w)} vs {_f(b)}; failure base rate "
        f"{_f(t[winner]['fail_base_rate'])})."
    )


def _body(results: dict[str, VariantResult], winner: str, loro_detail: bool) -> str:
    parts = [_verdict(results, winner), ""]
    for v, res in results.items():
        parts += [
            f"## Variant `{v}`",
            "",
            f"Rows {res.sizes}; labels {res.label_counts}.",
            "",
            "### Temporal split (test = latest 20%)",
            "",
            _metrics_table(res.temporal),
            "",
            "Per class (argmax of probabilities; baselines use their own rule):",
            "",
            _class_table(res.temporal),
            "",
            "### Calibration (isotonic, fit on the first half of the calibration slice, scored on test)",
            "",
            _calibration_table(res),
            "",
            "### P(fail) threshold (picked on the second half of the calibration slice)",
            "",
            _threshold_table(res),
            "",
        ]
        if res.loro:
            parts += [
                "### Leave-one-repo-out (macro mean over repos)",
                "",
                _metrics_table(_loro_macro(res)),
                "",
            ]
            if loro_detail or v == "all":
                repos = sorted(next(iter(res.loro.values())))
                parts += [
                    "Per repo, any-fail PR-AUC / flaky F1:",
                    "",
                    _table(
                        ["repo", *res.loro],
                        [
                            [
                                r,
                                *[
                                    f"{_f(res.loro[n][r]['pr_auc_any_fail'])} / {_f(res.loro[n][r]['flaky_f1'])}"
                                    for n in res.loro
                                ],
                            ]
                            for r in repos
                        ],
                    ),
                    "",
                ]
    return "\n".join(parts)


def write_report(
    path: Path,
    results: dict[str, VariantResult],
    winner: str,
    manifest: dict[str, Any],
    commit: str,
) -> None:
    """data/reports/testing_model_v1.md"""
    path.parent.mkdir(parents=True, exist_ok=True)
    head = (
        f"# Test expert pre-run model {manifest.get('version', 'v1')}: metrics\n\n"
        f"Dataset `{manifest.get('version')}` sha256 `{manifest.get('sha256')}`; code commit `{commit}`.\n\n"
        "Baselines: majority = train prior; last_outcome = previous run's label in the same series; "
        "rerun_heuristic = flaky when re-run rate (30 d) >= 0.2. Models: logreg, lightgbm (class weights). "
        "Class metrics use argmax of the probabilities. PR-AUC any-fail scores P(genuine)+P(flaky).\n\n"
    )
    path.write_text(head + _body(results, winner, loro_detail=False))


def write_card(
    path: Path,
    results: dict[str, VariantResult],
    winner: str,
    manifest: dict[str, Any],
    model_file: str,
    cols: list[str],
) -> None:
    """models/testing/v1/card.md"""
    res = results["all"]
    thr = res.threshold[winner]
    text = f"""# Model card: Test expert pre-run model {manifest.get("version", "v1")}

**Model:** `{winner}` ({model_file}), 3 classes (pass, genuine_fail, flaky_fail), isotonic-calibrated per class
(`calibrator.joblib`). Features: {len(cols)} pre-run features (`features.json`).

## Data
Dataset `{manifest.get("version")}`, sha256 `{manifest.get("sha256")}`. Rows (variant all): {res.label_counts}
(train {res.sizes["train"]}, calibration {res.sizes["calibration"]}, test {res.sizes["test"]}).
Labels come from rules (same-SHA re-runs and a silent-flaky rule), not human labels yet.

## Splits
Temporal: earliest 70% train, next 10% calibration, last 20% test (cut on created_at).
Leave-one-repo-out: only repos with >= 20 non-pass rows are held out. Trained on the train slice
only (the calibrator needs unseen rows), no refit on train+calibration.

## Threshold
Rule: {thr["rule"]}. P(fail) >= {_f(thr["threshold"])} flags a job. On the test slice that gives
precision {_f(thr["test"]["precision"])}, recall {_f(thr["test"]["recall"])}.

## Metrics vs baselines (temporal test, variant all)

{_metrics_table(res.temporal)}

{_verdict(results, winner)}

Full tables (all variants, leave-one-repo-out, calibration): `data/reports/testing_model_{manifest.get("version", "v1")}.md`.

## Limitations
- Cuts: only failed test-job logs plus every attempt of the same job on the same head_sha were parsed.
  History is at job-key granularity (v2 adds per-runner-OS history), no per-test history, no code vector.
  {"v2 adds the tree-sitter test-change group (tw_*); the dataset is not frozen until the human kappa round is done." if manifest.get("version") != "v1" else "No tree-sitter group in v1."}
- Label caveats: `flaky` = pass after fail on the same SHA (reliable) or a silent-flaky rule (low confidence);
  `genuine` can include failures nobody retried. Rule labels are not validated by a human yet (kappa pending).
- Selection bias: only runs that failed or share a SHA with a failed attempt have rich labels.
- The threshold was picked on a small held-out half of the calibration slice (few failures there), so it is noisy; expect drift in production.
- Test is the last 20% in time: later repo behaviour may differ.
"""
    path.write_text(text)
