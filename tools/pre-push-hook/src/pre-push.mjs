// PipelineIQ pre-push hook. Asks the API for failure risks and prints them.
// RULE: the push is ALWAYS allowed, except when a person at a terminal answers "n".
// Any error, timeout, bad response or missing key prints one line and lets the push go on.
import { createReadStream, createWriteStream, closeSync, openSync } from "node:fs";
import { resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { loadConfig } from "./config.mjs";
import { formatReport, hasAlerts } from "./format.mjs";
import { ZERO_SHA, buildPayload, repoFromUrl } from "./git.mjs";

export const TIMEOUT_MS = 10_000; // ARCHITECTURE 3.1: 10 seconds total budget

/** Parse the lines git sends on stdin: "<local ref> <local sha> <remote ref> <remote sha>". */
export function parseRefs(text) {
  return text
    .split("\n")
    .map((l) => l.trim().split(/\s+/))
    .filter((p) => p.length === 4)
    .map(([localRef, localSha, remoteRef, remoteSha]) => ({ localRef, localSha, remoteRef, remoteSha }));
}

/** Ask "Push anyway?" on the terminal. Resolves null when there is no terminal. */
export function ttyPrompt(question) {
  return new Promise((resolve) => {
    let input;
    let output;
    let tty = -1;
    try {
      // stdin carries git's ref list, so the person is reached through the controlling terminal.
      const fd = openSync("/dev/tty", "r+");
      tty = fd;
      input = createReadStream("", { fd, autoClose: false });
      output = createWriteStream("", { fd, autoClose: false });
    } catch {
      resolve(null);
      return;
    }
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      input.destroy();
      output.destroy();
      try {
        closeSync(tty);
      } catch {
        // already closed
      }
      resolve(v);
    };
    input.on("error", () => finish(null));
    output.on("error", () => finish(null));
    const rl = createInterface({ input, output });
    rl.on("error", () => finish(null));
    rl.question(question, (a) => {
      rl.close();
      finish(/^y(es)?$/i.test(a.trim()));
    });
    // Closed without an answer (no terminal, Ctrl-D): treat as "no terminal" and allow the push.
    rl.on("close", () => finish(null));
  });
}

/**
 * Run the hook. Returns the process exit code (0 = push may go on).
 * Everything external is injectable so tests need no real terminal or network.
 */
export async function run({
  args = [],
  stdinText = "",
  env = process.env,
  home = env.HOME,
  cwd = process.cwd(),
  fetchImpl = globalThis.fetch,
  err = (s) => process.stderr.write(`${s}\n`),
  prompt = ttyPrompt,
  timeoutMs = TIMEOUT_MS,
} = {}) {
  try {
    if (env.PIPELINEIQ_SKIP === "1") return 0;
    const { apiKey, apiUrl } = await loadConfig(env, home);
    if (!apiKey) {
      err("PipelineIQ: no API key (set PIPELINEIQ_API_KEY or put api_key=... in ~/.pipelineiq); skipping check.");
      return 0;
    }
    const [remote = "origin", remoteUrl = ""] = args;
    // DEFAULT: only the first branch ref being pushed is checked (one request, one 10 s budget).
    const ref = parseRefs(stdinText).find(
      (r) => r.localSha !== ZERO_SHA && r.localRef.startsWith("refs/heads/"),
    );
    if (!ref) return 0;
    const repo = repoFromUrl(remoteUrl);
    if (!repo) {
      err("PipelineIQ: could not read owner/name from the remote URL; skipping check.");
      return 0;
    }
    const deadline = Date.now() + timeoutMs;
    const payload = await buildPayload({
      cwd,
      remote,
      repo,
      head: ref.localSha,
      remoteSha: ref.remoteSha,
      branch: ref.remoteRef.replace(/^refs\/heads\//, ""),
    });
    const left = deadline - Date.now();
    if (left <= 0) {
      err("PipelineIQ: unavailable (timed out); pushing anyway.");
      return 0;
    }
    const res = await fetchImpl(`${apiUrl.replace(/\/+$/, "")}/predict`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${apiKey}` },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(left),
    });
    if (!res.ok) {
      err(`PipelineIQ: unavailable (HTTP ${res.status}); pushing anyway.`);
      return 0;
    }
    const body = await res.json();
    for (const line of formatReport(body)) err(line);
    if (!hasAlerts(body)) return 0;
    const answer = await prompt("Push anyway? (y/n) ");
    if (answer === null) return 0; // no terminal: warn and continue
    return answer ? 0 : 1;
  } catch (e) {
    // The message never contains the key: it is only ever placed in a request header.
    const timedOut = e?.name === "TimeoutError" || e?.name === "AbortError";
    err(`PipelineIQ: unavailable (${timedOut ? "timed out" : "error"}); pushing anyway.`);
    return 0;
  }
}

async function readStdin() {
  if (process.stdin.isTTY) return "";
  let s = "";
  for await (const chunk of process.stdin) s += chunk;
  return s;
}

// Run only when executed directly (not when imported by tests).
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  // Safety net: whatever goes wrong, never block the push.
  process.on("uncaughtException", () => process.exit(0));
  const code = await run({ args: process.argv.slice(2), stdinText: await readStdin() });
  process.exit(code);
}
