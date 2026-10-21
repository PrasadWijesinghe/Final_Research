import pytest
from features.workflow_parser import WorkflowParseError, parse_workflow

from shared.contracts import WorkflowJob

P = ".github/workflows/ci.yml"


def one(text: str, key: str = "test") -> WorkflowJob:
    jobs = {j.job_key: j for j in parse_workflow(P, text)}
    return jobs[key]


def test_matrix_node_versions_and_commands() -> None:
    job = one(
        """
jobs:
  test:
    name: Unit (${{ matrix.node }})
    runs-on: ubuntu-latest
    strategy:
      matrix:
        node: [18, 20.x, '22']
    steps:
      - uses: actions/setup-node@v4
        with:
          node-version: ${{ matrix.node }}
      - run: npm ci
      - run: |
          npm run build

          npm test -- --ci
"""
    )
    assert job.node_versions == ["18", "20.x", "22"]
    assert job.runs_on == ["ubuntu-latest"]
    assert job.job_name == "Unit (${{ matrix.node }})"
    assert job.commands == ["npm ci", "npm run build", "npm test -- --ci"]
    assert job.workflow_path == P and not job.uses_container


def test_matrix_include_and_literal_versions() -> None:
    job = one(
        """
jobs:
  test:
    runs-on: [self-hosted, linux]
    strategy:
      matrix:
        node: [18]
        include:
          - node: 21
    steps:
      - uses: actions/setup-node@v4
        with: {node-version: '${{ matrix.node }}'}
      - uses: actions/setup-node@v4
        with: {node-version: 16}
"""
    )
    assert job.node_versions == ["18", "21", "16"]
    assert job.runs_on == ["self-hosted", "linux"]


def test_matrix_runs_on_and_runner_group() -> None:
    a = one(
        """
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy: {matrix: {os: [ubuntu-latest, windows-latest]}}
    steps: []
"""
    )
    assert a.runs_on == ["ubuntu-latest", "windows-latest"]
    b = one("jobs:\n  test:\n    runs-on: {group: big, labels: [gpu]}\n    steps: []\n")
    assert b.runs_on == ["gpu"]


def test_matrix_keys_used_when_no_setup_node_step() -> None:
    job = one(
        "jobs:\n  test:\n    runs-on: x\n    strategy: {matrix: {node-version: [20, 22]}}\n"
        "    steps:\n      - run: npm test\n"
    )
    assert job.node_versions == ["20", "22"]


def test_no_node_versions_when_unresolvable() -> None:
    job = one(
        "jobs:\n  test:\n    runs-on: x\n    steps:\n"
        "      - uses: actions/setup-node@v4\n        with: {node-version-file: .nvmrc}\n"
    )
    assert job.node_versions == []


def test_permissions_job_overrides_workflow() -> None:
    text = """
permissions:
  contents: read
jobs:
  a:
    runs-on: x
  b:
    runs-on: x
    permissions: {contents: write, id-token: write}
  c:
    runs-on: x
    permissions: {}
  d:
    runs-on: x
    permissions: read-all
"""
    jobs = {j.job_key: j for j in parse_workflow(P, text)}
    assert jobs["a"].permissions == {"contents": "read"}
    assert jobs["b"].permissions == {"contents": "write", "id-token": "write"}
    assert jobs["c"].permissions == {}
    assert jobs["d"].permissions == {"*": "read"}


def test_container_flag_and_job_order() -> None:
    jobs = parse_workflow(
        P,
        "jobs:\n  z:\n    runs-on: x\n    container: {image: 'node:20'}\n  a:\n    runs-on: x\n",
    )
    assert [(j.job_key, j.uses_container) for j in jobs] == [("z", True), ("a", False)]


def test_reusable_workflow_job_has_no_steps() -> None:
    job = one("jobs:\n  test:\n    uses: ./.github/workflows/reusable.yml\n")
    assert job.commands == [] and job.runs_on == []


def test_commands_are_sanitised() -> None:
    token = "ghp_" + "a1B2c3D4e5" * 3 + "a1B2c3"
    job = one(f"jobs:\n  test:\n    runs-on: x\n    steps:\n      - run: echo {token}\n")
    assert token not in job.commands[0]
    assert "[REDACTED" in job.commands[0]


@pytest.mark.parametrize("text", ["a: [", "just a string", "name: x\n", "jobs: [1]"])
def test_invalid_workflow_raises(text: str) -> None:
    with pytest.raises(WorkflowParseError):
        parse_workflow(P, text)
