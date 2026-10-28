"""Matching jobs to the files of a run-attempt log archive (D41): pure functions, no network."""

from __future__ import annotations

import pytest
from archive_scope import zip_name
from collector.log_archive import match_entries, match_key, top_level_entries


def test_match_key_keeps_letters_and_digits_only() -> None:
    assert match_key("Build&Test: node-24, ubuntu-latest") == "buildtestnode24ubuntulatest"
    assert match_key("Build&Test node-24, ubuntu-latest") == "buildtestnode24ubuntulatest"
    assert match_key(None) == "" and match_key("") == "" and match_key(" -: ") == ""
    assert match_key("Café ÉTE") == match_key("café éte") == "cafééte"


@pytest.mark.parametrize("char", list('/\\:*?"<>|') + ["\t", "\x00"])
def test_stripped_file_name_characters_do_not_change_the_key(char: str) -> None:
    job_name = f"unit{char} tests"
    assert match_key(zip_name(job_name)) == match_key(job_name) == "unittests"


def test_top_level_entries_ignore_folders_and_odd_names() -> None:
    names = [
        "0_Build & Test Failed.txt",
        "2_Build&Test node-20, ubuntu-latest.txt",
        "10_Lint.txt",
        "Lint/system.txt",  # the per-job folder file
        "Lint/",  # a folder entry
        "1_Build / Test.txt",  # a path separator that GitHub did not clean: never trusted
        "3_back\\slash.txt",
        "readme.txt",  # no index
        "4_notes.log",  # not .txt
        "5_.txt",  # no name left
    ]
    assert top_level_entries(names) == {
        "buildtestfailed": ["0_Build & Test Failed.txt"],
        "buildtestnode20ubuntulatest": ["2_Build&Test node-20, ubuntu-latest.txt"],
        "lint": ["10_Lint.txt"],
    }


def test_job_name_starting_with_digits_keeps_its_own_digits() -> None:
    assert top_level_entries(["0_2_foo.txt", "1_a.txt.txt"]) == {
        "2foo": ["0_2_foo.txt"],
        "atxt": ["1_a.txt.txt"],
    }


def test_unique_on_both_sides_matches() -> None:
    entries = top_level_entries(["0_Lint.txt", "1_Build node-20.txt"])
    jobs = [(11, "Lint"), (12, "Build: node-20"), (13, "Deploy")]
    assert match_entries([13, 12, 11], jobs, entries) == {
        11: "0_Lint.txt",
        12: "1_Build node-20.txt",
    }


def test_two_jobs_with_one_key_match_nothing_even_if_one_is_already_stored() -> None:
    entries = top_level_entries(["0_x y.txt", "1_other.txt"])
    jobs = [(1, "x-y"), (2, "x y"), (3, "other")]  # job 2 is stored, so only 1 and 3 are pending
    assert match_entries([1, 3], jobs, entries) == {3: "1_other.txt"}


def test_two_files_with_one_key_match_nothing() -> None:
    entries = top_level_entries(["0_test (a).txt", "1_test a.txt", "2_b.txt"])
    jobs = [(1, "test (a)"), (2, "b")]
    assert match_entries([1, 2], jobs, entries) == {2: "2_b.txt"}


def test_the_same_file_name_twice_is_ambiguous() -> None:
    entries = top_level_entries(["0_Lint.txt", "0_Lint.txt"])
    assert match_entries([1], [(1, "Lint")], entries) == {}


def test_jobs_without_a_usable_name_or_file_are_not_matched() -> None:
    entries = top_level_entries(["0_Lint.txt"])
    jobs = [(1, None), (2, "???"), (3, "Missing")]
    assert match_entries([1, 2, 3, 99], jobs, entries) == {}  # 99 is not even a job of the attempt
