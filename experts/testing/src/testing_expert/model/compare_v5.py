"""Compare model v5 (v4 recipe + `ht_` per-test history) with model v4, on identical splits.

    uv run python -m testing_expert.model.compare_v5 --export

Dataset v4 has the same rows and labels as v3; the only change is the `ht_` feature group. So
"v4" here is the v4 recipe on the columns without `ht_` (that is exactly model v4's feature set)
and "v5" is the same recipe on all columns. Recipe = `compare_v4`: two separate monotone
LightGBM boosters, recency-weighted (30 day half-life) beta calibration fitted on out-of-fold
scores plus the first 60% of the calibration slice, thresholds from the last 40% (validation).

Configs: `v4` (no ht_, the ablation), `v5` (all), `v5_job` (only job-level ht_), `v5_touched`
(only the ht_touched_* group). Also: paired bootstrap, leave-one-repo-out, streak subset.
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
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score

from .compare import bootstrap_diff
from .compare_v4 import (
    CAL_FIT_SHARE,
    CHOSEN_HALF_LIFE,
    STREAK_MIN,
    _age_days,
    _auc,
    evaluate_event,
    oof_scores,
)
from .data import (
    apply_variant,
    eligible_repos,
    feature_columns,
    load_dataset,
    repo_root,
    temporal_split,
)
from .metrics import (
    apply_binary_calibrator,
    combine_any,
    fit_binary_calibrator,
    recency_weights,
)
from .models import (
    FLAKY_MONOTONE,
    GENUINE_MONOTONE,
    SEPARATE_KIND,
    fit_model,
    monotone_vector,
    write_features_json,
)
from .report import _f, _table

log = logging.getLogger(__name__)
CONFIGS = ("v4", "v5", "v5_job", "v5_touched")


def config_columns(cols: list[str], name: str) -> list[str]:
    """Feature columns of one config (see the module docstring)."""
    base = [c for c in cols if not c.startswith("ht_")]
    ht = [c for c in cols if c.startswith("ht_")]
    if name == "v4":
        return base
    if name == "v5":
        return [*base, *ht]
    if name == "v5_job":
        return [*base, *(c for c in ht if not c.startswith("ht_touched_"))]
    if name == "v5_touched":
        return [*base, *(c for c in ht if c.startswith("ht_touched_"))]
    raise ValueError(name)


def fit_recipe(
    train: pd.DataFrame, cal_fit: pd.DataFrame, cols: list[str]
) -> tuple[Any, dict[str, dict[str, Any]]]:
    """Fit the two boosters on `train` and their recency-weighted beta calibrators."""
    m = fit_model(SEPARATE_KIND, train, cols)
    raw_fit = m.predict_proba(cal_fit)
    oof_rows, oof = oof_scores(SEPARATE_KIND, train, cols)
    pool = pd.concat([oof_rows, cal_fit])
    age = _age_days(pool["created_at"], cal_fit["created_at"].max())
    y_pool = pool["y"].to_numpy()
    cals = {}
    for cls, col in (("gen", 1), ("flaky", 2)):
        cals[cls] = fit_binary_calibrator(
            (y_pool == col).astype(int),
            np.concatenate([oof[:, col], raw_fit[:, col]]),
            sample_weight=recency_weights(age, CHOSEN_HALF_LIFE),
        )
    return m, cals


def _scores(m: Any, cals: dict[str, dict[str, Any]], d: pd.DataFrame) -> dict[str, np.ndarray]:
    raw = m.predict_proba(d)
    g = apply_binary_calibrator(cals["gen"], raw[:, 1])
    f = apply_binary_calibrator(cals["flaky"], raw[:, 2])
    return {"any": combine_any(g, f), "gen": g, "flaky": f}


def _loro_task(args: tuple[str, str, Path]) -> dict[str, Any]:
    """Leave one repo out: raw V4-recipe scores of the v4 and v5 column sets on the held-out repo."""
    repo, dataset, root = args
    df, _m = load_dataset(dataset, root)
    cols = feature_columns(df)
    vdf = apply_variant(df, "all").reset_index(drop=True)
    held, rest = vdf[vdf["repo"] == repo], vdf[vdf["repo"] != repo]
    y = held["y"].to_numpy()
    yf = (y > 0).astype(int)
    out: dict[str, Any] = {"repo": repo}
    for cfg in ("v4", "v5", "v5_job"):
        p = fit_model(SEPARATE_KIND, rest, config_columns(cols, cfg)).predict_proba(held)
        s = combine_any(p[:, 1], p[:, 2])
        out[cfg] = {
            "any_pr": _auc(average_precision_score, yf, s),
            "any_roc": _auc(roc_auc_score, yf, s),
            "gen_pr": _auc(average_precision_score, (y == 1).astype(int), p[:, 1]),
            "flaky_pr": _auc(average_precision_score, (y == 2).astype(int), p[:, 2]),
        }
    return out


def run(root: Path, dataset: str = "v4", loro: bool = True, workers: int = 5) -> dict[str, Any]:
    """Everything the report needs, as plain numbers (plus fitted objects under `_export`)."""
    df, manifest = load_dataset(dataset, root)
    cols = feature_columns(df)
    vdf = apply_variant(df, "all").reset_index(drop=True)
    train, cal, test = temporal_split(vdf)
    h = int(len(cal) * CAL_FIT_SHARE)
    cal_fit, val = cal.iloc[:h], cal.iloc[h:]

    def streak_of(d: pd.DataFrame) -> np.ndarray:
        return d["h_consecutive_failures"].fillna(0).to_numpy() >= STREAK_MIN

    y_val, y_test = val["y"].to_numpy(), test["y"].to_numpy()
    st_test = streak_of(test)
    last_fail = test["_last_y"].to_numpy() > 0
    out: dict[str, Any] = {
        "dataset": dataset,
        "sizes": {"train": len(train), "cal_fit": len(cal_fit), "val": len(val), "test": len(test)},
        "fails": {
            "test": int((y_test > 0).sum()),
            "test_genuine": int((y_test == 1).sum()),
            "test_flaky": int((y_test == 2).sum()),
        },
        "streak": {"test_n": int(st_test.sum()), "test_fail": int((y_test[st_test] > 0).sum())},
        "n_features": {c: len(config_columns(cols, c)) for c in CONFIGS},
        "ht_coverage": {
            c: float(test[c].notna().mean()) for c in cols if c.startswith("ht_") and c in test
        },
    }
    ev: dict[str, Any] = {}
    scores: dict[str, dict[str, np.ndarray]] = {}
    exp: dict[str, Any] = {}
    imp: dict[str, Any] = {}
    for cfg in CONFIGS:
        log.info("fitting %s", cfg)
        c = config_columns(cols, cfg)
        m, cals = fit_recipe(train, cal_fit, c)
        sv, st = _scores(m, cals, val), _scores(m, cals, test)
        scores[cfg] = st
        ev[cfg] = {}
        for name, k in (("any", None), ("gen", 1), ("flaky", 2)):
            yv = (y_val > 0) if k is None else (y_val == k)
            yt = (y_test > 0) if k is None else (y_test == k)
            ev[cfg][name] = evaluate_event(
                yv.astype(int), sv[name], yt.astype(int), st[name], st_test, last_fail
            )
        exp[cfg] = (m, cals)
        if cfg == "v5":
            for nm, b in (("gen", m.est.genuine), ("flaky", m.est.flaky)):
                gain = dict(
                    zip(b.feature_name(), map(float, b.feature_importance("gain")), strict=True)
                )
                tot = sum(gain.values()) or 1.0
                imp[nm] = dict(
                    sorted(((k, v / tot) for k, v in gain.items()), key=lambda kv: -kv[1])[:8]
                )
                imp[f"{nm}_ht_share"] = sum(v for k, v in gain.items() if k.startswith("ht_")) / tot
    out["importance"] = imp
    out["eval"] = {
        k: {e: {m: v for m, v in r.items() if m != "pred"} for e, r in d.items()}
        for k, d in ev.items()
    }
    yt = (y_test > 0).astype(int)
    out["scores"] = {
        "y": yt.tolist(),
        "y_gen": (y_test == 1).astype(int).tolist(),
        "y_flaky": (y_test == 2).astype(int).tolist(),
        **{
            f"{cfg}_{e}": scores[cfg][e].tolist()
            for cfg in CONFIGS
            for e in ("any", "gen", "flaky")
        },
    }
    out["bootstrap"] = {}
    for other in ("v5", "v5_job", "v5_touched"):
        for key, ykey in (("any", "y"), ("gen", "y_gen"), ("flaky", "y_flaky")):
            y = np.asarray(out["scores"][ykey])
            pt, lo, hi = bootstrap_diff(
                y, np.asarray(scores["v4"][key]), np.asarray(scores[other][key])
            )
            out["bootstrap"][f"{other}:{key}"] = {
                "point": pt,
                "lo": lo,
                "hi": hi,
                "n_fail": int(y.sum()),
            }
    # rows that have usable ht_ history (parsed runs in 90d) vs not, v4 vs v5
    has = test["ht_parsed_runs_90d"].fillna(0).to_numpy() > 0
    out["subset_ht"] = {
        "n_with": int(has.sum()),
        "fail_with": int(yt[has].sum()),
        **{
            f"{cfg}_{part}": _auc(average_precision_score, yt[mask], scores[cfg]["any"][mask])
            for cfg in ("v4", "v5")
            for part, mask in (("with", has), ("without", ~has))
        },
    }
    if loro:
        repos = eligible_repos(vdf)
        with ProcessPoolExecutor(max_workers=workers) as ex:
            out["loro"] = list(ex.map(_loro_task, [(r, dataset, root) for r in repos]))
    out["_export"] = {
        "m": exp["v5"][0],
        "cals": exp["v5"][1],
        "cols": config_columns(cols, "v5"),
        "manifest": manifest,
        "thresholds": {k: ev["v5"][k]["threshold"] for k in ("any", "gen", "flaky")},
    }
    return out


def export(folder: Path, res: dict[str, Any]) -> None:
    """models/testing/v5/: same layout as v4 (two boosters, calibrators, thresholds, card)."""
    ex = res["_export"]
    m = ex["m"]
    folder.mkdir(parents=True, exist_ok=True)
    m.save(folder)
    joblib.dump({"kind": "separate", **ex["cals"]}, folder / "calibrator.joblib")
    cons = {
        nm: {
            k: v
            for k, v in zip(m.encoder.columns, monotone_vector(m.encoder.columns, tbl), strict=True)
            if v
        }
        for nm, tbl in (("genuine", GENUINE_MONOTONE), ("flaky", FLAKY_MONOTONE))
    }
    manifest = ex["manifest"]
    write_features_json(
        folder,
        m,
        {
            "kind": SEPARATE_KIND,
            "stages": "separate: model_genuine.txt = genuine vs rest, model_flaky.txt = flaky vs rest",
            "features": ex["cols"],
            "dataset_version": res["dataset"],
            "dataset_sha256": manifest.get("files", {}).get("rows.parquet", {}).get("sha256"),
            "calibrator": f"beta, recency-weighted (half-life {CHOSEN_HALF_LIFE} days), per class",
            "monotone_constraints": cons,
        },
    )
    e = res["eval"]["v5"]
    thr = {
        "score": "P(fail) = 1 - (1 - P(genuine_fail)) * (1 - P(flaky_fail)), each calibrated",
        "threshold": ex["thresholds"]["any"],
        "rule": e["any"]["rule"],
        "alert_rule": "alert = combined P(fail) >= threshold; per-class thresholds are kept for reasons and reports",
        "genuine_threshold": ex["thresholds"]["gen"],
        "flaky_threshold": ex["thresholds"]["flaky"],
        "test_slice": {k: e["any"][k] for k in ("precision", "recall", "flagged_share")},
        "test_slice_per_class": {
            c: {k: e[c][k] for k in ("precision", "recall", "flagged_share", "threshold")}
            for c in ("gen", "flaky")
        },
    }
    (folder / "threshold.json").write_text(json.dumps(thr, indent=2))
    a = e["any"]
    (folder / "card.md").write_text(
        f"# Test expert model {folder.name}\n\nDataset {res['dataset']}. The v4 recipe (two separate monotone LightGBM "
        f"binary models, recency-weighted beta calibration) plus the `ht_` per-test / per-file history group.\n\n"
        f"Temporal test, any-fail: PR-AUC {_f(a['pr_auc'])}, ROC-AUC {_f(a['roc_auc'])}, Brier {_f(a['brier'])}, "
        f"ECE {_f(a['ece'])}, threshold {a['threshold']:.3f} (precision {_f(a['precision'])}, recall {_f(a['recall'])}).\n\n"
        "Labels are rule labels (human kappa round pending): these numbers measure agreement with the rules, not truth.\n"
    )


def tables(r: dict[str, Any]) -> str:
    """Markdown tables of the raw results (the prose of the report is written around them)."""
    ev = r["eval"]
    L: list[str] = []
    for e, title in (
        ("any", "Any failure"),
        ("gen", "Genuine failure"),
        ("flaky", "Flaky failure"),
    ):
        rows = [
            [
                c,
                _f(ev[c][e]["pr_auc"]),
                _f(ev[c][e]["roc_auc"]),
                _f(ev[c][e]["brier"], 5),
                _f(ev[c][e]["ece"], 4),
                f"{ev[c][e]['threshold']:.3f}",
                f"{_f(ev[c][e]['precision'])} / {_f(ev[c][e]['recall'])}",
                f"{_f(ev[c][e]['streak_pr_auc'])}",
            ]
            for c in CONFIGS
        ]
        L.append(f"### {title}\n")
        L.append(
            _table(
                [
                    "config",
                    "PR-AUC",
                    "ROC-AUC",
                    "Brier",
                    "ECE",
                    "threshold",
                    "precision / recall",
                    "PR-AUC on streak rows",
                ],
                rows,
            )
            + "\n"
        )
    rows = []
    for k, v in r["bootstrap"].items():
        rows.append(
            [
                k.replace(":", ", ").replace("any", "any-fail").replace("gen", "genuine"),
                f"{v['point']:+.4f}",
                f"{v['lo']:+.4f} to {v['hi']:+.4f}",
                str(v["n_fail"]),
            ]
        )
    L.append("### Paired bootstrap, PR-AUC difference against v4 (1,000 resamples, 95% interval)\n")
    L.append(_table(["config, event", "difference", "interval", "failures"], rows) + "\n")
    s = r["subset_ht"]
    L.append(
        f"### Rows with and without readable history\n\n{s['n_with']:,} of {r['sizes']['test']:,} test rows have at least one earlier parsed run of their job in the last 90 days "
        f"({s['fail_with']} failures). Any-fail PR-AUC on them: v4 {_f(s['v4_with'])}, v5 {_f(s['v5_with'])}. On the rest: v4 {_f(s['v4_without'])}, v5 {_f(s['v5_without'])}.\n"
    )
    L.append("### Streak rows (3 or more failures in a row just before)\n")
    rows = [
        [
            c,
            _f(ev[c]["any"]["streak_mean_p"]),
            _f(ev[c]["any"]["streak_obs"]),
            _f(ev[c]["any"]["streak_pr_auc"]),
            _f(ev[c]["any"]["streak_precision"]),
            _f(ev[c]["any"]["streak_recall"]),
        ]
        for c in CONFIGS
    ]
    rows.append(
        [
            "same as last outcome",
            "-",
            _f(ev["v4"]["any"]["streak_obs"]),
            "-",
            _f(ev["v4"]["any"]["last_precision"]),
            _f(ev["v4"]["any"]["last_recall"]),
        ]
    )
    L.append(
        _table(
            [
                "config",
                "mean P(fail)",
                "observed",
                "PR-AUC",
                "precision at threshold",
                "recall at threshold",
            ],
            rows,
        )
        + "\n"
    )
    if "loro" in r:
        L.append("### Leave one repo out (PR-AUC, any-fail / genuine / flaky)\n")
        rows = [
            [
                x["repo"],
                *[
                    f"{_f(x[c]['any_pr'], 2)} / {_f(x[c]['gen_pr'], 2)} / {_f(x[c]['flaky_pr'], 2)}"
                    for c in ("v4", "v5", "v5_job")
                ],
                f"{x['v5']['any_pr'] - x['v4']['any_pr']:+.3f}",
                f"{x['v5_job']['any_pr'] - x['v4']['any_pr']:+.3f}",
            ]
            for x in r["loro"]
        ]
        wins = sum(1 for x in r["loro"] if x["v5"]["any_pr"] > x["v4"]["any_pr"])
        L.append(
            _table(
                ["held-out repo", "v4", "v5", "v5_job", "any-fail diff v5", "any-fail diff v5_job"],
                rows,
            )
            + "\n"
        )
        mean = lambda c, k: float(np.nanmean([x[c][k] for x in r["loro"]]))  # noqa: E731
        wins_j = sum(1 for x in r["loro"] if x["v5_job"]["any_pr"] > x["v4"]["any_pr"])
        L.append(
            f"Macro mean any-fail PR-AUC: v4 {mean('v4', 'any_pr'):.3f}, v5 {mean('v5', 'any_pr'):.3f}, v5_job {mean('v5_job', 'any_pr'):.3f}; "
            f"v5 better in {wins} of {len(r['loro'])} repos, v5_job in {wins_j}.\n"
        )
    L.append("### Importance (share of gain)\n")
    for nm in ("gen", "flaky"):
        top = ", ".join(f"`{k}` {v:.1%}" for k, v in r["importance"][nm].items())
        L.append(f"- {nm} model, `ht_` share {r['importance'][nm + '_ht_share']:.1%}; top: {top}.")
    return "\n".join(L)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    ap = argparse.ArgumentParser(prog="testing_expert.model.compare_v5", description=__doc__)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--no-loro", action="store_true")
    ap.add_argument("--export", action="store_true", help="write models/testing/v5/")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument(
        "--loro-only", action="store_true", help="re-run only leave-one-repo-out into --from-json"
    )
    ap.add_argument("--from-json", type=Path, default=None)
    ap.add_argument("--notes", type=Path, default=None, help="JSON with the written verdict etc.")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    root = repo_root()
    if args.from_json:
        res = json.loads(args.from_json.read_text())
        if args.loro_only:
            repos = [x["repo"] for x in res["loro"]]
            with ProcessPoolExecutor(max_workers=args.workers) as ex:
                res["loro"] = list(ex.map(_loro_task, [(r, res["dataset"], root) for r in repos]))
            args.from_json.write_text(json.dumps(res, default=float))
    else:
        res = run(root, loro=not args.no_loro, workers=args.workers)
        if args.export:
            export(root / "models" / "testing" / "v5", res)
        res.pop("_export")
        if args.json:
            args.json.write_text(json.dumps(res, default=float))
    notes = json.loads(args.notes.read_text()) if args.notes else {}
    sz, fl, sk = res["sizes"], res["fails"], res["streak"]
    head = (
        "# Test expert: model v5 vs v4\n\n"
        f"Dataset {res['dataset']} (same rows and labels as v3, plus the `ht_` group). Same time-ordered splits as v4: {sz['train']:,} train, "
        f"{sz['cal_fit'] + sz['val']:,} calibration, {sz['test']:,} test rows ({fl['test']} failures: {fl['test_genuine']} genuine, {fl['test_flaky']} flaky; "
        f"{sk['test_n']} streak rows, {sk['test_fail']} failing). Configs: **v4** = v4 recipe without `ht_` (model v4's own feature set, so also the ablation "
        f'"v5 minus ht_"; {res["n_features"]["v4"]} features), **v5** = same recipe with all `ht_` ({res["n_features"]["v5"]}), **v5_job** = only job-level `ht_`, '
        f"**v5_touched** = only the `ht_touched_*` group.\n\n"
    )
    body = "\n".join(f"## {k}\n\n{v}\n" for k, v in notes.items())
    path = args.out or root / "experts" / "testing" / "reports" / "v5_vs_v4.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(head + body + "\n## Numbers\n\n" + tables(res) + "\n")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
