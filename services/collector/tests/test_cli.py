from __future__ import annotations

from collector.cli import main


def test_main_stub_runs() -> None:
    assert main([]) == 0
