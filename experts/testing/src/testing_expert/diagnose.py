"""Diagnosis of a failed test job.

The verdict part is real (P4.9 triage): genuine, flaky or unclear, a confidence, `suggest_rerun`
(only for a confident flaky verdict) and evidence lines from the log, the diff, the failing tests'
history, same-commit jobs and the pre-run risk. The top knowledge-base cases (P4.10), one per fix
commit, ride along in `retrieved_cases`.

The narrative (root cause and remediation, P4.11) comes from the LLM gateway when it can: the
prompt in `rag/prompts/diagnose.md` is filled with the cleaned failing-test excerpt, the triage
verdict and evidence, the stack-versus-diff overlap and the retrieved cases, and the reply is a
fixed JSON schema. The remediation may cite only retrieved case ids; any other id is dropped and a
note line says so. When the gateway is unset, down, over its cap, slow or returns an invalid answer,
or runs the mock provider (placeholder answers), the narrative is built by `rag/template.py` from
the triage result and the best case's fix summary, `llm_model` is None (or says "placeholder"), and
a note line says why. The verdict, `BehaviourDetails` and evidence never depend on the LLM.

A log that shows no Jest, Mocha or Vitest failure is not a test failure: when it matches
dependency, build or infra patterns (or the LLM says so) the answer is `status="misrouted"` with a
`suggested_category`. When triage cannot run at all (no bundle) the whole diagnosis is the stub.
"""

# ruff: noqa: E501
import logging
import time
from dataclasses import dataclass

from pydantic import ValidationError

from shared.contracts import (
    BehaviourDetails,
    Diagnosis,
    EvidenceLine,
    FailureCategory,
    Remediation,
    RetrievedCase,
    RoutedFailure,
)

from .config import EXPERT, make_details
from .kb.retrieve import Hit, retrieve_for_failure
from .rag import template
from .rag.gateway import GatewayReply, GatewayUnavailable, complete
from .rag.misroute import detect
from .rag.prompt import REPLY_SCHEMA, CaseInfo, PromptInput, prompt_version, render
from .rag.settings import DiagnosisSettings, load_settings
from .triage.features import LogEvidence, Reason, read_log
from .triage.runtime import TriageResult, log_text, triage

logger = logging.getLogger(__name__)

# DEFAULT: the stub claims no confidence, since nothing was analysed.
STUB_CONFIDENCE = 0.0
STUB_ROOT_CAUSE = "Stub expert: no analysis was done. This is placeholder text."
STUB_REMEDIATION = "Stub expert: no fix suggested. Read the cited log line and the full log."
_MAX_EVIDENCE_CHARS = 1_000  # EvidenceLine.text limit in the contract


def _evidence_text(failure: RoutedFailure) -> str:
    """Last non-empty line of the log excerpt, or the failed step's name if the log is empty."""
    lines = [line.strip() for line in failure.log_excerpt.splitlines() if line.strip()]
    text = lines[-1] if lines else f"failed step: {failure.failed_steps[0].name}"
    return text[:_MAX_EVIDENCE_CHARS]


# EvidenceLine.source only knows log / stack_trace / diff / config (contract v1.1.0), so history,
# same-commit and pre-run evidence are logged as "log" lines with a short prefix.
# DEFAULT: proposal for the next contract version is an extra source value (see the report).
_PREFIX = {"history": "History: ", "same_sha": "Same commit: ", "pre_run": "Pre-run model: "}
_SOURCE = {"history": "log", "same_sha": "log", "pre_run": "log"}
# DEFAULT: at most 12 evidence lines per diagnosis (the strongest first).
MAX_EVIDENCE = 12


def _evidence_line(r: Reason) -> EvidenceLine:
    lean = f" [points to {r.leans}]" if r.leans != "neutral" else ""
    return EvidenceLine(
        source=_SOURCE.get(r.source, r.source),  # type: ignore[arg-type]
        text=(_PREFIX.get(r.source, "") + r.text + lean)[:1_000],
        path=r.path,
        line=r.line,
    )


def _retrieved(failure: RoutedFailure, t: TriageResult) -> list[Hit]:
    """Top knowledge-base cases for this failure (P4.10, one per fix commit); [] when none."""
    try:
        return list(retrieve_for_failure(failure, t.evidence.log))
    except Exception:
        logger.warning("knowledge-base retrieval failed, no cases attached", exc_info=True)
        return []


def _retrieved_case(h: Hit) -> RetrievedCase:
    return RetrievedCase(
        case_id=h.case_id,
        similarity=h.similarity,
        summary=h.failure_summary[:1_000],
        fix_summary=h.fix_summary,
        source_url=h.source_url,
    )


def _case_info(h: Hit) -> CaseInfo:
    return CaseInfo(h.case_id, h.failure_summary, h.fix_summary, h.tags, h.similarity)


@dataclass(frozen=True)
class Narrative:
    """The root cause and remediation, who wrote them and what to tell the reader."""

    root_cause: str
    remediation: Remediation
    llm_confidence: float | None  # None when no model wrote the text
    llm_model: str | None
    notes: tuple[str, ...]


def _llm_narrative(
    reply: GatewayReply, cited_pool: set[str], failure_id: str
) -> tuple[str, Remediation, float | None, list[str]]:
    """Map a validated model reply to contract fields; drops cited ids that were not retrieved."""
    out = reply.output
    root_cause = str(out["root_cause"]).strip()
    if not root_cause:
        raise ValueError("empty root_cause")
    rem = out["remediation"]
    cited = list(dict.fromkeys(str(c) for c in out.get("cited_case_ids", [])))
    valid = [c for c in cited if c in cited_pool]
    notes: list[str] = []
    if len(valid) != len(cited):
        n = len(cited) - len(valid)
        logger.warning(
            "model cited case ids that were not retrieved, dropped",
            extra={"failure_id": failure_id, "dropped": n},
        )
        notes.append(f"The model cited {n} case id(s) that were not retrieved; they were removed.")
    patch = rem.get("suggested_patch")
    remediation = Remediation(
        summary=str(rem["summary"]).strip()[:500],
        steps=[str(x) for x in rem.get("steps", [])],
        suggested_patch=str(patch) if patch else None,
        cites_case_ids=valid,
    )
    conf = out.get("confidence")
    llm_conf = max(0.0, min(1.0, float(conf))) if isinstance(conf, (int, float)) else None
    return root_cause[:1_000], remediation, llm_conf, notes


def _template_narrative(
    t: TriageResult,
    names: list[str],
    infos: list[CaseInfo],
    notes: list[str],
    model: str | None,
    cfg: DiagnosisSettings,
) -> Narrative:
    leaning = [r.text for r in t.reasons if r.leans == t.verdict][:2]
    touched = t.evidence.overlap.first_hunk_frame
    path = touched[0] if touched else t.evidence.overlap.first_changed_stack_file
    n = template.build(
        verdict=t.verdict,
        failing_tests=names,
        families=[k for k, v in t.evidence.log.errors.items() if v],
        leaning_reasons=leaning,
        touched_path=path or t.evidence.overlap.first_test_file,
        suggest_rerun=t.suggest_rerun,
        cases=infos,
        min_similarity=cfg.template_min_similarity,
    )
    return Narrative(
        n.root_cause,
        Remediation(summary=n.summary, steps=n.steps, cites_case_ids=n.cites),
        None,
        model,
        tuple(notes),
    )


def _narrative(
    failure: RoutedFailure,
    t: TriageResult,
    names: list[str],
    hits: list[Hit],
    cfg: DiagnosisSettings,
) -> Narrative:
    """Ask the gateway for the narrative; any problem falls back to the template with a note."""
    infos = [_case_info(h) for h in hits]
    try:
        inp = PromptInput(
            job_name=failure.job_name,
            framework=t.evidence.log.framework,  # the real runner name, not the contract one
            failures=t.evidence.log.failures,
            log_tail=failure.log_excerpt[-cfg.max_excerpt_chars :],
            verdict=t.verdict,
            confidence=t.confidence,
            evidence=[_evidence_line(r).text for r in t.reasons],
            overlap=t.evidence.overlap,
            cases=infos,
        )
        reply = complete(render(inp, cfg), REPLY_SCHEMA, prompt_version(), cfg.timeout_seconds)
    except GatewayUnavailable as exc:
        logger.warning("llm gateway unavailable: %s", exc.reason)
        note = f"No language model wrote this text ({exc.reason}); it was built from the triage result and the knowledge base."
        return _template_narrative(t, names, infos, [note], None, cfg)
    except Exception:
        logger.warning("diagnosis prompt could not be built or sent", exc_info=True)
        note = "No language model wrote this text (the prompt could not be built); it was built from the triage result and the knowledge base."
        return _template_narrative(t, names, infos, [note], None, cfg)
    if reply.model.lower().startswith("mock"):
        # The mock provider returns placeholder text built from the schema, never a real analysis.
        note = f"The LLM gateway runs the {reply.model} placeholder provider, so this text was built from the triage result and the knowledge base."
        label = f"{reply.model} (placeholder answers, text from template)"
        return _template_narrative(t, names, infos, [note], label, cfg)
    try:
        root, remediation, llm_conf, notes = _llm_narrative(
            reply, {h.case_id for h in hits}, str(failure.failure_id)
        )
    except (KeyError, TypeError, ValueError, ValidationError):
        logger.warning("model answer could not be mapped to a diagnosis", exc_info=True)
        note = "The model's answer could not be used, so this text was built from the triage result and the knowledge base."
        return _template_narrative(t, names, infos, [note], None, cfg)
    return Narrative(root, remediation, llm_conf, reply.model, tuple(notes))


def _note_lines(notes: tuple[str, ...]) -> list[EvidenceLine]:
    """Notes about how the text was made, as evidence lines (the contract has no notes field)."""
    return [EvidenceLine(source="log", text=f"Diagnosis note: {n}"[:1_000]) for n in notes]


def _triaged(failure: RoutedFailure, t: TriageResult, started: float) -> Diagnosis:
    names = [f.full_name or (f.file or "") for f in t.evidence.log.failures]
    names = [n for n in names if n] or [failure.failed_steps[0].name]
    risk = t.evidence.prerun_flaky
    details = BehaviourDetails(
        verdict=t.verdict,
        verdict_confidence=t.confidence,
        framework=t.framework,
        failing_tests=names[:20],
        stack_touches_changed_code=t.evidence.overlap.touches_changed_code,
        pre_run_flaky_risk=risk if risk is not None and 0.0 <= risk <= 1.0 else None,
        suggest_rerun=t.suggest_rerun,
    )
    hits = _retrieved(failure, t)
    nar = _narrative(failure, t, names, hits, load_settings())
    remediation = nar.remediation
    if t.suggest_rerun and not any(
        "re-run" in s.lower() or "rerun" in s.lower() for s in remediation.steps
    ):
        rerun = "Re-run the failed job once; a pass on the same commit confirms it was flaky."
        remediation = remediation.model_copy(update={"steps": [rerun, *remediation.steps]})
    # DEFAULT: with a model, the diagnosis is no more confident than the lower of the triage
    # verdict confidence and the model's own; the template keeps the triage confidence.
    conf = t.confidence if nar.llm_confidence is None else min(t.confidence, nar.llm_confidence)
    return Diagnosis(
        failure_id=failure.failure_id,
        expert=EXPERT,
        status="diagnosed",
        root_cause=nar.root_cause,
        confidence=conf,
        evidence=[_evidence_line(r) for r in t.reasons[:MAX_EVIDENCE]] + _note_lines(nar.notes),
        retrieved_cases=[_retrieved_case(h) for h in hits],
        remediation=remediation,
        details=details,
        llm_model=nar.llm_model,
        prompt_version=prompt_version() if nar.llm_model else None,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


# --- not a test failure ---------------------------------------------------------------------------

_REDIRECTABLE = ("dependency", "build", "infra", "other")


def _misrouted(failure: RoutedFailure, ev: LogEvidence, started: float) -> Diagnosis | None:
    """`status="misrouted"` when the log is not a test failure and a category fits, else None."""
    cfg = load_settings()
    text = log_text(failure)
    match = detect(text, cfg)
    category: str | None = None
    lines: list[str] = []
    model: str | None = None
    why = "its log shows no Jest, Mocha or Vitest failure"
    confidence = 0.0
    if match is not None:
        category, lines = match.category, list(match.lines)
        # DEFAULT: pattern confidence is the match score over 6 (two strong patterns), max 0.9.
        confidence = min(0.9, match.score / 6.0)
        why += f" and it matches {match.category} patterns"
    else:
        try:
            inp = PromptInput(
                job_name=failure.job_name,
                framework=ev.framework if ev.framework != "unknown" else "none found",
                failures=(),
                log_tail=text[-cfg.max_excerpt_chars :],
                verdict=None,
                confidence=None,
                evidence=[],
                overlap=None,
                cases=[],
            )
            reply = complete(render(inp, cfg), REPLY_SCHEMA, prompt_version(), cfg.timeout_seconds)
        except Exception as exc:  # GatewayUnavailable or a prompt problem: no opinion from a model
            logger.info("no model opinion on a non-test failure: %s", type(exc).__name__)
            return None
        said = reply.output.get("suggested_category")
        if reply.model.lower().startswith("mock") or not reply.output.get("misrouted"):
            return None
        if said not in _REDIRECTABLE:
            return None
        category, model = str(said), reply.model
        lines = [ln.strip()[:300] for ln in text.splitlines() if ln.strip()][-1:]
        conf = reply.output.get("confidence")
        confidence = max(0.0, min(1.0, float(conf))) if isinstance(conf, (int, float)) else 0.5
        why += " and the language model agrees it is not a test failure"
    return Diagnosis(
        failure_id=failure.failure_id,
        expert=EXPERT,
        status="misrouted",
        suggested_category=FailureCategory(category),
        root_cause=f"This does not look like a test failure: {why}."[:1_000],
        confidence=confidence,
        evidence=[EvidenceLine(source="log", text=ln) for ln in lines]
        or [EvidenceLine(source="log", text=_evidence_text(failure))],
        remediation=Remediation(
            summary=f"Look at the {category} diagnosis for this failure; the test expert has nothing to add."
        ),
        llm_model=model,
        prompt_version=prompt_version() if model else None,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


def diagnose(failure: RoutedFailure) -> Diagnosis:
    """Triage the failure and write the narrative; misrouted or the stub when triage cannot run."""
    started = time.perf_counter()
    try:
        result = triage(failure)
    except Exception:
        logger.warning("triage failed, answering with the stub", exc_info=True)
        result = None
    if result is not None and result.reasons:
        return _triaged(failure, result, started)
    try:
        ev = read_log(log_text(failure), f"{failure.repo.owner}/{failure.repo.name}")
        if not ev.is_test_failure:
            misrouted = _misrouted(failure, ev, started)
            if misrouted is not None:
                return misrouted
    except Exception:
        logger.warning("misrouting check failed, answering with the stub", exc_info=True)
    return _stub(failure, started)


def _stub(failure: RoutedFailure, started: float) -> Diagnosis:
    """The placeholder diagnosis: fixed text and the last log line."""
    return Diagnosis(
        failure_id=failure.failure_id,
        expert=EXPERT,
        status="diagnosed",
        root_cause=STUB_ROOT_CAUSE,
        confidence=STUB_CONFIDENCE,
        evidence=[EvidenceLine(source="log", text=_evidence_text(failure))],
        retrieved_cases=[],
        remediation=Remediation(summary=STUB_REMEDIATION),
        details=make_details(failure),
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
