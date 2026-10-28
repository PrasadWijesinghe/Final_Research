# Pre-push hook

A small Node.js script that runs when you type `git push`. It sends your unpushed changes to the
PipelineIQ API, prints any failure risks it gets back, and lets you decide whether to push.

**It never gets in your way.** If the API is down, slow (over 10 seconds), returns an error, or you
have no API key, the hook prints one line and the push goes ahead. The only time a push stops is
when a person at a terminal answers `n` to "Push anyway?", or when you turn on `block_when_no_tty`
(see below).

## What it sends

For the branch you are pushing (only the first branch if you push several; deletes and tags are
ignored):

- the diff of each changed file (up to 200 files; a patch over 1 MB is left out),
- the full content of changed config files (`package.json`, `tsconfig*.json`, jest/vitest/eslint/
  webpack/vite configs, Dockerfiles, workflows, and so on; lockfiles never),
- the workflow files you did not change,
- repo (`owner/name`, read from the remote URL), branch, commit SHA, remote tip SHA and commit time.

The API tags the files and scrubs secrets itself. Nothing is sent to GitHub. Your API key travels
only in the `Authorization` header and is never printed or logged.

## Install

You need Node 22 or newer and a personal API key (create one in the dashboard settings).

1. Save the key in `~/.pipelineiq/config` (`~/.pipelineiq` is a folder for all local PipelineIQ files):

   ```
   api_key=piq_...
   api_url=http://127.0.0.1:8000
   ```

   Because the file holds your key, run `chmod 600 ~/.pipelineiq/config`. An older single file
   `~/.pipelineiq` (not a folder) is still read.

   Or use the environment variables `PIPELINEIQ_API_KEY` and `PIPELINEIQ_API_URL`
   (the environment wins). `api_url` defaults to `http://127.0.0.1:8000`.
2. From inside the repo you want to protect:

   ```
   node /path/to/pipelineiq/tools/pre-push-hook/src/install.mjs
   ```

   This writes `.git/hooks/pre-push`. If that file already exists and is not ours, the installer
   refuses; add `--force` to replace it.

## Uninstall

```
node /path/to/pipelineiq/tools/pre-push-hook/src/install.mjs --uninstall
```

This removes the hook only if it is ours. Delete `.git/hooks/pre-push` by hand works too.

## Skipping it once

`PIPELINEIQ_SKIP=1 git push`, or the usual `git push --no-verify`.

## What you see

```
PipelineIQ: 1 risk flagged for this push
  ! test: 72% chance of failure (alert at 50%)
      - Changed test has an unawaited promise [b.test.js:1]
  (build: not ready, timeout)
Push anyway? (y/n)
```

The question appears only when a risk was flagged and a terminal is available. The hook reads your
answer from `/dev/tty` because git uses the hook's normal input for the list of refs. With no
terminal (CI, an editor's push button) the warning is printed and the push continues.

## Pushes with no terminal (GUI clients, AI agents)

Some pushes have nobody to ask: a Git app with a push button, or an AI agent running `git push`.
When the hook cannot open a terminal, after it gets the risk answer it:

1. **Shows a desktop notification** with a short summary (how many risks, the highest one and its
   top reason) and a link to the commit in the dashboard, `<dashboard_url>/#/commits/<sha>`.
   macOS uses `osascript`, Linux uses `notify-send`. On any other system, or if that program is
   missing, it quietly does nothing. A failed notification never stops or delays the push.
2. **Optionally blocks the push.** Off by default. Turn it on in `~/.pipelineiq/config`:

   ```
   block_when_no_tty=true
   dashboard_url=http://localhost:5173
   ```

   With `block_when_no_tty=true`, no terminal and at least one flagged risk, the hook exits with an
   error and prints plain text saying what risk was found, that the push was blocked because
   `block_when_no_tty` is on, and how to get past it: `git push --no-verify`, or set
   `block_when_no_tty=false`. That text is written so an AI agent can read it and act on it.

`dashboard_url` defaults to `http://localhost:5173`. The block only happens when the API answered
and flagged a risk. API down, timeout, missing key or any other error still lets the push go ahead.

## Run the tests

```
make check          # includes the hook tests
cd tools/pre-push-hook && npm test
```

The tests use Node's built-in test runner, real temporary git repos and a fake local HTTP server.
No real network. They cover risky, clean, timeout, backend down, HTTP errors, missing key, bad git
state and the installer.

## Files

- `src/pre-push.mjs`: the hook (`run()` holds the logic).
- `src/git.mjs`: builds the payload with the `git` command.
- `src/config.mjs`: reads the key, URLs and `block_when_no_tty`.
- `src/format.mjs`: turns the API answer into text.
- `src/notify.mjs`: the desktop notification (macOS and Linux).
- `src/install.mjs`: install and uninstall.

Payload shape: `PrePushPayload` in `services/features/src/features/pre_push.py`; endpoint:
`POST /predict` in `services/api`.
