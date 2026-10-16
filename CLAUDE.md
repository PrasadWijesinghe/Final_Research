# CLAUDE.md

PipelineIQ: a 4-member research platform that predicts and diagnoses failures in Node.js
GitHub Actions pipelines. Python services; TypeScript dashboard and pre-push hook.

## Docs (open only what the task needs)

- `docs/PLAN.md`: tasks and acceptance criteria. Start here.
- `docs/CONVENTIONS.md`: read before writing code.
- `docs/ARCHITECTURE.md`: services, flows, endpoints, folders.
- `docs/DATA.md`: collector, database, tagging, labels, datasets.
- `shared/contracts/`: the only interface between services.
- `docs/experts/`: the expert interface; `test.md` for the Test expert.
- `docs/COMPONENTS.md`: what's built so far, with links to each component's guide.
- `docs/DECISIONS.md`: why things are the way they are.

## Rules

1. Don't change `shared/contracts/`. Propose changes instead.
2. Experts import only from `shared/`, never from each other.
3. No secrets in code, git, logs or LLM prompts.
4. `make check` must pass before a task is done.

## Working

One `docs/PLAN.md` task at a time. When done, explain it in the component's `README.md`
(plain language, see CONVENTIONS) and tick the task. If docs conflict, stop and ask.
Mark any default you invent with `# DEFAULT:`. End each session with an entry in `docs/AI_LOG.md`.

Commit messages: no `Co-Authored-By` trailer and no Claude session link. AI use is recorded in
`docs/AI_LOG.md` instead.

## Agents (`.claude/agents/`)

Delegate instead of doing the work yourself: `scaffolder` (Haiku) for boilerplate, `implementer` (Sonnet)
to build a task, `reviewer` (Sonnet) to check it, `doc-writer` (Haiku) to document it. Give each the task ID.
