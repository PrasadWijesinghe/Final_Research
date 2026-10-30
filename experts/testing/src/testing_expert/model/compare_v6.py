"""Model v6 = v4 recipe + the "still failing" rule + a calibration-chosen threshold + an ht_ decision.

    uv run python -m testing_expert.model.compare_v6 --export models/testing_candidates/v6 \
        --json data/reports/v6_results.json

Same dataset (v4: 212,000 rows, `ht_` group included) and the same time-ordered splits as
`compare_v5` (148,425 train / 21,186 calibration / 42,389 test). Three changes, each decided
WITHOUT the test slice:

1. The rule (`model/rule.py`): final P = max(model P, rule P); rule P is the empirical fail rate
   of earlier rows by "failures in a row" bucket, learned on train + calibration only.
2. The threshold: maximum F1 on the whole calibration slice. Other operating points ("precision
   >= 0.30", "top 5% of pushes") are reported next to it. The test slice only reports.
3. The job-level `ht_` group (9 features) is in v6 only if it beats the v4 columns on the
   calibration slice AND on the out-of-fold scores of the train period (`decide_ht`).
"""

# ruff: noqa: E501  (long lines are prose templates and table cells)

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, precision_recall_curve, roc_auc_score

from . import rule as rulemod
from .compare_v4 import (
    CAL_FIT_SHARE,
    CHOSEN_HALF_LIFE,
    OOF_BLOCKS,
    OOF_FIRST,
    STREAK_MIN,
    _age_days,
    _auc,
    oof_scores,
)
from .compare_v5 import config_columns
from .data import (
    apply_variant,
    eligible_repos,
    feature_columns,
    load_dataset,
    repo_root,
    temporal_split,
)
from .metrics import (
    MIN_FLAGGED,
    apply_binary_calibrator,
    combine_any,
    ece,
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
# DEFAULT: the "precision >= 0.30" operating point (brief) and "top 5% of pushes" (brief).
PRECISION_POINT = 0.30
TOP_SHARE = 0.05
# DEFAULT: the shipped default threshold is the maximum-F1 point on the calibration slice (brief).
DEFAULT_RULE = "max_f1_on_calibration"
N_BOOT = 1000
# DEFAULT: the thresholds shown in the curve table (a spread of P(fail) values).
CURVE_THRESHOLDS = (0.03, 0.05, 0.07, 0.09, 0.11, 0.13, 0.15, 0.17, 0.20, 0.25, 0.30, 0.40, 0.50)
SUBSETS = ("all", "streak", "rule_active", "non_streak")


# --- small helpers -----------------------------------------------------------------------------


def _brier(y: np.ndarray, s: np.ndarray) -> float:
    return float(((s - y) ** 2).mean())


def prf(
    y: np.ndarray, s: np.ndarray, thr: float, mask: np.ndarray | None = None
) -> dict[str, float]:
    """Precision, recall, F1, alert rate and counts at `s >= thr` (optionally within a subset)."""
    if mask is not None:
        y, s = y[mask], s[mask]
    pred = s >= thr
    tp = float((pred & (y > 0)).sum())
    pos = float((y > 0).sum())
    p = tp / pred.sum() if pred.sum() else float("nan")
    r = tp / pos if pos else float("nan")
    f1 = 2 * p * r / (p + r) if pred.sum() and tp else 0.0
    return {
        "precision": p,
        "recall": r,
        "f1": f1,
        "alert_rate": float(pred.mean()) if len(pred) else float("nan"),
        "n": int(len(y)),
        "fails": int(pos),
        "flagged": int(pred.sum()),
    }


def operating_points(y: np.ndarray, s: np.ndarray) -> dict[str, dict[str, float]]:
    """Thresholds chosen on (y, s) only: max F1, max recall at precision >= 0.30, top 5%."""
    yf = (y > 0).astype(int)
    prec, rec, thr = precision_recall_curve(yf, s)
    prec, rec = prec[:-1], rec[:-1]
    flagged = len(s) - np.searchsorted(np.sort(s), thr, side="left")
    ok = flagged >= MIN_FLAGGED  # same support floor as `metrics.pick_threshold`
    if not ok.any():
        ok = np.ones_like(ok)
    f1 = np.where(ok, 2 * prec * rec / np.maximum(prec + rec, 1e-12), -1.0)
    i_f1 = int(np.argmax(f1))
    p_ok = ok & (prec >= PRECISION_POINT)
    i_p = int(np.flatnonzero(p_ok)[np.argmax(rec[p_ok])]) if p_ok.any() else None
    top = float(np.quantile(s, 1 - TOP_SHARE))
    pts = {"max_f1": {"threshold": float(thr[i_f1])}}
    if i_p is not None:
        pts["precision_0.30"] = {"threshold": float(thr[i_p])}
    pts["top_5pct"] = {"threshold": top}
    return pts


def paired_bootstrap(
    n: int, stats: dict[str, Callable[[np.ndarray], float]], seed: int = 0
) -> dict[str, dict[str, float]]:
    """Paired bootstrap over rows: each stat gets an index array; returns point and 95% interval."""
    rng = np.random.default_rng(seed)
    point = {k: f(np.arange(n)) for k, f in stats.items()}
    draws: dict[str, list[float]] = {k: [] for k in stats}
    for _ in range(N_BOOT):
        idx = rng.integers(0, n, n)
        for k, f in stats.items():
            val = f(idx)
            if not np.isnan(val):
                draws[k].append(val)
    out = {}
    for k, vals in draws.items():
        lo, hi = np.percentile(vals, [2.5, 97.5]) if vals else (float("nan"), float("nan"))
        out[k] = {"point": float(point[k]), "lo": float(lo), "hi": float(hi)}
    return out


def _f1_at(y: np.ndarray, s: np.ndarray, thr: float) -> Callable[[np.ndarray], float]:
    def f(idx: np.ndarray) -> float:
        return prf(y[idx], s[idx], thr)["f1"]

    return f


def _pr_diff(y: np.ndarray, a: np.ndarray, b: np.ndarray) -> Callable[[np.ndarray], float]:
    def f(idx: np.ndarray) -> float:
        yi = y[idx]
        if yi.min() == yi.max():
            return float("nan")
        return float(average_precision_score(yi, b[idx]) - average_precision_score(yi, a[idx]))

    return f


def _stat_diff(
    fn: Callable[[np.ndarray, np.ndarray], float], y: np.ndarray, a: np.ndarray, b: np.ndarray
) -> Callable[[np.ndarray], float]:
    def f(idx: np.ndarray) -> float:
        return float(fn(y[idx], b[idx]) - fn(y[idx], a[idx]))

    return f


# --- fitting -----------------------------------------------------------------------------------


def fit_recipe_oof(
    train: pd.DataFrame, cal_fit: pd.DataFrame, cols: list[str]
) -> tuple[Any, dict[str, dict[str, Any]], pd.DataFrame, np.ndarray]:
    """As `compare_v5.fit_recipe`, but also returns the out-of-fold rows and raw scores."""
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
    return m, cals, oof_rows, oof


def score(m: Any, cals: dict[str, dict[str, Any]], d: pd.DataFrame) -> dict[str, np.ndarray]:
    """Calibrated model-only scores: any-fail, genuine, flaky."""
    return score_raw(cals, m.predict_proba(d))


def score_raw(cals: dict[str, dict[str, Any]], raw: np.ndarray) -> dict[str, np.ndarray]:
    """Same, from raw class scores (n, 3)."""
    g = apply_binary_calibrator(cals["gen"], raw[:, 1])
    f = apply_binary_calibrator(cals["flaky"], raw[:, 2])
    return {"any": combine_any(g, f), "gen": g, "flaky": f}


def with_rule(s: dict[str, np.ndarray], r: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Final scores: max(model, rule) for any-fail, genuine and flaky separately."""
    return {k: rulemod.combine(s[k], r[k]) for k in ("any", "gen", "flaky")}


def _rule_scores(rule: dict[str, Any], d: pd.DataFrame) -> dict[str, np.ndarray]:
    r = rulemod.apply_rule(rule, d)
    return {"any": r["any"], "gen": r["genuine"], "flaky": r["flaky"]}


def decide_ht(res: dict[str, Any]) -> dict[str, Any]:
    """Include the job-level ht_ group only if it wins on the calibration slice AND on the OOF scores.

    # DEFAULT: "wins" = higher any-fail PR-AUC (model only, calibrated) on the 21,186-row
    # calibration slice and on the pooled out-of-fold rows of the train period. Both must be
    # positive. The test slice is not looked at.
    """
    d = res["ht_decision"]
    d["include"] = bool(d["cal_diff"] > 0 and d["oof_diff"] > 0)
    return d


# --- leave one repo out --------------------------------------------------------------------------


def _loro_task(args: tuple[str, str, Path, dict[str, Any], list[str]]) -> dict[str, Any]:
    """Held-out repo: v4 and v6 columns, model-only and with the rule (rule fitted on the rest)."""
    repo, dataset, root, cals, v6_cols = args
    df, _m = load_dataset(dataset, root)
    all_cols = feature_columns(df)
    vdf = apply_variant(df, "all").reset_index(drop=True)
    held, rest = vdf[vdf["repo"] == repo], vdf[vdf["repo"] != repo]
    yf = (held["y"].to_numpy() > 0).astype(int)
    rl = _rule_scores(rulemod.fit_rule(rest), held)
    out: dict[str, Any] = {"repo": repo, "n": len(held), "fails": int(yf.sum())}
    for name, cols in (("v4", config_columns(all_cols, "v4")), ("v6", v6_cols)):
        # DEFAULT: held-out raw scores are mapped to probabilities with the main run's calibrators
        # (same recipe, so the raw scale is comparable) so that the rule's P can be compared to them.
        s = score_raw(cals, fit_model(SEPARATE_KIND, rest, cols).predict_proba(held))
        fin = with_rule(s, rl)
        out[name] = {
            "model": _auc(average_precision_score, yf, s["any"]),
            "rule": _auc(average_precision_score, yf, fin["any"]),
            "roc_rule": _auc(roc_auc_score, yf, fin["any"]),
        }
    return out


# --- the run -----------------------------------------------------------------------------------


def run(
    root: Path,
    dataset: str = "v4",
    loro: bool = True,
    workers: int = 5,
    force_ht: bool | None = None,
) -> dict[str, Any]:
    """Everything the report needs, as plain numbers (fitted objects under `_export`)."""
    df, manifest = load_dataset(dataset, root)
    all_cols = feature_columns(df)
    vdf = apply_variant(df, "all").reset_index(drop=True)
    train, cal, test = temporal_split(vdf)
    h = int(len(cal) * CAL_FIT_SHARE)
    cal_fit = cal.iloc[:h]
    y_cal, y_test = cal["y"].to_numpy(), test["y"].to_numpy()
    yf_cal, yf_test = (y_cal > 0).astype(int), (y_test > 0).astype(int)

    rule = rulemod.fit_rule(pd.concat([train, cal]))
    r_cal, r_test = _rule_scores(rule, cal), _rule_scores(rule, test)
    ra_test = rulemod.apply_rule(rule, test)
    masks = {
        "all": np.ones(len(test), bool),
        "streak": test["h_consecutive_failures"].fillna(0).to_numpy() >= STREAK_MIN,
        "rule_active": ra_test["active"],
        "non_streak": ~ra_test["active"],
    }
    out: dict[str, Any] = {
        "dataset": dataset,
        "sizes": {"train": len(train), "cal_fit": len(cal_fit), "cal": len(cal), "test": len(test)},
        "fails": {
            "cal": int(yf_cal.sum()),
            "test": int(yf_test.sum()),
            "test_genuine": int((y_test == 1).sum()),
            "test_flaky": int((y_test == 2).sum()),
        },
        "rule": rule,
        "subset_sizes": {
            k: {"n": int(m.sum()), "fails": int(yf_test[m].sum())} for k, m in masks.items()
        },
    }
    # rule table with the test slice's observed rates (for the report, not for any choice)
    idx_t = rulemod.bucket_index(ra_test["count"])
    out["rule_test_obs"] = [
        {
            "n": int((idx_t == i).sum()),
            "obs": float(yf_test[idx_t == i].mean()) if (idx_t == i).any() else None,
        }
        for i in range(len(rule["buckets"]))
    ]
    idx_tr = rulemod.bucket_index(rulemod.previous_failures(train)[0])
    idx_ca = rulemod.bucket_index(rulemod.previous_failures(cal)[0])
    out["rule_train_cal_obs"] = [
        {
            "train_n": int((idx_tr == i).sum()),
            "train_obs": float((train["y"].to_numpy()[idx_tr == i] > 0).mean())
            if (idx_tr == i).any()
            else None,
            "cal_n": int((idx_ca == i).sum()),
            "cal_obs": float(yf_cal[idx_ca == i].mean()) if (idx_ca == i).any() else None,
        }
        for i in range(len(rule["buckets"]))
    ]

    # --- fit the two column sets -------------------------------------------------------------
    cfgs = {"v4": config_columns(all_cols, "v4"), "v6job": config_columns(all_cols, "v5_job")}
    fits: dict[str, Any] = {}
    sc_cal: dict[str, dict[str, np.ndarray]] = {}
    sc_test: dict[str, dict[str, np.ndarray]] = {}
    sc_oof: dict[str, np.ndarray] = {}
    oof_rows: pd.DataFrame | None = None
    for name, cols in cfgs.items():
        log.info("fitting %s (%d features)", name, len(cols))
        m, cals, oof_rows, oof = fit_recipe_oof(train, cal_fit, cols)
        fits[name] = (m, cals)
        sc_cal[name], sc_test[name] = score(m, cals, cal), score(m, cals, test)
        sc_oof[name] = score_raw(cals, oof)["any"]
    assert oof_rows is not None
    y_oof = (oof_rows["y"].to_numpy() > 0).astype(int)

    # --- decision on the ht_ group (no test slice) ------------------------------------------------
    blocks = np.array_split(np.arange(len(oof_rows)), OOF_BLOCKS - OOF_FIRST)
    per_block = [
        {
            "v4": _auc(average_precision_score, y_oof[b], sc_oof["v4"][b]),
            "v6job": _auc(average_precision_score, y_oof[b], sc_oof["v6job"][b]),
            "fails": int(y_oof[b].sum()),
        }
        for b in blocks
    ]
    boot_cal = paired_bootstrap(
        len(cal), {"d": _pr_diff(yf_cal, sc_cal["v4"]["any"], sc_cal["v6job"]["any"])}
    )["d"]
    oof_pr = {k: _auc(average_precision_score, y_oof, sc_oof[k]) for k in cfgs}
    cal_pr = {k: _auc(average_precision_score, yf_cal, sc_cal[k]["any"]) for k in cfgs}
    out["ht_decision"] = {
        "cal_pr": cal_pr,
        "cal_diff": cal_pr["v6job"] - cal_pr["v4"],
        "cal_ci": [boot_cal["lo"], boot_cal["hi"]],
        "cal_brier": {k: _brier(yf_cal, sc_cal[k]["any"]) for k in cfgs},
        "cal_ece": {k: ece(yf_cal, sc_cal[k]["any"]) for k in cfgs},
        "oof_pr": oof_pr,
        "oof_diff": oof_pr["v6job"] - oof_pr["v4"],
        "oof_rows": len(oof_rows),
        "oof_fails": int(y_oof.sum()),
        "per_block": per_block,
        "blocks_won": int(sum(1 for b in per_block if b["v6job"] > b["v4"])),
    }
    dec = decide_ht(out)
    if force_ht is not None:
        dec["include"], dec["forced"] = force_ht, True
    chosen = "v6job" if dec["include"] else "v4"
    out["chosen_columns"] = chosen
    log.info("ht decision: include=%s", dec["include"])

    # --- pipelines -------------------------------------------------------------------------------
    shipped_thr = (
        float(json.loads((root / "models/testing/v4/threshold.json").read_text())["threshold"])
        if (root / "models/testing/v4/threshold.json").exists()
        else 0.16910749202723852
    )
    pipes: dict[str, dict[str, Any]] = {}

    def add(
        key: str,
        label: str,
        cal_s: dict[str, np.ndarray],
        test_s: dict[str, np.ndarray],
        thr: float | None,
        how: str,
    ) -> None:
        ops = operating_points(y_cal, cal_s["any"])
        t = ops["max_f1"]["threshold"] if thr is None else thr
        pipes[key] = {
            "label": label,
            "cal": cal_s,
            "test": test_s,
            "threshold": t,
            "how": how,
            "ops": ops,
        }

    add(
        "v4_shipped",
        "v4 as served (threshold from validation, no rule)",
        sc_cal["v4"],
        sc_test["v4"],
        shipped_thr,
        "validation (precision/F0.5 rule of v4)",
    )
    add(
        "v4_newthr",
        "v4 model, threshold = max F1 on calibration",
        sc_cal["v4"],
        sc_test["v4"],
        None,
        DEFAULT_RULE,
    )
    f4c, f4t = with_rule(sc_cal["v4"], r_cal), with_rule(sc_test["v4"], r_test)
    add(
        "v4_rule",
        "v4 model + rule, threshold = max F1 on calibration",
        f4c,
        f4t,
        None,
        DEFAULT_RULE,
    )
    if chosen != "v4":
        add(
            "v6_norule",
            "v6 model (v4 + job-level ht_), no rule, threshold = max F1 on calibration",
            sc_cal[chosen],
            sc_test[chosen],
            None,
            DEFAULT_RULE,
        )
    f6c, f6t = with_rule(sc_cal[chosen], r_cal), with_rule(sc_test[chosen], r_test)
    add("v6", "v6 = model + rule, threshold = max F1 on calibration", f6c, f6t, None, DEFAULT_RULE)
    add(
        "rule_only",
        "rule alone (no model)",
        {"any": r_cal["any"], "gen": r_cal["gen"], "flaky": r_cal["flaky"]},
        {"any": r_test["any"], "gen": r_test["gen"], "flaky": r_test["flaky"]},
        None,
        DEFAULT_RULE,
    )

    out["pipelines"] = {}
    for key, p in pipes.items():
        s_t, t = p["test"], p["threshold"]
        ev: dict[str, Any] = {"label": p["label"], "threshold": t, "how": p["how"]}
        ev["ops"] = {
            name: {
                **op,
                "cal": prf(y_cal, p["cal"]["any"], op["threshold"]),
                "test": prf(y_test, s_t["any"], op["threshold"]),
            }
            for name, op in p["ops"].items()
        }
        for e, yk in (
            ("any", yf_test),
            ("gen", (y_test == 1).astype(int)),
            ("flaky", (y_test == 2).astype(int)),
        ):
            ev[e] = {
                "pr_auc": _auc(average_precision_score, yk, s_t[e]),
                "roc_auc": _auc(roc_auc_score, yk, s_t[e]),
                "brier": _brier(yk, s_t[e]),
                "ece": ece(yk, s_t[e]),
            }
        ev["at_threshold"] = prf(y_test, s_t["any"], t)
        ev["subsets"] = {
            k: {
                **prf(y_test, s_t["any"], t, m),
                "pr_auc": _auc(average_precision_score, yf_test[m], s_t["any"][m])
                if m.any()
                else float("nan"),
            }
            for k, m in masks.items()
        }
        out["pipelines"][key] = ev

    # curve on calibration (and test, for information) for the v6 final score
    s6c, s6t = pipes["v6"]["cal"]["any"], pipes["v6"]["test"]["any"]
    out["curve"] = [
        {"threshold": t, "cal": prf(y_cal, s6c, t), "test": prf(y_test, s6t, t)}
        for t in sorted(
            {*CURVE_THRESHOLDS, *(round(b["any"], 4) for b in rule["buckets"] if b["any"] > 0)}
        )
    ]
    # baselines on subsets: same as last labelled outcome (dataset), and raw "previous run failed"
    last = test["_last_y"].to_numpy() > 0
    out["baselines"] = {
        "same_as_last_label": {
            k: prf(y_test, last.astype(float), 0.5, m) for k, m in masks.items()
        },
        "previous_run_failed_raw": {
            k: prf(y_test, ra_test["active"].astype(float), 0.5, m) for k, m in masks.items()
        },
    }

    # --- paired bootstrap v6 vs v4 (as served) ----------------------------------------------------
    a, b = pipes["v4_shipped"], pipes["v6"]
    ta, tb = a["threshold"], b["threshold"]
    ys = {"any": yf_test, "gen": (y_test == 1).astype(int), "flaky": (y_test == 2).astype(int)}
    stats: dict[str, Callable[[np.ndarray], float]] = {}
    for e in ("any", "gen", "flaky"):
        stats[f"pr_{e}"] = _pr_diff(ys[e], a["test"][e], b["test"][e])
    stats["ece_any"] = _stat_diff(ece, yf_test, a["test"]["any"], b["test"]["any"])
    stats["brier_any"] = _stat_diff(_brier, yf_test, a["test"]["any"], b["test"]["any"])

    def f1_diff(idx: np.ndarray) -> float:
        return (
            prf(yf_test[idx], b["test"]["any"][idx], tb)["f1"]
            - prf(yf_test[idx], a["test"]["any"][idx], ta)["f1"]
        )

    stats["f1_any"] = f1_diff
    ns = masks["non_streak"]
    ya_ns, aa_ns, bb_ns = yf_test[ns], a["test"]["any"][ns], b["test"]["any"][ns]
    out["bootstrap"] = paired_bootstrap(len(test), stats)
    out["bootstrap_non_streak"] = paired_bootstrap(
        int(ns.sum()), {"pr_any": _pr_diff(ya_ns, aa_ns, bb_ns)}
    )["pr_any"]
    out["bootstrap_rule_vs_model"] = paired_bootstrap(
        len(test),
        {
            "pr_any": _pr_diff(
                yf_test,
                pipes["v6_norule"]["test"]["any"] if "v6_norule" in pipes else sc_test["v4"]["any"],
                b["test"]["any"],
            )
        },
    )["pr_any"]

    if loro:
        repos = eligible_repos(vdf)
        cals_main = fits[chosen][1]
        with ProcessPoolExecutor(max_workers=workers) as ex:
            out["loro"] = list(
                ex.map(_loro_task, [(r, dataset, root, cals_main, cfgs[chosen]) for r in repos])
            )

    m_c, cals_c = fits[chosen]
    out["_export"] = {
        "m": m_c,
        "cals": cals_c,
        "cols": cfgs[chosen],
        "manifest": manifest,
        "rule": rule,
        "threshold": pipes["v6"]["threshold"],
        "ops": out["pipelines"]["v6"]["ops"],
    }
    return out


# --- bundle ------------------------------------------------------------------------------------


def export(folder: Path, res: dict[str, Any]) -> None:
    """Bundle in the v4 layout (two boosters, calibrators, thresholds, card) plus `rule.json`."""
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
            "rule": "rule.json: final P = max(model P, still-failing rule P)",
            "ht_job_level": res["chosen_columns"] == "v6job",
        },
    )
    (folder / "rule.json").write_text(json.dumps(ex["rule"], indent=2))
    ev = res["pipelines"]["v6"]
    thr = {
        "score": "P(fail) = max(1 - (1 - P(genuine_fail)) * (1 - P(flaky_fail)), still-failing rule P)",
        "threshold": ex["threshold"],
        "rule": DEFAULT_RULE,
        "alert_rule": "alert = final P(fail) >= threshold (the RiskOutput contract ties alert to probability)",
        "operating_points": {
            k: {"threshold": v["threshold"], "cal": v["cal"], "test": v["test"]}
            for k, v in ex["ops"].items()
        },
        "test_slice": {
            k: ev["at_threshold"][k] for k in ("precision", "recall", "f1", "alert_rate")
        },
    }
    (folder / "threshold.json").write_text(json.dumps(thr, indent=2))
    a = ev["any"]
    t = ev["at_threshold"]
    (folder / "card.md").write_text(
        f"# Test expert model {folder.name}\n\nDataset {res['dataset']}. The v4 recipe (two separate monotone LightGBM binary models, "
        f'recency-weighted beta calibration) plus the "still failing" rule (`rule.json`)'
        f"{' and the job-level `ht_` features' if res['chosen_columns'] == 'v6job' else ''}.\n\n"
        f"Temporal test, any-fail: PR-AUC {_f(a['pr_auc'])}, ROC-AUC {_f(a['roc_auc'])}, Brier {_f(a['brier'])}, ECE {_f(a['ece'])}; "
        f"threshold {ex['threshold']:.4f} (max F1 on the calibration slice): precision {_f(t['precision'])}, recall {_f(t['recall'])}, "
        f"alert rate {_f(t['alert_rate'], 4)}.\n\n"
        "Labels are rule labels (human kappa round pending): these numbers measure agreement with the rules, not truth.\n"
    )


# --- report ------------------------------------------------------------------------------------


def tables(r: dict[str, Any]) -> str:
    """Markdown tables of the raw results (the prose is written around them)."""
    P = r["pipelines"]
    L: list[str] = []
    order = [
        k for k in ("v4_shipped", "v4_newthr", "v4_rule", "v6_norule", "v6", "rule_only") if k in P
    ]

    L.append("### Any failure (test slice)\n")
    rows = []
    for k in order:
        e, t = P[k]["any"], P[k]["at_threshold"]
        rows.append(
            [
                P[k]["label"],
                _f(e["pr_auc"]),
                _f(e["roc_auc"]),
                _f(e["brier"], 5),
                _f(e["ece"], 4),
                f"{P[k]['threshold']:.4f}",
                _f(t["precision"]),
                _f(t["recall"]),
                _f(t["f1"]),
                _f(t["alert_rate"], 4),
            ]
        )
    L.append(
        _table(
            [
                "pipeline",
                "PR-AUC",
                "ROC-AUC",
                "Brier",
                "ECE",
                "threshold",
                "precision",
                "recall",
                "F1",
                "alert rate",
            ],
            rows,
        )
        + "\n"
    )
    for e, title in (("gen", "Genuine failure"), ("flaky", "Flaky failure")):
        L.append(f"### {title} (test slice, probability scores)\n")
        rows = [
            [
                P[k]["label"],
                _f(P[k][e]["pr_auc"]),
                _f(P[k][e]["roc_auc"]),
                _f(P[k][e]["brier"], 5),
                _f(P[k][e]["ece"], 4),
            ]
            for k in order
            if k != "rule_only"
        ]
        L.append(_table(["pipeline", "PR-AUC", "ROC-AUC", "Brier", "ECE"], rows) + "\n")

    L.append(
        "### By kind of row (test slice; precision / recall / alert rate at each pipeline's own threshold)\n"
    )
    sz = r["subset_sizes"]
    names = {
        "all": "all rows",
        "streak": "streak rows (3+ failures in a row)",
        "rule_active": "previous run failed (rule applies)",
        "non_streak": "non-streak rows (previous run passed)",
    }
    for sub in SUBSETS:
        L.append(
            f"**{names[sub]}**: {sz[sub]['n']:,} rows, {sz[sub]['fails']} failures ({sz[sub]['fails'] / max(1, sz[sub]['n']):.1%}).\n"
        )
        rows = []
        for k in order:
            s = P[k]["subsets"][sub]
            rows.append(
                [
                    P[k]["label"],
                    _f(s["pr_auc"]),
                    f"{_f(s['precision'])} / {_f(s['recall'])}",
                    _f(s["f1"]),
                    _f(s["alert_rate"], 4),
                ]
            )
        for bk, bl in (
            ("same_as_last_label", "baseline: same as last labelled outcome"),
            ("previous_run_failed_raw", "baseline: previous run failed (raw history)"),
        ):
            s = r["baselines"][bk][sub]
            rows.append(
                [
                    bl,
                    "-",
                    f"{_f(s['precision'])} / {_f(s['recall'])}",
                    _f(s["f1"]),
                    _f(s["alert_rate"], 4),
                ]
            )
        L.append(
            _table(["pipeline", "PR-AUC", "precision / recall", "F1", "alert rate"], rows) + "\n"
        )

    L.append("### Precision / recall curve of v6 (final score), threshold by threshold\n")
    L.append(
        "Calibration columns are what the threshold choice saw; test columns are for reading only.\n"
    )
    rows = [
        [
            f"{c['threshold']:.4f}",
            _f(c["cal"]["precision"]),
            _f(c["cal"]["recall"]),
            _f(c["cal"]["f1"]),
            _f(c["cal"]["alert_rate"], 4),
            _f(c["test"]["precision"]),
            _f(c["test"]["recall"]),
            _f(c["test"]["f1"]),
            _f(c["test"]["alert_rate"], 4),
        ]
        for c in r["curve"]
    ]
    L.append(
        _table(
            [
                "threshold",
                "cal precision",
                "cal recall",
                "cal F1",
                "cal alert rate",
                "test precision",
                "test recall",
                "test F1",
                "test alert rate",
            ],
            rows,
        )
        + "\n"
    )

    L.append("### Operating points of v6 (all chosen on the calibration slice)\n")
    rows = []
    for name, lab in (
        ("max_f1", "max F1 (the default)"),
        ("precision_0.30", "precision >= 0.30, most recall"),
        ("top_5pct", "top 5% of pushes"),
    ):
        op = P["v6"]["ops"].get(name)
        if op is None:
            rows.append([lab, "not reachable on calibration", "", "", "", "", ""])
            continue
        rows.append(
            [
                lab,
                f"{op['threshold']:.4f}",
                f"{_f(op['cal']['precision'])} / {_f(op['cal']['recall'])}",
                _f(op["cal"]["alert_rate"], 4),
                f"{_f(op['test']['precision'])} / {_f(op['test']['recall'])}",
                _f(op["test"]["f1"]),
                _f(op["test"]["alert_rate"], 4),
            ]
        )
    L.append(
        _table(
            [
                "operating point",
                "threshold",
                "calibration precision / recall",
                "cal alert rate",
                "test precision / recall",
                "test F1",
                "test alert rate",
            ],
            rows,
        )
        + "\n"
    )

    L.append("### The rule (learned on train + calibration only)\n")
    rows = []
    for i, b in enumerate(r["rule"]["buckets"]):
        tc, to = r["rule_train_cal_obs"][i], r["rule_test_obs"][i]
        rows.append(
            [
                b["label"],
                f"{b['n']:,}",
                _f(b["any"]),
                _f(b["genuine"]),
                _f(b["flaky"]),
                f"{tc['train_n']:,} / {_f(tc['train_obs'])}",
                f"{tc['cal_n']:,} / {_f(tc['cal_obs'])}",
                f"{to['n']:,} / {_f(to['obs'])}",
            ]
        )
    L.append(
        _table(
            [
                "failures in a row",
                "train+cal rows",
                "rule P(any)",
                "rule P(genuine)",
                "rule P(flaky)",
                "train rows / rate",
                "cal rows / rate",
                "test rows / rate (not used)",
            ],
            rows,
        )
        + "\n"
    )

    d = r["ht_decision"]
    L.append("### Job-level ht_ decision (no test slice)\n")
    L.append(
        _table(
            ["where", "v4 columns", "v4 + job-level ht_", "difference", "note"],
            [
                [
                    "calibration slice, any-fail PR-AUC",
                    _f(d["cal_pr"]["v4"]),
                    _f(d["cal_pr"]["v6job"]),
                    f"{d['cal_diff']:+.4f}",
                    f"95% interval {d['cal_ci'][0]:+.4f} to {d['cal_ci'][1]:+.4f}, {r['fails']['cal']} failures",
                ],
                [
                    "out-of-fold train blocks, pooled PR-AUC",
                    _f(d["oof_pr"]["v4"]),
                    _f(d["oof_pr"]["v6job"]),
                    f"{d['oof_diff']:+.4f}",
                    f"{d['oof_rows']:,} rows, {d['oof_fails']} failures; ht_ better in {d['blocks_won']} of {len(d['per_block'])} blocks",
                ],
                [
                    "calibration slice, Brier",
                    _f(d["cal_brier"]["v4"], 5),
                    _f(d["cal_brier"]["v6job"], 5),
                    f"{d['cal_brier']['v6job'] - d['cal_brier']['v4']:+.5f}",
                    "",
                ],
                [
                    "calibration slice, ECE",
                    _f(d["cal_ece"]["v4"], 4),
                    _f(d["cal_ece"]["v6job"], 4),
                    f"{d['cal_ece']['v6job'] - d['cal_ece']['v4']:+.4f}",
                    "",
                ],
            ],
        )
        + "\n"
    )
    L.append(
        "Per OOF block (any-fail PR-AUC, v4 / with ht_): "
        + "; ".join(
            f"{_f(b['v4'])} / {_f(b['v6job'])} ({b['fails']} failures)" for b in d["per_block"]
        )
        + ".\n"
    )

    L.append(
        "### Paired bootstrap, v6 minus v4 as served (test slice, 1,000 resamples, 95% interval)\n"
    )
    names_b = {
        "pr_any": "any-fail PR-AUC",
        "pr_gen": "genuine PR-AUC",
        "pr_flaky": "flaky PR-AUC",
        "ece_any": "any-fail ECE (lower is better)",
        "brier_any": "any-fail Brier (lower is better)",
        "f1_any": "F1 at each one's own threshold",
    }
    rows = [
        [names_b[k], f"{v['point']:+.4f}", f"{v['lo']:+.4f} to {v['hi']:+.4f}"]
        for k, v in r["bootstrap"].items()
    ]
    bn = r["bootstrap_non_streak"]
    rows.append(
        [
            "any-fail PR-AUC on non-streak rows only",
            f"{bn['point']:+.4f}",
            f"{bn['lo']:+.4f} to {bn['hi']:+.4f}",
        ]
    )
    br = r["bootstrap_rule_vs_model"]
    rows.append(
        [
            "any-fail PR-AUC, v6 minus v6 without the rule (all rows)"
            if "v6_norule" in P
            else "any-fail PR-AUC, v6 minus v4 model alone (what the rule adds)",
            f"{br['point']:+.4f}",
            f"{br['lo']:+.4f} to {br['hi']:+.4f}",
        ]
    )
    L.append(_table(["measure", "difference", "interval"], rows) + "\n")

    if "loro" in r:
        L.append("### Leave one repo out (any-fail PR-AUC on the held-out repo)\n")
        rows = []
        for x in r["loro"]:
            rows.append(
                [
                    x["repo"],
                    f"{x['n']:,} / {x['fails']}",
                    _f(x["v4"]["model"], 3),
                    _f(x["v6"]["model"], 3),
                    _f(x["v4"]["rule"], 3),
                    _f(x["v6"]["rule"], 3),
                    f"{x['v6']['rule'] - x['v4']['model']:+.3f}",
                ]
            )
        L.append(
            _table(
                [
                    "held-out repo",
                    "rows / fails",
                    "v4 model",
                    "v6 model",
                    "v4 + rule",
                    "v6 + rule",
                    "v6 + rule minus v4",
                ],
                rows,
            )
            + "\n"
        )
        mean = lambda c, k: float(np.nanmean([x[c][k] for x in r["loro"]]))  # noqa: E731
        wins = sum(1 for x in r["loro"] if x["v6"]["rule"] > x["v4"]["model"])
        L.append(
            f"Macro mean: v4 model {mean('v4', 'model'):.3f}, v6 model {mean('v6', 'model'):.3f}, v4 + rule {mean('v4', 'rule'):.3f}, v6 + rule {mean('v6', 'rule'):.3f}; v6 + rule ahead of v4 in {wins} of {len(r['loro'])} repos.\n"
        )
    return "\n".join(L)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    ap = argparse.ArgumentParser(prog="testing_expert.model.compare_v6", description=__doc__)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--no-loro", action="store_true")
    ap.add_argument(
        "--export", type=Path, default=None, help="bundle folder, e.g. models/testing_candidates/v6"
    )
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--from-json", type=Path, default=None)
    ap.add_argument(
        "--force-ht",
        choices=("yes", "no"),
        default=None,
        help="override the ht_ decision (experiments only)",
    )
    ap.add_argument(
        "--notes", type=Path, default=None, help="JSON with the written sections of the report"
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    root = repo_root()
    if args.from_json:
        res = json.loads(args.from_json.read_text())
    else:
        res = run(
            root,
            loro=not args.no_loro,
            workers=args.workers,
            force_ht=None if args.force_ht is None else args.force_ht == "yes",
        )
        if args.export:
            export(args.export if args.export.is_absolute() else root / args.export, res)
        res.pop("_export")
        if args.json:
            args.json.write_text(json.dumps(res, default=float))
    notes = json.loads(args.notes.read_text()) if args.notes else {}
    sz, fl = res["sizes"], res["fails"]
    head = (
        "# Test expert: model v6 vs v4\n\n"
        f"Dataset {res['dataset']} (212,000 rows). The same time-ordered splits as v5: {sz['train']:,} train, {sz['cal']:,} calibration, {sz['test']:,} test rows "
        f"({fl['test']} failures: {fl['test_genuine']} genuine, {fl['test_flaky']} flaky; the calibration slice has {fl['cal']} failures). "
        '**v4** is the model in service. **v6** is the v4 recipe plus (1) the "still failing" rule, (2) a threshold chosen on the calibration slice only, '
        f"(3) the job-level `ht_` features only if they win without the test slice (here: {'included' if res['chosen_columns'] == 'v6job' else 'not included'}).\n\n"
    )
    body = "\n".join(f"## {k}\n\n{v}\n" for k, v in notes.items())
    path = args.out or root / "experts" / "testing" / "reports" / "v6_vs_v4.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(head + body + "\n## Numbers\n\n" + tables(res) + "\n")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
