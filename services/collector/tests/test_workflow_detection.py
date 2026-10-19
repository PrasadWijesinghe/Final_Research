from __future__ import annotations

from pathlib import Path

import pytest
from collector.repo_config import ConfigError
from collector.workflow_detection import (
    detect_relevant_workflow,
    load_detection_rules,
)

FIXTURES = Path(__file__).parent / "fixtures" / "workflows"
REAL_RULES = Path(__file__).resolve().parents[3] / "config" / "workflow_detection.yaml"


def detect(name: str, member: str = "test"):  # type: ignore[no-untyped-def]
    return detect_relevant_workflow((FIXTURES / name).read_text(encoding="utf-8"), member)


@pytest.mark.parametrize(
    ("fixture", "job", "command"),
    [
        ("mongoose_test.yml", "test", "npm test"),
        ("vite_ci.yml", "test", "pnpm run test-unit"),
        ("vitest_multiline.yml", "unit", "pnpm test"),
        ("yarn_test.yml", "build", "yarn test --ci --coverage"),
        ("npx_jest.yml", "jest", "npx jest --runInBand --coverage"),
        ("mocha_direct.yml", "integration", "./node_modules/.bin/mocha --exit test/*/*.js"),
        ("vitest_run.yml", "browser", "pnpm exec vitest run --project browser"),
        ("npm_run_test_matrix.yml", "test", "npm run test:unit -- --reporter=dot"),
    ],
)
def test_positive_workflows(fixture: str, job: str, command: str) -> None:
    result = detect(fixture)
    assert result.relevant is True
    assert result.reason == "matched"
    assert (job, command) in result.matches


@pytest.mark.parametrize(
    "fixture", ["neg_lint_only.yml", "neg_release.yml", "neg_codeql_reusable.yml"]
)
def test_negative_workflows(fixture: str) -> None:
    result = detect(fixture)
    assert result.relevant is False
    assert result.reason == "no match"
    assert result.matches == ()


def test_lint_job_in_same_file_is_not_a_match() -> None:
    result = detect("vite_ci.yml")
    assert {job for job, _ in result.matches} == {"test"}


def test_on_key_parsed_as_true_is_handled() -> None:
    # YAML 1.1: `on:` becomes the boolean key True; detection must still work.
    import yaml

    text = (FIXTURES / "mongoose_test.yml").read_text(encoding="utf-8")
    assert True in yaml.safe_load(text)
    assert detect_relevant_workflow(text, "test").relevant is True


@pytest.mark.parametrize("member", ["dependency", "build", "infra"])
def test_other_members_keep_all_workflows(member: str) -> None:
    result = detect("neg_release.yml", member)
    assert result.relevant is True
    assert result.reason == "no rule for member"


@pytest.mark.parametrize(
    "text",
    ["", "just a string", "- a\n- b\n", "jobs: [unclosed", "a: b: c"],
)
def test_unparseable_or_non_mapping(text: str) -> None:
    result = detect_relevant_workflow(text, "test")
    assert result.relevant is False and result.reason == "unparseable"


def test_odd_shapes_do_not_crash() -> None:
    text = "jobs:\n  a: null\n  b:\n    steps: oops\n  c:\n    steps: [1, {run: 5}, {uses: x}]\n"
    assert detect_relevant_workflow(text, "test").relevant is False
    assert detect_relevant_workflow("name: x\n", "test").relevant is False


def test_real_rules_file_loads() -> None:
    assert "test" in load_detection_rules(REAL_RULES).members


def test_bad_rules_files(tmp_path: Path) -> None:
    bad_regex = tmp_path / "r.yaml"
    bad_regex.write_text("members:\n  test:\n    run_patterns: ['(']\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="bad regex"):
        load_detection_rules(bad_regex)
    bad_shape = tmp_path / "s.yaml"
    bad_shape.write_text("members:\n  test:\n    run_patterns: []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="run_patterns"):
        load_detection_rules(bad_shape)
    with pytest.raises(ConfigError, match="cannot load"):
        load_detection_rules(tmp_path / "missing.yaml")
