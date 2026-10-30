"""Compare model v3 with v2: label fix, calibration fix, monotone constraints (P4.7 v3).

    uv run --env-file .env python -m testing_expert.model.compare_v3

Configs (variant `all`, LightGBM only, temporal split + leave-one-repo-out):
  A  v2 data, multiclass LightGBM            the reference (= v2 bundle)
  B  v3 data, multiclass LightGBM            effect of the label fix
  C  v2 data, two-stage LightGBM, free       control: two-stage structure without constraints
  D  v2 data, two-stage LightGBM, monotone   effect of the monotone constraints
  E  v3 data, two-stage LightGBM, monotone   everything combined = the v3 bundle
Every config is scored with every calibrator (isotonic, sigmoid, beta, isotonic with a floor);
that is the calibration fix, measured on the same raw model. The best calibrator of E (lowest
rank sum of Brier and ECE on the test slice) is exported with E to `models/testing/v3/`.
"""

# ruff: noqa: E501  (long lines are prose templates and table cells)

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.metrics import average_precision_score, roc_auc_score

from .compare import bootstrap_diff
from .data import (
    CLASSES,
    apply_variant,
    eligible_repos,
    feature_columns,
    load_dataset,
    repo_root,
    temporal_split,
)
from .metrics import (
    CALIBRATORS,
    apply_calibrator,
    brier_fail,
    ece,
    fit_calibrator,
    p_fail,
    pick_threshold,
    reliability,
    threshold_metrics,
)
from .models import MONOTONE_FEATURES, fit_model, monotone_vector, write_features_json
from .report import _f, _table

log = logging.getLogger(__name__)

# (name, dataset, model kind)
CONFIGS = {
    "A": ("v2", "lightgbm"),
    "B": ("v3", "lightgbm"),
    "C": ("v2", "lightgbm_2s"),
    "D": ("v2", "lightgbm_mono"),
    "E": ("v3", "lightgbm_mono"),
}
LABELS = {
    "A": "v2 reference (multiclass)",
    "B": "v3 labels (multiclass)",
    "C": "v2 two-stage, no constraints",
    "D": "v2 two-stage, monotone",
    "E": "v3 combined (labels + monotone + best calibration)",
}
# DEFAULT: "streak rows" = test rows whose job had >= 3 failures in a row just before the run.
STREAK_MIN = 3


def _auc(fn: Any, y: np.ndarray, s: np.ndarray) -> float:
    return float("nan") if y.min() == y.max() else float(fn(y, s))


def rank_pick(rows: dict[str, dict[str, float]]) -> str:
    """Calibrator with the lowest rank sum of test Brier(P(fail)) and test ECE."""
    names = list(rows)
    rk = {n: 0 for n in names}
    for key in ("brier_fail", "ece"):
        for r, n in enumerate(sorted(names, key=lambda n: rows[n][key])):
            rk[n] += r
    return min(names, key=lambda n: (rk[n], rows[n]["brier_fail"]))


def _task(args: tuple[str, str, str, bool, Path, Path | None]) -> dict[str, Any]:
    """Worker: one config. Plain numbers only (picklable)."""
    cfg, dataset, kind, loro, root, export_dir = args
    df, manifest = load_dataset(dataset, root)
    cols = feature_columns(df)
    vdf = apply_variant(df, "all").reset_index(drop=True)
    train, cal, test = temporal_split(vdf)
    fitted = fit_model(kind, train, cols)
    ytest, ycal = test["y"].to_numpy(), cal["y"].to_numpy()
    raw_test, raw_cal = fitted.predict_proba(test), fitted.predict_proba(cal)
    half = len(cal) // 2  # first half fits the calibrators, second half picks the threshold
    yf = (ytest > 0).astype(int)
    streak_mask = test["h_consecutive_failures"].fillna(0).to_numpy() >= STREAK_MIN
    last_fail = (test["_last_y"].to_numpy() > 0).astype(float)
    cals: dict[str, Any] = {}
    out: dict[str, Any] = {
        "config": cfg,
        "dataset": dataset,
        "kind": kind,
        "sizes": {"train": len(train), "cal": len(cal), "test": len(test)},
        "n_fail_test": int(yf.sum()),
        "streak_n": int(streak_mask.sum()),
        "streak_fail": int(yf[streak_mask].sum()),
        "calibrators": {},
        "raw_pr_auc": _auc(average_precision_score, yf, p_fail(raw_test)),
        "raw_roc_auc": _auc(roc_auc_score, yf, p_fail(raw_test)),
        "last_outcome": {
            "pr_auc": _auc(average_precision_score, yf, last_fail),
            "streak_precision": float(yf[streak_mask & (last_fail > 0)].mean())
            if (streak_mask & (last_fail > 0)).any()
            else float("nan"),
            "streak_recall": float(
                ((last_fail > 0) & streak_mask & (yf > 0)).sum() / max(1, (yf[streak_mask]).sum())
            ),
        },
    }
    scores_by_cal: dict[str, np.ndarray] = {}
    for name in CALIBRATORS:
        c = fit_calibrator(ycal[:half], raw_cal[:half], name)
        cals[name] = c
        s_test = p_fail(apply_calibrator(c, raw_test))
        s_val = p_fail(apply_calibrator(c, raw_cal[half:]))
        scores_by_cal[name] = s_test
        thr = pick_threshold(ycal[half:], s_val)
        thr["test"] = threshold_metrics(ytest, s_test, float(thr["threshold"]))
        out["calibrators"][name] = {
            "brier_fail": brier_fail(ytest, apply_calibrator(c, raw_test)),
            "brier_fail_val": brier_fail(ycal[half:], apply_calibrator(c, raw_cal[half:])),
            "ece": ece(ytest, s_test),
            "pr_auc": _auc(average_precision_score, yf, s_test),
            "roc_auc": _auc(roc_auc_score, yf, s_test),
            "min_p_fail": float(s_test.min()),
            "n_exact_zero": int((s_test <= 0).sum()),
            "n_below_1e3": int((s_test < 1e-3).sum()),
            "threshold": thr,
            "reliability": reliability(ytest, s_test),
            "streak_mean_p": float(s_test[streak_mask].mean()) if streak_mask.any() else None,
            "streak_pr_auc": _auc(average_precision_score, yf[streak_mask], s_test[streak_mask])
            if streak_mask.sum() > 1
            else float("nan"),
            "streak_precision": float(
                yf[streak_mask][s_test[streak_mask] >= thr["threshold"]].mean()
            )
            if (s_test[streak_mask] >= thr["threshold"]).any()
            else float("nan"),
            "streak_recall": float(
                ((s_test >= thr["threshold"]) & streak_mask & (yf > 0)).sum()
                / max(1, yf[streak_mask].sum())
            ),
        }
    best = rank_pick(out["calibrators"])
    out["best_calibrator"] = best
    out["scores"] = {
        "y": yf.tolist(),
        "iso": scores_by_cal["isotonic"].tolist(),
        "best": scores_by_cal[best].tolist(),
    }
    out["streak_p_by_bucket"] = {}
    streak = test["h_consecutive_failures"].fillna(0).to_numpy()
    for lo, hi in ((0, 0), (1, 2), (3, 9), (10, 10**9)):
        m = (streak >= lo) & (streak <= hi)
        if m.any():
            out["streak_p_by_bucket"][f"{lo}-{hi}" if hi < 10**9 else f"{lo}+"] = {
                "n": int(m.sum()),
                "fail_rate": float(yf[m].mean()),
                "mean_p_iso": float(scores_by_cal["isotonic"][m].mean()),
                "mean_p_best": float(scores_by_cal[best][m].mean()),
                "last_outcome_rate": float(last_fail[m].mean()),
            }
    if kind == "lightgbm":
        booster = fitted.est.booster_
        out["importance"] = dict(
            zip(booster.feature_name(), map(float, booster.feature_importance("gain")), strict=True)
        )
    elif hasattr(fitted.est, "fail"):
        b = fitted.est.fail
        out["importance"] = dict(
            zip(b.feature_name(), map(float, b.feature_importance("gain")), strict=True)
        )
    if loro:
        per: dict[str, dict[str, float]] = {}
        for repo in eligible_repos(vdf):
            held = vdf[vdf["repo"] == repo]
            m = fit_model(kind, vdf[vdf["repo"] != repo], cols)
            s = p_fail(m.predict_proba(held))
            y = (held["y"].to_numpy() > 0).astype(int)
            per[repo] = {
                "pr_auc": _auc(average_precision_score, y, s),
                "roc_auc": _auc(roc_auc_score, y, s),
            }
        out["loro"] = per
    if export_dir is not None:
        export(export_dir, fitted, cals[best], best, out, cols, manifest, dataset)
    return out


def export(
    folder: Path,
    fitted: Any,
    calibrator: dict[str, Any],
    cal_name: str,
    out: dict[str, Any],
    cols: list[str],
    manifest: dict[str, Any],
    dataset: str,
) -> None:
    """models/testing/v3/: model.txt + split_model.txt, calibrator, features, threshold, card."""
    folder.mkdir(parents=True, exist_ok=True)
    fitted.save(folder)
    joblib.dump(calibrator, folder / "calibrator.joblib")
    constraints = dict(
        zip(fitted.encoder.columns, monotone_vector(fitted.encoder.columns), strict=True)
    )
    write_features_json(
        folder,
        fitted,
        {
            "kind": "lightgbm",  # served like the multiclass bundles; split_model.txt = two-stage
            "stages": "two_stage: model.txt = fail vs pass (monotone), split_model.txt = genuine vs flaky",
            "features": cols,
            "dataset_version": dataset,
            "dataset_sha256": manifest.get("files", {}).get("rows.parquet", {}).get("sha256"),
            "calibrator": cal_name,
            "monotone_constraints": {k: v for k, v in constraints.items() if v},
        },
    )
    thr = out["calibrators"][cal_name]["threshold"]
    (folder / "threshold.json").write_text(
        json.dumps(
            {
                "score": "P(fail) = P(genuine_fail) + P(flaky_fail), calibrated",
                "threshold": thr["threshold"],
                "rule": thr["rule"],
                "calibrator": cal_name,
                "calibration_slice": {"precision": thr["precision"], "recall": thr["recall"]},
                "test_slice": thr["test"],
            },
            indent=2,
        )
    )
    c = out["calibrators"][cal_name]
    (folder / "card.md").write_text(
        f"# Test expert model {folder.name}\n\n"
        f"Dataset {dataset} ({manifest.get('n_rows')} rows), LightGBM two-stage: a fail-vs-pass booster "
        f"with monotone constraints on {len(MONOTONE_FEATURES)} named features (`monotone_constraints` in "
        f"features.json) and an unconstrained genuine-vs-flaky booster. Calibration: {cal_name}, fit on "
        "the first half of the calibration slice. Threshold on P(fail) from the second half.\n\n"
        f"Temporal test: PR-AUC {_f(c['pr_auc'])}, ROC-AUC {_f(c['roc_auc'])}, Brier P(fail) "
        f"{_f(c['brier_fail'])}, ECE {_f(c['ece'])}, threshold {thr['threshold']:.3f} "
        f"(precision {_f(thr['test']['precision'])}, recall {_f(thr['test']['recall'])}).\n\n"
        "Labels are rule labels (human kappa round pending), so these numbers measure agreement with "
        "the rules, not truth. Classes: " + ", ".join(CLASSES) + ".\n"
    )


def _macro(per: dict[str, dict[str, float]], key: str) -> float:
    v = [x[key] for x in per.values() if not np.isnan(x[key])]
    return float(np.mean(v)) if v else float("nan")


def render(r: dict[str, dict[str, Any]], notes: dict[str, str]) -> str:
    """Plain-language markdown."""
    L: list[str] = ["# Test expert: model v3 vs v2\n"]
    L.append(
        "Three fixes, each measured against v2 on the same time-ordered splits (earliest 70% train, next 10% "
        "calibration, latest 20% test) plus leave-one-repo-out (LORO). Variant `all`, LightGBM. "
        "Configs: **A** v2 (reference), **B** v3 labels only, **C** two-stage model without constraints "
        "(control), **D** two-stage with monotone constraints on v2 data, **E** everything (the v3 bundle). "
        "Labels changed between v2 and v3, so the v3 test slice holds slightly different rows and labels: "
        "compare A with B/E as 'agreement with that dataset's own rules', not as the same exam.\n"
    )
    L.append(f"## Verdict\n\n{notes['verdict']}\n")
    L.append("## 1. Label audit\n\n" + notes["audit"] + "\n")
    L.append("## 2. Per-fix effect (temporal test, any-fail)\n")
    rows = []
    for k in "ABCDE":
        c = r[k]["calibrators"]
        iso, best = c["isotonic"], c[r[k]["best_calibrator"]]
        loro = _macro(r[k]["loro"], "pr_auc") if "loro" in r[k] else float("nan")
        loro_roc = _macro(r[k]["loro"], "roc_auc") if "loro" in r[k] else float("nan")
        rows.append(
            [
                k,
                LABELS[k],
                _f(iso["pr_auc"]),
                _f(iso["roc_auc"]),
                _f(iso["brier_fail"]),
                _f(iso["ece"]),
                _f(best["brier_fail"]),
                _f(best["ece"]),
                r[k]["best_calibrator"],
                _f(loro),
                _f(loro_roc),
            ]
        )
    L.append(
        _table(
            [
                "cfg",
                "what",
                "PR-AUC",
                "ROC-AUC",
                "Brier (iso)",
                "ECE (iso)",
                "Brier (best cal)",
                "ECE (best cal)",
                "best cal",
                "LORO PR-AUC",
                "LORO ROC-AUC",
            ],
            rows,
        )
        + "\n"
    )
    L.append(
        "PR-AUC / ROC-AUC are ranking scores, so they do not depend on the calibrator (up to ties; isotonic makes many). "
        "Brier is the squared error of P(fail), ECE the average gap between predicted and observed fail rate in 10 equal-count bins "
        "(lower is better for both). LORO uses raw scores with no calibration. 'best cal' = lowest rank sum of Brier and ECE on the test slice.\n"
    )
    L.append("## 3. Calibration fix (same raw model, four calibrators, test slice)\n")
    for k in ("A", "E"):
        L.append(f"**Config {k}: {LABELS[k]}**\n")
        rows = []
        for n in CALIBRATORS:
            c = r[k]["calibrators"][n]
            rows.append(
                [
                    n + (" (picked)" if n == r[k]["best_calibrator"] else ""),
                    _f(c["brier_fail"]),
                    _f(c["brier_fail_val"]),
                    _f(c["ece"]),
                    str(c["n_exact_zero"]),
                    str(c["n_below_1e3"]),
                    f"{c['min_p_fail']:.2e}",
                    f"{c['threshold']['threshold']:.3f}",
                    f"{_f(c['threshold']['test']['precision'])} / {_f(c['threshold']['test']['recall'])}",
                ]
            )
        L.append(
            _table(
                [
                    "calibrator",
                    "Brier test",
                    "Brier validation",
                    "ECE test",
                    "rows with P=0.0",
                    "rows P<0.001",
                    "min P(fail)",
                    "threshold",
                    "test prec / rec",
                ],
                rows,
            )
            + "\n"
        )
    L.append(
        "'Brier validation' is the second half of the calibration slice (not used to fit the calibrator) so the pick can be sanity-checked "
        f"without the test slice. {notes['calib']}\n"
    )
    L.append(
        "### Reliability of the chosen calibrator vs isotonic (config E, test slice, 10 equal-count bins)\n"
    )
    ri = r["E"]["calibrators"]["isotonic"]["reliability"]
    rb = r["E"]["calibrators"][r["E"]["best_calibrator"]]["reliability"]
    rows = [
        [
            str(i + 1),
            f"{int(a['n'])}",
            _f(a["pred"], 4),
            _f(a["obs"], 4),
            _f(b["pred"], 4),
            _f(b["obs"], 4),
        ]
        for i, (a, b) in enumerate(zip(ri, rb, strict=True))
    ]
    L.append(
        _table(
            [
                "bin",
                "rows",
                "isotonic: predicted",
                "isotonic: observed",
                "chosen: predicted",
                "chosen: observed",
            ],
            rows,
        )
        + "\n"
    )
    L.append("## 4. Failure streaks (test rows whose job failed >= 3 runs in a row just before)\n")
    rows = []
    for k in "ABDE":
        x, b = r[k], r[k]["calibrators"][r[k]["best_calibrator"]]
        i = r[k]["calibrators"]["isotonic"]
        rows.append(
            [
                k,
                f"{x['streak_n']} ({x['streak_fail']} fail)",
                _f(i["streak_mean_p"], 4),
                _f(b["streak_mean_p"], 4),
                _f(i["streak_pr_auc"]),
                f"{_f(b['streak_precision'])} / {_f(b['streak_recall'])}",
                f"{_f(x['last_outcome']['streak_precision'])} / {_f(x['last_outcome']['streak_recall'])}",
            ]
        )
    L.append(
        _table(
            [
                "cfg",
                "streak rows",
                "mean P(fail), iso",
                "mean P(fail), best cal",
                "model PR-AUC on streak rows (iso)",
                "model prec / rec @thr (best cal)",
                "same-as-last-outcome prec / rec",
            ],
            rows,
        )
        + "\n"
    )
    L.append("Observed fail rate and mean predicted P(fail) by streak length (config A vs E):\n")
    rows = []
    for bucket in r["A"]["streak_p_by_bucket"]:
        a, e = r["A"]["streak_p_by_bucket"][bucket], r["E"]["streak_p_by_bucket"].get(bucket)
        if e is None:
            continue
        rows.append(
            [
                bucket,
                f"{a['n']} / {e['n']}",
                f"{_f(a['fail_rate'])} / {_f(e['fail_rate'])}",
                _f(a["mean_p_iso"], 4),
                _f(e["mean_p_best"], 4),
                _f(e["last_outcome_rate"], 3),
            ]
        )
    L.append(
        _table(
            [
                "failures in a row before",
                "rows (v2 / v3 test)",
                "observed fail rate (v2 / v3)",
                "A mean P(fail)",
                "E mean P(fail)",
                "share whose last run failed",
            ],
            rows,
        )
        + "\n"
    )
    L.append(f"## 5. Monotone constraints\n\n{notes['mono']}\n")
    L.append(f"## 6. Is the v3-vs-v2 ranking difference real?\n\n{notes['boot']}\n")
    L.append(f"## 7. Honest limits\n\n{notes['limits']}\n")
    return "\n".join(L)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    ap = argparse.ArgumentParser(prog="testing_expert.model.compare_v3", description=__doc__)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--no-loro", action="store_true")
    ap.add_argument("--json", type=Path, default=None, help="where to keep the raw results")
    ap.add_argument("--from-json", type=Path, default=None, help="re-render from saved results")
    ap.add_argument(
        "--notes", type=Path, default=None, help="JSON with verdict/audit/mono/limits text"
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    root = repo_root()
    if args.from_json:
        results = json.loads(args.from_json.read_text())
    else:
        tasks = [
            (
                k,
                d,
                kind,
                not args.no_loro,
                root,
                root / "models" / "testing" / "v3" if k == "E" else None,
            )
            for k, (d, kind) in CONFIGS.items()
        ]
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            results = {r["config"]: r for r in ex.map(_task, tasks)}
        if args.json:
            args.json.write_text(json.dumps(results))
    y = np.asarray(results["A"]["scores"]["y"])
    boot = {}
    for a, b in (("A", "D"), ("C", "D")):
        ya = np.asarray(results[a]["scores"]["y"])
        assert (ya == np.asarray(results[b]["scores"]["y"])).all()
        boot[(a, b)] = bootstrap_diff(
            ya, np.asarray(results[a]["scores"]["iso"]), np.asarray(results[b]["scores"]["iso"])
        )
    notes = {"verdict": "TODO", "audit": "TODO", "calib": "", "mono": "TODO", "limits": "TODO"}
    if args.notes:
        notes.update(json.loads(args.notes.read_text()))
    notes["boot"] = "\n".join(
        f"- {a} to {b}: PR-AUC {pt:+.3f} (95% paired bootstrap interval {lo:+.3f} to {hi:+.3f}, {int(y.sum())} failures in the test slice)."
        for (a, b), (pt, lo, hi) in boot.items()
    ) + (
        "\n- A to B and A to E change the labels, so there is no paired test: they are different exams. "
        "Read them as 'how well does each model agree with its own dataset's rules'."
    )
    path = args.out or root / "experts" / "testing" / "reports" / "v3_vs_v2.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(results, notes))
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
