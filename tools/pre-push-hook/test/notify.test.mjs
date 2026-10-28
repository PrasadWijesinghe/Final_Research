import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, describe, it } from "node:test";
import { appleScriptEscape, notificationCommand, sendNotification } from "../src/notify.mjs";
import { run } from "../src/pre-push.mjs";
import { cleanResponse, fakeApi, makeRepo, riskyResponse } from "./helpers.mjs";

describe("notification command", () => {
  it("macOS uses osascript with escaped quotes and no shell", () => {
    const c = notificationCommand("darwin", "PipelineIQ", 'say "hi" \\ now\nline');
    assert.equal(c.file, "osascript");
    assert.deepEqual(c.args.slice(0, 1), ["-e"]);
    assert.equal(
      c.args[1],
      'display notification "say \\"hi\\" \\\\ now line" with title "PipelineIQ"',
    );
    assert.equal(appleScriptEscape('a"b'), 'a\\"b');
  });
  it("linux uses notify-send with title and body as args", () => {
    assert.deepEqual(notificationCommand("linux", "T", "body"), { file: "notify-send", args: ["T", "body"] });
  });
  it("other platforms are skipped", async () => {
    assert.equal(notificationCommand("win32", "T", "b"), null);
    let called = false;
    assert.equal(await sendNotification("T", "b", { platform: "win32", exec: () => (called = true) }), false);
    assert.equal(called, false);
  });
  it("sendNotification calls exec with the command per platform", async () => {
    const calls = [];
    const exec = (file, args, opts, cb) => {
      calls.push([file, args]);
      cb(null);
    };
    assert.equal(await sendNotification("T", "b", { platform: "darwin", exec }), true);
    assert.equal(await sendNotification("T", "b", { platform: "linux", exec }), true);
    assert.deepEqual(calls.map((c) => c[0]), ["osascript", "notify-send"]);
  });
  it("missing binary or throwing exec resolves false", async () => {
    const missing = (f, a, o, cb) => cb(new Error("ENOENT"));
    assert.equal(await sendNotification("T", "b", { platform: "linux", exec: missing }), false);
    const boom = () => {
      throw new Error("x");
    };
    assert.equal(await sendNotification("T", "b", { platform: "linux", exec: boom }), false);
  });
});

describe("hook without a terminal", () => {
  let repo;
  before(() => {
    repo = makeRepo();
  });
  function setup(api, { conf = "", opts = {} } = {}) {
    const lines = [];
    const notes = [];
    const home = mkdtempSync(join(tmpdir(), "piq-home-"));
    if (conf) writeFileSync(join(home, ".pipelineiq"), conf);
    return {
      lines,
      notes,
      opts: {
        args: ["origin", "git@github.com:acme/widgets.git"],
        stdinText: `refs/heads/feat ${repo.head} refs/heads/feat ${repo.base}\n`,
        env: { PIPELINEIQ_API_KEY: "piq_fake", PIPELINEIQ_API_URL: api?.url ?? "http://127.0.0.1:1", HOME: home },
        home,
        cwd: repo.dir,
        err: (s) => lines.push(s),
        hasTty: () => false,
        notify: async (title, msg) => {
          notes.push([title, msg]);
          return true;
        },
        prompt: async () => {
          throw new Error("must not prompt");
        },
        ...opts,
      },
    };
  }

  it("notifies with summary and default dashboard link, allows push", async () => {
    const api = await fakeApi(() => [200, riskyResponse]);
    const t = setup(api);
    assert.equal(await run(t.opts), 0);
    await api.close();
    assert.equal(t.notes.length, 1);
    assert.match(t.notes[0][1], /1 risk flagged.*test 72%/);
    assert.ok(t.notes[0][1].endsWith(`http://localhost:5173/#/commits/${repo.head}`));
  });

  it("uses dashboard_url from ~/.pipelineiq", async () => {
    const api = await fakeApi(() => [200, cleanResponse]);
    const t = setup(api, { conf: "dashboard_url=https://dash.example.com/\n" });
    assert.equal(await run(t.opts), 0);
    await api.close();
    assert.match(t.notes[0][1], /No risks flagged/);
    assert.ok(t.notes[0][1].endsWith(`https://dash.example.com/#/commits/${repo.head}`));
  });

  it("with a terminal, no notification is sent", async () => {
    const api = await fakeApi(() => [200, riskyResponse]);
    const t = setup(api, { opts: { hasTty: () => true, prompt: async () => true } });
    assert.equal(await run(t.opts), 0);
    await api.close();
    assert.equal(t.notes.length, 0);
  });

  it("block_when_no_tty blocks risky pushes with a clear message", async () => {
    const api = await fakeApi(() => [200, riskyResponse]);
    const t = setup(api, { conf: "block_when_no_tty=true\n" });
    assert.equal(await run(t.opts), 1);
    await api.close();
    const out = t.lines.join("\n");
    assert.match(out, /PUSH BLOCKED/);
    assert.match(out, /test 72%/);
    assert.match(out, /block_when_no_tty is on/);
    assert.match(out, /git push --no-verify/);
    assert.match(out, /block_when_no_tty=false/);
    assert.equal(t.notes.length, 1);
  });

  it("block_when_no_tty off by default: risky push allowed", async () => {
    const api = await fakeApi(() => [200, riskyResponse]);
    const t = setup(api);
    assert.equal(await run(t.opts), 0);
    await api.close();
  });

  it("block_when_no_tty does not block a clean push", async () => {
    const api = await fakeApi(() => [200, cleanResponse]);
    const t = setup(api, { conf: "block_when_no_tty=true\n" });
    assert.equal(await run(t.opts), 0);
    await api.close();
  });

  it("block_when_no_tty with a terminal still uses the prompt", async () => {
    const api = await fakeApi(() => [200, riskyResponse]);
    const t = setup(api, { conf: "block_when_no_tty=true\n", opts: { hasTty: () => true, prompt: async () => true } });
    assert.equal(await run(t.opts), 0);
    await api.close();
  });

  it("notification failure (throw) never blocks or crashes", async () => {
    const api = await fakeApi(() => [200, riskyResponse]);
    const t = setup(api, {
      opts: {
        notify: async () => {
          throw new Error("no display");
        },
      },
    });
    assert.equal(await run(t.opts), 0);
    await api.close();
  });

  it("notification failure still lets block_when_no_tty block (by design)", async () => {
    const api = await fakeApi(() => [200, riskyResponse]);
    const t = setup(api, {
      conf: "block_when_no_tty=true\n",
      opts: {
        notify: async () => {
          throw new Error("no display");
        },
      },
    });
    assert.equal(await run(t.opts), 1);
    await api.close();
  });

  it("errors allow the push even with block_when_no_tty on", async () => {
    const t = setup(null, { conf: "block_when_no_tty=true\n" }); // backend down
    assert.equal(await run(t.opts), 0);
    const api = await fakeApi(() => [500, {}]);
    const t2 = setup(api, { conf: "block_when_no_tty=true\n" });
    assert.equal(await run(t2.opts), 0);
    await api.close();
    const t3 = setup(null, { conf: "block_when_no_tty=true\n", opts: { hasTty: () => { throw new Error("x"); } } });
    assert.equal(await run(t3.opts), 0);
  });
});
