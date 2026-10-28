"""Tests for the real local pre-push demonstration connector."""

from infra_pre_push import PIPELINE_ROOT, ZERO_SHA, _push_lines


def test_push_lines_parses_git_hook_input() -> None:
    head = "a" * 40
    base = "b" * 40
    assert _push_lines(f"refs/heads/demo {head} refs/heads/demo {base}\n") == [
        ("refs/heads/demo", head, "refs/heads/demo", base)
    ]


def test_push_lines_ignores_malformed_rows() -> None:
    assert _push_lines("not a git push row\nshort row\n") == []
    assert len(ZERO_SHA) == 40


def test_hook_uses_pipelineiq_file_tag_configuration() -> None:
    assert (PIPELINE_ROOT / "config" / "file_tags.yaml").is_file()
