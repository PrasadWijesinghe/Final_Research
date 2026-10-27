"""Normalisation for retrieval: volatile values become placeholders."""

from __future__ import annotations

import pytest
from testing_expert.parsing import normalise, parse_frames, signature

CASES = [
    ("550e8400-e29b-41d4-a716-446655440000 failed", "<uuid> failed"),
    ("at 2026-08-10T16:12:20.9525588Z it broke", "at <ts> it broke"),
    ("at 2026-08-10 16:12:20 it broke", "at <ts> it broke"),
    ("started 12:30:05 ended 12:30:09.250", "started <ts> ended <ts>"),
    ("cache-bust ?1792496055046:168:34", "cache-bust ?<ts>:168:34"),
    ("✓ works (5 ms)", "✓ works (<dur>)"),
    ("took 58.70s in total", "took <dur> in total"),
    ("Done in 1m 20s", "Done in <dur> <dur>"),
    ("Tests: 3 passed (1m)", "Tests: 3 passed (<dur>)"),
    ("timeout of 2000 ms exceeded", "timeout of <dur> exceeded"),
    ("connect ECONNREFUSED 127.0.0.1:5432", "connect ECONNREFUSED 127.0.0.1:<port>"),
    ("listening on localhost:3000", "listening on localhost:<port>"),
    ("GET http://example.com:8080/x", "GET http://example.com:<port>/x"),
    ("server started on port 8443", "server started on port <port>"),
    ("object at 0x7ffd1234abcd", "object at <addr>"),
    ("ENOENT /tmp/jest_abc123/cache/a.json", "ENOENT <tmp>"),
    ("open /var/folders/zz/abc/T/x.js:3:4", "open <tmp>:3:4"),
    (r"open C:\Users\RUNNER~1\AppData\Local\Temp\a\b.js", "open <tmp>"),
    ("sha 3d3c42e5aac5ba805825da76410c181273ba90b1", "sha <hex>"),
    ("many   spaces\there", "many spaces here"),
    # things that must survive
    ("at f (src/a.js:12:34)", "at f (src/a.js:12:34)"),
    ("expected 5 to equal 6", "expected 5 to equal 6"),
    ("node v22.1.0", "node v22.1.0"),
    ("deadbeef is short", "deadbeef is short"),
    ("abcdefghijklmnopqrstuvwx stays", "abcdefghijklmnopqrstuvwx stays"),
]


@pytest.mark.parametrize(("text", "expected"), CASES)
def test_normalise(text: str, expected: str) -> None:
    assert normalise(text) == expected


def test_normalise_is_idempotent() -> None:
    text = "2026-08-10T16:12:20Z 550e8400-e29b-41d4-a716-446655440000 (5 ms) /tmp/x/y localhost:80"
    once = normalise(text)
    assert normalise(once) == once


def test_two_runs_of_the_same_failure_normalise_equal() -> None:
    a = "Error: connect ECONNREFUSED 127.0.0.1:51234 at 12:01:02 (153 ms) /tmp/jest_a1b2c3/x.js"
    b = "Error: connect ECONNREFUSED 127.0.0.1:60001 at 09:15:44 (87 ms) /tmp/jest_ffeedd/x.js"
    assert normalise(a) == normalise(b)


def test_signature_uses_type_first_line_and_top_project_frames() -> None:
    frames = parse_frames(
        [
            "    at node_modules_user (node_modules/dep/index.js:1:1)",
            "    at process.tick (node:internal/process/task_queues:95:5)",
            "    at one (src/a.ts:10:1)",
            "    at two (src/b.ts:20:2)",
            "    at three (src/c.ts:30:3)",
            "    at four (src/d.ts:40:4)",
        ]
    )
    sig = signature("TypeError", "x is not a function (5 ms)\nsecond line", frames)
    assert (
        sig
        == "TypeError: x is not a function (<dur>) | src/a.ts:one | src/b.ts:two | src/c.ts:three"
    )


def test_signature_ignores_line_numbers() -> None:
    f1 = parse_frames(["    at one (src/a.ts:10:1)"])
    f2 = parse_frames(["    at one (src/a.ts:99:7)"])
    assert signature("Error", "boom", f1) == signature("Error", "boom", f2)


def test_signature_without_inputs() -> None:
    assert signature(None, None, []) == "Error"
    assert signature("AssertionError", "", []) == "AssertionError"
