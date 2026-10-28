#!/usr/bin/env node
// Installer. Usage: node install.mjs [--uninstall] [--force] [repo-path]
// DEFAULT: writes .git/hooks/pre-push directly (no husky: no dependencies, works in any repo).
import { chmod, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { git } from "./git.mjs";

export const MARKER = "# pipelineiq-pre-push-hook";

const here = dirname(fileURLToPath(import.meta.url));

export function hookScript(scriptPath = join(here, "pre-push.mjs")) {
  const q = scriptPath.replace(/'/g, `'\\''`);
  return `#!/bin/sh
${MARKER}
# Installed by tools/pre-push-hook. Remove with: node tools/pre-push-hook/src/install.mjs --uninstall
# If node is missing the push simply goes on.
command -v node >/dev/null 2>&1 || exit 0
[ -f '${q}' ] || exit 0
exec node '${q}' "$@"
`;
}

async function hookPath(repo) {
  const p = await git(["rev-parse", "--git-path", "hooks/pre-push"], repo);
  return resolve(repo, p.trim());
}

/** Install or remove the hook. Returns a message. Throws on a foreign hook unless force. */
export async function install({ repo = process.cwd(), uninstall = false, force = false } = {}) {
  const target = await hookPath(repo);
  let existing = null;
  try {
    existing = await readFile(target, "utf8");
  } catch {
    // none yet
  }
  const ours = existing !== null && existing.includes(MARKER);
  if (uninstall) {
    if (existing === null) return "No pre-push hook installed.";
    if (!ours) throw new Error(`${target} is not a PipelineIQ hook; leaving it alone.`);
    await rm(target);
    return `Removed ${target}`;
  }
  if (existing !== null && !ours && !force) {
    throw new Error(`${target} already exists and is not ours. Re-run with --force to replace it.`);
  }
  await mkdir(dirname(target), { recursive: true });
  await writeFile(target, hookScript());
  await chmod(target, 0o755);
  return `Installed ${target}`;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const argv = process.argv.slice(2);
  const repo = argv.find((a) => !a.startsWith("--")) ?? process.cwd();
  try {
    console.log(await install({ repo, uninstall: argv.includes("--uninstall"), force: argv.includes("--force") }));
  } catch (e) {
    console.error(e.message);
    process.exit(1);
  }
}
