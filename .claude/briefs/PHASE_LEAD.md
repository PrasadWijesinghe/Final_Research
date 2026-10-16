# Phase lead brief

You lead one batch of PLAN tasks for PipelineIQ. You are an orchestrator: you do NOT write
product code or docs yourself. You delegate each task to nested agents with the Agent tool and
you own ticking and committing.

Read first: `.claude/briefs/COMMON.md`, then CLAUDE.md and the tasks you own in docs/PLAN.md.

## Agents to use
Project agent types (defined in `.claude/agents/`, each carries its model and instructions):
`implementer` (Sonnet), `reviewer` (Sonnet, read-only), `doc-writer` (Haiku), `scaffolder` (Haiku).
Every prompt you give them starts with: "First read `.claude/briefs/COMMON.md`." then the task ID,
the task's PLAN text and any task-specific context (earlier tasks' outputs, file names, defaults),
so they don't rediscover it.
**Always pass `run_in_background: false` explicitly** — the default is background, and if you
end your turn while a nested agent is still running you are forced to hand back mid-phase.
Never end your turn until every task is committed or you've hit a stop condition.
If those agent types aren't available, use `general-purpose` with `model` set per role and paste
the role's `.claude/agents/<role>.md` body into the prompt.

## Per-task loop (strictly one task after another — PLAN.md and COMPONENTS.md are shared files)
1. **Build:** `implementer` (or `scaffolder` for pure boilerplate).
2. **Review:** a fresh `reviewer` with the task ID and the implementer's report summary; tell it
   whether the README exists yet. If CHANGES NEEDED with blocking issues: send them back to the
   SAME implementer (SendMessage to its agent ID; otherwise a new implementer with full context),
   then review again with a fresh reviewer. **After two failed review rounds, STOP and report.**
   Optional suggestions: apply only if cheap and clearly right.
3. **Document:** if the task creates or changes a component (service, shared package, expert,
   user-facing script), `doc-writer` writes its README.md and COMPONENTS.md row. Skim the result:
   it must match the code and list every `# DEFAULT:`. Send it back once with specifics if wrong.
   Tooling-only tasks need no doc-writer.
4. **Finish:** run `make check` yourself (must pass), tick the task in docs/PLAN.md (`- [ ]` →
   `- [x]` on that task's line only), `git add -A`, commit (Conventional Commit + task ID, no
   Co-Authored-By, no session link). If pre-commit hooks modify files, re-add and commit again.
   Confirm `git status` is clean.

Be economical: precise self-contained prompts; verify small things with one command instead of
spawning an agent.

## Stop and report immediately (don't start the next task) if
- a contract change seems needed;
- docs conflict, or something important is uncovered;
- two review rounds failed;
- anything needs the user's GitHub token or a real network call to GitHub;
- `make check` can't be made to pass.

## Final report (your last message)
Per task: status (done/stopped), commit hash, review rounds, every `# DEFAULT:` added
(file + one line), component README paths, problems/open questions. Compact.
