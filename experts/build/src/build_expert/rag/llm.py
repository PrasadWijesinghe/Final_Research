"""Optional LLM rewrite of a Build diagnosis through the team's LLM gateway (services/llm_gateway).

Off unless BUILD_LLM=on and LLM_GATEWAY_URL is set (the gateway only has a mock provider until
the team picks one, decision O5). The model sees only sanitised text: the parsed error lines,
the written root cause and steps, and the retrieved cases. It may cite only retrieved case ids;
an answer that cites anything else, or any error, keeps the written diagnosis.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from shared.contracts import EvidenceLine, Remediation, RetrievedCase, RoutedFailure

from ..labels import LabelDecision

logger = logging.getLogger(__name__)

PROMPT_VERSION = "build-diagnose-v1"
# DEFAULT: one diagnosis waits at most 30 s for the gateway.
TIMEOUT_S = 30.0
SYSTEM = (
    "You explain failed CI build and code-quality jobs (TypeScript, ESLint, Prettier, bundlers) "
    "to developers. Use only the facts given. Be specific and short. Never invent file names, "
    "error codes or past cases. Cite a past case only by an id from the list given."
)
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string", "minLength": 1, "maxLength": 1000},
        "summary": {"type": "string", "minLength": 1, "maxLength": 500},
        "steps": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "cites_case_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["root_cause", "summary", "steps", "cites_case_ids"],
    "additionalProperties": False,
}


def enabled() -> bool:
    """True when BUILD_LLM=on and a gateway URL is set."""
    return os.environ.get("BUILD_LLM", "off").strip().lower() == "on" and bool(
        os.environ.get("LLM_GATEWAY_URL", "").strip()
    )


def _user_message(
    failure: RoutedFailure,
    decision: LabelDecision,
    cause: str,
    fix: Remediation,
    cases: list[RetrievedCase],
    evidence: list[EvidenceLine],
) -> str:
    facts = {
        "repo": failure.repo.full_name,
        "job": failure.job_name,
        "failed_step": failure.failed_steps[0].name,
        "kind": decision.sub_label,
        "error_lines": [e.text for e in evidence],
        "draft_root_cause": cause,
        "draft_steps": fix.steps,
        "past_cases": [
            {"id": c.case_id, "summary": c.summary, "fix": c.fix_summary} for c in cases
        ],
    }
    return (
        "Rewrite the draft diagnosis for this failed job so a developer can act on it. "
        "Keep every fact; add nothing that is not in the facts.\n" + json.dumps(facts, indent=1)
    )


def refine_with_llm(
    failure: RoutedFailure,
    decision: LabelDecision,
    cause: str,
    fix: Remediation,
    cases: list[RetrievedCase],
    evidence: list[EvidenceLine],
) -> tuple[str, Remediation, str, str] | None:
    """(root cause, remediation, model, prompt version) from the LLM, or None to keep the draft."""
    if not enabled():
        return None
    url = os.environ["LLM_GATEWAY_URL"].rstrip("/") + "/complete"
    body = {
        "expert": "build",
        "prompt_version": PROMPT_VERSION,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": _user_message(failure, decision, cause, fix, cases, evidence),
            },
        ],
        "json_schema": SCHEMA,
    }
    try:
        response = httpx.post(url, json=body, timeout=TIMEOUT_S)
        response.raise_for_status()
        data = response.json()
        out = data["output"]
        allowed = {c.case_id for c in cases}
        cites = [c for c in out["cites_case_ids"] if c in allowed]
        if len(cites) != len(out["cites_case_ids"]):
            logger.warning("llm cited unknown cases; keeping the written diagnosis")
            return None
        remediation = Remediation(
            summary=out["summary"][:500], steps=list(out["steps"]), cites_case_ids=cites
        )
        return out["root_cause"][:1000], remediation, str(data["model"]), PROMPT_VERSION
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "llm refine failed; keeping the written diagnosis", extra={"error": str(exc)}
        )
        return None
