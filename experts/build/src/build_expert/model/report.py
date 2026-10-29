"""Turn `metrics.json` into a Markdown report people can read (and paste into slides)."""

from __future__ import annotations

from typing import Any

GLOSSARY = (
    "**How to read the numbers.** *Precision*: of the commits the model warned about, the share "
    "that really had a build failure. *Recall*: of the real build failures, the share it warned "
    "about. *F1*: one number that balances the two. *PR-AUC*: how well the scores rank failures "
    "above passes over all thresholds (the share of failures is the value a random guess gets). "
    "*ROC-AUC*: the chance a random failure gets a higher score than a random pass (0.5 = guess)."
)


def _f(value: Any, pct: bool = False) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.1%}" if pct else f"{value:.3f}"
    return str(value)


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return out + [""]


def _metric_row(name: str, m: dict[str, Any]) -> list[str]:
    return [
        name,
        _f(m.get("precision")),
        _f(m.get("recall")),
        _f(m.get("f1")),
        _f(m.get("pr_auc")),
        _f(m.get("roc_auc")),
        _f(m.get("flagged_share"), pct=True),
    ]


METRIC_HEADER = ["Method", "Precision", "Recall", "F1", "PR-AUC", "ROC-AUC", "Commits flagged"]


def _target_section(target: str, out: dict[str, Any], main: bool) -> list[str]:
    lines = [f"### Target `{target}`", ""]
    split = out.get("split", {})
    test = split.get("test", {})
    if test.get("rows"):
        share = test["positives"] / test["rows"]
        lines.append(
            f"Test part: {test['rows']} commits, {test['positives']} with a build failure "
            f"({share:.1%}, the PR-AUC of a random guess)."
        )
        lines.append("")
    rows = [
        _metric_row(f"Baseline: {b['description']}", b["test"])
        for b in out.get("baselines", {}).values()
    ]
    model = out.get("model", {})
    if "skipped" in model:
        lines += _table(METRIC_HEADER, rows)
        lines += [f"Model not trained: {model['skipped']}", ""]
        return lines
    rows.append(_metric_row("**Model** (best-F1 threshold)", model["test"]["at_f1"]))
    rows.append(
        _metric_row("**Model** (high-precision threshold, D03)", model["test"]["at_operating"])
    )
    lines += _table(METRIC_HEADER, rows)
    best_base = max((b["test"]["f1"] for b in out.get("baselines", {}).values()), default=0.0)
    f1 = model["test"]["at_f1"]["f1"]
    verdict = "higher than" if f1 > best_base else "not higher than"
    lines.append(
        f"H1 check: the model's F1 ({f1:.3f}) is {verdict} the best baseline's ({best_base:.3f})."
    )
    lines.append(
        f"The model uses {model['trees']} trees; thresholds (calibrated probability): best-F1 "
        f"{model['threshold_f1']:.3f}, high-precision {model['threshold_operating']:.3f}."
    )
    lines.append("")
    if main and out.get("lead_time", {}).get("warnings"):
        lt = out["lead_time"]
        lines += [
            "**Lead time (H2).** For the "
            f"{lt['warnings']} correct high-precision warnings, the warning comes at the commit's "
            f"cut-off, a median of {lt['median_minutes']:.0f} minutes before its CI finished "
            f"(middle half: {lt['p25_minutes']:.0f} to {lt['p75_minutes']:.0f} minutes). A "
            "pre-push prediction comes earlier still: before the commit is pushed.",
            "",
        ]
    return lines


def render_report(payload: dict[str, Any]) -> str:
    """The whole report as Markdown."""
    ds = payload.get("dataset", {})
    lines = [
        f"# Build model report: {payload.get('model', '')}",
        "",
        f"Trained {str(payload.get('trained_at', ''))[:16].replace('T', ' ')} UTC on dataset "
        f"`{ds.get('id')}`: {ds.get('commits')} commits from {ds.get('first_cutoff')} to "
        f"{ds.get('last_cutoff')}, {ds.get('labels', {}).get('y_build', 0)} with a build failure. "
        f"Code version `{str(payload.get('git_commit') or '')[:10]}`.",
        "",
        GLOSSARY,
        "",
        "## 1. Data",
        "",
    ]
    repo_rows = [
        [
            repo,
            str(r["commits"]),
            str(r["build_failures"]),
            f"{r['build_failures'] / r['commits']:.1%}" if r["commits"] else "–",
            str(r["other_failures"]),
        ]
        for repo, r in ds.get("repos", {}).items()
    ]
    lines += _table(["Repo", "Commits", "Build failures", "Share", "Other failures"], repo_rows)
    first: dict[str, Any] = next(iter(payload.get("targets", {}).values()), {})
    split_rows = [
        [part, _f(p.get("from")), _f(p.get("to")), str(p.get("rows")), str(p.get("positives"))]
        for part, p in first.get("split", {}).items()
    ]
    lines.append("Time-ordered split (oldest commits train, newest test):")
    lines.append("")
    lines += _table(["Part", "From", "To", "Commits", "Build failures"], split_rows)
    lines.append(
        f"{payload.get('dropped_by_embargo', 0)} commits were left out because their CI finished "
        "after the next part began (no peeking at the future)."
    )
    cov = ds.get("coverage", {})
    lines.append(
        "Share of commits with each history input: last build on the branch "
        f"{_f(cov.get('branch_last_build_failed'), True)}, last ESLint warning count "
        f"{_f(cov.get('last_lint_warnings'), True)}, last TypeScript error count "
        f"{_f(cov.get('last_type_errors'), True)}."
    )
    lines += ["", "## 2. Model against the baselines (H1)", ""]
    for i, (target, out) in enumerate(payload.get("targets", {}).items()):
        lines += _target_section(target, out, main=i == 0)

    if payload.get("ablation"):
        lines += ["## 3. Which feature groups matter (H3)", ""]
        lines.append(
            "The model retrained without one group at a time, on the same split. A drop in "
            "PR-AUC means the group helps."
        )
        lines.append("")
        abl_rows = []
        for name, m in payload["ablation"].items():
            if "skipped" in m:
                abl_rows.append([name, "–", "–", "–", m["skipped"]])
            else:
                abl_rows.append([name, _f(m["pr_auc"]), _f(m["roc_auc"]), _f(m["f1"]), ""])
        lines += _table(["Features", "PR-AUC", "ROC-AUC", "F1", "Note"], abl_rows)

    if payload.get("top_factors"):
        lines += ["## 4. Top risk factors (SHAP, test part)", ""]
        groups = payload.get("group_importance", {})
        lines.append(
            "Share of the model's attention by group: "
            + ", ".join(f"{g} {v:.0%}" for g, v in groups.items())
            + "."
        )
        lines.append("")
        lines += _table(
            ["#", "Feature", "Group", "Share", "Reads as"],
            [
                [str(i + 1), f"`{t['feature']}`", t["group"], f"{t['share']:.1%}", t["description"]]
                for i, t in enumerate(payload["top_factors"])
            ],
        )

    main: dict[str, Any] = next(iter(payload.get("targets", {}).values()), {})
    if main.get("per_repo_test"):
        lines += ["## 5. Per repo (test part, best-F1 threshold)", ""]
        lines += _table(
            ["Repo", "Commits", "Failures", "Precision", "Recall", "F1", "PR-AUC"],
            [
                [
                    repo,
                    str(m["n"]),
                    str(m["positives"]),
                    _f(m["precision"]),
                    _f(m["recall"]),
                    _f(m["f1"]),
                    _f(m["pr_auc"]),
                ]
                for repo, m in main["per_repo_test"].items()
            ],
        )

    loro = payload.get("leave_one_repo_out")
    if loro:
        lines += ["## 6. Leave one repo out (does it work on a repo it never saw?)", ""]
        loro_rows = []
        for repo, m in loro["repos"].items():
            if "skipped" in m:
                loro_rows.append([repo, "–", "–", "–", "–", f"skipped: {m['skipped']}"])
            else:
                loro_rows.append(
                    [repo, _f(m["precision"]), _f(m["recall"]), _f(m["f1"]), _f(m["pr_auc"]), ""]
                )
        lines += _table(["Held-out repo", "Precision", "Recall", "F1", "PR-AUC", "Note"], loro_rows)
        s = loro.get("summary", {})
        if s.get("repos_scored"):
            lines.append(
                f"Mean over {s['repos_scored']} repos: F1 {_f(s.get('mean_f1'))}, "
                f"PR-AUC {_f(s.get('mean_pr_auc'))}."
            )
            lines.append("")

    if payload.get("sub_targets"):
        lines += ["## 7. Sub-risks (test part, best-F1 threshold)", ""]
        sub_rows = []
        for target, m in payload["sub_targets"].items():
            if "skipped" in m:
                sub_rows.append([f"`{target}`", "–", "–", "–", "–", m["skipped"]])
            else:
                t = m["test"]
                sub_rows.append(
                    [
                        f"`{target}`",
                        str(t["positives"]),
                        _f(t["precision"]),
                        _f(t["recall"]),
                        _f(t["pr_auc"]),
                        "",
                    ]
                )
        lines += _table(["Target", "Failures", "Precision", "Recall", "PR-AUC", "Note"], sub_rows)

    if payload.get("notes"):
        lines += ["## Notes", ""]
        lines += [f"- {n}" for n in payload["notes"]]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
