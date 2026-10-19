# Collector

**In one sentence:** The collector reads each team member's list of GitHub repositories, checks the list is valid, and decides which GitHub Actions workflow files matter for that member.

**Status:** partial · **Owner:** shared · **Plan tasks:** P1.2 (backfill, daily sync and status come in P1.5 to P1.6; export in P1.7)

## What it does

PipelineIQ learns from real CI failures on real GitHub repositories. Each of the four team members (dependency, build, test, infra) picks a set of repositories that fit their topic. Those lists live in `config/repos/<member>.yaml`.

The collector is the part that reads those lists and prepares the work. It does two jobs in this stage:

1. **Repo config loader.** It opens a member's YAML file, checks every field, and turns it into clean Python objects. If the file has a mistake, it stops with a message that names the file, the repository and the field.
2. **Workflow detection.** A GitHub repository can have many workflows (automated job files under `.github/workflows/`). Most are not relevant to a member. For example, the Test member only cares about workflows that run tests. Detection looks inside a workflow file and says whether it is relevant, and why.

Nothing is written to the database yet, and nothing is fetched from GitHub by this part. The backfill and daily sync that will use these results come in later tasks.

## How it works

### Repo config loader (`repo_config.py`)

1. Read the file as UTF-8 text. If it cannot be read, stop with "cannot read file".
2. Parse the YAML. YAML is a plain-text format for settings. The loader uses the safe parser, so a file cannot run code. If the YAML is broken, stop with "invalid YAML".
3. Check the top level. It must be a mapping (a list of `key: value` pairs). It must have a `member` field, and the value must be one of `dependency`, `build`, `test` or `infra`. It must match the file name: `config/repos/test.yaml` must say `member: test`.
4. Check the `repos` field. It must exist and be a list. Use `repos: []` for an empty list.
5. Check each repo entry, one at a time:
   - It must be a mapping.
   - `full_name` and `licence` are required text values. `full_name` must look like `owner/repo`.
   - `enabled` is optional and defaults to true. It must be a real YAML true or false. The text `'yes'` is rejected.
   - `workflows` is optional and defaults to an empty list. It must be a list of text values.
   - No two entries may name the same repository. The check ignores upper and lower case, because GitHub names do not care about case.
6. Keep any extra keys (for example `framework`, `flaky`, `note`) so they can be read later. They are not checked.

The loader stops at the first bad entry. It reports that one problem, so fixing one error may reveal the next.

### Workflow detection (`workflow_detection.py`)

Detection is driven by `config/workflow_detection.yaml`. That file holds, for each member, two lists of regular expressions. A regular expression (regex) is a pattern that matches text. `run_patterns` say which commands count. `exclude_patterns` say which commands do not count even if they match.

```mermaid
flowchart TD
    A[Workflow file text] --> B{Rule for this member?}
    B -- no --> K[Keep it: no rule for member]
    B -- yes --> C{Valid YAML mapping?}
    C -- no --> U[Not relevant: unparseable]
    C -- yes --> D[Collect every run: line, with its job]
    D --> E[Split each line on && & || ; and pipes]
    E --> F{Matches a run pattern and no exclude pattern?}
    F -- yes --> M[Relevant: matched]
    F -- no --> N[Not relevant: no match]
```

1. Find the member's rule in `config/workflow_detection.yaml`. If there is none, keep the workflow. This applies today to `dependency`, `build` and `infra`.
2. Parse the workflow file as YAML. If it is not valid, or its top level is not a mapping, mark it not relevant with reason "unparseable".
3. Walk every job and collect each line of every `run:` step, together with the job's key. A `run:` step is a shell command in a workflow. Steps that only use another action (`uses:`) are skipped. Blank lines are dropped.
4. Split each line into single commands. The splitting characters are `&&`, `&`, `||`, `;` and `|`. So `npm ci && npm test` becomes two commands, and each is judged on its own.
5. A command counts if at least one run pattern matches it and no exclude pattern matches it. "Matches" means the regex is found anywhere in the command.
6. If any command counts, the file is relevant, with reason "matched". The result lists each (job, command) pair that counted. If none counts, the reason is "no match".

Today the Test member's rules are:

- **Run patterns:** `npm`, `pnpm`, `yarn` or `bun` running `test` (including `npm run test:unit` and `pnpm -r test`); `turbo`, `nx` or `lerna` running a test target; and the runners `jest`, `mocha` or `vitest` themselves.
- **Exclude patterns:** installing or removing packages (`npm install`, `yarn add`, `npm ci` and similar), and `echo` or `printf` lines that only mention a runner.

## How to run it

```bash
# run the collector's tests (from the repository root)
uv run pytest services/collector/tests

# the CLI exists but does nothing yet except parse arguments
uv run collector --help
```

When the tests pass, pytest prints a line ending in `passed` and no failures. `collector --help` prints the usage text with the program name `collector` and the description "PipelineIQ data collector". The subcommands `backfill`, `daily` and `status` are not there yet.

The full check that must pass before a task is done is `make check`. It needs Docker only for the database tests, not for this part.

## Inputs and outputs

|     | What | Where / format |
|---|---|---|
| In | One member's repo list | `config/repos/<member>.yaml` (YAML) |
| In | Which workflows count as relevant, per member | `config/workflow_detection.yaml` (YAML of regex lists) |
| In | A workflow file to check | Text of a `.github/workflows/*.yml` file, passed in by the caller |
| Out | Validated repo list | `RepoConfig` object, with a list of `RepoEntry` objects |
| Out | Relevance decision for one workflow | `DetectionResult`: `relevant` (true or false), `reason` (text) and `matches` (list of job and command pairs) |
| Out | Error | `ConfigError`, a kind of `ValueError`, whose message gives the file path, repo and field |

## Settings

| Setting | Where | Default | What it changes |
|---|---|---|---|
| Member names | `MEMBERS` in `repo_config.py` | `dependency`, `build`, `test`, `infra` | Which member names are accepted |
| Config folder | Argument `config_dir` of `load_member_config` and `load_all_repo_configs` | The `config/` folder of the source checkout | Where the repo lists are read from |
| Detection rules file | Argument `path` of `load_detection_rules` | `config/workflow_detection.yaml` in the checkout | Which commands count as relevant, per member |
| `full_name` | Each repo entry | Required | The GitHub repository, as `owner/repo` |
| `licence` | Each repo entry | Required | The repository's licence, as text |
| `enabled` | Each repo entry | `true` | Whether `enabled_repos()` includes the repo |
| `workflows` | Each repo entry | Empty list (auto-detect) | Explicit workflow paths. Read by the loader only; used by the backfill in P1.5 |
| Run and exclude patterns | `config/workflow_detection.yaml`, per member | Test member has rules; others have none | Which commands make a workflow relevant |

## Example

From the test suite. The Mongoose project has a workflow file. Its `test` job has these steps:

```yaml
jobs:
  test:
    steps:
      - run: npm install
      - name: Run tests
        run: npm test
```

Detection for the `test` member answers:

- `relevant`: true
- `reason`: matched
- `matches`: one pair, job `test` and command `npm test`

The `npm install` line is not a match. Installing packages is excluded.

A repo entry in `config/repos/test.yaml` looks like this:

```yaml
- {full_name: Automattic/mongoose, licence: MIT, framework: mocha, flaky: 14}
```

It loads with `licence` set to `MIT`, `enabled` set to true, `workflows` set to an empty list, and `framework` and `flaky` kept as extras.

If the licence is missing, the loader reports something like this, which names the file, the repository and the field:

```
config/repos/test.yaml: repo 'a/b': field 'licence': <reason from the validator>
```

## Limitations and known issues

**Not built yet**

- The CLI (`cli.py`) only parses arguments. It has no `backfill`, `daily`, `status` or export commands. Those come in P1.5 to P1.7.
- Nothing stores detection results yet. The plan says they are stored per workflow file version (`detected_at_sha`) by the backfill in P1.5.
- The `workflows` list is loaded but not used. Auto-detect is not yet wired to the repo list.
- Only `run:` steps are read. Jobs that only call a reusable workflow (`uses:`) are not looked inside. A job's own name is not matched either; only its shell commands are.
- Detection only sees the workflow text it is given. It does not fetch files from GitHub.
- Detection reads the rules file from disk if no rules object is passed in. Callers that check many workflows should load the rules once and pass them in.

**Behaviour to know about**

- A workflow that cannot be parsed is marked not relevant for members with a rule. For members with no rule, it is kept, because the no-rule check runs first.
- Loading all four configs with `load_all_repo_configs` stops at the first file that fails.
- Only the first bad repo entry in a file is reported.
- The loader does not check that a repository exists on GitHub or that its licence is correct. The test list notes that licences are "best-known values" to be confirmed later.
- `config/workflow_detection.yaml` currently has uncommitted changes on this branch. This guide describes the file as it is in the working copy.

**Every `# DEFAULT:` in this component**

- `services/collector/src/collector/repo_config.py`, line 15: `DEFAULT_CONFIG_DIR` is found from the location of the source file, four folders up to the repository root, then `config/`. This works in a source checkout. A packaged install would point to the wrong place, so callers should pass `config_dir` explicitly outside a checkout.
- `services/collector/src/collector/workflow_detection.py`, line 98: members with no detection rule (`dependency`, `build`, `infra`) keep every workflow until their owner adds a rule. Reason: keeping a workflow is safer than dropping data that may matter.

`config/workflow_detection.yaml` has no `# DEFAULT:` comments.

## Related

- Plan: `docs/PLAN.md`, task P1.2 (this part), P1.5 (backfill), P1.6 (daily and status), P1.7 (export).
- Design: `docs/DATA.md` section 2 (repo lists and auto-detection). Section 3 (the collector).
- Config files: `config/repos/*.yaml`, `config/workflow_detection.yaml`.
- Tests: `services/collector/tests/test_repo_config.py`, `services/collector/tests/test_workflow_detection.py`, `services/collector/tests/test_cli.py`.
- The GitHub client that will fetch workflow files: `shared/github/README.md`.
