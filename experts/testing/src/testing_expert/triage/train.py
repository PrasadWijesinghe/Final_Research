"""Train, validate and export the triage classifier (P4.9).

Run: `python -m testing_expert.triage.train --dataset data/datasets/testing/triage_v1 --out models/testing/triage/v1`

Protocol (all on the labelled failures; `genuine` = 0, `flaky` = 1):

1. Temporal split on the failed job's finish time: the first 70% is the development set, the last
   30% the untouched test set.
2. Model choice and floors on the development set only, by forward-chaining validation: the dev
   rows are cut into 5 time blocks and each block 2..5 is scored by a model trained on the blocks
   before it. The out-of-fold scores pick the model (PR-AUC for flaky) and the confidence floors.
3. The chosen model is refit on the whole development set and scored once on the test set.
4. Leave-one-repo-out: each large repo is held out in turn, same model and floors.
5. Baselines on the same rows: always-genuine, per-test reputation (the per-test labelling
   approach), and a hand-written rules-only verdict.
6. Ablation: drop one evidence group at a time (re-choosing the floors each time).
7. Final bundle: refit on all rows with the floors from step 2.
"""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .features import FEATURE_NAMES, GROUPS
from .model import (
    KINDS,
    MAX_FALSE_FLAKY,
    MIN_GENUINE_PRECISION,
    Kind,
    choose_floors,
    evaluate,
    fit_model,
    save_bundle,
    verdict_metrics,
)

log = logging.getLogger(__name__)

# DEFAULT: 70% development / 30% test by time; 5 forward-chaining blocks inside development.
DEV_FRAC = 0.70
BLOCKS = 5
# DEFAULT: a repo is held out in leave-one-repo-out only with at least 30 failures, 5 of each class.
LORO_MIN_ROWS = 30
LORO_MIN_PER_CLASS = 5


def repo_root() -> Path:
    """Repository root (this file is in experts/testing/src/testing_expert/triage)."""
    return Path(__file__).resolve().parents[5]


def load(folder: Path, name: str = "rows.parquet") -> pd.DataFrame:
    """Read the triage table, sorted by failure time, with `y` (1 = flaky)."""
    df = pd.read_parquet(folder / name)
    df["failed_at"] = pd.to_datetime(df["failed_at"], utc=True)
    df = df.sort_values(["failed_at", "job_id"], kind="stable").reset_index(drop=True)
    df["y"] = (df["label"] == "flaky").astype(int)
    return df


def split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(development, test) by time."""
    cut = int(len(df) * DEV_FRAC)
    return df.iloc[:cut], df.iloc[cut:]


def forward_oof(dev: pd.DataFrame, kind: Kind, cols: list[str]) -> tuple[pd.DataFrame, np.ndarray]:
    """Out-of-fold calibrated scores for blocks 2..BLOCKS of `dev` (earlier blocks train)."""
    edges = [k * len(dev) // BLOCKS for k in range(BLOCKS + 1)]
    parts, scores = [], []
    for k in range(1, BLOCKS):
        train, blk = dev.iloc[: edges[k]], dev.iloc[edges[k] : edges[k + 1]]
        m = fit_model(kind, train, train["y"].to_numpy(), cols)
        parts.append(blk)
        scores.append(m.predict_flaky(blk))
    return pd.concat(parts), np.concatenate(scores)


def protocol(
    df: pd.DataFrame,
    kind: Kind,
    cols: list[str],
    max_false_flaky: float = MAX_FALSE_FLAKY,
) -> dict[str, Any]:
    """Steps 1-3 for one model kind and feature set; returns scores, floors and test metrics."""
    dev, test = split(df)
    oof_rows, oof_p = forward_oof(dev, kind, cols)
    y_oof = oof_rows["y"].to_numpy()
    ff, gf = choose_floors(y_oof, oof_p, max_false_flaky)
    model = fit_model(kind, dev, dev["y"].to_numpy(), cols)
    model.flaky_floor, model.genuine_floor = ff, gf
    p_test = model.predict_flaky(test)
    return {
        "kind": kind,
        "flaky_floor": ff,
        "genuine_floor": gf,
        "dev_oof": evaluate(y_oof, oof_p, ff, gf),
        "test": evaluate(test["y"].to_numpy(), p_test, ff, gf),
        "model": model,
        "p_test": p_test,
        "oof_y": y_oof,
        "oof_p": oof_p,
    }


def loro(df: pd.DataFrame, kind: Kind, cols: list[str], ff: float, gf: float) -> dict[str, Any]:
    """Leave-one-repo-out: pooled and per-repo metrics with the given floors."""
    counts = df.groupby("repo")["y"].agg(["size", "sum"])
    repos = sorted(
        r
        for r, c in counts.iterrows()
        if c["size"] >= LORO_MIN_ROWS
        and c["sum"] >= LORO_MIN_PER_CLASS
        and c["size"] - c["sum"] >= LORO_MIN_PER_CLASS
    )
    ys, ps, per_repo = [], [], {}
    for repo in repos:
        held = df[df["repo"] == repo]
        train = df[df["repo"] != repo]
        m = fit_model(kind, train, train["y"].to_numpy(), cols)
        p = m.predict_flaky(held)
        ys.append(held["y"].to_numpy())
        ps.append(p)
        per_repo[repo] = evaluate(held["y"].to_numpy(), p, ff, gf)
    y_all, p_all = np.concatenate(ys), np.concatenate(ps)
    macro = {
        k: float(np.nanmean([m[k] for m in per_repo.values()]))
        for k in (
            "coverage",
            "balanced_accuracy",
            "macro_f1",
            "flaky_f1",
            "false_flaky_rate",
            "pr_auc_flaky",
            "roc_auc",
        )
    }
    return {
        "repos": repos,
        "pooled": evaluate(y_all, p_all, ff, gf),
        "per_repo": per_repo,
        "macro_mean": macro,
    }


# --- baselines -------------------------------------------------------------------------------


def _verdicts_always_genuine(df: pd.DataFrame) -> np.ndarray:
    return np.full(len(df), "genuine", dtype=object)


def _verdicts_per_test_reputation(df: pd.DataFrame) -> np.ndarray:
    """The per-test labelling approach: a test that ever flipped on a commit is called flaky."""
    flaky = df["th_max_mixed_shas"].fillna(0) > 0
    return np.where(flaky, "flaky", "genuine").astype(object)


def _verdicts_rules(df: pd.DataFrame) -> np.ndarray:
    """Hand-written rules over the same evidence (no learning); see `docs` in the report."""
    out = []
    for r in df.itertuples():
        touches = (
            (r.ov_test_file_changed == 1)
            or (r.ov_stack_files_changed > 0)
            or (r.ov_frame_in_hunk == 1)
        )
        if r.ss_same_job_pass > 0:
            out.append("flaky")  # the same job already passed on this commit
        elif touches:
            out.append("genuine")  # the failure runs through code this commit changed
        elif r.ss_same_job_fail > 0:
            out.append("genuine")  # it keeps failing on this commit
        elif r.er_leans_flaky == 1 and r.er_leans_genuine == 0:
            out.append("flaky")  # an environment-looking error and no overlap with the diff
        elif r.th_max_mixed_shas > 0 and r.ov_files_known == 1:
            out.append("flaky")  # a known flaky test and the diff is not involved
        else:
            out.append("unclear")
    return np.array(out, dtype=object)


BASELINES = {
    "always genuine": _verdicts_always_genuine,
    "per-test reputation (flaky if the test ever flipped)": _verdicts_per_test_reputation,
    "rules only (hand-written, no learning)": _verdicts_rules,
}


def baselines(df: pd.DataFrame) -> dict[str, dict[str, float]]:
    """Metrics of each baseline on `df`."""
    y = df["y"].to_numpy()
    return {name: verdict_metrics(y, fn(df)) for name, fn in BASELINES.items()}


# --- the whole run ---------------------------------------------------------------------------


def run(folder: Path, out: Path | None, target: float = MAX_FALSE_FLAKY) -> dict[str, Any]:
    """All experiments; writes the bundle to `out` when given. Returns the results dict."""
    df = load(folder)
    df = df[df["is_test_failure"]].reset_index(drop=True)  # only failures the parser understood
    all_cols = list(FEATURE_NAMES)
    results: dict[str, Any] = {
        "n_rows": len(df),
        "labels": df["label"].value_counts().to_dict(),
        "target_false_flaky": target,
        "min_genuine_precision": MIN_GENUINE_PRECISION,
    }
    dev, test = split(df)
    results["dev_range"] = [str(dev["failed_at"].min()), str(dev["failed_at"].max())]
    results["test_range"] = [str(test["failed_at"].min()), str(test["failed_at"].max())]
    results["n_dev"], results["n_test"] = len(dev), len(test)
    runs = {kind: protocol(df, kind, all_cols, target) for kind in KINDS}
    # model choice on the dev out-of-fold PR-AUC (the test set plays no part)
    chosen: Kind = max(KINDS, key=lambda k: runs[k]["dev_oof"]["pr_auc_flaky"])
    results["candidates"] = {
        k: {m: runs[k][m] for m in ("flaky_floor", "genuine_floor", "dev_oof", "test")}
        for k in KINDS
    }
    results["chosen"] = chosen
    best = runs[chosen]
    ff, gf = best["flaky_floor"], best["genuine_floor"]
    results["flaky_floor"], results["genuine_floor"] = ff, gf
    results["test"] = best["test"]
    results["test_baselines"] = baselines(test)
    results["all_baselines"] = baselines(df)
    results["loro"] = loro(df, chosen, all_cols, ff, gf)
    # floors versus target: the false-flaky / coverage trade-off on the dev out-of-fold scores
    results["floor_sweep"] = [
        {
            "max_false_flaky": t,
            **dict(
                zip(
                    ("flaky_floor", "genuine_floor"),
                    choose_floors(best["oof_y"], best["oof_p"], t),
                    strict=True,
                )
            ),
        }
        for t in (0.02, 0.05, 0.10, 0.15, 0.20)
    ]
    for row in results["floor_sweep"]:
        row["test"] = evaluate(
            test["y"].to_numpy(), best["p_test"], row["flaky_floor"], row["genuine_floor"]
        )
    # ablation: drop one evidence group at a time, floors re-chosen on dev
    results["ablation"] = {}
    for group, names in GROUPS.items():
        cols = [c for c in all_cols if c not in names]
        r = protocol(df, chosen, cols, target)
        results["ablation"][f"without {group}"] = {
            k: r[k] for k in ("flaky_floor", "genuine_floor", "dev_oof", "test")
        }
    only = {}
    for group, names in GROUPS.items():
        r = protocol(df, chosen, list(names), target)
        only[f"only {group}"] = {
            k: r[k] for k in ("flaky_floor", "genuine_floor", "dev_oof", "test")
        }
    results["only_group"] = only
    # label confidence: train on high-confidence labels only, test on the same (high) test rows
    high = df[df["label_confidence"] != "low"].reset_index(drop=True)
    r = protocol(high, chosen, all_cols, target)
    results["high_confidence_only"] = {
        k: r[k] for k in ("flaky_floor", "genuine_floor", "dev_oof", "test")
    }
    results["high_confidence_only"]["n_rows"] = len(high)
    # what the short router excerpt costs: same model, test rows parsed from the last 20k chars
    try:
        ex = load(folder, "rows_excerpt.parquet")
        ex = ex[ex["job_id"].isin(test["job_id"])].set_index("job_id").loc[test["job_id"]]
        p_ex = best["model"].predict_flaky(ex)
        results["test_excerpt_only"] = evaluate(ex["y"].to_numpy(), p_ex, ff, gf)
        results["test_excerpt_parsed_share"] = float(ex["is_test_failure"].mean())
    except (FileNotFoundError, KeyError):
        pass
    # final model on all rows
    final = fit_model(chosen, df, df["y"].to_numpy(), all_cols)
    final.flaky_floor, final.genuine_floor = ff, gf
    results["bundle"] = None
    if out is not None:
        meta = {
            "built_at": datetime.now(UTC).isoformat(),
            "git_commit": _git_commit(),
            "n_train": len(df),
            "labels": results["labels"],
            "target_false_flaky": target,
            "min_genuine_precision": MIN_GENUINE_PRECISION,
            "dataset": folder.name,
            "groups": {g: list(n) for g, n in GROUPS.items()},
        }
        save_bundle(final, out, meta, card(results))
        results["bundle"] = str(out)
    return results


def card(r: dict[str, Any]) -> str:
    """Short model card for the bundle folder."""
    t = r["test"]
    return (
        f"# Test expert triage model v1\n\n"
        f"Classifier: {r['chosen']} on {len(FEATURE_NAMES)} evidence features (stack vs diff, "
        f"error type, test history, same-SHA evidence at failure time, pre-run flaky risk), "
        f"trained on {r['n_rows']} labelled failures ({r['labels']}).\n\n"
        f"Verdict: flaky when P(flaky) >= {r['flaky_floor']:.3f}; genuine when P(genuine) >= "
        f"{r['genuine_floor']:.3f}; otherwise unclear. `suggest_rerun` only for flaky.\n\n"
        f"Temporal test ({r['n_test']} failures): coverage {t['coverage']:.2f}, false-flaky rate "
        f"{t['false_flaky_rate']:.3f}, flaky F1 {t['flaky_f1']:.2f}, PR-AUC {t['pr_auc_flaky']:.2f}.\n\n"
        "Labels are rule labels (human kappa round pending): the numbers measure agreement with the "
        "rules, not with the truth.\n"
    )


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root(),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _clean(o: Any) -> Any:
    if isinstance(o, dict):
        return {
            str(k): _clean(v)
            for k, v in o.items()
            if k not in ("model", "p_test", "oof_y", "oof_p")
        }
    if isinstance(o, list | tuple):
        return [_clean(v) for v in o]
    if isinstance(o, np.floating | float):
        return None if np.isnan(o) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    return o


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    ap = argparse.ArgumentParser(prog="testing_expert.triage.train", description=__doc__)
    ap.add_argument("--dataset", type=Path, default=repo_root() / "data/datasets/testing/triage_v1")
    ap.add_argument(
        "--out", type=Path, default=None, help="bundle folder, e.g. models/testing/triage/v1"
    )
    ap.add_argument("--results", type=Path, default=None, help="write results JSON here")
    ap.add_argument("--target", type=float, default=MAX_FALSE_FLAKY)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    res = _clean(run(args.dataset, args.out, args.target))
    if args.results:
        args.results.parent.mkdir(parents=True, exist_ok=True)
        args.results.write_text(json.dumps(res, indent=1))
    print(
        json.dumps(
            {k: res[k] for k in ("chosen", "flaky_floor", "genuine_floor", "test")}, indent=1
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
