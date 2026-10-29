"""Router on real tool output: the Build expert's log fixtures (experts/build/tests/fixtures/logs).

Each build failure must go to the Build expert first; the dependency and test failures in the
same fixture set must not.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from api.router import decide, load_rules

from shared.contracts import Expert

LOGS = Path(__file__).resolve().parents[3] / "experts" / "build" / "tests" / "fixtures" / "logs"
RULES = load_rules()


@pytest.mark.parametrize(
    ("fixture", "step", "expert"),
    [
        ("tsc_typecheck_fail.log", "Run pnpm run typecheck", Expert.BUILD),
        ("tsc_pretty_build_fail.log", "Run npm run build", Expert.BUILD),
        ("eslint_fail.log", "Run pnpm run lint", Expert.BUILD),
        ("eslint_max_warnings.log", "Run npm run lint:ci", Expert.BUILD),
        ("prettier_fail.log", "Check formatting", Expert.BUILD),
        ("vite_build_fail.log", "Run npm run build", Expert.BUILD),
        ("webpack_build_fail.log", "Bundle", Expert.BUILD),
        ("esbuild_fail.log", "Run npx esbuild src/main.js --bundle", Expert.BUILD),
        ("dependency_eresolve.log", "Run npm install", Expert.DEPENDENCY),
        ("vitest_test_fail.log", "Run pnpm exec vitest run", Expert.TEST),
    ],
)
def test_real_failure_logs_route_to_the_right_expert(
    fixture: str, step: str, expert: Expert
) -> None:
    decision = decide(RULES, [step], (LOGS / fixture).read_text(encoding="utf-8"))
    assert decision.experts and decision.experts[0] is expert


def test_build_log_signals_alone_beat_a_neutral_step_name() -> None:
    # With an uninformative step name the log patterns must still pick Build.
    for fixture in ("prettier_fail.log", "esbuild_fail.log", "eslint_max_warnings.log"):
        decision = decide(RULES, ["Run ./ci.sh"], (LOGS / fixture).read_text(encoding="utf-8"))
        assert decision.experts[:1] == [Expert.BUILD], fixture
