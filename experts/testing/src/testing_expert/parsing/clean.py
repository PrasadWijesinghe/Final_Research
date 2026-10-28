"""Turns a raw GitHub Actions job log into plain lines, split into steps.

What it removes: the byte-order mark, the `2026-08-10T16:12:20.9525588Z ` timestamp GitHub puts
in front of every line, ANSI colour codes, carriage-return progress redraws, other control
characters and the `##[group]`, `##[endgroup]`, `##[error]` and `##[start-action ...]` markers.
A `##[group]Run <cmd>` marker starts a new step, so callers can see which command printed which
lines.

Safety: it reads one line at a time, cuts lines at `max_line_bytes`, and when the log is over
`max_input_bytes` it keeps only the head and the tail. It never raises on odd content (random
bytes, truncated archives); a damaged gzip stream just ends the log early with a warning.
"""

from __future__ import annotations

import gzip
import io
import re
import zlib
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from testing_expert.parsing.settings import CleanSettings, ParsingConfig, load_config

_TIMESTAMP = re.compile(r"^﻿?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z(?: |$)")
_ANSI = re.compile(
    r"\x1b\[[0-9;:?<=>]*[ -/]*[@-~]"  # CSI: colours, cursor moves
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC: window titles, hyperlinks
    r"|\x1b[@-Z\\-_]"  # two-character escapes
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# `##[kind]text` or `##[kind key=value;...]`. Only the kinds in _TEXT_MARKERS carry a message worth
# keeping; the rest (start-action, end-action, add-matcher, ...) are runner bookkeeping.
_MARKER = re.compile(r"^##\[(?P<kind>[A-Za-z][\w-]*)(?: [^\]]*)?\](?P<rest>.*)$")
_TEXT_MARKERS = frozenset({"error", "warning", "notice", "debug", "command", "section"})
_RUN_PREFIX = "Run "
# A line our own tooling inserts where it cut text out. The `##[endgroup]` of an open `Run` group
# may have been in the cut part, so the marker also ends the group header.
_CUT_MARKER = re.compile(r"^\[(?:fixture trimmed|log truncated): \d+ lines omitted\]$")
_GZIP_MAGIC = b"\x1f\x8b"


class _LineSource(Protocol):
    """Anything that can read a line of bytes (a file, a BytesIO, a GzipFile)."""

    def readline(self, size: int = -1, /) -> bytes:
        """Read one line, at most `size` bytes."""
        ...


@dataclass(frozen=True, slots=True)
class Step:
    """One workflow step: the command that started it and which cleaned lines it printed.

    `start` and `end` index into `CleanedLog.lines` (end is exclusive). The first step has an
    empty `command`: it holds the runner's own setup output before the first `Run` group.
    """

    command: str
    script: tuple[str, ...]
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class CleanedLog:
    """A cleaned job log: output lines in order, the steps, and what was cut."""

    lines: tuple[str, ...]
    steps: tuple[Step, ...]
    truncated: bool = False
    warnings: tuple[str, ...] = ()
    bytes_read: int = 0
    lines_dropped: int = 0

    def step_lines(self, step: Step) -> tuple[str, ...]:
        """The output lines of one step."""
        return self.lines[step.start : step.end]

    def text(self) -> str:
        """All output lines joined with newlines."""
        return "\n".join(self.lines)


def clean_line(raw: str) -> str:
    """Strip BOM, timestamp, ANSI codes, carriage-return redraws and control characters."""
    line = raw.rstrip("\n")
    if "\r" in line:
        # A terminal shows only what was written after the last carriage return.
        parts = [p for p in line.split("\r") if p]
        line = parts[-1] if parts else ""
    m = _TIMESTAMP.match(line)
    if m:
        line = line[m.end() :]
    elif line.startswith("﻿"):
        line = line[1:]
    if "\x1b" in line:
        line = _ANSI.sub("", line)
    if not line.isprintable():
        line = _CONTROL.sub("", line)
    return line


def _iter_raw_lines(fh: _LineSource, max_line: int) -> Iterator[tuple[bytes, int]]:
    """Yield (line bytes cut at max_line, original size). Reads at most max_line + 1 at a time."""
    while True:
        chunk = fh.readline(max_line + 1)
        if not chunk:
            return
        size = len(chunk)
        if size > max_line and not chunk.endswith(b"\n"):
            while True:  # skip the rest of the over-long line without holding it
                more = fh.readline(65536)
                size += len(more)
                if not more or more.endswith(b"\n"):
                    break
            chunk = chunk[:max_line]
        yield chunk, size


def _select(
    fh: _LineSource, cfg: CleanSettings, warnings: list[str]
) -> tuple[list[bytes], list[bytes], int, int, bool]:
    """Read the stream and keep the head and the tail. Returns head, tail, dropped, bytes, cut."""
    head: list[bytes] = []
    head_size = 0
    tail: deque[bytes] = deque()
    tail_size = 0
    tail_budget = max(cfg.max_input_bytes - cfg.head_bytes, 0)
    dropped = 0
    scanned = 0
    cut = False
    try:
        for chunk, size in _iter_raw_lines(fh, cfg.max_line_bytes):
            scanned += size
            if scanned > cfg.max_scan_bytes:
                cut = True
                warnings.append(f"stopped reading after {cfg.max_scan_bytes} bytes")
                break
            if head_size < cfg.head_bytes:
                head.append(chunk)
                head_size += len(chunk)
                continue
            tail.append(chunk)
            tail_size += len(chunk)
            while tail_size > tail_budget and tail:
                tail_size -= len(tail.popleft())
                dropped += 1
    except (EOFError, gzip.BadGzipFile, zlib.error, OSError) as exc:
        # A damaged archive: keep what was read.
        cut = True
        warnings.append(f"log stream ended early: {type(exc).__name__}")
    return head, list(tail), dropped, scanned, cut or dropped > 0


def clean_stream(fh: _LineSource, config: ParsingConfig | None = None) -> CleanedLog:
    """Clean a binary stream of log text (plain, not gzip)."""
    cfg = (config or load_config()).clean
    warnings: list[str] = []
    head, tail, dropped, scanned, truncated = _select(fh, cfg, warnings)
    lines: list[str] = []
    steps: list[Step] = []
    command = ""
    script: list[str] = []
    step_start = 0
    in_script = False

    def close_step() -> None:
        if steps or lines or script or command:
            steps.append(Step(command, tuple(script), step_start, len(lines)))

    def feed(chunks: list[bytes]) -> None:
        nonlocal command, script, step_start, in_script
        for chunk in chunks:
            line = clean_line(chunk.decode("utf-8", errors="replace"))
            m = _MARKER.match(line) if line.startswith("##[") else None
            if m:
                kind, rest = m.group("kind"), m.group("rest")
                if kind == "group":
                    if rest.startswith(_RUN_PREFIX):
                        close_step()
                        command = rest[len(_RUN_PREFIX) :].strip()
                        script = []
                        step_start = len(lines)
                        in_script = True
                    else:
                        lines.append(rest)
                    continue
                if kind == "endgroup":
                    in_script = False
                    continue
                if kind not in _TEXT_MARKERS:
                    continue  # ##[start-action ...] and other bookkeeping lines
                line = rest  # ##[error] and friends: keep the message text
            if in_script and _CUT_MARKER.match(line):
                in_script = False  # the cut swallowed the endgroup: the rest is output
            if in_script:
                script.append(line)
                if len(script) >= cfg.max_script_lines:
                    in_script = False  # a group that never closed: the rest is output
                continue
            lines.append(line)

    feed(head)
    if dropped:
        lines.append(f"[log truncated: {dropped} lines omitted]")
        in_script = False  # the cut may have landed inside a group header
    feed(tail)
    close_step()
    if dropped:
        warnings.append(f"log over {cfg.max_input_bytes} bytes: kept head and tail")
    return CleanedLog(
        lines=tuple(lines),
        steps=tuple(steps),
        truncated=truncated,
        warnings=tuple(warnings),
        bytes_read=scanned,
        lines_dropped=dropped,
    )


def clean_bytes(data: bytes, config: ParsingConfig | None = None) -> CleanedLog:
    """Clean log text held in memory; gzip data is detected and decompressed."""
    if data[:2] == _GZIP_MAGIC:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            return clean_stream(gz, config)
    return clean_stream(io.BytesIO(data), config)


def clean_text(text: str, config: ParsingConfig | None = None) -> CleanedLog:
    """Clean a log held as a string."""
    return clean_bytes(text.encode("utf-8", errors="replace"), config)


def clean_file(path: Path | str, config: ParsingConfig | None = None) -> CleanedLog:
    """Clean a log file on disk (.gz or plain), reading it as a stream.

    Raises only if the file cannot be opened; a damaged archive gives a shortened log.
    """
    with open(path, "rb") as raw:
        if raw.read(2) == _GZIP_MAGIC:
            raw.seek(0)
            with gzip.GzipFile(fileobj=raw) as gz:
                return clean_stream(gz, config)
        raw.seek(0)
        return clean_stream(raw, config)
