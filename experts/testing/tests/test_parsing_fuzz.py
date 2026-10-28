"""The cleaner, the stack parser and the normaliser never raise, and a huge log cleans fast."""

from __future__ import annotations

import gzip
import random
import time
from pathlib import Path

import pytest
from testing_expert.parsing import (
    clean_bytes,
    clean_file,
    normalise,
    parse_frame,
    parse_frames,
    signature,
)

SEED = 42  # DEFAULT: fixed seed so a failure is reproducible
LOG_LIKE = (
    "2026-08-10T16:12:20.9525588Z ##[group]Run yarn jest\n"
    "2026-08-10T16:12:20.9525588Z env:\n"
    "2026-08-10T16:12:20.9525588Z ##[endgroup]\n"
    "2026-08-10T16:12:21.1Z \x1b[1m\x1b[31mFAIL\x1b[39m\x1b[22m packages/x/test/a.js (12 s)\n"
    "2026-08-10T16:12:21.2Z   \x1b[1m● a › b\x1b[22m\n"
    "2026-08-10T16:12:21.3Z     TypeError: x is not a function\n"
    "2026-08-10T16:12:21.4Z       at getUsageInBody (packages/x/src/loop.ts:54:57)\n"
    "2026-08-10T16:12:21.5Z       at /home/runner/work/R/R/node_modules/q/i.js?1792496055046:1:2\n"
    "2026-08-10T16:12:21.6Z       at tick (node:internal/process/task_queues:95:5)\n"
    "2026-08-10T16:12:21.7Z  ❯ Object.test D:\\a\\R\\R\\src\\x.ts:44:10\n"
    "2026-08-10T16:12:21.8Z Tests:       1 failed, 2 passed, 3 total\r\n"
    "2026-08-10T16:12:21.9Z ##[error]Process completed with exit code 1.\n"
).encode()

FRAGMENTS = [
    "at ", " (", ")", ":", "\\", "/", "12", "3", "❯ ", "file://", "webpack://x/./", "?", "node:",
    "<anonymous>", "async ", "new ", " [as x]", "index 0", "eval at ", ", ", "D:", "..", "...",
    "node_modules/", "/tmp/", "\x00", "\t", " ", "é", "\ufffd",
]  # fmt: skip


def _random_text(rng: random.Random) -> str:
    return "".join(rng.choice(FRAGMENTS) for _ in range(rng.randint(0, 40)))


def test_random_bytes_never_raise() -> None:
    rng = random.Random(SEED)
    for _ in range(300):
        data = rng.randbytes(rng.randint(0, 4096))
        cleaned = clean_bytes(data)
        for line in cleaned.lines:
            parse_frame(line)
            normalise(line)
        parse_frames(cleaned.lines)


def test_random_bytes_with_a_gzip_header_never_raise() -> None:
    rng = random.Random(SEED + 1)
    for _ in range(100):
        clean_bytes(b"\x1f\x8b" + rng.randbytes(rng.randint(0, 512)))


def test_truncated_and_mutated_log_text_never_raises() -> None:
    rng = random.Random(SEED + 2)
    for _ in range(400):
        cut = rng.randint(0, len(LOG_LIKE))
        data = bytearray(LOG_LIKE[:cut])
        for _ in range(rng.randint(0, 5)):  # flip a few bytes
            if data:
                data[rng.randrange(len(data))] = rng.randrange(256)
        cleaned = clean_bytes(bytes(data))
        frames = parse_frames(cleaned.lines)
        signature("Error", "\n".join(cleaned.lines), frames)


def test_truncated_gzip_never_raises() -> None:
    blob = gzip.compress(LOG_LIKE * 50)
    rng = random.Random(SEED + 3)
    for _ in range(100):
        clean_bytes(blob[: rng.randint(0, len(blob))])


def test_stack_parser_and_normaliser_survive_random_fragments() -> None:
    rng = random.Random(SEED + 4)
    for _ in range(3000):
        text = _random_text(rng)
        parse_frame(text)
        parse_frame("    at " + text)
        parse_frame(" ❯ " + text)
        normalise(text)
        signature(text or None, text, [])


@pytest.mark.slow
def test_a_50_mb_log_cleans_in_under_10_seconds(tmp_path: Path) -> None:
    block = []
    for i in range(2000):
        block.append(f"2026-08-10T16:12:{i % 60:02d}.9525588Z \x1b[32m✓\x1b[39m test {i} (5 ms)")
        frame = f"at f (/home/runner/work/R/R/packages/x/src/f{i}.ts:{i}:12)"
        block.append(f"2026-08-10T16:12:20.9525588Z     {frame}")
        block.append("2026-08-10T16:12:20.9525588Z plain output line padded to a realistic length")
        if i % 500 == 0:
            block.append("2026-08-10T16:12:20.9525588Z ##[group]Run yarn test")
            block.append("2026-08-10T16:12:20.9525588Z ##[endgroup]")
    chunk = ("\n".join(block) + "\n").encode()
    path = tmp_path / "big.log.gz"
    written = 0
    with gzip.open(path, "wb", compresslevel=1) as fh:
        while written < 50 * 1024 * 1024:
            fh.write(chunk)
            written += len(chunk)

    start = time.perf_counter()
    cleaned = clean_file(path)
    elapsed = time.perf_counter() - start

    assert cleaned.bytes_read >= 50 * 1024 * 1024
    assert not cleaned.truncated  # 50 MB is under the 64 MB cap
    assert len(cleaned.steps) > 100
    assert elapsed < 10, f"cleaning 50 MB took {elapsed:.1f} s"
