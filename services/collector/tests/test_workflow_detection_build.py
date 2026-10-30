"""Workflow detection rules for the Build & Code Quality member (config/workflow_detection.yaml)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from collector.workflow_detection import detect_relevant_workflow

FIXTURES = Path(__file__).parent / "fixtures" / "workflows"


def _one(command: str) -> bool:
    text = f"on: push\njobs:\n  j:\n    steps:\n      - run: {command!r}\n"
    return detect_relevant_workflow(text, "build").relevant


def test_monorepo_fixture_matches_every_build_style_job() -> None:
    result = detect_relevant_workflow(
        (FIXTURES / "build_monorepo.yml").read_text(encoding="utf-8"), "build"
    )
    assert result.relevant is True
    assert ("quality", "pnpm -r --parallel typecheck") in result.matches
    assert ("quality", "npx prettier --check .") in result.matches
    assert ("bundle", "npx tsc -b tsconfig.build.json") in result.matches
    assert {job for job, _ in result.matches} == {"quality", "bundle"}


@pytest.mark.parametrize("fixture", ["vite_ci.yml", "neg_lint_only.yml", "vitest_multiline.yml"])
def test_existing_fixtures_with_lint_or_build_steps_match(fixture: str) -> None:
    text = (FIXTURES / fixture).read_text(encoding="utf-8")
    assert detect_relevant_workflow(text, "build").relevant is True


@pytest.mark.parametrize(
    "fixture",
    ["neg_build_docker.yml", "neg_release.yml", "mongoose_test.yml", "npx_jest.yml"],
)
def test_workflows_without_build_steps_do_not_match(fixture: str) -> None:
    text = (FIXTURES / fixture).read_text(encoding="utf-8")
    assert detect_relevant_workflow(text, "build").relevant is False


@pytest.mark.parametrize(
    "command",
    [
        "npm run build",
        "pnpm build",
        "yarn lint:ci",
        "yarn workspace @x/y build",
        "pnpm --filter ./pkg build",
        "pnpm -C packages/core build",
        "pnpm -r --parallel typecheck",
        "npm run type-check",
        "pnpm check-types",
        "pnpm run build:types",
        "bun run check",
        "npm run format:check",
        "npm run lint -- --max-warnings=0",
        "node --run build",
        "npx tsc --noEmit",
        "yarn tsc -b",
        "./node_modules/.bin/eslint .",
        "pnpm exec vue-tsc --noEmit",
        "npx vite build",
        "npx next build",
        "npx webpack --mode production",
        "rollup -c",
        "npx prettier --check .",
        "npx @biomejs/biome ci .",
        "nx run-many -t lint typecheck",
        "npx hereby lint",
        "npm ci && npm run build",
        "NODE_ENV=production npx nx build web",
        "node_modules/.bin/tsc -p .",
    ],
)
def test_build_commands_match(command: str) -> None:
    assert _one(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "npm ci",
        "npm install --save-dev eslint",
        "pnpm add -D typescript",
        "echo 'run eslint'",
        "cd packages/webpack",
        "docker build -t x .",
        "go build ./...",
        "npm test",
        "npm run test:unit",
        "npx eslint-plugin-jest-check --help",
        "npm publish",
        "turbo run test --filter=build-utils",
        "cat webpack.config.js",
        "npm run docs:build",
        "pnpm run prebuild-check",
        "vp exec pkg-pr-new publish './packages/rspack' './packages/webpack'",
        "# webpack is configured in another workflow",
        "ERROR_MESSAGE='Running pnpm run lint may fix this'",
    ],
)
def test_non_build_commands_do_not_match(command: str) -> None:
    assert _one(command) is False


@pytest.mark.parametrize(
    "command",
    [
        "npm " + "--a " * 40 + "!",
        "npm " + "-a b " * 40 + "!",
        "pnpm run " + "--a=b " * 40 + "!",
        "turbo " + "x " * 200 + "!",
        "prettier " + "x " * 200 + "!",
    ],
)
def test_repeated_flags_do_not_backtrack_catastrophically(command: str) -> None:
    start = time.perf_counter()
    assert _one(command) is False
    assert time.perf_counter() - start < 0.5
