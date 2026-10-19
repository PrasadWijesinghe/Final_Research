# Sanitiser

**In one sentence:** Removes secrets (tokens, keys, passwords, auth headers) from CI log text before anything is stored, and turns GitHub login names into salted hashes.

**Status:** Working · **Owner:** shared · **Plan tasks:** P1.4

## What it does

CI logs often contain secrets by accident. A failed step may print a GitHub token in a clone URL, an `Authorization` header in a curl call, or a whole private key in a debug dump. The collector must not store those. The sanitiser is the step that removes them.

It takes a block of text and replaces each secret with a label such as `[REDACTED:github_token]`. The label says what kind of secret was removed, and the rest of the text stays readable. It also counts how many secrets of each type it replaced. The collector stores those counts with each log, so the team can see how often secrets appear without seeing the secrets themselves.

The same package also hashes author and reviewer logins. A hash lets the team tell two commits came from the same person, without storing the GitHub login. The hash uses a team salt (a secret string added before hashing), so someone who only has the hashes cannot easily guess the logins.

## How it works

Every call to `sanitise` runs the same steps in the same order:

1. The custom patterns are loaded from `config/sanitise_patterns.yaml`. The file is read once per process and cached.
2. Each custom pattern (a regular expression, a text-matching rule) runs over the whole text, in the order listed in the file. Each match is replaced by its label.
3. detect-secrets runs over the text, one line at a time. detect-secrets is an open-source library with a detector for each cloud or service token format. Only provider-specific detectors are switched on (see below).
4. The result is returned as a `SanitiseResult`: the cleaned `text`, a `counts` dictionary (for example `{"github_token": 2}`), and a `total`.

```mermaid
flowchart LR
    A[Raw log text] --> B[Custom patterns<br/>from config YAML]
    B --> C[detect-secrets<br/>provider detectors, line by line]
    C --> D[Cleaned text + counts]
```

### Redaction labels

Every secret becomes `[REDACTED:<type>]`. The type is one of these:

| Type | What it catches |
|---|---|
| `github_token` | GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, and fine-grained `github_pat_`) |
| `npm_token` | npm access tokens that start with `npm_` |
| `aws_access_key` | AWS access key IDs (`AKIA…` or `ASIA…`) |
| `aws_secret_key` | A 40-character AWS secret key that sits next to the word "aws" and "secret" |
| `jwt` | JSON Web Tokens (three dot-separated base64 parts that start `eyJ`) |
| `private_key` | A whole PEM private key block, from `BEGIN … PRIVATE KEY` to `END`. A block with no `END` line is redacted to the end of the text |
| `auth_header` | The value after `Authorization:` (keeps the word `Bearer`, `Basic`, `token` or `digest`) |
| `url_credentials` | The `user:password` part of a URL such as `https://user:pass@host` |
| Provider types | Anything detect-secrets finds. The name is the detector's name in lower case with underscores, for example `slack_token` |

For most custom patterns the whole match is replaced. Patterns with a `group` replace only that part. For example, `Authorization: Bearer abc…` keeps `Authorization: Bearer ` and replaces only the token.

### Salted hashing

`hash_identity(login)` returns `sha256(IDENTITY_SALT + login)` as a 64-character hex string. The function refuses to run without a salt. It raises an error that names `IDENTITY_SALT`. It never falls back to a default salt, so a missing salt cannot produce hashes that everyone shares.

## How to run it

The sanitiser is a library. Other code imports it. To check it on your machine:

```bash
export PATH="$HOME/.local/bin:$PATH"
uv sync --all-packages
uv run pytest shared/tests/test_sanitise.py
```

Set `IDENTITY_SALT` in your `.env` file before you call `hash_identity` (copy the placeholder from `.env.example` and fill in the team's salt, shared privately). The tests set their own salt.

What you should see when it works: all tests in `test_sanitise.py` pass. Some of them run a 3-million-character input and check it finishes in under 30 seconds.

If the tests fail with an error about a missing `shared` module or an old behaviour, your installed copy of `shared` is stale. Reinstall it:

```bash
uv sync --all-packages --reinstall-package pipelineiq-shared
```

## Inputs and outputs

| | What | Where / format |
|---|---|---|
| In | Log text to clean | A Python `str` passed to `sanitise(text)` |
| In | Team salt for hashing | `IDENTITY_SALT` in `.env` |
| In | Custom pattern list | `config/sanitise_patterns.yaml`, or the file named by `SANITISE_PATTERNS_FILE` |
| Out | Cleaned text and counts | `SanitiseResult(text, counts, total)` |
| Out | Hashed login | A 64-character lowercase hex string from `hash_identity(login)` |

### Public functions

- `sanitise(text, *, patterns=None, path=None) -> SanitiseResult`. Cleans the text. Pass `patterns` to use your own list of `SecretPattern` objects, or `path` to load patterns from another YAML file. With neither, the default file is used.
- `hash_identity(login) -> str`. Returns the salted hash. Raises `RuntimeError` if `IDENTITY_SALT` is unset or empty.
- `load_patterns(path=None) -> tuple[SecretPattern, ...]`. Loads and compiles the custom patterns. Results are cached per path.

The module does not write to logs. The tests check that a secret never shows up in captured log output.

## Settings

| Setting | Where | Default | What it changes |
|---|---|---|---|
| `IDENTITY_SALT` | `.env` | None (required) | The salt for `hash_identity`. Without it, hashing fails. There is no default |
| `SANITISE_PATTERNS_FILE` | `.env` (commented out in `.env.example`) | Unset | Points to a different patterns YAML. If unset, the code searches up from its own folder for `config/sanitise_patterns.yaml` |
| `config/sanitise_patterns.yaml` | `config/` | Shipped file | The custom patterns, applied in order |

### Adding a pattern

Each entry in the `patterns:` list has these keys:

- `type` (required). The label used in `[REDACTED:<type>]` and in the counts.
- `pattern` (required). A Python regular expression.
- `group` (optional). The name of a capture group, written as `(?P<name>…)`. Only that group is replaced.
- `flags` (optional). A list of `ignorecase`, `dotall` or `multiline`.

Keep patterns simple. Avoid nested repeats such as `(a+)+`, which can take very long on long lines. The file says the same.

After you edit the YAML, restart the process. The patterns are cached, so a running process keeps the old list.

## Example

This test case shows a clone command with a password in the URL.

Input:

```
git clone https://deploy-bot:s3cretPass@github.com/acme/app.git
```

Output:

```
git clone https://[REDACTED:url_credentials]@github.com/acme/app.git
```

Counts: `{"url_credentials": 1}`. The user name and password are both gone. The host and path stay, so the log is still useful.

## Limitations and known issues

### Known gaps (not redacted today)

- **Short `Authorization` values.** Values shorter than 8 characters are not redacted.
- **`X-Api-Key` headers.** Not covered by any pattern.
- **JSON `SecretAccessKey`.** An AWS secret key written as JSON (`"SecretAccessKey": "…"`) is caught only if the word `aws` is on the same line. Otherwise it is missed.
- **Legacy npm tokens.** Old npm tokens in the form `_authToken=<uuid>` are not matched by the `npm_` pattern. Only the new `npm_` format is covered.
- **URL passwords with `/` or `@`.** The URL pattern stops at `/` and `@`, so a password that contains either is only partly redacted or not redacted.
- **Suffix text stays.** Only the token itself is replaced. Any text attached right after it, such as `_suffix`, is kept.

### Performance

- Roughly 2.3 seconds per megabyte of text, as measured for this component.
- detect-secrets skips lines that are shorter than 20 characters once stripped, and lines longer than 20,000 characters (including the newline). Secrets on those lines are not found by detect-secrets. Very long minified lines are the usual case.
- The custom patterns scan the whole text, including the long lines that detect-secrets skips.

### `# DEFAULT:` choices

Every default this component invented, from `shared/sanitise`:

1. **detect-secrets plugins** (`sanitiser.py`). Only the 20 provider-specific detectors run: Artifactory, AWS key, Azure Storage key, basic auth, Cloudant, Discord bot token, GitHub token, IBM Cloud IAM, IBM COS HMAC, JWT, Mailchimp, npm, OpenAI, private key, SendGrid, Slack, Softlayer, Square OAuth, Stripe and Twilio. The entropy detector (flags random-looking strings) and the keyword detector (flags words such as "password =") are off. Both would flag commit SHAs, UUIDs and cache keys, which are normal in CI logs.
2. **`MAX_DS_LINE_LENGTH = 20_000`** (`sanitiser.py`). detect-secrets skips lines longer than this, because it is slow on huge minified lines. The custom patterns still scan them.
3. **`MIN_DS_LINE_LENGTH = 20`** (`sanitiser.py`). detect-secrets skips lines shorter than this after stripping, because they cannot hold a supported secret.
4. **`SANITISE_PATTERNS_FILE` and the search path** (`patterns.py`). The env variable overrides the location. Otherwise the code searches the parent folders of the module for `config/sanitise_patterns.yaml`. This works from the repo and from an editable install. A normal (non-editable) install placed outside the repo will not find the file, and the code raises `FileNotFoundError` unless `SANITISE_PATTERNS_FILE` is set.

## Related

- Plan task: P1.4 in `docs/PLAN.md`.
- Data rules this implements: `docs/DATA.md` section 8 (privacy and security).
- Config file: `config/sanitise_patterns.yaml`.
- Env variables: `.env.example` (`IDENTITY_SALT`, `SANITISE_PATTERNS_FILE`).
- Callers: the collector stores the cleaned log text and the redaction counts (`docs/DATA.md` section 3, step 5). Commit author logins are stored as `author_hash` (`docs/DATA.md` section 4.1).
- Tests: `shared/tests/test_sanitise.py`, with fake secrets built by string concatenation so no scanner flags them.
