"""Rules router (DECISIONS D19): score a failed job per expert from `config/router_rules.yaml`.

Score per expert = sum of the weights of the rules that match (each rule counts once): step-name
rules against the failed step names, log rules against the lines of the log excerpt.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from redis import Redis

from shared.contracts import Expert, FailureCategory, RepoRef, RoutedFailure
from shared.contracts.routed_failure import FailedStep

from .failure_queue import enqueue_failure
from .store import Store

logger = logging.getLogger(__name__)

# api/router.py -> api -> src -> services/api -> services -> repo root
DEFAULT_RULES_PATH = Path(__file__).resolve().parents[4] / "config" / "router_rules.yaml"
MAX_EXPERTS = 2


class RouterConfigError(ValueError):
    """The rules file is missing or malformed."""


@dataclass(frozen=True)
class Rule:
    """One weighted pattern."""

    pattern: re.Pattern[str]
    weight: float


@dataclass(frozen=True)
class ExpertRules:
    """Step-name and log rules of one expert."""

    step_name: tuple[Rule, ...] = ()
    log: tuple[Rule, ...] = ()


@dataclass(frozen=True)
class RouterRules:
    """Parsed `router_rules.yaml`."""

    two_expert_margin: float
    min_score: float
    experts: dict[Expert, ExpertRules] = field(default_factory=dict)


def _rules(raw: Any, where: str) -> tuple[Rule, ...]:
    out: list[Rule] = []
    for item in raw or []:
        try:
            out.append(Rule(re.compile(str(item["pattern"])), float(item["weight"])))
        except (KeyError, TypeError, ValueError, re.error) as exc:
            raise RouterConfigError(f"bad rule in {where}: {item!r} ({exc})") from exc
    return tuple(out)


def load_rules(path: Path | str | None = None) -> RouterRules:
    """Load the rules from `path`, ROUTER_RULES_PATH, or `config/router_rules.yaml`."""
    p = Path(path or os.environ.get("ROUTER_RULES_PATH", "").strip() or DEFAULT_RULES_PATH)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        experts: dict[Expert, ExpertRules] = {}
        for name, block in data["experts"].items():
            experts[Expert(name)] = ExpertRules(
                step_name=_rules(block.get("step_name"), f"{name}.step_name"),
                log=_rules(block.get("log"), f"{name}.log"),
            )
        return RouterRules(
            two_expert_margin=float(data["two_expert_margin"]),
            min_score=float(data["min_score"]),
            experts=experts,
        )
    except (OSError, KeyError, TypeError, AttributeError, yaml.YAMLError) as exc:
        raise RouterConfigError(f"cannot read router rules {p}: {exc}") from exc
    except ValueError as exc:  # unknown expert name
        raise RouterConfigError(f"invalid router rules {p}: {exc}") from exc


class FailedJob(BaseModel):
    """A failed job as the router needs it: a RoutedFailure without the routing result."""

    repo: RepoRef
    head_sha: str
    run_id: int
    run_attempt: int = Field(default=1, ge=1)
    job_id: int
    job_name: str
    workflow_path: str
    job_key: str | None = None
    failed_steps: list[FailedStep] = Field(min_length=1)
    log_ref: str
    log_excerpt: str = Field(max_length=20_000)
    context_id: uuid.UUID | None = None
    replay: bool = False


@dataclass(frozen=True)
class Decision:
    """Outcome of scoring: category, ranked experts (0 to 2), confidence and all scores."""

    category: FailureCategory
    experts: list[Expert]
    confidence: float
    scores: dict[Expert, float]


def score(rules: RouterRules, step_names: list[str], log_text: str) -> dict[Expert, float]:
    """Score every expert: sum of the weights of its rules that match."""
    lines = log_text.splitlines()
    scores: dict[Expert, float] = {}
    for expert, er in rules.experts.items():
        total = sum(r.weight for r in er.step_name if any(r.pattern.search(n) for n in step_names))
        total += sum(r.weight for r in er.log if any(r.pattern.search(ln) for ln in lines))
        scores[expert] = total
    return scores


def decide(rules: RouterRules, step_names: list[str], log_text: str) -> Decision:
    """Pick the top expert, plus the runner-up when it is within the relative margin."""
    scores = score(rules, step_names, log_text)
    # DEFAULT: ties keep the order of the rules file (sorted() is stable).
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    if not ranked or ranked[0][1] <= 0 or ranked[0][1] < rules.min_score:
        return Decision(FailureCategory.UNKNOWN, [], 0.0, scores)
    top_expert, top = ranked[0]
    chosen = [top_expert]
    if len(ranked) > 1:
        second_expert, second = ranked[1]
        # DEFAULT: the runner-up must also reach min_score, so one stray weak match does not
        # trigger a second diagnosis.
        if second >= rules.min_score and (top - second) / top <= rules.two_expert_margin:
            chosen.append(second_expert)
    # DEFAULT: confidence = the winner's share of the total score (1.0 when nothing else matched).
    confidence = top / sum(scores.values())
    return Decision(FailureCategory(top_expert.value), chosen[:MAX_EXPERTS], confidence, scores)


@dataclass(frozen=True)
class RouteResult:
    """Routing outcome; `failure` is None for unknown (nothing is stored or sent)."""

    decision: Decision
    failure: RoutedFailure | None


def route_job(rules: RouterRules, job: FailedJob) -> RouteResult:
    """Score a failed job and build the RoutedFailure (None when the category is unknown)."""
    decision = decide(rules, [s.name for s in job.failed_steps], job.log_excerpt)
    if not decision.experts:
        # The contract needs at least one expert in `sent_to`, so an unknown failure cannot be a
        # RoutedFailure (open question for the lead).
        logger.info(
            "failure not routed: no rule matched",
            extra={"run_id": job.run_id, "job_id": job.job_id},
        )
        return RouteResult(decision, None)
    failure = RoutedFailure(
        context_id=job.context_id,
        repo=job.repo,
        head_sha=job.head_sha,
        run_id=job.run_id,
        run_attempt=job.run_attempt,
        job_id=job.job_id,
        job_name=job.job_name,
        workflow_path=job.workflow_path,
        job_key=job.job_key,
        failed_steps=job.failed_steps,
        log_ref=job.log_ref,
        log_excerpt=job.log_excerpt,
        predicted_category=decision.category,
        routing_confidence=min(1.0, decision.confidence),
        sent_to=decision.experts,
        replay=job.replay,
    )
    return RouteResult(decision, failure)


def route_failed_job(
    job: FailedJob, store: Store, connection: Redis, rules: RouterRules | None = None
) -> RouteResult:
    """Route, save the RoutedFailure, then enqueue it (for the webhook in P3.1 and replay)."""
    result = route_job(rules or load_rules(), job)
    if result.failure is not None:
        store.save_routed_failure(result.failure)  # saved first so GET /failures/{id} finds it
        enqueue_failure(connection, result.failure)
    return result
