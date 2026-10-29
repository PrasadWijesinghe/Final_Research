import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";

export function sh(cwd, ...args) {
  return execFileSync("git", args, { cwd, encoding: "utf8" }).trim();
}

/** A temp repo with a base commit and a second commit touching code, package.json and a workflow. */
export function makeRepo() {
  const dir = mkdtempSync(join(tmpdir(), "piq-hook-"));
  sh(dir, "init", "-q", "-b", "main");
  sh(dir, "config", "user.email", "t@example.com");
  sh(dir, "config", "user.name", "T");
  sh(dir, "config", "commit.gpgsign", "false");
  mkdirSync(join(dir, ".github/workflows"), { recursive: true });
  writeFileSync(join(dir, "a.js"), "console.log(1);\n");
  writeFileSync(join(dir, "package.json"), '{"name":"x"}\n');
  writeFileSync(join(dir, ".github/workflows/ci.yml"), "name: ci\non: push\njobs: {}\n");
  sh(dir, "add", "-A");
  sh(dir, "commit", "-q", "-m", "base");
  const base = sh(dir, "rev-parse", "HEAD");
  writeFileSync(join(dir, "a.js"), "console.log(2);\n");
  writeFileSync(join(dir, "package.json"), '{"name":"x","version":"2"}\n');
  writeFileSync(join(dir, "b.test.js"), "test('x',()=>{});\n");
  sh(dir, "add", "-A");
  sh(dir, "commit", "-q", "-m", "change");
  return { dir, base, head: sh(dir, "rev-parse", "HEAD") };
}

/** Fake API. `handler(req, body)` returns [status, json] or the string "hang". */
export async function fakeApi(handler) {
  const seen = [];
  const server = createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const parsed = body ? JSON.parse(body) : null;
      seen.push({ url: req.url, method: req.method, headers: req.headers, body: parsed });
      const out = handler(req, parsed);
      if (out === "hang") return; // never answers
      res.writeHead(out[0], { "content-type": "application/json" });
      res.end(JSON.stringify(out[1]));
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  return {
    url: `http://127.0.0.1:${server.address().port}`,
    seen,
    close: () => {
      server.closeAllConnections();
      return new Promise((r) => server.close(r));
    },
  };
}

export const riskyResponse = {
  context_id: "00000000-0000-0000-0000-000000000001",
  head_sha: "x",
  budget_ms: 10000,
  elapsed_ms: 50,
  results: [
    {
      expert: "test",
      state: "ready",
      latency_ms: 40,
      risks: [
        {
          expert: "test",
          scope: "commit",
          probability: 0.72,
          threshold: 0.5,
          alert: true,
          top_factors: [{ reason: "Changed test has an unawaited promise", path: "b.test.js", line: 1 }],
        },
      ],
    },
    { expert: "build", state: "not_ready", reason: "timeout", latency_ms: 10000, risks: [] },
  ],
};

export const cleanResponse = {
  ...riskyResponse,
  results: [{ expert: "test", state: "ready", latency_ms: 5, risks: [{ expert: "test", scope: "commit", probability: 0.1, threshold: 0.5, alert: false, top_factors: [] }] }],
};
