// CommonJS require() test — proves `require('mailsocket-sdk')` (the dist/cjs
// build) resolves MailsocketClient without ESM interop shenanigans.
const { test } = require("node:test");
const assert = require("node:assert/strict");

test("require() returns MailsocketClient from the CJS build", () => {
  const mod = require("../dist/cjs/index.cjs");
  assert.equal(typeof mod.MailsocketClient, "function");
  assert.equal(typeof mod.DEFAULT_BASE_URL, "string");
  const client = new mod.MailsocketClient("ms_live_test1234567890", {
    baseUrl: "https://example.test/api/v1",
  });
  assert.ok(client instanceof mod.MailsocketClient);
  assert.equal(typeof client.sendTestCode, "function");
});

test("require() also exposes the error classes", () => {
  const { MailsocketError, AuthError, NotFound, RateLimited, WaitTimeout } = require("../dist/cjs/index.cjs");
  assert.ok(AuthError.prototype instanceof MailsocketError);
  assert.ok(NotFound.prototype instanceof MailsocketError);
  assert.ok(RateLimited.prototype instanceof MailsocketError);
  assert.ok(WaitTimeout.prototype instanceof MailsocketError);
});

test("dist/cjs is marked commonjs via its own package.json", () => {
  const pkg = require("../dist/cjs/package.json");
  assert.equal(pkg.type, "commonjs");
});
