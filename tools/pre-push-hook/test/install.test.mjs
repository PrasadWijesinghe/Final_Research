import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { existsSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, it } from "node:test";
import { MARKER, install } from "../src/install.mjs";
import { fakeApi, makeRepo, riskyResponse } from "./helpers.mjs";

describe("installer", () => {
  it("installs, refuses foreign hook, forces, uninstalls", async () => {
    const { dir } = makeRepo();
    const hook = join(dir, ".git/hooks/pre-push");
    await install({ repo: dir });
    assert.ok(readFileSync(hook, "utf8").includes(MARKER));
    assert.ok(statSync(hook).mode & 0o100);
    await install({ repo: dir }); // idempotent
    await install({ repo: dir, uninstall: true });
    assert.ok(!existsSync(hook));

    writeFileSync(hook, "#!/bin/sh\necho mine\n");
    await assert.rejects(install({ repo: dir }), /not ours/);
    await assert.rejects(install({ repo: dir, uninstall: true }), /not a PipelineIQ hook/);
    await install({ repo: dir, force: true });
    assert.ok(readFileSync(hook, "utf8").includes(MARKER));
  });

  it("installed hook runs end to end through git (no key: push goes ahead)", async () => {
    const { dir } = makeRepo();
    await install({ repo: dir });
    const remote = join(dir, "..", `remote-${Date.now()}.git`);
    execFileSync("git", ["init", "-q", "--bare", remote]);
    execFileSync("git", ["remote", "add", "origin", remote], { cwd: dir });
    const out = execFileSync("git", ["push", "origin", "main"], {
      cwd: dir,
      env: { ...process.env, PIPELINEIQ_API_KEY: "", HOME: dir },
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
    assert.ok(out !== null);
    assert.match(execFileSync("git", ["ls-remote", remote], { encoding: "utf8" }), /refs\/heads\/main/);
  });

  it("hook through git prints risks and still pushes when no terminal exists", async () => {
    const api = await fakeApi(() => [200, riskyResponse]);
    const { dir } = makeRepo();
    await install({ repo: dir });
    const remote = join(dir, "..", `remote2-${Date.now()}.git`);
    execFileSync("git", ["init", "-q", "--bare", remote]);
    execFileSync("git", ["remote", "add", "origin", remote], { cwd: dir });
    // Only the "does not block" property is asserted: no terminal means warn and continue.
    const res = await new Promise((resolve) => {
      {
        const p = spawn("git", ["push", "origin", "main"], {
          cwd: dir,
          env: { ...process.env, PIPELINEIQ_API_KEY: "piq_fake", PIPELINEIQ_API_URL: api.url, HOME: dir },
          stdio: ["ignore", "pipe", "pipe"],
          detached: true, // no controlling terminal: /dev/tty is unavailable
        });
        let err = "";
        p.stderr.on("data", (c) => (err += c));
        p.on("close", (code) => resolve({ code, err }));
      }
    });
    await api.close();
    assert.equal(res.code, 0, res.err);
    assert.match(execFileSync("git", ["ls-remote", remote], { encoding: "utf8" }), /refs\/heads\/main/);
  });
});
