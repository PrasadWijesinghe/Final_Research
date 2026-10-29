"""The committed log fixtures are safe to publish and well formed (no database needed)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from testing_expert.parsing import load_config
from testing_expert.parsing.make_fixtures import EMAIL_RE, FRAMEWORKS, fixture_name, load_manifest

from shared.sanitise import sanitise

LOGS = Path(__file__).parent / "fixtures" / "logs"
SETTINGS = load_config().fixtures
FIXTURES = sorted(LOGS.glob("*/*.log"))
# DEFAULT: the task asks for 12 jest, 13 mocha, 13 vitest and 4 other fixtures at least.
MINIMUMS = {"jest": 12, "mocha": 13, "vitest": 13, "other": 4}
TIMESTAMP = re.compile(r"^﻿?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ", re.MULTILINE)
STUB_KEYS = {"framework", "repo", "job_id", "conclusion"}


def _ids(paths: list[Path]) -> list[str]:
    return [f"{p.parent.name}/{p.name}" for p in paths]


def test_there_are_enough_fixtures_per_framework() -> None:
    counts = {fw: len(list((LOGS / fw).glob("*.log"))) for fw in FRAMEWORKS}
    for fw, minimum in MINIMUMS.items():
        assert counts[fw] >= minimum, f"{fw}: {counts[fw]} fixtures, need {minimum}"
    for fw in ("jest", "mocha", "vitest"):
        assert counts[fw] >= SETTINGS.min_per_framework


@pytest.mark.parametrize("path", FIXTURES, ids=_ids(FIXTURES))
def test_fixture_is_clean(path: Path) -> None:
    data = path.read_bytes()
    text = data.decode("utf-8")  # must be valid UTF-8
    assert len(data) <= SETTINGS.max_bytes, f"{len(data)} bytes is over the cap"
    assert max((len(x.encode()) for x in text.split("\n")), default=0) <= SETTINGS.max_line_bytes
    assert sanitise(text).total == 0, "the sanitiser still finds a secret"
    stray = [m for m in EMAIL_RE.findall(text) if m != SETTINGS.email_replacement]
    assert stray == [], "an email address is left in the fixture"
    assert text.endswith("\n")
    # Timestamps are kept on purpose: the cleaner is tested against them.
    assert TIMESTAMP.search(text), "no GitHub timestamp prefix: was this fixture stripped?"


@pytest.mark.parametrize("path", FIXTURES, ids=_ids(FIXTURES))
def test_fixture_has_matching_expected_stub(path: Path) -> None:
    expected_path = path.with_suffix(".expected.json")
    assert expected_path.is_file(), f"missing {expected_path.name}"
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    assert STUB_KEYS <= expected.keys()
    assert expected["framework"] == path.parent.name
    assert expected["conclusion"] in {"success", "failure", "timed_out", "cancelled"}
    assert path.name == f"{fixture_name(expected['repo'], expected['job_id'])}.log"


def test_manifest_and_files_agree() -> None:
    manifest = load_manifest(LOGS / "manifest.yaml")
    listed = {f"{e.framework}/{fixture_name(e.repo, e.job_id)}.log" for e in manifest.fixtures}
    on_disk = {f"{p.parent.name}/{p.name}" for p in FIXTURES}
    assert listed == on_disk
    assert all(e.reason for e in manifest.fixtures), "every fixture needs a reason"


def test_expected_files_have_a_log() -> None:
    for expected in LOGS.glob("*/*.expected.json"):
        assert expected.with_name(expected.name.replace(".expected.json", ".log")).is_file()


def test_every_framework_folder_keeps_some_ansi_colours() -> None:
    # Colour codes are kept on purpose, like timestamps, so the cleaner stays tested.
    for fw in ("jest", "vitest"):
        assert any("\x1b[" in p.read_text(encoding="utf-8") for p in (LOGS / fw).glob("*.log")), fw
