# Build log fixtures

How these files were made, so nobody mistakes them for downloaded CI logs.

**Tool output is real.** On 22 Oct 2026 a small TypeScript project with deliberate mistakes was
built with TypeScript 5, ESLint 9 (typescript-eslint), Prettier 3, Vite 6, webpack 5, esbuild,
Vitest 3, npm 10 and pnpm 9, and each tool's output was captured unchanged (the npm ERESOLVE
error came from a real dependency conflict while installing those tools; the out-of-memory crash
from Node 22 with a tiny heap limit).

**The GitHub framing is added.** Each output was wrapped in the shape of a GitHub Actions job
log: a timestamp on every line, `##[group]Run <command>` / `##[endgroup]` around each step's
header, setup steps before it, `##[error]Process completed with exit code N.` after a failing
step, and clean-up lines at the end. The local checkout path was replaced by the runner path
`/home/runner/work/acme-web/acme-web`. Some webpack resolver trace lines are kept as printed.

| File | Failing step | Tool output |
|---|---|---|
| `tsc_typecheck_fail.log` | `pnpm run typecheck` (after a passing lint step with 2 warnings) | `tsc -p tsconfig.json`, 7 errors |
| `tsc_pretty_build_fail.log` | `npm run build` | `tsc --pretty` with colour codes |
| `eslint_fail.log` | `pnpm run lint` | ESLint stylish, 4 errors and 1 warning, pnpm's ELIFECYCLE line |
| `eslint_max_warnings.log` | `npm run lint:ci` | warnings only, `--max-warnings 0` exceeded |
| `prettier_fail.log` | `npm run format:check` | `prettier --check`, 2 files |
| `vite_build_fail.log` | `npm run build` | Vite 6 / Rollup unresolved import |
| `webpack_build_fail.log` | `npm run build:wp` | webpack 5 `Module not found` |
| `esbuild_fail.log` | `npx esbuild …` | esbuild unresolved import |
| `lint_pass_with_warnings.log` | none (passing job) | ESLint with 2 warnings, exit code 0 |
| `heap_oom_build.log` | `npm run build` | Node heap out of memory (not a build failure) |
| `vitest_test_fail.log` | `pnpm exec vitest run` | a failing Vitest test (not a build failure) |
| `dependency_eresolve.log` | `npm install` | npm ERESOLVE (not a build failure) |
| `oxfmt_check_fail.log` | `pnpm run format:check` | **Real CI log** (langchain-ai/langchainjs, 30 Sep 2026): `oxfmt --check .`, 1 file |
| `custom_lint_script_fail.log` | `pnpm lint:framework-vocabulary` | **Real CI log** (prisma/prisma, 18 Sep 2026), trimmed to the preamble and the failing step: a repo-specific lint script, no known tool |

The two real logs come from the first backfill, already sanitised by the collector. Both were
failures the first dataset could not place: oxfmt output was not recognised (now it is), and the
custom script is correctly a build failure with no known tool. Add more real logs as they turn up.
