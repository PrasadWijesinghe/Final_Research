"""Compare model v4 with v3: recent-window calibration, then separate genuine and flaky models.

    uv run python -m testing_expert.model.compare_v4 --export

Same dataset (v3) and the same time-ordered splits as v3: earliest 70% train, next 10%
calibration, latest 20% test. The calibration slice is cut in time order: first 60% is used to fit
calibrators, last 40% is the held-out recent *validation* part (thresholds and the calibration
pick use it; the test slice never chooses anything).

Part A, calibration. Boosters are fit on train only. To calibrate on more than the 10% slice
without in-sample optimism, expanding-window out-of-fold (OOF) scores are made over the train
period: block k is scored by a model trained on blocks < k. The calibration pool is those OOF
scores plus the first 60% of the calibration slice.
Part B, model structure. V3 (two-stage booster), V3C (V3 booster, new calibration) and V4 (two
separate binary boosters, each with its own constraints, calibration and threshold).
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
    apply_calibrator,
    combine_any,
    ece,
    fit_binary_calibrator,
    fit_calibrator,
    p_fail,
    pick_threshold,
    recency_weights,
    threshold_metrics,
)
from .models import (
    FLAKY_MONOTONE,
    GENUINE_MONOTONE,
    MONOTONE_FEATURES,
    SEPARATE_KIND,
    fit_model,
    monotone_vector,
    write_features_json,
)
from .report import _f, _table

log = logging.getLogger(__name__)
STREAK_MIN = 3  # DEFAULT: same streak definition as v3 (>= 3 failures in a row just before)
# DEFAULT: calibration-fit share of the calibration slice; the rest is held-out validation.
CAL_FIT_SHARE = 0.6
# DEFAULT: expanding-window OOF over the train period: 8 blocks, scoring blocks 2..7 (a model
# needs at least a quarter of the train period before it scores anything).
OOF_BLOCKS = 8
OOF_FIRST = 2
# DEFAULT: recency half-lives and recent windows tried for the calibration pool (days / rows).
HALF_LIVES: tuple[int | None, ...] = (None, 7, 14, 30, 60)
WINDOWS: tuple[int, ...] = (15000, 30000, 60000)
# DEFAULT: chosen calibration (see the report): recency-weighted beta, 30-day half-life.
CHOSEN_HALF_LIFE = 30
V3_KIND, V4_KIND = "lightgbm_mono", SEPARATE_KIND


def _auc(fn: Any, y: np.ndarray, s: np.ndarray) -> float:
    return float("nan") if len(y) == 0 or y.min() == y.max() else float(fn(y, s))


def _age_days(ts: pd.Series, newest: pd.Timestamp) -> np.ndarray:
    return ((newest - ts).dt.total_seconds() / 86400).to_numpy()


def _brier01(y: np.ndarray, s: np.ndarray) -> float:
    return float(((s - y) ** 2).mean())


def oof_scores(kind: str, train: pd.DataFrame, cols: list[str]) -> tuple[pd.DataFrame, np.ndarray]:
    """Expanding-window out-of-fold raw class scores over the train period (rows, (n, 3))."""
    n = len(train)
    edges = [k * n // OOF_BLOCKS for k in range(OOF_BLOCKS + 1)]
    parts, scores = [], []
    for k in range(OOF_FIRST, OOF_BLOCKS):
        m = fit_model(kind, train.iloc[: edges[k]], cols)
        blk = train.iloc[edges[k] : edges[k + 1]]
        parts.append(blk)
        scores.append(m.predict_proba(blk))
    return pd.concat(parts), np.vstack(scores)


def block_metrics(y: np.ndarray, s: np.ndarray, streak: np.ndarray) -> dict[str, float]:
    """Brier, ECE and streak-subset mean prediction vs observed rate for one 0/1 event."""
    return {
        "brier": _brier01(y, s),
        "ece": ece(y, s),
        "streak_mean_p": float(s[streak].mean()) if streak.any() else float("nan"),
        "streak_obs": float(y[streak].mean()) if streak.any() else float("nan"),
    }


def evaluate_event(
    y_val: np.ndarray,
    s_val: np.ndarray,
    y_test: np.ndarray,
    s_test: np.ndarray,
    streak: np.ndarray,
    last_fail: np.ndarray,
) -> dict[str, Any]:
    """Everything measured for one 0/1 event: ranking, calibration, threshold, streak subset."""
    thr = pick_threshold(y_val, s_val)
    t = float(thr["threshold"])
    pred = s_test >= t
    m = threshold_metrics(y_test, s_test, t)
    return {
        "pr_auc": _auc(average_precision_score, y_test, s_test),
        "roc_auc": _auc(roc_auc_score, y_test, s_test),
        "brier": _brier01(y_test, s_test),
        "brier_val": _brier01(y_val, s_val),
        "ece": ece(y_test, s_test),
        "ece_val": ece(y_val, s_val),
        "threshold": t,
        "rule": thr["rule"],
        "precision": m["precision"],
        "recall": m["recall"],
        "flagged_share": m["flagged_share"],
        "streak_mean_p": float(s_test[streak].mean()) if streak.any() else float("nan"),
        "streak_obs": float(y_test[streak].mean()) if streak.any() else float("nan"),
        "streak_pr_auc": _auc(average_precision_score, y_test[streak], s_test[streak]),
        "streak_precision": float(y_test[streak & pred].mean())
        if (streak & pred).any()
        else float("nan"),
        "streak_recall": float((pred & streak & (y_test > 0)).sum() / max(1, y_test[streak].sum())),
        "last_precision": float(y_test[streak & last_fail].mean())
        if (streak & last_fail).any()
        else float("nan"),
        "last_recall": float(
            (last_fail & streak & (y_test > 0)).sum() / max(1, y_test[streak].sum())
        ),
        "pred": pred.tolist(),
    }


def _loro_task(args: tuple[str, str, str, Path]) -> dict[str, Any]:
    """Leave one repo out: raw scores of V3 and V4 on the held-out repo."""
    repo, _, dataset, root = args
    df, _m = load_dataset(dataset, root)
    cols = feature_columns(df)
    vdf = apply_variant(df, "all").reset_index(drop=True)
    held = vdf[vdf["repo"] == repo]
    rest = vdf[vdf["repo"] != repo]
    y = held["y"].to_numpy()
    out: dict[str, Any] = {"repo": repo}
    m3 = fit_model(V3_KIND, rest, cols).predict_proba(held)
    m4 = fit_model(V4_KIND, rest, cols).predict_proba(held)
    yf = (y > 0).astype(int)
    s4 = combine_any(m4[:, 1], m4[:, 2])
    out["v3"] = {
        "any_pr": _auc(average_precision_score, yf, p_fail(m3)),
        "any_roc": _auc(roc_auc_score, yf, p_fail(m3)),
        "gen_pr": _auc(average_precision_score, (y == 1).astype(int), m3[:, 1]),
        "flaky_pr": _auc(average_precision_score, (y == 2).astype(int), m3[:, 2]),
    }
    out["v4"] = {
        "any_pr": _auc(average_precision_score, yf, s4),
        "any_roc": _auc(roc_auc_score, yf, s4),
        "gen_pr": _auc(average_precision_score, (y == 1).astype(int), m4[:, 1]),
        "flaky_pr": _auc(average_precision_score, (y == 2).astype(int), m4[:, 2]),
    }
    return out


def run(root: Path, dataset: str = "v3", loro: bool = True, workers: int = 5) -> dict[str, Any]:
    """Everything the report needs, as plain numbers."""
    df, manifest = load_dataset(dataset, root)
    cols = feature_columns(df)
    vdf = apply_variant(df, "all").reset_index(drop=True)
    train, cal, test = temporal_split(vdf)
    h = int(len(cal) * CAL_FIT_SHARE)
    cal_fit, val = cal.iloc[:h], cal.iloc[h:]
    streak_of = lambda d: d["h_consecutive_failures"].fillna(0).to_numpy() >= STREAK_MIN  # noqa: E731
    y_val, y_test = (val["y"].to_numpy()), (test["y"].to_numpy())
    st_val, st_test = streak_of(val), streak_of(test)
    last_fail = test["_last_y"].to_numpy() > 0
    out: dict[str, Any] = {
        "dataset": dataset,
        "sizes": {"train": len(train), "cal_fit": len(cal_fit), "val": len(val), "test": len(test)},
        "fails": {
            "val": int((y_val > 0).sum()),
            "test": int((y_test > 0).sum()),
            "test_genuine": int((y_test == 1).sum()),
            "test_flaky": int((y_test == 2).sum()),
            "val_genuine": int((y_val == 1).sum()),
            "val_flaky": int((y_val == 2).sum()),
        },
        "streak": {
            "val_n": int(st_val.sum()),
            "val_fail": int((y_val[st_val] > 0).sum()),
            "test_n": int(st_test.sum()),
            "test_fail": int((y_test[st_test] > 0).sum()),
        },
    }
    # ---- fit boosters on train
    m3 = fit_model(V3_KIND, train, cols)
    m4 = fit_model(V4_KIND, train, cols)
    imp = dict(
        zip(
            m3.est.fail.feature_name(),
            map(float, m3.est.fail.feature_importance("gain")),
            strict=True,
        )
    )
    tot = sum(imp.values())
    out["base_signal"] = {
        k: imp.get(k, 0.0) / tot
        for k in (
            "h_consecutive_failures",
            "h_last_failed",
            "h_fail_rate_7d",
            "h_weighted_flip_rate",
        )
    }
    out["base_signal"]["constrained"] = {
        k: int(MONOTONE_FEATURES.get(k, 0)) for k in ("h_consecutive_failures", "h_last_failed")
    }
    raw3 = {n: m3.predict_proba(d) for n, d in (("fit", cal_fit), ("val", val), ("test", test))}
    raw4 = {n: m4.predict_proba(d) for n, d in (("fit", cal_fit), ("val", val), ("test", test))}
    # ---- OOF pools
    oof_rows3, oof3 = oof_scores(V3_KIND, train, cols)
    oof_rows4, oof4 = oof_scores(V4_KIND, train, cols)
    pool = pd.concat([oof_rows3, cal_fit])
    newest = cal_fit["created_at"].max()
    age = _age_days(pool["created_at"], newest)
    yf_pool = (pool["y"].to_numpy() > 0).astype(int)
    p_pool3 = np.concatenate([p_fail(oof3), p_fail(raw3["fit"])])
    # ---- Part A: calibration candidates on the V3 booster, any-fail
    yv = (y_val > 0).astype(int)
    yt = (y_test > 0).astype(int)
    cands: dict[str, dict[str, Any]] = {}
    cands["v3 style: calibration slice only (first 60%)"] = fit_binary_calibrator(
        yf_pool[-len(cal_fit) :], p_pool3[-len(cal_fit) :]
    )
    for w in WINDOWS:
        cands[f"recent window: last {w} pool rows"] = fit_binary_calibrator(
            yf_pool[-w:], p_pool3[-w:]
        )
    for hl in HALF_LIVES:
        label = (
            "whole pool, no weights" if hl is None else f"whole pool, recency half-life {hl} days"
        )
        cands[label] = fit_binary_calibrator(
            yf_pool, p_pool3, sample_weight=recency_weights(age, hl)
        )
    part_a: dict[str, Any] = {}
    for name, c in cands.items():
        sv = apply_binary_calibrator(c, p_fail(raw3["val"]))
        st = apply_binary_calibrator(c, p_fail(raw3["test"]))
        part_a[name] = {
            "val": block_metrics(yv, sv, st_val),
            "test": evaluate_event(yv, sv, yt, st, st_test, last_fail),
        }
    out["part_a"] = part_a
    # rolling-origin check on the pre-test period (pool + validation), also for a streak-aware map
    out["rolling"] = _rolling_check(pool, p_pool3, val, p_fail(raw3["val"]))
    chosen = fit_binary_calibrator(
        yf_pool, p_pool3, sample_weight=recency_weights(age, CHOSEN_HALF_LIFE)
    )
    # ---- Part B
    q3 = {n: raw3[n][:, 1] / np.maximum(raw3[n][:, 1] + raw3[n][:, 2], 1e-12) for n in raw3}
    # V3 as shipped: 3-class beta calibration on the first 60% of the calibration slice
    v3cal = fit_calibrator(cal_fit["y"].to_numpy(), raw3["fit"], "beta")
    c3 = {n: apply_calibrator(v3cal, raw3[n]) for n in ("val", "test")}
    scores: dict[str, dict[str, dict[str, np.ndarray]]] = {"v3": {}, "v3c": {}, "v4": {}}
    for n in ("val", "test"):
        scores["v3"][n] = {"any": p_fail(c3[n]), "gen": c3[n][:, 1], "flaky": c3[n][:, 2]}
        anyc = apply_binary_calibrator(chosen, p_fail(raw3[n]))
        scores["v3c"][n] = {"any": anyc, "gen": anyc * q3[n], "flaky": anyc * (1 - q3[n])}
    # V4: each class own calibration (same recipe), pool = OOF of the separate models + cal_fit
    pool4_y = pool["y"].to_numpy()
    cals4 = {}
    for cls, col in (("gen", 1), ("flaky", 2)):
        p_pool = np.concatenate([oof4[:, col], raw4["fit"][:, col]])
        cals4[cls] = fit_binary_calibrator(
            (pool4_y == col).astype(int),
            p_pool,
            sample_weight=recency_weights(age, CHOSEN_HALF_LIFE),
        )
    # same models with the v3-style calibration (slice only), to separate structure from calibration
    cals4_old = {
        cls: fit_binary_calibrator(
            (cal_fit["y"].to_numpy() == col).astype(int), raw4["fit"][:, col]
        )
        for cls, col in (("gen", 1), ("flaky", 2))
    }
    scores["v4old"] = {}
    for n in ("val", "test"):
        g = apply_binary_calibrator(cals4["gen"], raw4[n][:, 1])
        f = apply_binary_calibrator(cals4["flaky"], raw4[n][:, 2])
        scores["v4"][n] = {"any": combine_any(g, f), "gen": g, "flaky": f}
        go = apply_binary_calibrator(cals4_old["gen"], raw4[n][:, 1])
        fo = apply_binary_calibrator(cals4_old["flaky"], raw4[n][:, 2])
        scores["v4old"][n] = {"any": combine_any(go, fo), "gen": go, "flaky": fo}
    ev: dict[str, Any] = {}
    for cfg, sc in scores.items():
        ev[cfg] = {}
        for ev_name, y_v, y_t in (
            ("any", yv, yt),
            ("gen", (y_val == 1).astype(int), (y_test == 1).astype(int)),
            ("flaky", (y_val == 2).astype(int), (y_test == 2).astype(int)),
        ):
            ev[cfg][ev_name] = evaluate_event(
                y_v, sc["val"][ev_name], y_t, sc["test"][ev_name], st_test, last_fail
            )
    # V4 alert rule "either class crosses its own threshold" vs the combined score threshold
    either = np.asarray(ev["v4"]["gen"]["pred"]) | np.asarray(ev["v4"]["flaky"]["pred"])
    out["v4_either"] = {
        "precision": float(yt[either].mean()) if either.any() else float("nan"),
        "recall": float((either & (yt > 0)).sum() / max(1, yt.sum())),
        "flagged_share": float(either.mean()),
        "streak_precision": float(yt[either & st_test].mean())
        if (either & st_test).any()
        else float("nan"),
        "streak_recall": float((either & st_test & (yt > 0)).sum() / max(1, yt[st_test].sum())),
    }
    out["eval"] = {
        k: {e: {m: v for m, v in r.items() if m != "pred"} for e, r in d.items()}
        for k, d in ev.items()
    }
    out["scores"] = {
        "y": yt.tolist(),
        "y_gen": (y_test == 1).astype(int).tolist(),
        "y_flaky": (y_test == 2).astype(int).tolist(),
        **{
            f"{cfg}_{e}": scores[cfg]["test"][e].tolist()
            for cfg in ("v3", "v3c", "v4")
            for e in ("any", "gen", "flaky")
        },
    }
    # streak buckets
    streak = test["h_consecutive_failures"].fillna(0).to_numpy()
    buckets = {}
    for lo, hi in ((0, 0), (1, 2), (3, 9), (10, 10**9)):
        m = (streak >= lo) & (streak <= hi)
        if m.any():
            buckets[f"{lo}-{hi}" if hi < 10**9 else f"{lo}+"] = {
                "n": int(m.sum()),
                "fail_rate": float(yt[m].mean()),
                "v3": float(scores["v3"]["test"]["any"][m].mean()),
                "v3c": float(scores["v3c"]["test"]["any"][m].mean()),
                "v4": float(scores["v4"]["test"]["any"][m].mean()),
                "last": float(last_fail[m].mean()),
            }
    out["buckets"] = buckets
    # reliability of any-fail for v3 and v4
    out["reliability"] = {
        cfg: _reliability(yt, scores[cfg]["test"]["any"]) for cfg in ("v3", "v3c", "v4")
    }
    # importances of the two V4 models
    for name, b in (("gen", m4.est.genuine), ("flaky", m4.est.flaky)):
        gain = dict(zip(b.feature_name(), map(float, b.feature_importance("gain")), strict=True))
        total = sum(gain.values()) or 1.0
        out[f"importance_{name}"] = dict(
            sorted(((k, v / total) for k, v in gain.items()), key=lambda kv: -kv[1])[:6]
        )
    if loro:
        repos = eligible_repos(vdf)
        with ProcessPoolExecutor(max_workers=workers) as ex:
            res = list(ex.map(_loro_task, [(r, "", dataset, root) for r in repos]))
        out["loro"] = res
    out["_export"] = {
        "m4": m4,
        "cals4": cals4,
        "chosen_v3c": chosen,
        "thresholds": {
            "any": ev["v4"]["any"]["threshold"],
            "gen": ev["v4"]["gen"]["threshold"],
            "flaky": ev["v4"]["flaky"]["threshold"],
        },
        "cols": cols,
        "manifest": manifest,
    }
    return out


def _reliability(y: np.ndarray, s: np.ndarray, bins: int = 10) -> list[dict[str, float]]:
    order = np.argsort(s, kind="stable")
    return [
        {"n": float(len(c)), "pred": float(s[c].mean()), "obs": float(y[c].mean())}
        for c in np.array_split(order, bins)
        if len(c)
    ]


def _rolling_check(
    pool: pd.DataFrame, p_pool: np.ndarray, val: pd.DataFrame, p_val: np.ndarray
) -> dict[str, dict[str, float]]:
    """Pre-test rolling-origin check: calibrate on everything earlier, score the next block.

    Also tries a streak-aware map (streak flag as an extra input to the beta map). This is the
    evidence for choosing without the test slice: the validation slice alone has only a handful
    of failures among streak rows.
    """
    full = pd.concat([pool, val])
    p = np.concatenate([p_pool, p_val])
    y = (full["y"].to_numpy() > 0).astype(int)
    streak = full["h_consecutive_failures"].fillna(0).to_numpy()
    n, blocks = len(full), 8
    edges = [k * n // blocks for k in range(blocks + 1)]
    out: dict[str, dict[str, float]] = {}
    for label, aware, hl in (
        ("plain, no weights", False, None),
        ("plain, half-life 30d", False, 30),
        ("plain, half-life 60d", False, 60),
        ("streak-aware, no weights", True, None),
        ("streak-aware, half-life 30d", True, 30),
    ):
        sq, cnt, eces, sm, so, sn = 0.0, 0, [], 0.0, 0.0, 0
        for j in range(3, blocks):
            fit_i, ev_i = slice(0, edges[j]), slice(edges[j], edges[j + 1])
            fd = full.iloc[fit_i]
            ex_fit = (streak[fit_i] >= STREAK_MIN).astype(float)[:, None] if aware else None
            ex_ev = (streak[ev_i] >= STREAK_MIN).astype(float)[:, None] if aware else None
            age = _age_days(fd["created_at"], fd["created_at"].max())
            c = fit_binary_calibrator(
                y[fit_i], p[fit_i], sample_weight=recency_weights(age, hl), extra=ex_fit
            )
            s = apply_binary_calibrator(c, p[ev_i], ex_ev)
            yy = y[ev_i]
            m = streak[ev_i] >= STREAK_MIN
            sq += float(((s - yy) ** 2).sum())
            cnt += len(yy)
            eces.append(ece(yy, s))
            sm += float(s[m].sum())
            so += float(yy[m].sum())
            sn += int(m.sum())
        out[label] = {
            "brier": sq / cnt,
            "ece": float(np.mean(eces)),
            "streak_pred": sm / max(sn, 1),
            "streak_obs": so / max(sn, 1),
        }
    return out


def export(folder: Path, res: dict[str, Any]) -> None:
    """models/testing/v4/: two boosters, per-class calibrators and thresholds, features, card."""
    ex = res["_export"]
    m4 = ex["m4"]
    folder.mkdir(parents=True, exist_ok=True)
    m4.save(folder)
    joblib.dump({"kind": "separate", **ex["cals4"]}, folder / "calibrator.joblib")
    constraints = {
        "genuine": {
            k: v
            for k, v in zip(
                m4.encoder.columns,
                monotone_vector(m4.encoder.columns, GENUINE_MONOTONE),
                strict=True,
            )
            if v
        },
        "flaky": {
            k: v
            for k, v in zip(
                m4.encoder.columns, monotone_vector(m4.encoder.columns, FLAKY_MONOTONE), strict=True
            )
            if v
        },
    }
    manifest = ex["manifest"]
    write_features_json(
        folder,
        m4,
        {
            "kind": SEPARATE_KIND,
            "stages": "separate: model_genuine.txt = genuine vs rest, model_flaky.txt = flaky vs rest",
            "features": ex["cols"],
            "dataset_version": res["dataset"],
            "dataset_sha256": manifest.get("files", {}).get("rows.parquet", {}).get("sha256"),
            "calibrator": f"beta, recency-weighted (half-life {CHOSEN_HALF_LIFE} days), per class",
            "monotone_constraints": constraints,
        },
    )
    e = res["eval"]["v4"]
    thr_file = {
        "score": "P(fail) = 1 - (1 - P(genuine_fail)) * (1 - P(flaky_fail)), each calibrated",
        "threshold": ex["thresholds"]["any"],
        "rule": e["any"]["rule"],
        "alert_rule": "alert = combined P(fail) >= threshold (the RiskOutput contract ties alert to probability); per-class thresholds are kept for reasons and reports",
        "genuine_threshold": ex["thresholds"]["gen"],
        "flaky_threshold": ex["thresholds"]["flaky"],
        "test_slice": {k: e["any"][k] for k in ("precision", "recall", "flagged_share")},
        "test_slice_per_class": {
            c: {k: e[c][k] for k in ("precision", "recall", "flagged_share", "threshold")}
            for c in ("gen", "flaky")
        },
    }
    (folder / "threshold.json").write_text(json.dumps(thr_file, indent=2))
    a = e["any"]
    (folder / "card.md").write_text(
        f"# Test expert model {folder.name}\n\nDataset {res['dataset']}. Two separate LightGBM binary models, "
        f"genuine-fail vs rest and flaky-fail vs rest, each with its own monotone constraints "
        f"({len(constraints['genuine'])} and {len(constraints['flaky'])} features, see features.json), class weights, "
        f"beta calibration (fit on out-of-fold scores of the train period plus the calibration slice, recency half-life "
        f"{CHOSEN_HALF_LIFE} days) and threshold. P(fail) = 1 - (1-pg)(1-pf).\n\n"
        f"Temporal test, any-fail: PR-AUC {_f(a['pr_auc'])}, ROC-AUC {_f(a['roc_auc'])}, Brier {_f(a['brier'])}, ECE {_f(a['ece'])}, "
        f"threshold {a['threshold']:.3f} (precision {_f(a['precision'])}, recall {_f(a['recall'])}).\n\n"
        "Labels are rule labels (human kappa round pending): these numbers measure agreement with the rules, not truth.\n"
    )


# --------------------------------------------------------------------------------------- report


def _macro(rows: list[dict[str, Any]], cfg: str, key: str) -> float:
    v = [r[cfg][key] for r in rows if not np.isnan(r[cfg][key])]
    return float(np.mean(v)) if v else float("nan")


def render(r: dict[str, Any], notes: dict[str, str]) -> str:
    """Plain-language markdown."""
    ev = r["eval"]
    L: list[str] = ["# Test expert: model v4 vs v3\n"]
    sz, fl, sk = r["sizes"], r["fails"], r["streak"]
    L.append(
        f"Dataset {r['dataset']} (unchanged), the same time-ordered splits as v3: {sz['train']:,} train rows, "
        f"{sz['cal_fit'] + sz['val']:,} calibration rows (first 60%, {sz['cal_fit']:,}, fit the calibrators; last 40%, {sz['val']:,}, is a held-out "
        f"recent validation part used for thresholds and picks), {sz['test']:,} test rows ({fl['test']} failures: {fl['test_genuine']} genuine, "
        f"{fl['test_flaky']} flaky; {sk['test_n']} streak rows, {sk['test_fail']} of them failing). "
        "Configs: **V3** = the shipped v3 (two-stage booster, 3-class beta calibration), **V3C** = the same booster with the new "
        "calibration, **V4** = two separate binary models (genuine vs rest, flaky vs rest) with the new calibration.\n"
    )
    L.append(f"## Verdict\n\n{notes['verdict']}\n")
    L.append("## 1. Calibration fix (V3 booster, any-fail)\n")
    L.append(
        "The raw booster is the same in every row; only the map from raw score to probability changes. "
        "'Pool' = out-of-fold scores over the train period (expanding window, each block scored by a model trained only on earlier blocks) "
        "plus the first 60% of the calibration slice, so calibration sees far more history than the 10% slice. "
        "Validation = the held-out last 40% of the calibration slice. The test columns are shown but did **not** choose anything.\n"
    )
    rows = []
    for name, a in r["part_a"].items():
        v, t = a["val"], a["test"]
        rows.append(
            [
                name,
                _f(v["brier"], 5),
                _f(v["ece"], 4),
                _f(t["brier"], 5),
                _f(t["ece"], 4),
                f"{_f(t['streak_mean_p'], 3)} (obs {_f(t['streak_obs'], 3)})",
                f"{t['threshold']:.3f}",
                f"{_f(t['precision'])} / {_f(t['recall'])}",
            ]
        )
    L.append(
        _table(
            [
                "calibration",
                "Brier val",
                "ECE val",
                "Brier test",
                "ECE test",
                "streak mean P(fail), test (observed)",
                "threshold (val)",
                "test prec / rec",
            ],
            rows,
        )
        + "\n"
    )
    L.append(
        f"Streak rows in validation: {sk['val_n']} with only {sk['val_fail']} failures, so validation cannot say anything about streaks. "
        "Pre-test rolling-origin check (calibrate on everything earlier, score the next block, 5 blocks over the pool plus validation):\n"
    )
    rows = [
        [k, _f(v["brier"], 5), _f(v["ece"], 4), _f(v["streak_pred"], 3), _f(v["streak_obs"], 3)]
        for k, v in r["rolling"].items()
    ]
    L.append(
        _table(
            [
                "calibration",
                "Brier",
                "mean ECE",
                "streak rows: mean P(fail)",
                "streak rows: observed",
            ],
            rows,
        )
        + "\n"
    )
    L.append(f"{notes['calib']}\n")
    bs = r["base_signal"]
    L.append(
        "### Does the booster have the base signal?\n\n"
        f"`h_consecutive_failures` and `h_last_failed` are both in the feature set and both carry a +1 monotone constraint "
        f"(share of total gain in the fail booster: {bs['h_consecutive_failures']:.2%} and {bs['h_last_failed']:.2%}; the strongest history feature, "
        f"`h_weighted_flip_rate`, has {bs['h_weighted_flip_rate']:.2%}). {notes['signal']}\n"
    )
    L.append("## 2. V4 against V3, any-fail (temporal test)\n")
    rows = []
    for cfg, lab in (
        ("v3", "V3 shipped"),
        ("v3c", "V3C (new calibration)"),
        ("v4old", "V4 with v3-style calibration"),
        ("v4", "V4"),
    ):
        a = ev[cfg]["any"]
        rows.append(
            [
                lab,
                _f(a["pr_auc"]),
                _f(a["roc_auc"]),
                _f(a["brier"], 5),
                _f(a["ece"], 4),
                f"{a['threshold']:.3f}",
                f"{_f(a['precision'])} / {_f(a['recall'])}",
            ]
        )
    L.append(
        _table(
            ["config", "PR-AUC", "ROC-AUC", "Brier", "ECE", "threshold", "prec / rec @ thr"], rows
        )
        + "\n"
    )
    e = r["v4_either"]
    L.append(
        f"V4 'either class crosses its own threshold' alert rule: precision {_f(e['precision'])}, recall {_f(e['recall'])}, "
        f"flags {e['flagged_share']:.2%} of test rows (the combined-score threshold rule above flags {ev['v4']['any']['flagged_share']:.2%}).\n"
    )
    L.append("## 3. Per class (temporal test)\n")
    rows = []
    for cls, lab in (("gen", "genuine fail"), ("flaky", "flaky fail")):
        for cfg, cl in (("v3", "V3"), ("v3c", "V3C"), ("v4", "V4")):
            a = ev[cfg][cls]
            rows.append(
                [
                    lab,
                    cl,
                    _f(a["pr_auc"]),
                    _f(a["roc_auc"]),
                    _f(a["brier"], 5),
                    _f(a["ece"], 4),
                    f"{a['threshold']:.3f}",
                    f"{_f(a['precision'])} / {_f(a['recall'])}",
                ]
            )
    L.append(
        _table(
            [
                "class",
                "config",
                "PR-AUC",
                "ROC-AUC",
                "Brier",
                "ECE",
                "threshold (val)",
                "prec / rec @ thr",
            ],
            rows,
        )
        + "\n"
    )
    L.append(
        "V3 and V3C have no per-class thresholds of their own; to compare like with like, each class score of those configs gets the same "
        "threshold rule on the same validation part. V3C splits the new any-fail probability with the booster's raw genuine share.\n"
    )
    L.append("## 4. Failure streaks (test rows whose job failed >= 3 runs in a row just before)\n")
    rows = []
    for cfg, lab in (("v3", "V3"), ("v3c", "V3C"), ("v4", "V4")):
        a = ev[cfg]["any"]
        rows.append(
            [
                lab,
                f"{_f(a['streak_mean_p'], 4)} (observed {_f(a['streak_obs'], 4)})",
                _f(a["streak_pr_auc"]),
                f"{_f(a['streak_precision'])} / {_f(a['streak_recall'])}",
                f"{_f(a['last_precision'])} / {_f(a['last_recall'])}",
            ]
        )
    L.append(
        _table(
            [
                "config",
                "mean P(fail) on streak rows",
                "PR-AUC on streak rows",
                "model prec / rec @ thr",
                "same-as-last-outcome prec / rec",
            ],
            rows,
        )
        + "\n"
    )
    rows = [
        [
            k,
            str(b["n"]),
            _f(b["fail_rate"], 3),
            _f(b["v3"], 4),
            _f(b["v3c"], 4),
            _f(b["v4"], 4),
            _f(b["last"], 3),
        ]
        for k, b in r["buckets"].items()
    ]
    L.append(
        _table(
            [
                "failures in a row before",
                "rows",
                "observed fail rate",
                "V3 mean P",
                "V3C mean P",
                "V4 mean P",
                "share whose last run failed",
            ],
            rows,
        )
        + "\n"
    )
    L.append("## 5. Reliability of any-fail P (test, 10 equal-count bins)\n")
    rv = r["reliability"]
    rows = [
        [
            str(i + 1),
            str(int(a["n"])),
            _f(a["pred"], 4),
            _f(a["obs"], 4),
            _f(b["pred"], 4),
            _f(b["obs"], 4),
            _f(c["pred"], 4),
            _f(c["obs"], 4),
        ]
        for i, (a, b, c) in enumerate(zip(rv["v3"], rv["v3c"], rv["v4"], strict=True))
    ]
    L.append(
        _table(
            ["bin", "rows", "V3 pred", "V3 obs", "V3C pred", "V3C obs", "V4 pred", "V4 obs"], rows
        )
        + "\n"
    )
    if "loro" in r:
        rs = r["loro"]
        L.append(
            f"## 6. Leave-one-repo-out ({len(rs)} repos, raw scores, no calibration, macro mean)\n"
        )
        rows = [
            [
                lab,
                _f(_macro(rs, cfg, "any_pr")),
                _f(_macro(rs, cfg, "any_roc")),
                _f(_macro(rs, cfg, "gen_pr")),
                _f(_macro(rs, cfg, "flaky_pr")),
            ]
            for cfg, lab in (("v3", "V3"), ("v4", "V4"))
        ]
        L.append(
            _table(
                ["config", "any-fail PR-AUC", "any-fail ROC-AUC", "genuine PR-AUC", "flaky PR-AUC"],
                rows,
            )
            + "\n"
        )
    L.append("## 7. Is the ranking difference real? (paired bootstrap, 95%)\n")
    L.append(notes["boot"] + "\n")
    L.append("## 8. What each V4 model leans on (share of total gain, top 6)\n")
    for cls, lab in (("gen", "genuine-vs-rest"), ("flaky", "flaky-vs-rest")):
        L.append(
            f"- {lab}: " + ", ".join(f"`{k}` {v:.1%}" for k, v in r[f"importance_{cls}"].items())
        )
    L.append(f"\n## 9. Constraints (DEFAULTs in `model/models.py`)\n\n{notes['constraints']}\n")
    L.append(f"## 10. Honest limits\n\n{notes['limits']}\n")
    return "\n".join(L)


def bootstrap_notes(r: dict[str, Any]) -> str:
    """Paired bootstrap lines for V3 vs V4 and V3C vs V4, any-fail and per class."""
    s = r["scores"]
    lines = []
    for a, b in (("v3", "v4"), ("v3c", "v4")):
        for key, ykey, lab in (
            ("any", "y", "any-fail"),
            ("gen", "y_gen", "genuine"),
            ("flaky", "y_flaky", "flaky"),
        ):
            y = np.asarray(s[ykey])
            pt, lo, hi = bootstrap_diff(y, np.asarray(s[f"{a}_{key}"]), np.asarray(s[f"{b}_{key}"]))
            lines.append(
                f"- {a.upper()} to {b.upper()}, {lab} PR-AUC: {pt:+.3f} (interval {lo:+.3f} to {hi:+.3f}, {int(y.sum())} failures)."
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    ap = argparse.ArgumentParser(prog="testing_expert.model.compare_v4", description=__doc__)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--no-loro", action="store_true")
    ap.add_argument("--export", action="store_true", help="write models/testing/v4/")
    ap.add_argument("--json", type=Path, default=None, help="keep raw results here")
    ap.add_argument("--from-json", type=Path, default=None)
    ap.add_argument("--notes", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    root = repo_root()
    if args.from_json:
        res = json.loads(args.from_json.read_text())
    else:
        res = run(root, loro=not args.no_loro, workers=args.workers)
        if args.export:
            export(root / "models" / "testing" / "v4", res)
        res.pop("_export")
        if args.json:
            args.json.write_text(json.dumps(res, default=float))
    notes = {k: "TODO" for k in ("verdict", "calib", "signal", "constraints", "limits")}
    if args.notes:
        notes.update(json.loads(args.notes.read_text()))
    notes["boot"] = bootstrap_notes(res)
    path = args.out or root / "experts" / "testing" / "reports" / "v4_vs_v3.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(res, notes))
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
