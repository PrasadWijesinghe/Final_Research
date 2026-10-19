# AI use log

Record every session where an AI tool produced code, docs or analysis. This feeds the
AI-use disclosure in the final report.

| Date | Tool | Who | What it produced | How it was checked | Files |
|---|---|---|---|---|---|
| 2026-10-16 | Claude (claude.ai) | Member 3 | Planning: architecture, data design, decisions, plan, contracts v1.1 (Pydantic, 29 tests), starter configs, repo selection research | Contract tests run; decisions reviewed and agreed one by one by Member 3 | `CLAUDE.md`, `docs/*`, `shared/contracts/*`, `config/*` |
| 2026-10-19 | Claude Code (phase lead with implementer, reviewer and doc-writer agents) | Member 3 | Phase 1 data collection P1.2 to P1.7: repo config and workflow detection, re-run check script, sanitiser, collector backfill, daily and status, export and import, with READMEs | `make check` per task; independent reviewer agent per task; Docker integration tests written but not yet run | `services/collector/*`, `shared/sanitise/*`, `scripts/*`, `config/*`, `docs/*` |
