"""Loads config/test_log_parsing.yaml into typed, compiled settings (one copy per process)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_ENV = "TEST_LOG_PARSING_FILE"
_FILENAME = "test_log_parsing.yaml"


@dataclass(frozen=True)
class CleanSettings:
    """Limits for reading a log (see `clean:` in the YAML)."""

    max_input_bytes: int
    head_bytes: int
    max_line_bytes: int
    max_scan_bytes: int
    max_script_lines: int


@dataclass(frozen=True)
class ParseSettings:
    """Limits for what the framework parsers keep (see `parse:` in the YAML)."""

    max_line_chars: int
    failure_raw_max_chars: int
    message_max_chars: int
    max_body_lines: int
    max_failures: int
    max_results: int
    test_file: re.Pattern[str]
    test_path: re.Pattern[str]
    detect_min_score: int


@dataclass(frozen=True)
class StackSettings:
    """How stack-frame paths are recognised and mapped (see `stack:` in the YAML)."""

    max_line_chars: int
    max_raw_chars: int
    temp_paths: tuple[re.Pattern[str], ...]
    workspace_prefixes: tuple[re.Pattern[str], ...]
    dependency_marker: str
    node_internal_patterns: tuple[re.Pattern[str], ...]
    elided_path: re.Pattern[str]


@dataclass(frozen=True)
class NormaliseRule:
    """One find-and-replace step of `normalise()`."""

    name: str
    pattern: re.Pattern[str]
    replace: str


@dataclass(frozen=True)
class NormaliseSettings:
    """The ordered normalisation rules and the signature shape (see `normalise:`)."""

    signature_frames: int
    signature_message_chars: int
    rules: tuple[NormaliseRule, ...]


@dataclass(frozen=True)
class FixtureSettings:
    """Limits for the fixture tooling and its cleanliness test (see `fixtures:`)."""

    seed: int
    max_bytes: int
    max_line_bytes: int
    email_replacement: str
    keep_head_lines: int
    min_per_framework: int


@dataclass(frozen=True)
class ParsingConfig:
    """Everything configurable about log parsing."""

    clean: CleanSettings
    parse: ParseSettings
    stack: StackSettings
    normalise: NormaliseSettings
    fixtures: FixtureSettings


def find_config_file() -> Path:
    """Locate config/test_log_parsing.yaml: env override, else walk up from this file."""
    # DEFAULT: TEST_LOG_PARSING_FILE overrides; otherwise search the parents of this file for
    # config/test_log_parsing.yaml (same approach as the sanitiser's pattern file).
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config" / _FILENAME
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"config/{_FILENAME} not found above {Path(__file__).resolve()}; set {CONFIG_ENV}"
    )


def _section(raw: dict[str, Any], name: str, source: str) -> dict[str, Any]:
    value = raw.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"{source}: missing section '{name}'")
    return value


def _compile(pattern: object, source: str, what: str) -> re.Pattern[str]:
    try:
        return re.compile(str(pattern))
    except re.error as exc:
        raise ValueError(f"{source}: {what} is not a valid regex: {exc}") from exc


def parse_config(raw: object, source: str = "<config>") -> ParsingConfig:
    """Validate and compile an already-parsed YAML document."""
    if not isinstance(raw, dict):
        raise ValueError(f"{source}: expected a mapping at the top level")
    try:
        clean = _section(raw, "clean", source)
        parse = _section(raw, "parse", source)
        stack = _section(raw, "stack", source)
        norm = _section(raw, "normalise", source)
        fix = _section(raw, "fixtures", source)
        rules = norm["rules"]
        if not isinstance(rules, list) or not rules:
            raise ValueError(f"{source}: normalise.rules must be a non-empty list")
        return ParsingConfig(
            clean=CleanSettings(
                max_input_bytes=int(clean["max_input_bytes"]),
                head_bytes=int(clean["head_bytes"]),
                max_line_bytes=int(clean["max_line_bytes"]),
                max_scan_bytes=int(clean["max_scan_bytes"]),
                max_script_lines=int(clean["max_script_lines"]),
            ),
            parse=ParseSettings(
                max_line_chars=int(parse["max_line_chars"]),
                failure_raw_max_chars=int(parse["failure_raw_max_chars"]),
                message_max_chars=int(parse["message_max_chars"]),
                max_body_lines=int(parse["max_body_lines"]),
                max_failures=int(parse["max_failures"]),
                max_results=int(parse["max_results"]),
                test_file=_compile(parse["test_file_pattern"], source, "parse.test_file_pattern"),
                test_path=_compile(parse["test_path_pattern"], source, "parse.test_path_pattern"),
                detect_min_score=int(parse["detect_min_score"]),
            ),
            stack=StackSettings(
                max_line_chars=int(stack["max_line_chars"]),
                max_raw_chars=int(stack["max_raw_chars"]),
                temp_paths=tuple(
                    _compile(p, source, "stack.temp_paths") for p in stack["temp_paths"]
                ),
                workspace_prefixes=tuple(
                    _compile(p, source, "stack.workspace_prefixes")
                    for p in stack["workspace_prefixes"]
                ),
                dependency_marker=str(stack["dependency_marker"]),
                node_internal_patterns=tuple(
                    _compile(p, source, "stack.node_internal_patterns")
                    for p in stack["node_internal_patterns"]
                ),
                elided_path=_compile(stack["elided_path"], source, "stack.elided_path"),
            ),
            normalise=NormaliseSettings(
                signature_frames=int(norm["signature_frames"]),
                signature_message_chars=int(norm["signature_message_chars"]),
                rules=tuple(
                    NormaliseRule(
                        name=str(r["name"]),
                        pattern=_compile(r["pattern"], source, f"rule '{r['name']}'"),
                        replace=str(r["replace"]),
                    )
                    for r in rules
                ),
            ),
            fixtures=FixtureSettings(
                seed=int(fix["seed"]),
                max_bytes=int(fix["max_bytes"]),
                max_line_bytes=int(fix["max_line_bytes"]),
                email_replacement=str(fix["email_replacement"]),
                keep_head_lines=int(fix["keep_head_lines"]),
                min_per_framework=int(fix["min_per_framework"]),
            ),
        )
    except KeyError as exc:
        raise ValueError(f"{source}: missing key {exc}") from exc
    except (TypeError, AttributeError) as exc:
        raise ValueError(f"{source}: malformed value: {exc}") from exc


@lru_cache(maxsize=4)
def _load(path: str) -> ParsingConfig:
    with open(path, encoding="utf-8") as fh:
        return parse_config(yaml.safe_load(fh), path)


def load_config(path: Path | str | None = None) -> ParsingConfig:
    """Return the parsing settings from `path`, or from the default config file (cached)."""
    return _load(str(path if path is not None else find_config_file()))
