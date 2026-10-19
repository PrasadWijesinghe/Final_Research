"""Tests for shared.sanitise. Fake secrets are built by concatenation (scanner-safe)."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from shared.sanitise import hash_identity, load_patterns, sanitise
from shared.sanitise.patterns import parse_patterns

FIXTURES = Path(__file__).parent / "fixtures" / "clean_logs"

_A36 = "a1B2c3D4e5F6" * 3  # 36 alphanumerics
FAKE_GH = {
    "ghp": "gh" + "p_" + _A36,
    "gho": "gh" + "o_" + _A36,
    "ghu": "gh" + "u_" + _A36,
    "ghs": "gh" + "s_" + _A36,
    "ghr": "gh" + "r_" + _A36,
    "pat": "github" + "_pat_" + "11ABCDEFG0" + "abcdefghijklmnopqrstuv_wxyz0123456789ABCDEFGHIJ",
}
FAKE_NPM = "npm" + "_" + _A36
FAKE_AKIA = "AKIA" + "FAKEFAKEFAKE1234"
FAKE_ASIA = "ASIA" + "FAKEFAKEFAKE1234"
FAKE_AWS_SECRET = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCY" + "FAKEFAKEKE"
_B64 = "abcdefghijklmnop"
FAKE_JWT = "ey" + "J" + _B64 + "." + "ey" + "J" + _B64 + "." + "SflKxwRJSMeKKF2QT4fwpM"
FAKE_PEM = (
    "-----BEGIN " + "RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEAfakefakefakefakefakefakefakefake\n"
    "fakefakefakefakefakefakefakefakefakefakefakefake\n"
    "-----END " + "RSA PRIVATE KEY-----"
)


def _only(text: str) -> tuple[str, dict[str, int]]:
    res = sanitise(text)
    return res.text, res.counts


@pytest.mark.parametrize("kind", list(FAKE_GH))
def test_github_tokens(kind: str) -> None:
    token = FAKE_GH[kind]
    out, counts = _only(f"cloning with {token} now")
    assert token not in out
    assert out == "cloning with [REDACTED:github_token] now"
    assert counts == {"github_token": 1}


def test_npm_token() -> None:
    out, counts = _only(f"//registry.npmjs.org/:_authToken={FAKE_NPM}")
    assert FAKE_NPM not in out
    assert "[REDACTED:npm_token]" in out
    assert counts["npm_token"] == 1


@pytest.mark.parametrize("key", [FAKE_AKIA, FAKE_ASIA])
def test_aws_access_key(key: str) -> None:
    out, counts = _only(f"AWS_ACCESS_KEY_ID={key}")
    assert key not in out
    assert counts == {"aws_access_key": 1}


def test_aws_secret_key() -> None:
    assert len(FAKE_AWS_SECRET) == 40
    out, counts = _only(f"aws_secret_access_key = {FAKE_AWS_SECRET}")
    assert FAKE_AWS_SECRET not in out
    assert out == "aws_secret_access_key = [REDACTED:aws_secret_key]"
    assert counts == {"aws_secret_key": 1}


def test_jwt() -> None:
    out, counts = _only(f"session={FAKE_JWT}; path=/")
    assert FAKE_JWT not in out
    assert out == "session=[REDACTED:jwt]; path=/"
    assert counts == {"jwt": 1}


def test_private_key_block_with_log_prefixes() -> None:
    prefixed = "\n".join("2026-03-10T08:00:00.000Z " + ln for ln in FAKE_PEM.split("\n"))
    out, counts = _only("before\n" + prefixed + "\nafter")
    assert "fakefake" not in out
    assert "before" in out and "after" in out
    assert counts == {"private_key": 1}


def test_private_key_truncated_block() -> None:
    text = "x\n-----BEGIN " + "PRIVATE KEY-----\nMIIfakefakefake\n"
    out, counts = _only(text)
    assert "MIIfake" not in out
    assert counts == {"private_key": 1}


@pytest.mark.parametrize(
    "line,keep",
    [
        ("Authorization: Bearer abcdef1234567890xyz", "Authorization: Bearer "),
        ("authorization: token abcdef1234567890xyz", "authorization: token "),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "Authorization: Basic "),
        ('-H "Authorization: Bearer abcdef1234567890xyz"', '-H "Authorization: Bearer '),
        ("Authorization: abcdef1234567890xyz", "Authorization: "),
    ],
)
def test_authorization_header(line: str, keep: str) -> None:
    out, counts = _only(line)
    assert "abcdef1234567890xyz" not in out and "dXNlcjpw" not in out
    assert out.startswith(keep + "[REDACTED:auth_header]")
    assert counts == {"auth_header": 1}


def test_authorization_already_masked_is_untouched() -> None:
    out, counts = _only("Authorization: Bearer ***")
    assert out == "Authorization: Bearer ***"
    assert counts == {}


def test_url_credentials() -> None:
    out, counts = _only("git clone https://deploy-bot:s3cretPass@github.com/acme/app.git")
    assert "s3cretPass" not in out and "deploy-bot" not in out
    assert out == "git clone https://[REDACTED:url_credentials]@github.com/acme/app.git"
    assert counts == {"url_credentials": 1}


def test_detect_secrets_catches_extra_provider() -> None:
    slack = "xox" + "b-" + "123456789012-1234567890123-" + "abcdefghijklmnopqrstuvwx"
    out, counts = _only(f"SLACK_TOKEN={slack}")
    assert slack not in out
    assert sum(counts.values()) == 1


def test_counts_multiple_and_mixed() -> None:
    text = f"{FAKE_GH['ghp']} and {FAKE_GH['gho']}\n{FAKE_AKIA}\n{FAKE_JWT}\n"
    res = sanitise(text)
    assert res.counts == {"github_token": 2, "aws_access_key": 1, "jwt": 1}
    assert res.total == 4


def test_idempotent() -> None:
    once = sanitise(f"x {FAKE_GH['ghp']} Authorization: Bearer abcdef1234567890xyz")
    twice = sanitise(once.text)
    assert twice.text == once.text and twice.counts == {}


def test_secrets_are_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("DEBUG"):
        sanitise(f"token {FAKE_GH['ghp']}")
    assert FAKE_GH["ghp"] not in caplog.text


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.log")), ids=lambda p: p.stem)
def test_no_false_positives_on_clean_logs(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    res = sanitise(text)
    assert res.counts == {}
    assert res.text == text


def test_ten_clean_logs_exist() -> None:
    assert len(list(FIXTURES.glob("*.log"))) >= 10


def test_large_input_is_fast() -> None:
    lines = [
        f"2026-03-10T08:00:{i % 60:02d}Z npm http fetch GET 200 https://registry.npmjs.org/pkg{i}"
        f" 12ms sha512-{'A1b2' * 22}== key {hashlib.sha1(str(i).encode()).hexdigest()}"
        for i in range(30_000)
    ]
    big = "\n".join(lines)
    assert len(big) > 3_000_000
    start = time.perf_counter()
    res = sanitise(big)
    assert time.perf_counter() - start < 30
    assert res.counts == {}


@pytest.mark.parametrize(
    "chunk",
    [
        "a" * 2_000_000,
        "a:" * 1_000_000,
        "://" + "a:" * 500_000,
        "eyJ" + "a" * 1_000_000,
        "Authorization" + " " * 1_000_000,
        "aws secret " + "A" * 1_000_000,
        "-----BEGIN " + "PRIVATE KEY-----" + "x" * 1_000_000,
        "ghp_" + "a" * 1_000_000,
        "npm_" + "a" * 1_000_000,
        "AKIA" + "A" * 1_000_000,
        "A" * 500_000 + "=" * 500_000,
    ],
    ids=lambda c: c[:12],
)
def test_no_catastrophic_backtracking(chunk: str) -> None:
    start = time.perf_counter()
    sanitise(chunk)
    assert time.perf_counter() - start < 20


def test_hash_identity_uses_salt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IDENTITY_SALT", "pepper")
    assert hash_identity("octocat") == hashlib.sha256(b"pepperoctocat").hexdigest()
    monkeypatch.setenv("IDENTITY_SALT", "other")
    assert hash_identity("octocat") != hashlib.sha256(b"pepperoctocat").hexdigest()


@pytest.mark.parametrize("value", [None, ""])
def test_hash_identity_requires_salt(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("IDENTITY_SALT", raising=False)
    else:
        monkeypatch.setenv("IDENTITY_SALT", value)
    with pytest.raises(RuntimeError, match="IDENTITY_SALT"):
        hash_identity("octocat")


def test_patterns_file_found_and_overridable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert {p.type for p in load_patterns()} >= {"github_token", "jwt", "private_key"}
    custom = tmp_path / "p.yaml"
    custom.write_text("patterns:\n  - type: demo\n    pattern: 'zzz[0-9]+'\n", encoding="utf-8")
    monkeypatch.setenv("SANITISE_PATTERNS_FILE", str(custom))
    from shared.sanitise.patterns import find_patterns_file

    assert find_patterns_file() == custom
    assert sanitise("a zzz123 b", path=custom).text.startswith("a [REDACTED:demo]")


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {"patterns": []},
        {"patterns": [{"type": "x"}]},
        {"patterns": [{"type": "x", "pattern": "("}]},
        {"patterns": [{"type": "x", "pattern": "a", "group": "nope"}]},
        {"patterns": [{"type": "x", "pattern": "a", "flags": ["bogus"]}]},
    ],
)
def test_invalid_pattern_config(raw: object) -> None:
    with pytest.raises(ValueError):
        parse_patterns(raw, "test")


@pytest.mark.parametrize(
    "token,body",
    [
        ("gh" + "p_" + "a" * 36 + "_suffix", "a" * 36),
        ("gh" + "s_" + "a" * 300, "a" * 40),
        ("github" + "_pat_" + "a" * 22 + "_" + "b" * 60 + "_x", "b" * 20),
        ("npm" + "_" + "a" * 36 + "_x", "a" * 36),
        ("npm" + "_" + "a" * 300, "a" * 40),
        ("AKIA" + "A" * 16 + "_x", "A" * 16),
        ("ASIA" + "B" * 16 + "_tail", "B" * 16),
    ],
)
def test_token_with_suffix_or_long_body_does_not_leak(token: str, body: str) -> None:
    out = sanitise(f"x {token} y").text
    assert body not in out
    assert "[REDACTED:" in out


def test_blank_and_short_lines_pass_through() -> None:
    text = "\n\nok\n   \nshort line\n"
    res = sanitise(text)
    assert res.text == text and res.counts == {}
