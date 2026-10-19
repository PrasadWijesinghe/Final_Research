"""Secret redaction (detect-secrets plus custom regexes) and identity hashing."""

from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from detect_secrets.core.scan import scan_line
from detect_secrets.settings import transient_settings

from shared.sanitise.patterns import SecretPattern, load_patterns

SALT_ENV = "IDENTITY_SALT"

# DEFAULT: only detect-secrets' provider-specific detectors; the entropy and keyword detectors are
# left out because they flag commit SHAs, UUIDs and cache keys that are normal in CI logs.
_DS_PLUGINS = [
    "ArtifactoryDetector",
    "AWSKeyDetector",
    "AzureStorageKeyDetector",
    "BasicAuthDetector",
    "CloudantDetector",
    "DiscordBotTokenDetector",
    "GitHubTokenDetector",
    "IbmCloudIamDetector",
    "IbmCosHmacDetector",
    "JwtTokenDetector",
    "MailchimpDetector",
    "NpmDetector",
    "OpenAIDetector",
    "PrivateKeyDetector",
    "SendGridDetector",
    "SlackDetector",
    "SoftlayerDetector",
    "SquareOAuthDetector",
    "StripeDetector",
    "TwilioKeyDetector",
]

# DEFAULT: detect-secrets only scans lines up to this length (it is slow on huge minified lines);
# the custom regexes still scan the whole text.
MAX_DS_LINE_LENGTH = 20_000

# DEFAULT: lines shorter than this (after stripping) cannot hold any supported secret; skip them.
MIN_DS_LINE_LENGTH = 20


@dataclass(frozen=True)
class SanitiseResult:
    """Sanitised text plus how many secrets of each type were replaced."""

    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        """Total number of redactions."""
        return sum(self.counts.values())


def _slug(secret_type: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", secret_type.lower()).strip("_")


def _apply_pattern(text: str, pat: SecretPattern, counts: Counter[str]) -> str:
    label = f"[REDACTED:{pat.type}]"

    def repl(m: re.Match[str]) -> str:
        counts[pat.type] += 1
        if pat.group is None:
            return label
        start = m.start(pat.group) - m.start()
        end = m.end(pat.group) - m.start()
        whole = m.group(0)
        return whole[:start] + label + whole[end:]

    return pat.regex.sub(repl, text)


def _apply_detect_secrets(text: str, counts: Counter[str]) -> str:
    settings = {"plugins_used": [{"name": n} for n in _DS_PLUGINS]}
    out: list[str] = []
    with transient_settings(settings):
        for line in text.splitlines(keepends=True):
            if MIN_DS_LINE_LENGTH <= len(line.strip()) and len(line) <= MAX_DS_LINE_LENGTH:
                found = {(s.secret_value, s.type) for s in scan_line(line) if s.secret_value}
                for value, kind in found:
                    n = line.count(value)
                    if n:
                        counts[_slug(kind)] += n
                        line = line.replace(value, f"[REDACTED:{_slug(kind)}]")
            out.append(line)
    return "".join(out)


def sanitise(
    text: str, *, patterns: tuple[SecretPattern, ...] | None = None, path: Path | str | None = None
) -> SanitiseResult:
    """Replace secrets in `text` with `[REDACTED:<type>]` and count them per type."""
    pats = patterns if patterns is not None else load_patterns(path)
    counts: Counter[str] = Counter()
    for pat in pats:
        text = _apply_pattern(text, pat, counts)
    text = _apply_detect_secrets(text, counts)
    return SanitiseResult(text=text, counts=dict(counts))


def hash_identity(login: str) -> str:
    """Return sha256(IDENTITY_SALT + login) as hex; raises if IDENTITY_SALT is unset."""
    salt = os.environ.get(SALT_ENV)
    if not salt:
        raise RuntimeError(
            f"{SALT_ENV} is not set. Put the team's shared salt in .env (see .env.example); "
            "there is deliberately no default."
        )
    return hashlib.sha256((salt + login).encode("utf-8")).hexdigest()
