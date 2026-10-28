// Builds the /predict payload (PrePushPayload, services/features/src/features/pre_push.py)
// from the local repository using only the git CLI.
import { execFile } from "node:child_process";

export const ZERO_SHA = "0".repeat(40);
// DEFAULT: caps keep the request small; the API ignores content of non-config files anyway.
export const MAX_FILES = 200;
export const MAX_PATCH_BYTES = 1024 * 1024; // same limit as config/file_tags.yaml
export const MAX_CONTENT_BYTES = 200 * 1024;


// DEFAULT: the hook guesses "config file" by name; the API re-tags with config/file_tags.yaml.
const CONFIG_RES = [
  /(^|\/)package\.json$/,
  /(^|\/)tsconfig[^/]*\.json$/,
  /(^|\/)(jest|vitest|playwright|webpack|vite|rollup|babel|eslint)\.config\.[^/]+$/,
  /(^|\/)\.mocharc[^/]*$/,
  /(^|\/)\.babelrc[^/]*$/,
  /(^|\/)\.eslintrc[^/]*$/,
  /(^|\/)\.prettierrc[^/]*$/,
  /(^|\/)esbuild\.[^/]+$/,
  /(^|\/)Dockerfile[^/]*$/,
  /(^|\/)docker-compose[^/]*\.ya?ml$/,
  /^(vercel\.json|netlify\.toml|fly\.toml)$/,
];
const LOCKFILE_RE = /(^|\/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml)$/;
export const WORKFLOW_RE = /^\.github\/workflows\/[^/]+\.ya?ml$/;

export function isConfigFile(path) {
  return !LOCKFILE_RE.test(path) && (WORKFLOW_RE.test(path) || CONFIG_RES.some((r) => r.test(path)));
}

export function git(args, cwd, { allowFail = false } = {}) {
  return new Promise((resolve, reject) => {
    execFile("git", args, { cwd, maxBuffer: 64 * 1024 * 1024, encoding: "utf8" }, (err, stdout) => {
      if (err && !allowFail) reject(err);
      else resolve(err ? null : stdout);
    });
  });
}

/** Turn a remote URL into "owner/name", or null. */
export function repoFromUrl(url) {
  const m = /[:/]([^/:\s]+)\/([^/\s]+?)(?:\.git)?\/?$/.exec(url || "");
  return m ? `${m[1]}/${m[2]}` : null;
}

async function pickBase(cwd, head, remoteSha, remote) {
  if (remoteSha && remoteSha !== ZERO_SHA) {
    const ok = await git(["cat-file", "-e", `${remoteSha}^{commit}`], cwd, { allowFail: true });
    if (ok !== null) return remoteSha;
  }
  // New branch (or remote tip unknown locally): fork point from the remote's default branch.
  for (const ref of [`refs/remotes/${remote}/HEAD`, `refs/remotes/${remote}/main`, `refs/remotes/${remote}/master`]) {
    const mb = await git(["merge-base", head, ref], cwd, { allowFail: true });
    if (mb && mb.trim()) return mb.trim();
  }
  // DEFAULT: no known fork point, so the diff is just the head commit against its parent.
  const parent = await git(["rev-parse", "--verify", "-q", `${head}^`], cwd, { allowFail: true });
  return parent && parent.trim() ? parent.trim() : null;
}

function splitZ(s) {
  const parts = s.split("\0");
  if (parts.length && parts[parts.length - 1] === "") parts.pop();
  return parts;
}

async function changedFiles(cwd, base, head) {
  const statusOut = base
    ? await git(["diff", "-M", "--name-status", "-z", base, head], cwd)
    : await git(["diff-tree", "-r", "-M", "--name-status", "-z", "--root", head], cwd);
  const numOut = base
    ? await git(["diff", "-M", "--numstat", "-z", base, head], cwd)
    : await git(["diff-tree", "-r", "-M", "--numstat", "-z", "--root", head], cwd);
  const tokens = splitZ(statusOut);
  const entries = [];
  for (let i = 0; i < tokens.length; ) {
    const code = tokens[i++];
    if (code === "" ) continue;
    const letter = code[0];
    if (letter === "R" || letter === "C") {
      entries.push({ status: "renamed", previous_path: tokens[i], path: tokens[i + 1] });
      i += 2;
    } else {
      const path = tokens[i++];
      const status = letter === "A" ? "added" : letter === "D" ? "removed" : "modified";
      entries.push({ status, path, previous_path: null });
    }
  }
  // numstat -z: "add\tdel\tpath\0" or for renames "add\tdel\t\0old\0new\0"
  const nums = new Map();
  const nt = splitZ(numOut);
  for (let i = 0; i < nt.length; ) {
    const m = /^(\S+)\t(\S+)\t(.*)$/s.exec(nt[i++]);
    if (!m) continue;
    let path = m[3];
    if (path === "") {
      path = nt[i + 1];
      i += 2;
    }
    nums.set(path, { add: m[1] === "-" ? null : Number(m[1]), del: m[2] === "-" ? null : Number(m[2]) });
  }
  return entries.map((e) => ({ ...e, nums: nums.get(e.path) ?? { add: 0, del: 0 } }));
}

/**
 * Build the PrePushPayload for one pushed ref. Throws on git failures (the caller allows the push).
 */
export async function buildPayload({ cwd, remote, repo, head, remoteSha, branch }) {
  const base = await pickBase(cwd, head, remoteSha, remote);
  const all = await changedFiles(cwd, base, head);
  const entries = all.slice(0, MAX_FILES);
  const files = [];
  for (const e of entries) {
    let patch = null;
    if (e.nums.add !== null) {
      const args = base
        ? ["diff", "-M", base, head, "--", e.path]
        : ["show", "--format=", "-M", head, "--", e.path];
      const p = await git(args, cwd, { allowFail: true });
      if (p !== null && Buffer.byteLength(p) <= MAX_PATCH_BYTES) patch = p;
    }
    let content = null;
    if (e.status !== "removed" && isConfigFile(e.path)) {
      const c = await git(["show", `${head}:${e.path}`], cwd, { allowFail: true });
      if (c !== null && Buffer.byteLength(c) <= MAX_CONTENT_BYTES) content = c;
    }
    files.push({
      path: e.path,
      previous_path: e.previous_path,
      status: e.status,
      additions: e.nums.add ?? 0,
      deletions: e.nums.del ?? 0,
      patch,
      content,
    });
  }
  // Workflow files at head that this push did not touch.
  const changed = new Set(all.map((e) => e.path));
  const tree = (await git(["ls-tree", "-r", "--name-only", head, "--", ".github/workflows"], cwd, { allowFail: true })) ?? "";
  const workflows = [];
  for (const path of tree.split("\n").filter((p) => WORKFLOW_RE.test(p))) {
    if (changed.has(path)) continue;
    const c = await git(["show", `${head}:${path}`], cwd, { allowFail: true });
    if (c !== null && Buffer.byteLength(c) <= MAX_CONTENT_BYTES) workflows.push({ path, content: c });
  }
  const committedAt = (await git(["show", "-s", "--format=%cI", head], cwd)).trim();
  return {
    repo,
    head_sha: head,
    base_sha: base,
    branch,
    committed_at: committedAt,
    files,
    workflows,
  };
}
