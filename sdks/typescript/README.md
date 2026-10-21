# mailsocket — TypeScript SDK

The official TypeScript client for the [mailsocket](https://mailsocket.app) v1 REST API.
Ephemeral email inboxes, message retrieval, and — the whole point — **wait for the OTP in one line**.

```ts
import { MailsocketClient } from "mailsocket-sdk";

const client = new MailsocketClient("ms_live_..."); // your API key from the dashboard
const inbox = await client.createInbox("signup");   // { id: "inbox_...", address: "..." }

const started = Date.now() / 1000;                  // unix seconds, taken BEFORE the trigger
// hand inbox.address to whatever form sends the code, then:
const { otp, confidence } = await client.waitForOtp(inbox.id, { since: started }); // waits up to 60s
console.log(otp);         // "123456"
console.log(confidence);  // 0.95
```

Take `started` just before you trigger the email and pass it as `since`: a
code that lands before `waitForOtp` starts is still caught, and an older code
in a reused inbox is not. `since` takes unix seconds (fractions are fine), an
ISO 8601 string or a message id, so `Date.now() / 1000` is the right unit.

No email handy? `client.sendTestCode(inbox.id)` drops a sample code into the
inbox so you can see the whole loop work:

```ts
const started = Date.now() / 1000;
await client.sendTestCode(inbox.id);                 // { message_id: "msg_...", sent_at: "..." }
const { otp } = await client.waitForOtp(inbox.id, { since: started });
console.log(otp);         // the sample code
```

No polling loop. No regex over the email body. `waitForOtp` long-polls the server
(which already knows how to extract the code) and hands back a deterministic
result with a confidence score.

## Install

```bash
npm install mailsocket-sdk
```

Zero runtime dependencies — uses the global `fetch`. Node 18+. Ships TypeScript types.
Ships both ESM and CommonJS builds:

```ts
// ESM / TypeScript
import { MailsocketClient } from "mailsocket-sdk";
```

```js
// CommonJS
const { MailsocketClient } = require("mailsocket-sdk");
```

## API

```ts
const client = new MailsocketClient(apiKey, { baseUrl: "https://dash.mailsocket.app/api/v1" });
```

| Method | Returns |
| --- | --- |
| `createInbox(label?)` | `Promise<Inbox>` |
| `listInboxes(limit?, cursor?)` | `Promise<Page<Inbox>>` — `.data` + `.nextCursor` / `.hasMore` |
| `getInbox(inboxId)` | `Promise<InboxDetail>` |
| `deleteInbox(inboxId)` | `Promise<void>` |
| `listMessages(inboxId, { hasOtp?, subjectContains?, sender?, ... })` | `Promise<Page<MessageSummary>>` — `sender` maps to the API's `from` filter |
| `getLatest(inboxId)` | `Promise<Message>` |
| `getMessage(messageId)` | `Promise<Message>` |
| `sendTestCode(inboxId)` | `Promise<TestCodeResult>` — `message_id`, `sent_at` of a sample OTP email put straight into the inbox (no mail server; 5 per inbox / 20 per account per hour) |

### The moat

```ts
const started = Date.now() / 1000; // just before you trigger the email
const otpResult = await client.waitForOtp("inbox_abc123", { timeout: 60_000, minConfidence: 0, since: started });
const linkResult = await client.waitForLink("inbox_abc123", { timeout: 60_000, since: started });
const eitherResult = await client.wait("inbox_abc123", { timeout: 60_000, minConfidence: 0, since: started }); // otp OR link
```

All three return a `Promise<WaitResult>`. The options object is optional;
`timeout`/`minConfidence` shown above are the defaults. `since` is not a
default: omitting it means "server picks the request start time" (see
below), so pass a cutoff taken before the trigger as shown.

| Method | Options (all optional) | Returns |
| --- | --- | --- |
| `waitForOtp(inboxId, options?)` | `timeout: 60000`, `minConfidence: 0`, `since: <omitted>` | `Promise<WaitResult>` |
| `waitForLink(inboxId, options?)` | `timeout: 60000`, `since: <omitted>` | `Promise<WaitResult>` |
| `wait(inboxId, options?)` | `timeout: 60000`, `minConfidence: 0`, `since: <omitted>` | `Promise<WaitResult>` (otp OR link) |

`WaitResult` exposes `.otp`, `.confidence`, `.magicLink` (and `.link` as an alias),
plus the full `.message`. Semantics:

- Each HTTP call blocks server-side for up to 25s (`timeout` is clamped to `[1, 25]`);
  the SDK re-calls until the **overall** `timeout` (milliseconds, default 60 000) elapses.
- `since` (omitted by default) only matches messages received strictly after
  that point. Omitting it uses the server's own default: the request start
  time, so a code that arrives during this call is caught and a stale one
  already sitting in a reused inbox is not. Pass `since: 0` to restore the
  old "any message already in the inbox" behaviour (useful right after
  `createInbox`, where there's nothing stale to match), or a message id /
  timestamp to continue after a specific point.
- `200` → the matching message. `204` → nothing yet, re-call immediately.
- `429` → honours `Retry-After` and retries within the deadline. `404` → throws.
- On overall deadline → `WaitTimeout`.

## Errors

`MailsocketError` (base) with subclasses:

- `AuthError` — HTTP 401.
- `NotFound` — HTTP 404 (also an unknown, deleted or foreign inbox in `sendTestCode`).
- `RateLimited` — HTTP 429, carries `.subcode` (`rate_limited`, `too_many_wait_requests`, `wait_capacity`) and `.retryAfter` (seconds).
- `WaitTimeout` — no matching message within the overall deadline.

`sendTestCode` on a disabled inbox throws the base `MailsocketError` with
`code: "inbox_disabled"` and `status: 409`. Over its hourly quota it throws
`RateLimited`; `.retryAfter` says how many seconds until it frees up.

`instanceof` works even if your app ends up with both the ESM and the CommonJS copy
loaded (e.g. an ESM app using a CJS dependency that also uses the SDK). An error
thrown by either copy matches the other copy's classes.

## Develop

```bash
npm install
npm run build     # dual build: build:esm + build:cjs (see below)
npm test          # builds, then node:test against dist + the packed tarball
```

`npm run build` compiles twice with `tsc`, no bundler:

- `build:esm` — `tsconfig.json` → ESM in `dist/` (`index.js`, `.d.ts`, source maps).
- `build:cjs` — `tsconfig.cjs.json` → CommonJS in `dist/cjs/`, then
  `scripts/rename-cjs.mjs` renames `.js`/`.d.ts` to `.cjs`/`.d.cts`, rewrites
  internal specifiers to match, drops the (unshipped) CJS source maps and their
  `sourceMappingURL` comments, and writes `dist/cjs/package.json` (`{"type":"commonjs"}`).

`package.json` `exports` routes `import` → `dist/index.js` + `dist/index.d.ts` and
`require` → `dist/cjs/index.cjs` + `dist/cjs/index.d.cts`.

Tests make no live-API requests: they use a scripted `fetch` mock plus a few
local `127.0.0.1` HTTP servers. `test/packed.test.mjs` also runs `npm pack`, installs the
tarball into a temp project, and type-checks node16 ESM (`.mts`) and CJS (`.cts`)
consumers with `tsc --noEmit`.

## Changelog

- **0.3.1** — fix: a code arriving between two internal long-poll calls of
  `waitForOtp`/`waitForLink`/`wait` was missed. Without `options.since`, call 1
  still omits it; each later call sends the previous response's
  `Mailsocket-Wait-Since` header as `since`. No header (older server): same as
  0.3.0. An explicit `since` is unchanged.
- **0.3.0** — new `sendTestCode(inboxId)` (`POST /inboxes/{id}/test-code`):
  puts one sample OTP email into your own inbox without going through the
  mail server, and resolves with `{ message_id, sent_at }` (type
  `TestCodeResult`). README samples now take `const started = Date.now() / 1000`
  before the trigger and pass `{ since: started }`.
- **0.2.0** — BEHAVIOUR CHANGE: `waitForOtp`/`waitForLink`/`wait` now omit
  `since` by default instead of sending `since: 0` ("any message already in
  the inbox"). The old default silently returned a STALE OTP/link from a
  reused inbox. Pass `since: 0` explicitly to keep the old behaviour.
- **0.1.3** — added a CommonJS build (`dist/cjs`) alongside ESM, so `require("mailsocket-sdk")` works. See Install above for both usages. `instanceof` checks on the error classes work across the two builds, even when an app loads the package via both `import` and `require()`.
