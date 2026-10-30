"""Compare dataset v2 with v1 on the same rows and splits, plus feature-group ablations (P4.7 v2).

    uv run --env-file .env python -m testing_expert.model.compare --base v1 --new v2

Configs: base (all its features), new (all), new minus `tw_*`, new minus `h_os_*`. Each is trained
with exactly the P4.7 procedure (temporal split + leave-one-repo-out, isotonic calibration,
threshold on the calibration slice). Ablations run on variant `all` only, to save time.
"""

# ruff: noqa: E501  (long lines are prose templates and table cells)

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.metrics import average_precision_score

from .data import VARIANTS, apply_variant, feature_columns, load_dataset, repo_root, temporal_split
from .metrics import apply_calibrator, p_fail
from .models import MODELS
from .report import _f, _table
from .train import macro_mean, run_variant

log = logging.getLogger(__name__)
# DEFAULT: ablations only on variant `all`; the 3 variants are compared for base vs new only.
ABLATIONS = {"minus_tw": ("tw_",), "minus_h_os": ("h_os_",)}
# DEFAULT: 300 paired bootstrap resamples of the test slice (fast, enough for a rough interval).
N_BOOT = 300


def config_columns(cols: list[str], drop_prefixes: Sequence[str]) -> list[str]:
    """Feature columns without those starting with any of the given prefixes."""
    return [c for c in cols if not c.startswith(tuple(drop_prefixes))]


def _task(args: tuple[str, str, str, tuple[str, ...], str, bool, Path]) -> dict[str, Any]:
    """Worker: one (config, variant) run; returns plain, picklable numbers."""
    config, dataset, variant, drop, _, loro, root = args
    df, _m = load_dataset(dataset, root)
    cols = config_columns(feature_columns(df), drop)
    res = run_variant(df, variant, cols, loro=loro, names=MODELS)
    out: dict[str, Any] = {
        "config": config,
        "variant": variant,
        "n_features": len(cols),
        "sizes": res.sizes,
        "temporal": res.temporal,
        "calibration": res.calibration,
        "threshold": {k: {**v, "test": v["test"]} for k, v in res.threshold.items()},
        "loro": {
            m: {r: x["pr_auc_any_fail"] for r, x in per.items()} for m, per in res.loro.items()
        },
        "loro_roc": {m: macro_mean(per, "roc_auc_any_fail") for m, per in res.loro.items()},
    }
    vdf = apply_variant(df, variant).reset_index(drop=True)
    _, _, test = temporal_split(vdf)
    out["y_test_fail"] = (test["y"].to_numpy() > 0).astype(int)
    out["scores"] = {
        m: p_fail(apply_calibrator(res.calibrators[m], f.predict_proba(test)))
        for m, f in res.fitted.items()
    }
    if "lightgbm" in res.fitted and variant == "all":
        booster = res.fitted["lightgbm"].est.booster_
        gain = booster.feature_importance(importance_type="gain")
        out["importance"] = dict(zip(booster.feature_name(), map(float, gain), strict=True))
    return out


def bootstrap_diff(y: np.ndarray, a: np.ndarray, b: np.ndarray, seed: int = 0) -> tuple[float, ...]:
    """Paired bootstrap of PR-AUC(b) - PR-AUC(a): (point, lo95, hi95)."""
    rng = np.random.default_rng(seed)
    point = float(average_precision_score(y, b) - average_precision_score(y, a))
    diffs = []
    for _ in range(N_BOOT):
        i = rng.integers(0, len(y), len(y))
        if y[i].min() == y[i].max():
            continue
        diffs.append(average_precision_score(y[i], b[i]) - average_precision_score(y[i], a[i]))
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return point, float(lo), float(hi)


def render(
    results: dict[tuple[str, str], dict[str, Any]], base: str, new: str, notes: dict[str, Any]
) -> str:
    """Plain-language markdown comparison."""
    g = results.get
    lines = [f"# Test expert: dataset {new} vs {base}\n"]
    lines.append(
        f"Same {notes['n_rows']} labelled attempts and the same time-ordered splits in both. "
        f"`{base}` has {notes['n_base']} features; `{new}` adds the tree-sitter test-change group "
        f"`tw_*` ({notes['n_tw']}) and per-runner-OS history `h_os_*` ({notes['n_hos']}) for "
        f"{notes['n_new']}. Trained with the same procedure as P4.7 v1 (class weights, isotonic "
        "calibration, threshold from the calibration slice, leave-one-repo-out). "
        "LR numbers are re-run with the current code (it now adds missing-indicators), so they can "
        "differ slightly from the original v1 report.\n"
    )
    lines.append(f"## Verdict\n\n{notes['verdict']}\n")
    for scheme, title in (
        ("temporal", "Temporal split (test = latest 20%)"),
        ("loro", "Leave-one-repo-out (macro mean over repos)"),
    ):
        lines.append(f"## {title}\n")
        rows = []
        for v in VARIANTS:
            for m in MODELS:
                row = [v, m]
                for cfg in (base, new):
                    r = g((cfg, v))
                    if r is None:
                        row += ["n/a"] * (4 if scheme == "temporal" else 2)
                        continue
                    if scheme == "temporal":
                        t = r["temporal"][m]
                        c = r["calibration"][m]
                        th = r["threshold"][m]["test"]
                        row += [
                            _f(t["pr_auc_any_fail"]),
                            _f(t["roc_auc_any_fail"]),
                            _f(c["brier_fail_after"]),
                            f"{_f(th['precision'])} / {_f(th['recall'])}",
                        ]
                    else:
                        per = r["loro"][m]
                        vals = [x for x in per.values() if not np.isnan(x)]
                        row += [
                            _f(float(np.mean(vals)) if vals else float("nan")),
                            _f(r["loro_roc"][m]),
                        ]
                rows.append(row)
        if scheme == "temporal":
            hdr = ["variant", "model"]
            for cfg in (base, new):
                hdr += [
                    f"{cfg} PR-AUC",
                    f"{cfg} ROC-AUC",
                    f"{cfg} Brier P(fail)",
                    f"{cfg} prec / rec @thr",
                ]
        else:
            hdr = [
                "variant",
                "model",
                f"{base} PR-AUC",
                f"{base} ROC-AUC",
                f"{new} PR-AUC",
                f"{new} ROC-AUC",
            ]
        lines.append(_table(hdr, rows) + "\n")
    lines.append(
        "PR-AUC and ROC-AUC are for 'any fail' (P(genuine)+P(flaky)). Brier is for calibrated P(fail), lower is better. "
        "Precision / recall are 'any fail' on the test slice at the threshold each model picked on its own calibration slice "
        "(precision 0.8 is usually unreachable, so the rule falls back to best F0.5). Leave-one-repo-out uses raw "
        "(uncalibrated) scores, so it has no Brier or threshold. Precision and recall at the threshold are noisy: only about 0.3% of test rows (roughly 110) are flagged, and the threshold is chosen on a few dozen failures, so do not read small differences there.\n"
    )
    lines.append("## Ablation (variant all, temporal test, LightGBM and LR)\n")
    arows = []
    for cfg, label in (
        (base, f"{base} (reference)"),
        (new, f"{new} (all features)"),
        (f"{new}_minus_tw", f"{new} minus tw_"),
        (f"{new}_minus_h_os", f"{new} minus h_os_"),
    ):
        r = g((cfg, "all"))
        if r is None:
            continue
        for m in MODELS:
            t = r["temporal"][m]
            per = [x for x in r["loro"][m].values() if not np.isnan(x)]
            arows.append(
                [
                    label,
                    m,
                    str(r["n_features"]),
                    _f(t["pr_auc_any_fail"]),
                    _f(t["roc_auc_any_fail"]),
                    _f(r["calibration"][m]["brier_fail_after"]),
                    _f(float(np.mean(per)) if per else float("nan")),
                ]
            )
    lines.append(
        _table(
            [
                "config",
                "model",
                "features",
                "temporal PR-AUC",
                "temporal ROC-AUC",
                "Brier P(fail)",
                "LORO PR-AUC",
            ],
            arows,
        )
        + "\n"
    )
    lines.append(
        f"## Is the temporal difference real? (LightGBM, variant all)\n\n{notes['boot']}\n"
    )
    lines.append(f"## Top LightGBM features, {new} (gain, variant all)\n\n{notes['importance']}\n")
    lines.append(
        "## Missing values (`tw_*`)\n\n"
        "`tw_*` is None when the test change is unknown (no parsable diff), and 0 when the commit changes no test. "
        "LightGBM reads None as native missing (each split learns which side missing goes to). "
        "Logistic regression fills missing values with the training median and adds a missing-indicator column "
        "(`# DEFAULT:` in `models.py`), so unknown is not mistaken for a real value. Only about 0.1% of rows are unknown, "
        "so this choice barely matters here.\n"
    )
    return "\n".join(lines)


def verdict(
    base: str, new: str, tb: float, tn: float, lo: float, hi: float, results: dict[Any, Any]
) -> str:
    """Honest one-paragraph summary from the numbers (no spin: interval decides 'helps')."""

    def mean_loro(cfg: str, m: str) -> float:
        vals = [x for x in results[(cfg, "all")]["loro"][m].values() if not np.isnan(x)]
        return float(np.mean(vals)) if vals else float("nan")

    lb, ln = mean_loro(base, "lightgbm"), mean_loro(new, "lightgbm")
    if lo > 0:
        call = f"{new} helps on the temporal test: the gain is outside the bootstrap noise."
    elif hi < 0:
        call = f"{new} is worse than {base} on the temporal test."
    else:
        call = "No clear gain: the temporal difference is inside the bootstrap noise."
    loro_call = f"Leave-one-repo-out LightGBM PR-AUC moves {lb:.3f} to {ln:.3f}" + (
        " (better)."
        if ln > lb + 0.005
        else " (worse)."
        if ln < lb - 0.005
        else " (about the same)."
    )
    return (
        f"LightGBM any-fail PR-AUC on the temporal test moves {tb:.3f} to {tn:.3f}. {call} {loro_call} "
        "See the ablation table for which group carries the change. Labels are still rule labels "
        "(human kappa round pending), so these numbers measure agreement with the rules, not truth."
    )


def importance_table(imp: dict[str, float], top: int = 15) -> str:
    """Top features by LightGBM gain, with share of total gain."""
    total = sum(imp.values()) or 1.0
    rows = [
        [n, f"{v:,.0f}", f"{100 * v / total:.1f}%"]
        for n, v in sorted(imp.items(), key=lambda kv: -kv[1])[:top]
    ]
    return _table(["feature", "gain", "share"], rows)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    ap = argparse.ArgumentParser(prog="testing_expert.model.compare", description=__doc__)
    ap.add_argument("--base", default="v1")
    ap.add_argument("--new", default="v2")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--no-loro", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    root = repo_root()
    base, new = args.base, args.new
    loro = not args.no_loro
    tasks: list[tuple[str, str, str, tuple[str, ...], str, bool, Path]] = []
    for v in VARIANTS:
        tasks.append((base, base, v, (), v, loro, root))
        tasks.append((new, new, v, (), v, loro, root))
    for name, drop in ABLATIONS.items():
        tasks.append((f"{new}_{name}", new, "all", drop, "all", loro, root))
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        out = list(ex.map(_task, tasks))
    results = {(r["config"], r["variant"]): r for r in out}
    df_new, _ = load_dataset(new, root)
    ncols = feature_columns(df_new)
    n_tw = sum(c.startswith("tw_") for c in ncols)
    n_hos = sum(c.startswith("h_os_") for c in ncols)
    y = results[(base, "all")]["y_test_fail"]
    sb, sn = (
        results[(base, "all")]["scores"]["lightgbm"],
        results[(new, "all")]["scores"]["lightgbm"],
    )
    pt, lo, hi = bootstrap_diff(y, sb, sn)
    boot = (
        f"LightGBM any-fail PR-AUC {new} minus {base} on the test slice: {pt:+.3f} "
        f"(95% paired bootstrap interval {lo:+.3f} to {hi:+.3f}, {int(y.sum())} failures in the test slice). "
        + (
            "The interval excludes 0."
            if lo > 0 or hi < 0
            else "The interval includes 0, so this is not distinguishable from noise."
        )
    )
    tb = results[(base, "all")]["temporal"]["lightgbm"]["pr_auc_any_fail"]
    tn = results[(new, "all")]["temporal"]["lightgbm"]["pr_auc_any_fail"]
    notes = {
        "n_rows": f"{int(results[(new, 'all')]['sizes']['total']):,}",
        "n_base": results[(base, "all")]["n_features"],
        "n_new": results[(new, "all")]["n_features"],
        "n_tw": n_tw,
        "n_hos": n_hos,
        "boot": boot,
        "importance": importance_table(results[(new, "all")]["importance"]),
        "verdict": verdict(base, new, tb, tn, lo, hi, results),
    }
    path = args.out or root / "experts" / "testing" / "reports" / f"{new}_vs_{base}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(results, base, new, notes))
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
