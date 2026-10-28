import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, describe, it } from "node:test";
import { run } from "../src/pre-push.mjs";
import { cleanResponse, fakeApi, makeRepo, riskyResponse } from "./helpers.mjs";

const KEY = "piq_fake_test_key_not_real";
let repo;
before(() => {
  repo = makeRepo();
});

function setup(api, extra = {}) {
  const lines = [];
  const prompts = [];
  const home = mkdtempSync(join(tmpdir(), "piq-home-"));
  const stdinText = `refs/heads/feat ${repo.head} refs/heads/feat ${repo.base}\n`;
  return {
    lines,
    prompts,
    opts: {
      args: ["origin", "git@github.com:acme/widgets.git"],
      stdinText,
      env: { PIPELINEIQ_API_KEY: KEY, PIPELINEIQ_API_URL: api?.url ?? "http://127.0.0.1:1", HOME: home },
      home,
      cwd: repo.dir,
      err: (s) => lines.push(s),
      hasTty: () => true, // tests never touch the real terminal or desktop
      notify: async () => false,
      prompt: async (q) => {
        prompts.push(q);
        return extra.answer ?? null;
      },
      ...extra.opts,
    },
  };
}

describe("pre-push hook", () => {
  it("risky: sends payload, prints risks, asks, blocks on n", async () => {
    const api = await fakeApi(() => [200, riskyResponse]);
    const t = setup(api, { answer: false });
    const code = await run(t.opts);
    await api.close();
    assert.equal(code, 1);
    assert.equal(t.prompts.length, 1);
    const out = t.lines.join("\n");
    assert.match(out, /1 risk flagged/);
    assert.match(out, /72%/);
    assert.match(out, /unawaited promise/);
    assert.match(out, /build: not ready, timeout/);
    const req = api.seen[0];
    assert.equal(req.url, "/predict");
    assert.equal(req.headers.authorization, `Bearer ${KEY}`);
    const b = req.body;
    assert.equal(b.repo, "acme/widgets");
    assert.equal(b.head_sha, repo.head);
    assert.equal(b.base_sha, repo.base);
    assert.equal(b.branch, "feat");
    const pj = b.files.find((f) => f.path === "package.json");
    assert.match(pj.content, /"version":"2"/);
    assert.match(pj.patch, /^diff --git/);
    assert.equal(b.files.find((f) => f.path === "a.js").content, null);
    assert.equal(b.files.find((f) => f.path === "b.test.js").status, "added");
    assert.deepEqual(b.workflows.map((w) => w.path), [".github/workflows/ci.yml"]);
    assert.ok(!out.includes(KEY));
  });

  it("risky and user answers y: push allowed", async () => {
    const api = await fakeApi(() => [200, riskyResponse]);
    const t = setup(api, { answer: true });
    assert.equal(await run(t.opts), 0);
    await api.close();
  });

  it("risky without a terminal: warns and allows", async () => {
    const api = await fakeApi(() => [200, riskyResponse]);
    const t = setup(api, { answer: null });
    assert.equal(await run(t.opts), 0);
    await api.close();
    assert.match(t.lines.join("\n"), /flagged/);
  });

  it("clean: no prompt, allowed", async () => {
    const api = await fakeApi(() => [200, cleanResponse]);
    const t = setup(api, { answer: false });
    assert.equal(await run(t.opts), 0);
    await api.close();
    assert.equal(t.prompts.length, 0);
    assert.match(t.lines.join("\n"), /no risks flagged/);
  });

  it("timeout: allowed", async () => {
    const api = await fakeApi(() => "hang");
    const t = setup(api, { answer: false, opts: { timeoutMs: 400 } });
    const t0 = Date.now();
    assert.equal(await run(t.opts), 0);
    await api.close();
    assert.ok(Date.now() - t0 < 5000);
    assert.match(t.lines.join("\n"), /timed out/);
    assert.equal(t.prompts.length, 0);
  });

  it("backend down (connection refused): allowed", async () => {
    const t = setup(null, { answer: false });
    assert.equal(await run(t.opts), 0);
    assert.match(t.lines.join("\n"), /unavailable/);
  });

  it("backend 500 and 401: allowed", async () => {
    for (const status of [500, 401]) {
      const api = await fakeApi(() => [status, { detail: "no" }]);
      const t = setup(api);
      assert.equal(await run(t.opts), 0);
      await api.close();
      assert.match(t.lines.join("\n"), new RegExp(`HTTP ${status}`));
    }
  });

  it("garbage response body: allowed", async () => {
    const api = await fakeApi(() => [200, "not an object"]);
    const t = setup(api);
    assert.equal(await run(t.opts), 0);
    await api.close();
  });

  it("missing key: allowed, no request made", async () => {
    const api = await fakeApi(() => [200, riskyResponse]);
    const t = setup(api);
    t.opts.env = { HOME: t.opts.home };
    assert.equal(await run(t.opts), 0);
    await api.close();
    assert.equal(api.seen.length, 0);
    assert.match(t.lines.join("\n"), /no API key/);
  });

  it("reads key and url from ~/.pipelineiq", async () => {
    const api = await fakeApi(() => [200, cleanResponse]);
    const t = setup(api);
    writeFileSync(join(t.opts.home, ".pipelineiq"), `# comment\napi_key=${KEY}\napi_url=${api.url}\n`);
    t.opts.env = { HOME: t.opts.home };
    assert.equal(await run(t.opts), 0);
    await api.close();
    assert.equal(api.seen[0].headers.authorization, `Bearer ${KEY}`);
  });

  it("git failure (bad sha): allowed", async () => {
    const api = await fakeApi(() => [200, riskyResponse]);
    const t = setup(api, { answer: false });
    t.opts.stdinText = `refs/heads/feat ${"a".repeat(40)} refs/heads/feat ${"b".repeat(40)}\n`;
    assert.equal(await run(t.opts), 0);
    await api.close();
    assert.equal(api.seen.length, 0);
  });

  it("branch delete and tag pushes are ignored", async () => {
    const t = setup(null);
    t.opts.stdinText = `(delete) ${"0".repeat(40)} refs/heads/x ${repo.base}\nrefs/tags/v1 ${repo.head} refs/tags/v1 ${"0".repeat(40)}\n`;
    assert.equal(await run(t.opts), 0);
    assert.equal(t.lines.length, 0);
  });

  it("new branch: base falls back to parent commit", async () => {
    const api = await fakeApi(() => [200, cleanResponse]);
    const t = setup(api);
    t.opts.stdinText = `refs/heads/new ${repo.head} refs/heads/new ${"0".repeat(40)}\n`;
    assert.equal(await run(t.opts), 0);
    await api.close();
    assert.equal(api.seen[0].body.base_sha, repo.base);
  });

  it("never prints the key even when the request fails", async () => {
    const t = setup(null);
    await run(t.opts);
    assert.ok(!t.lines.join("\n").includes(KEY));
  });
});
