"""Contract tests. The valid_* builders double as worked examples for the team."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from shared.contracts import (
    MESSAGES,
    BehaviourDetails,
    BuildDetails,
    ChangedFile,
    CommitContext,
    Diagnosis,
    EvidenceLine,
    Expert,
    FailedStep,
    FailureCategory,
    FailureLabel,
    FileSnapshot,
    FileTag,
    Remediation,
    RepoRef,
    RetrievedCase,
    ReviewDecision,
    RiskFactor,
    RiskOutput,
    RoutedFailure,
    VerifiedCase,
    WorkflowJob,
)

SHA = "a" * 40
SHA2 = "b" * 40
PERSON = "c" * 64
REPO = RepoRef(owner="Automattic", name="mongoose")
T = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)


def valid_context() -> CommitContext:
    return CommitContext(
        repo=REPO,
        head_sha=SHA,
        base_sha=SHA2,
        event="pull_request",
        branch="fix/connection-timeout",
        pr_number=1234,
        author_hash=PERSON,
        committed_at=T,
        history_cutoff=T,
        changed_files=[
            ChangedFile(
                path="test/connection.test.js",
                status="modified",
                additions=12,
                deletions=3,
                patch="@@ -10,3 +10,12 @@ ...",
                tags=[FileTag.TEST],
            ),
            ChangedFile(
                path="lib/connection.js", status="modified", additions=4, deletions=1,
                tags=[FileTag.SOURCE],
            ),
        ],
        snapshots=[
            FileSnapshot(
                path="package-lock.json",
                tag=FileTag.LOCKFILE,
                base_ref="snapshots/mongoose/bbbb/package-lock.json.gz",
                head_ref="snapshots/mongoose/aaaa/package-lock.json.gz",
                changed=False,
            )
        ],
        workflow_jobs=[
            WorkflowJob(
                workflow_path=".github/workflows/test.yml",
                job_key="test",
                runs_on=["ubuntu-latest"],
                node_versions=["18", "20", "22"],
                commands=["npm ci", "npm test"],
            )
        ],
    )


def valid_risk() -> RiskOutput:
    return RiskOutput(
        context_id=uuid4(),
        expert=Expert.TEST,
        model_version="lgbm-2026-11-03",
        scope="job",
        workflow_path=".github/workflows/test.yml",
        job_key="test",
        probability=0.42,
        threshold=0.35,
        alert=True,
        sub_risks={"genuine_fail": 0.11, "flaky_fail": 0.31},
        top_factors=[
            RiskFactor(
                feature="unawaited_promises_in_changed_tests",
                value=2,
                shap_value=0.18,
                direction="raises",
                reason="Two promises in the changed test are not awaited",
                path="test/connection.test.js",
                line=48,
            )
        ],
        latency_ms=820,
    )


def valid_failure() -> RoutedFailure:
    return RoutedFailure(
        repo=REPO,
        head_sha=SHA,
        run_id=111,
        run_attempt=1,
        job_id=222,
        job_name="test (20)",
        workflow_path=".github/workflows/test.yml",
        job_key="test",
        failed_steps=[FailedStep(number=5, name="npm test", conclusion="failure")],
        log_ref="logs/mongoose/111/1/222.log.gz",
        log_excerpt="1 failing ... Timeout of 2000ms exceeded",
        predicted_category=FailureCategory.TEST,
        routing_confidence=0.58,
        sent_to=[Expert.TEST, Expert.INFRA],
    )


def valid_diagnosis() -> Diagnosis:
    return Diagnosis(
        failure_id=uuid4(),
        expert=Expert.TEST,
        status="diagnosed",
        root_cause="Test waits on a real network timeout; the changed code is not on the stack",
        confidence=0.74,
        evidence=[
            EvidenceLine(source="log", text="Error: Timeout of 2000ms exceeded", line=812),
        ],
        retrieved_cases=[
            RetrievedCase(
                case_id="case-17",
                similarity=0.81,
                summary="Mocha timeout waiting for MongoDB connection",
                fix_summary="Await the connection promise and raise the hook timeout",
            )
        ],
        remediation=Remediation(
            summary="Await the connection before the assertion",
            steps=["Add `await` before `conn.asPromise()` in the before hook"],
            cites_case_ids=["case-17"],
        ),
        details=BehaviourDetails(
            verdict="flaky",
            verdict_confidence=0.74,
            framework="mocha",
            failing_tests=["connection > reconnects after drop"],
            stack_touches_changed_code=False,
            pre_run_flaky_risk=0.31,
            suggest_rerun=True,
        ),
        llm_model="hosted-small",
        prompt_version="diagnose@3f2a1c",
        latency_ms=9_400,
    )


# ---- Happy paths -----------------------------------------------------------------


@pytest.mark.parametrize(
    "build", [valid_context, valid_risk, valid_failure, valid_diagnosis], ids=lambda f: f.__name__
)
def test_round_trip_json(build):
    msg = build()
    again = type(msg).model_validate_json(msg.model_dump_json())
    assert again == msg


def test_every_message_has_a_json_schema():
    for model in MESSAGES:
        schema = model.model_json_schema()
        assert "properties" in schema and "schema_version" in schema["properties"]


def test_files_with_filters_by_tag():
    ctx = valid_context()
    assert [f.path for f in ctx.files_with(FileTag.TEST)] == ["test/connection.test.js"]


def test_review_and_verified_case():
    decision = ReviewDecision(
        diagnosis_id=uuid4(), decision="accept", reviewer_hash=PERSON, cause_confirmed=True
    )
    case = VerifiedCase(
        expert=Expert.TEST,
        origin="live",
        failure_id=uuid4(),
        decision_id=decision.decision_id,
        repo=REPO,
        repo_licence="MIT",
        failure_summary="Timeout in reconnect test",
        log_signature="Timeout of 2000ms exceeded",
        root_cause="Unawaited connection promise",
        fix_summary="Await the connection",
        fix_commit_sha=SHA,
        verifying_run_id=333,
        verifying_run_passed=True,
        tags=["async_wait"],
    )
    assert case.verifying_run_passed is True


# ---- Each rule rejects bad data --------------------------------------------------


def _with(build, **changes):
    data = build().model_dump()
    data.update(changes)
    return type(build()).model_validate(data)


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        _with(valid_risk, surprise_field=1)


def test_pull_request_needs_number():
    with pytest.raises(ValidationError):
        _with(valid_context, pr_number=None)


def test_snapshot_cannot_have_content_and_ref():
    with pytest.raises(ValidationError):
        FileSnapshot(path="x", tag=FileTag.LOCKFILE, head_content="{}", head_ref="r", changed=True)


def test_alert_must_match_threshold():
    with pytest.raises(ValidationError):
        _with(valid_risk, probability=0.10)  # below threshold but alert=True


def test_alert_needs_a_reason():
    with pytest.raises(ValidationError):
        _with(valid_risk, top_factors=[])


def test_job_scope_needs_job_key():
    with pytest.raises(ValidationError):
        _with(valid_risk, job_key=None)


def test_shap_direction_must_match_sign():
    with pytest.raises(ValidationError):
        RiskFactor(feature="f", value=1, shap_value=-0.2, direction="raises", reason="r")


def test_router_cannot_repeat_an_expert():
    with pytest.raises(ValidationError):
        _with(valid_failure, sent_to=[Expert.TEST, Expert.TEST])


def test_router_sends_to_at_most_two():
    with pytest.raises(ValidationError):
        _with(valid_failure, sent_to=[Expert.TEST, Expert.INFRA, Expert.BUILD])


def test_diagnosis_needs_evidence():
    with pytest.raises(ValidationError):
        _with(valid_diagnosis, evidence=[])


def test_remediation_can_only_cite_retrieved_cases():
    with pytest.raises(ValidationError):
        _with(valid_diagnosis, retrieved_cases=[])


def test_details_must_match_expert():
    with pytest.raises(ValidationError):
        _with(valid_diagnosis, details=BuildDetails(tool="tsc").model_dump())


def test_rerun_only_suggested_for_flaky():
    with pytest.raises(ValidationError):
        BehaviourDetails(
            verdict="genuine", verdict_confidence=0.9, framework="jest",
            failing_tests=["t"], stack_touches_changed_code=True, suggest_rerun=True,
        )


def test_misrouted_needs_suggested_category():
    with pytest.raises(ValidationError):
        Diagnosis(failure_id=uuid4(), expert=Expert.TEST, status="misrouted", latency_ms=5)
    ok = Diagnosis(
        failure_id=uuid4(), expert=Expert.TEST, status="misrouted",
        suggested_category=FailureCategory.INFRA, latency_ms=5,
    )
    assert ok.details is None


def test_modify_needs_new_remediation():
    with pytest.raises(ValidationError):
        ReviewDecision(
            diagnosis_id=uuid4(), decision="modify", reviewer_hash=PERSON, cause_confirmed=True
        )


def test_reject_cannot_confirm_cause():
    with pytest.raises(ValidationError):
        ReviewDecision(
            diagnosis_id=uuid4(), decision="reject", reviewer_hash=PERSON, cause_confirmed=True
        )


def test_verified_case_needs_passing_run():
    with pytest.raises(ValidationError):
        VerifiedCase(
            expert=Expert.TEST, origin="mined", repo=REPO, repo_licence="MIT",
            failure_summary="s", log_signature="l", root_cause="r", fix_summary="f",
            fix_commit_sha=SHA, verifying_run_id=1, verifying_run_passed=False,
        )


def test_rule_label_needs_rule_name():
    with pytest.raises(ValidationError):
        FailureLabel(
            repo=REPO, run_id=1, run_attempt=1, job_id=2,
            category=FailureCategory.TEST, labelled_by="rule",
        )
    ok = FailureLabel(
        repo=REPO, run_id=1, run_attempt=1, job_id=2, category=FailureCategory.TEST,
        sub_label="flaky", labelled_by="rule", rule_name="rerun_pass_same_sha",
    )
    assert ok.sub_label == "flaky"


def test_secrets_never_needed_raw_author_is_rejected():
    with pytest.raises(ValidationError):
        _with(valid_context, author_hash="octocat")  # must be a 64-char hash


# ---- v1.1 additions ---------------------------------------------------------------


def test_pre_push_context_is_valid_without_pr():
    data = valid_context().model_dump()
    data.update(event="pre_push", pr_number=None, branch="feature/local-work")
    ctx = CommitContext.model_validate(data)
    assert ctx.event == "pre_push"


def test_pre_push_context_rejects_pr_number():
    with pytest.raises(ValidationError):
        _with(valid_context, event="pre_push")  # valid_context has pr_number=1234


def test_routed_failure_defaults_to_live():
    assert valid_failure().replay is False
    assert _with(valid_failure, replay=True).replay is True
