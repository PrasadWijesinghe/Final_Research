"""The PipelineIQ GitHub check run (P3.3): create it on the first results, update it on late ones.

Only the live webhook flow calls this module. Replay and the pre-push hook never do (they must not
touch GitHub), and a GitHub error here is logged and swallowed, never raised to the caller.
PipelineIQ advises only, so a finished check is never `failure`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from typing import Any

from shared.contracts import Expert, RiskOutput
from shared.github import GitHubClient, GitHubError

from .store import Store

logger = logging.getLogger(__name__)

CHECK_NAME = "PipelineIQ"
# DEFAULT: GitHub caps a check summary at 65535 characters; stay well under, and list at most
# 12 predictions per expert (test jobs can be many), highest risk first.
MAX_SUMMARY_CHARS = 60_000
MAX_ROWS_PER_EXPERT = 12
MAX_FACTORS = 3
_LABELS = {
    Expert.DEPENDENCY: "Dependency",
    Expert.BUILD: "Build",
    Expert.TEST: "Test",
    Expert.INFRA: "Infra",
}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _label(expert: Expert) -> str:
    return _LABELS.get(expert, expert.value.title())


def _text(value: str, limit: int = 200) -> str:
    """One line of expert text, safe inside markdown (no control characters, no pipes)."""
    return _CONTROL.sub(" ", value).replace("|", "/").strip()[:limit]


def _row(risk: RiskOutput) -> list[str]:
    name = f"**{_label(risk.expert)}**" + (f" `{_text(risk.job_key, 80)}`" if risk.job_key else "")
    if risk.status == "error" or risk.probability is None:
        return [f"- {name}: no score (error)"]
    verdict = "ALERT" if risk.alert else "ok"
    note = " (degraded: some inputs were missing)" if risk.status == "degraded" else ""
    lines = [
        f"- {name}: {verdict}, score {risk.probability:.2f} (alert at {risk.threshold:.2f}){note}"
    ]
    for factor in risk.top_factors[:MAX_FACTORS]:
        lines.append(f"  - {_text(factor.reason)}")
    return lines


def render_check(
    risks: Sequence[RiskOutput],
    pending: Sequence[Expert],
    *,
    head_sha: str,
    dashboard_url: str,
    final: bool = False,
) -> dict[str, Any]:
    """Build the check-run fields (status, conclusion, output, details_url) from the results.

    `final=True` means no more results will be asked for, so a check with nothing ready finishes.
    """
    details_url = f"{dashboard_url.rstrip('/')}/#/commits/{head_sha}"
    alerts = [r for r in risks if r.alert and r.status != "error"]
    # DEFAULT: neutral when any alert or any expert is still missing, success when all clear.
    # Never `failure`: PipelineIQ is an observer.
    if not risks and not final:
        title = "PipelineIQ is waiting for the experts"
        status, conclusion = "in_progress", None
    elif not risks:
        title = "PipelineIQ: no expert answered in time"
        status, conclusion = "completed", "neutral"
    elif alerts:
        names = ", ".join(sorted({_label(r.expert) for r in alerts}))
        count = len(alerts)
        title = f"PipelineIQ: {count} alert{'s' if count != 1 else ''} ({names})"
        status, conclusion = "completed", "neutral"
    else:
        title = "PipelineIQ: no alerts"
        status, conclusion = "completed", "neutral" if pending else "success"

    lines: list[str] = []
    for expert in Expert:
        mine = sorted(
            (r for r in risks if r.expert == expert),
            key=lambda r: r.probability if r.probability is not None else -1.0,
            reverse=True,
        )
        for risk in mine[:MAX_ROWS_PER_EXPERT]:
            lines.extend(_row(risk))
        if len(mine) > MAX_ROWS_PER_EXPERT:
            lines.append(f"- {_label(expert)}: {len(mine) - MAX_ROWS_PER_EXPERT} more not shown")
    if not lines:
        lines.append("No expert has a result yet.")
    if pending:
        waiting = ", ".join(_label(e) for e in pending)
        lines += [
            "",
            f"Still pending: {waiting}."
            + (" These did not answer in time." if final else " This check updates itself."),
        ]
    lines += [
        "",
        f"[Open the commit in the PipelineIQ dashboard]({details_url})",
        "",
        "PipelineIQ only advises. It never blocks a merge.",
    ]
    summary = "\n".join(lines)[:MAX_SUMMARY_CHARS]
    fields: dict[str, Any] = {
        "status": status,
        "output": {"title": title[:255], "summary": summary},
        "details_url": details_url,
    }
    if conclusion:
        fields["conclusion"] = conclusion
    return fields


class CheckPublisher:
    """Creates the check once per commit, then PATCHes the same one. Never raises."""

    def __init__(
        self,
        store: Store,
        dashboard_url: str,
        client_factory: Callable[[], GitHubClient],
    ) -> None:
        self._store = store
        self._dashboard_url = dashboard_url
        self._client_factory = client_factory

    def publish(
        self,
        repo: str,
        head_sha: str,
        risks: Sequence[RiskOutput],
        pending: Sequence[Expert],
        *,
        final: bool = False,
    ) -> bool:
        """Create or update the check; True when GitHub accepted it. Blocking: run in a thread."""
        fields = render_check(
            risks, pending, head_sha=head_sha, dashboard_url=self._dashboard_url, final=final
        )
        try:
            existing = self._store.get_check_run(repo, head_sha)
            with self._client_factory() as client:
                if existing is None:
                    body = {"name": CHECK_NAME, "head_sha": head_sha, **fields}
                    reply = client.write_json("POST", f"/repos/{repo}/check-runs", body)
                    self._store.save_check_run(repo, head_sha, int(reply["id"]))
                else:
                    client.write_json("PATCH", f"/repos/{repo}/check-runs/{existing}", fields)
        except (GitHubError, KeyError, TypeError, ValueError) as exc:
            # Only the exception type and GitHub's short message are logged, never the payload.
            logger.warning(
                "check run not posted",
                extra={"repo": repo, "sha": head_sha, "error": type(exc).__name__},
            )
            return False
        return True
