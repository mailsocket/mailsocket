// Packed-package consumer tests: these exercise the PUBLIC package surface
// (package.json "exports"/"files") exactly as a user gets it from npm, by
// running `npm pack`, installing the tarball into a throwaway project and
// resolving "mailsocket-sdk" by name — never via relative dist/ paths.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync, readFileSync, readdirSync, existsSync, realpathSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const PKG_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");
const TSC = join(PKG_DIR, "node_modules", "typescript", "bin", "tsc");

let work; // temp consumer project
let installed; // <work>/node_modules/mailsocket-sdk

before(() => {
  work = realpathSync(mkdtempSync(join(tmpdir(), "mailsocket-sdk-packed-")));
  const out = execFileSync("npm", ["pack", "--json", "--pack-destination", work], {
    cwd: PKG_DIR,
    encoding: "utf8",
  });
  const tarball = join(work, JSON.parse(out)[0].filename);
  writeFileSync(join(work, "package.json"), JSON.stringify({ name: "consumer", private: true }) + "\n");
  // Zero runtime deps, so this installs purely from the local tarball.
  execFileSync(
    "npm",
    ["install", "--offline", "--no-audit", "--no-fund", "--no-package-lock", tarball],
    { cwd: work, encoding: "utf8", stdio: "pipe" },
  );
  installed = join(work, "node_modules", "mailsocket-sdk");
  assert.ok(existsSync(join(installed, "package.json")), "tarball was not installed");
});

after(() => {
  if (work) rmSync(work, { recursive: true, force: true });
});

function tscNoEmit(entry) {
  const tsconfig = {
    compilerOptions: {
      module: "node16",
      moduleResolution: "node16",
      target: "ES2022",
      lib: ["ES2022", "DOM"],
      strict: true,
      noEmit: true,
      // Deliberately NOT skipLibCheck: the shipped .d.ts/.d.cts must type-check too.
      skipLibCheck: false,
      types: [],
    },
    files: [entry],
  };
  const cfg = join(work, `tsconfig.${entry.replace(/\W/g, "_")}.json`);
  writeFileSync(cfg, JSON.stringify(tsconfig, null, 2));
  try {
    execFileSync(process.execPath, [TSC, "-p", cfg], { cwd: work, encoding: "utf8", stdio: "pipe" });
  } catch (err) {
    assert.fail(`tsc --noEmit failed for ${entry}:\n${err.stdout}${err.stderr}`);
  }
}

// ---------------------------------------------------------------- item 1

test("packed: node16 ESM consumer (.mts, import) type-checks", () => {
  writeFileSync(
    join(work, "esm-consumer.mts"),
    [
      'import { MailsocketClient, MailsocketError, WaitTimeout, type WaitResult, type TestCodeResult } from "mailsocket-sdk";',
      'const c: MailsocketClient = new MailsocketClient("ms_live_x", { baseUrl: "https://example.test/api/v1" });',
      "const p: Promise<WaitResult> = c.waitForOtp(\"inbox_1\", { since: Date.now() / 1000 });",
      "const t: Promise<TestCodeResult> = c.sendTestCode(\"inbox_1\");",
      "void t.then((r) => r.message_id.startsWith(\"msg_\") && typeof r.sent_at === \"string\");",
      "void p.catch((e: unknown) => e instanceof WaitTimeout || e instanceof MailsocketError);",
      "",
    ].join("\n"),
  );
  tscNoEmit("esm-consumer.mts");
});

test("packed: node16 CJS consumer (.cts, import = require) type-checks", () => {
  writeFileSync(
    join(work, "cjs-consumer.cts"),
    [
      'import sdk = require("mailsocket-sdk");',
      'const c: sdk.MailsocketClient = new sdk.MailsocketClient("ms_live_x", { baseUrl: "https://example.test/api/v1" });',
      "const p: Promise<sdk.WaitResult> = c.waitForOtp(\"inbox_1\", { since: Date.now() / 1000 });",
      "const t: Promise<sdk.TestCodeResult> = c.sendTestCode(\"inbox_1\");",
      "void t.then((r) => r.message_id.startsWith(\"msg_\") && typeof r.sent_at === \"string\");",
      "void p.catch((e: unknown) => e instanceof sdk.WaitTimeout || e instanceof sdk.MailsocketError);",
      "export = c;",
      "",
    ].join("\n"),
  );
  tscNoEmit("cjs-consumer.cts");
});

test("packed: runtime import and require resolve to the ESM and CJS builds", async () => {
  const req = createRequire(join(work, "index.cjs"));
  assert.equal(req.resolve("mailsocket-sdk"), join(installed, "dist", "cjs", "index.cjs"));
  const cjs = req("mailsocket-sdk");
  assert.equal(typeof cjs.MailsocketClient, "function");
  const esmUrl = pathToFileURL(join(installed, "dist", "index.js")).href;
  const esm = await import(esmUrl);
  assert.equal(typeof esm.MailsocketClient, "function");
  // Name-based import from inside the consumer project hits the ESM entry.
  const probe = join(work, "probe.mjs");
  writeFileSync(probe, 'import.meta.resolve && console.log(import.meta.resolve("mailsocket-sdk"));\n');
  const resolved = execFileSync(process.execPath, [probe], { cwd: work, encoding: "utf8" }).trim();
  assert.equal(resolved, esmUrl);
});

// ---------------------------------------------------------------- item 2

const ERROR_CLASSES = ["MailsocketError", "AuthError", "NotFound", "RateLimited", "WaitTimeout"];

async function loadBothCopies() {
  const cjs = createRequire(join(work, "index.cjs"))("mailsocket-sdk");
  const esm = await import(pathToFileURL(join(installed, "dist", "index.js")).href);
  return { esm, cjs };
}

test("packed: ESM and CJS copies really are distinct classes (the hazard exists)", async () => {
  const { esm, cjs } = await loadBothCopies();
  for (const name of ERROR_CLASSES) assert.notEqual(esm[name], cjs[name], name);
});

test("packed: errors from either copy are instanceof the other copy's classes", async () => {
  const { esm, cjs } = await loadBothCopies();
  for (const [from, to] of [[esm, cjs], [cjs, esm]]) {
    for (const name of ERROR_CLASSES) {
      const err = new from[name]("boom", {});
      assert.ok(err instanceof to[name], `${name} not instanceof other-copy ${name}`);
      assert.ok(err instanceof to.MailsocketError, `${name} not instanceof other-copy MailsocketError`);
      assert.ok(err instanceof from[name], `${name} lost same-copy instanceof`);
      assert.ok(err instanceof Error);
      // ...and NOT instanceof unrelated sibling subclasses in either copy.
      for (const other of ERROR_CLASSES) {
        if (other === name || other === "MailsocketError") continue;
        assert.ok(!(err instanceof to[other]), `${name} wrongly instanceof other-copy ${other}`);
        assert.ok(!(err instanceof from[other]), `${name} wrongly instanceof same-copy ${other}`);
      }
    }
    // A base MailsocketError is not any subclass.
    const base = new from.MailsocketError("x");
    for (const other of ERROR_CLASSES.slice(1)) assert.ok(!(base instanceof to[other]));
  }
});

test("packed: unrelated / look-alike errors are not instanceof SDK classes", async () => {
  const { esm, cjs } = await loadBothCopies();
  class NotFound extends Error {}
  const plain = new Error("x");
  const sameName = new NotFound("x");
  sameName.name = "NotFound";
  const spoof = { name: "NotFound", message: "x", [Symbol.for("mailsocket.MailsocketError")]: ["NotFound", "MailsocketError"] };
  for (const copy of [esm, cjs]) {
    for (const name of ERROR_CLASSES) {
      for (const v of [plain, sameName, spoof, null, undefined, "NotFound", 42]) {
        assert.ok(!(v instanceof copy[name]), `${String(v?.name ?? v)} wrongly instanceof ${name}`);
      }
    }
  }
});

test("packed: user subclasses keep normal instanceof semantics", async () => {
  const { esm, cjs } = await loadBothCopies();
  class MyErr extends esm.NotFound {}
  const mine = new MyErr("x");
  assert.ok(mine instanceof MyErr);
  assert.ok(mine instanceof cjs.NotFound);
  assert.ok(mine instanceof cjs.MailsocketError);
  assert.ok(!(new cjs.NotFound("x") instanceof MyErr));
  assert.equal(mine.name, "MyErr");
});

test("packed: errors thrown by the client of one copy are caught by the other copy's classes", async () => {
  const { esm, cjs } = await loadBothCopies();
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ error: { code: "not_found", message: "nope" } }), {
    status: 404,
    headers: { "content-type": "application/json" },
  });
  try {
    for (const [from, to] of [[esm, cjs], [cjs, esm]]) {
      const c = new from.MailsocketClient("ms_live_test1234567890", { baseUrl: "https://example.test/api/v1" });
      await assert.rejects(c.getInbox("inbox_1"), (e) => e instanceof to.NotFound && e instanceof to.MailsocketError);
    }
  } finally {
    globalThis.fetch = realFetch;
  }
});

// ---------------------------------------------------------------- item 3

function walkFiles(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((d) =>
    d.isDirectory() ? walkFiles(join(dir, d.name)) : [join(dir, d.name)],
  );
}

test("packed: no shipped file references a missing source map", () => {
  const files = walkFiles(join(installed, "dist"));
  const cjsFiles = files.filter((f) => f.endsWith(".cjs") || f.endsWith(".d.cts"));
  assert.ok(cjsFiles.length >= 8, "expected the CJS build in the tarball");
  const broken = [];
  for (const f of files.filter((f) => /\.(c?js|d\.c?ts)$/.test(f))) {
    for (const m of readFileSync(f, "utf8").matchAll(/^\/\/# sourceMappingURL=(.+)$/gm)) {
      if (!existsSync(join(dirname(f), m[1].trim()))) broken.push(`${f.slice(installed.length + 1)} -> ${m[1]}`);
    }
  }
  assert.deepEqual(broken, []);
  // CJS build ships without maps, so it must not advertise any.
  for (const f of cjsFiles) assert.doesNotMatch(readFileSync(f, "utf8"), /sourceMappingURL/, f);
});
