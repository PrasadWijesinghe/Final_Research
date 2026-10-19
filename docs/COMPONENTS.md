# Components

A plain-language guide to every part of the system. Each component has a `README.md` in its
own folder, written from `docs/templates/COMPONENT_README.md`. Start here to understand
what exists and how far along it is.

| Component | What it does | Guide | Status |
|---|---|---|---|
| Contracts | The messages services send each other | `shared/contracts/README.md` | Working (v1.1) |
| Database layer | Describes the raw and platform tables and creates them with Alembic migrations | `shared/db/README.md` | Working (P0.6) |
| GitHub client | Reads the GitHub REST API and raw files with rate-limit waits, retries and date-window splitting | `shared/github/README.md` | Working (P1.1) |
| Collector | Loads and validates each member's repo list and decides which workflow files are relevant | `services/collector/README.md` | In progress (P1.2; backfill/daily/status/export come in P1.5-P1.7) |

<!-- Claude Code: add a row whenever a component gets its README, and keep Status current. -->
