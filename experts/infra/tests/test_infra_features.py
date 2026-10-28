"""Serving feature extraction tests for the Infra model."""

import json
from pathlib import Path

from infra_expert.features import prepared_features_from_context, raw_features_from_context
from infra_expert.features.context import workflow_signals

from shared.contracts import CommitContext

FIXTURES = Path(__file__).parent / "fixtures"


def _context() -> CommitContext:
    body = json.loads((FIXTURES / "commit_context.json").read_text("utf-8"))
    return CommitContext.model_validate(body)


def test_context_maps_to_training_schema() -> None:
    raw = raw_features_from_context(_context())
    assert raw["Trigger_Event"] == "push"
    assert raw["Branch_Category"] == "Default branch"
    assert raw["Runner_Labels"] == "ubuntu-latest"
    assert raw["Files_Changed_Count"] == 1
    assert raw["Lines_Added"] == 3
    assert raw["Lines_Deleted"] == 1

    prepared = prepared_features_from_context(_context())
    assert len(prepared) == 1
    assert prepared.iloc[0]["Total_Lines_Changed"] == 4
    assert prepared.iloc[0]["Infra_File_Change_Ratio"] == 0


def test_unknown_workflow_does_not_invent_negative_flags() -> None:
    import math

    raw = raw_features_from_context(_context())
    assert math.isnan(raw["Docker_Used"])
    assert math.isnan(raw["Permissions_Defined"])
    assert math.isnan(raw["Workflow_Line_Count"])


def test_workflow_flags_match_collector_semantics() -> None:
    # Regression for actual collector/serving differences: empty permissions,
    # restore-only cache, service containers, docker:// actions and final newline.
    workflow = (
        "permissions: {}\njobs:\n  ci:\n    runs-on: ubuntu-latest\n"
        "    services:\n      db:\n        image: postgres:17\n"
        "    steps:\n      - uses: actions/cache/restore@v4\n"
        "      - uses: docker://node:22\n"
    )
    result = workflow_signals(workflow)
    assert result["Permissions_Defined"] is True
    assert result["Docker_Used"] is True
    assert result["Cache_Action_Used"] is True
    assert result["Third_Party_Action_Count"] == 1
    assert result["Workflow_Line_Count"] == len(workflow.split("\n"))


def test_changing_image_name_is_not_an_existing_model_signal() -> None:
    # Honest coverage test: existing feature schema cannot distinguish these contents.
    template = "jobs:\n  ci:\n    runs-on: ubuntu-latest\n    container: {}\n"
    assert workflow_signals(template.format("node:22-alpine")) == workflow_signals(
        template.format("ghcr.io/pipelineiq-demo/infra-test-image-that-does-not-exist:never")
    )
