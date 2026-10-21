import { test } from "node:test";
import assert from "node:assert/strict";

import {
  MailsocketClient,
  MailsocketError,
  AuthError,
  NotFound,
  RateLimited,
  WaitTimeout,
} from "../dist/index.js";

const BASE = "https://example.test/api/v1";
const API_KEY = "ms_live_test1234567890";

/** The platform fetch, captured before any test installs a scripted mock. */
const REAL_FETCH = globalThis.fetch;

const OTP_MESSAGE = {
  id: "msg_123",
  inbox_id: "inbox_abc",
  from: "Acme <no-reply@acme.test>",
  to: ["user@in.inboxpipe.net"],
  cc: [],
  subject: "Your code",
  received_at: "2026-09-23T12:00:00Z",
  status: "parsed",
  otp: "123456",
  otp_confidence: 0.95,
  magic_link: null,
  links: [],
  detected_otp: "123456",
  envelope_from: "no-reply@acme.test",
  envelope_recipient: "user@in.inboxpipe.net",
  text: "Code: 123456",
  html: "<p>Code: 123456</p>",
};

const LINK_MESSAGE = {
  ...OTP_MESSAGE,
  id: "msg_456",
  otp: null,
  otp_confidence: null,
  magic_link: "https://example.test/go?token=abc",
  links: ["https://example.test/go?token=abc"],
};

/** Install a scripted fetch; returns the recorded (url, init) calls. */
function installFetch(responses) {
  const calls = [];
  const queue = responses.map((r) => ({ ...r }));
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    const next = queue.shift();
    if (!next) throw new Error("mock fetch: no more scripted responses");
    return {
      status: next.status,
      headers: new Headers(next.headers ?? {}),
      text: async () => next.body ?? "",
    };
  };
  return calls;
}

function client(overrides = {}) {
  return new MailsocketClient(API_KEY, { baseUrl: BASE, requestTimeoutMs: 0, ...overrides });
}

test("waitForOtp resolves with otp on first 200", async () => {
  const calls = installFetch([{ status: 200, body: JSON.stringify({ data: OTP_MESSAGE }) }]);

  const result = await client().waitForOtp("inbox_abc");

  assert.equal(result.otp, "123456");
  assert.equal(result.confidence, 0.95);
  assert.equal(result.message.id, "msg_123");
  assert.equal(calls.length, 1);
  assert.ok(calls[0].url.includes("/messages/wait"));
  assert.ok(calls[0].url.includes("require=otp"));
});

test("waitForOtp omits since by default (server uses request start time; no stale OTP from a reused inbox)", async () => {
  const calls = installFetch([{ status: 200, body: JSON.stringify({ data: OTP_MESSAGE }) }]);

  await client().waitForOtp("inbox_abc");

  assert.equal(new URL(calls[0].url).searchParams.get("since"), null);
});

test("waitForOtp sends an explicit since=0 literally (old behaviour, opt-in)", async () => {
  const calls = installFetch([{ status: 200, body: JSON.stringify({ data: OTP_MESSAGE }) }]);

  await client().waitForOtp("inbox_abc", { since: 0 });

  assert.equal(new URL(calls[0].url).searchParams.get("since"), "0");
});

test("waitForOtp forwards an explicit since instead of the default", async () => {
  const calls = installFetch([{ status: 200, body: JSON.stringify({ data: OTP_MESSAGE }) }]);

  await client().waitForOtp("inbox_abc", { since: "msg_999" });

  assert.equal(new URL(calls[0].url).searchParams.get("since"), "msg_999");
});

test("waitForOtp keeps polling through 204s then returns", async () => {
  const calls = installFetch([
    { status: 204 },
    { status: 204 },
    { status: 200, body: JSON.stringify({ data: OTP_MESSAGE }) },
  ]);

  const result = await client().waitForOtp("inbox_abc", { timeout: 60_000 });

  assert.equal(result.otp, "123456");
  assert.equal(calls.length, 3);
  for (const { url } of calls) {
    assert.ok(url.includes("timeout="));
    assert.ok(url.includes("require=otp"));
  }
});

// -- 0.3.1: wait cursor from Mailsocket-Wait-Since ---------------------------

const CURSOR_1 = "2026-10-12T03:04:05.123456Z";
const CURSOR_2 = "2026-10-12T03:04:30.654321Z";
const H = "Mailsocket-Wait-Since";

const sinceOf = (url) => new URL(url).searchParams.get("since");

// [label, method, options, per-call response headers (last = the 200), expected since per call]
const CURSOR_CASES = [
  ["otp: iteration 2 sends the header of the 204", "waitForOtp", {}, [{ [H]: CURSOR_1 }, { [H]: CURSOR_1 }, {}], [null, CURSOR_1, CURSOR_1]],
  ["link: iteration 2 sends the header", "waitForLink", {}, [{ [H]: CURSOR_1 }, {}], [null, CURSOR_1]],
  ["any: iteration 2 sends the header", "wait", {}, [{ [H]: CURSOR_1 }, {}], [null, CURSOR_1]],
  ["follows the latest header value", "waitForOtp", {}, [{ [H]: CURSOR_1 }, { [H]: CURSOR_2 }, {}], [null, CURSOR_1, CURSOR_2]],
  ["old server (no / empty header) keeps omitting since", "waitForOtp", {}, [{}, { [H]: "" }, {}], [null, null, null]],
  ["explicit since is never replaced", "waitForOtp", { since: "msg_anchor" }, [{ [H]: CURSOR_1 }, {}], ["msg_anchor", "msg_anchor"]],
  ["explicit since: 0 is kept", "waitForOtp", { since: 0 }, [{ [H]: CURSOR_1 }, {}], ["0", "0"]],
];

for (const [label, method, options, headers, expected] of CURSOR_CASES) {
  test(`wait cursor: ${label}`, async () => {
    const final = method === "waitForLink" ? LINK_MESSAGE : OTP_MESSAGE;
    const calls = installFetch([
      ...headers.slice(0, -1).map((h) => ({ status: 204, headers: h })),
      { status: 200, headers: headers.at(-1), body: JSON.stringify({ data: final }) },
    ]);

    await client()[method]("inbox_abc", options);

    assert.deepEqual(calls.map((c) => sinceOf(c.url)), expected);
  });
}

test("wait cursor survives a 429 retry and is not shared between two waits", async () => {
  const calls = installFetch([
    { status: 204, headers: { [H]: CURSOR_1 } },
    {
      status: 429,
      headers: { "Retry-After": "0" },
      body: JSON.stringify({ error: { code: "wait_capacity", message: "busy", fields: {} } }),
    },
    { status: 200, body: JSON.stringify({ data: OTP_MESSAGE }) },
    { status: 200, body: JSON.stringify({ data: OTP_MESSAGE }) },
  ]);

  const c = client();
  await c.waitForOtp("inbox_abc");
  await c.waitForOtp("inbox_abc");

  assert.deepEqual(calls.map((x) => sinceOf(x.url)), [null, CURSOR_1, CURSOR_1, null]);
});

test("waitForOtp respects overall timeout -> WaitTimeout", async () => {
  // A 204 means "the server blocked for `timeout` ms and found nothing".
  // Model that with an injectable clock: each 204 advances time by the
  // server-side timeout requested, so the overall client deadline (60s) is
  // eventually exhausted across multiple 25s-clamped calls.
  let now = 0;
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    const timeout = Number(new URL(String(url)).searchParams.get("timeout"));
    now += timeout * 1000; // the server blocked this long
    return { status: 204, headers: new Headers({}), text: async () => "" };
  };

  await assert.rejects(
    client({ now: () => now }).waitForOtp("inbox_abc", { timeout: 60_000 }),
    WaitTimeout,
  );

  assert.ok(calls.length >= 3);
  // Server-side timeout stayed clamped to <= 25s on every call.
  for (const { url } of calls) {
    const timeout = Number(new URL(url).searchParams.get("timeout"));
    assert.ok(timeout <= 25, `timeout ${timeout} not clamped`);
  }
});

test("waitForOtp retries on 429 and returns otp", async () => {
  const calls = installFetch([
    {
      status: 429,
      headers: { "Retry-After": "0" },
      body: JSON.stringify({
        error: { code: "too_many_wait_requests", message: "Too many", fields: {} },
      }),
    },
    { status: 200, body: JSON.stringify({ data: OTP_MESSAGE }) },
  ]);

  const result = await client().waitForOtp("inbox_abc", { timeout: 60_000 });

  assert.equal(result.otp, "123456");
  assert.equal(calls.length, 2);
});

test("429 on a non-wait endpoint raises RateLimited with subcode", async () => {
  installFetch([
    {
      status: 429,
      headers: { "Retry-After": "7" },
      body: JSON.stringify({
        error: { code: "rate_limited", message: "Rate limit exceeded.", fields: {} },
      }),
    },
  ]);

  await assert.rejects(client().listInboxes(), (err) => {
    assert.ok(err instanceof RateLimited);
    assert.equal(err.subcode, "rate_limited");
    assert.equal(err.retryAfter, 7);
    assert.equal(err.status, 429);
    return true;
  });
});

test("401 raises AuthError", async () => {
  installFetch([
    { status: 401, body: JSON.stringify({ error: { code: "authentication_required", message: "Auth required.", fields: {} } }) },
  ]);

  await assert.rejects(client().listInboxes(), AuthError);
});

test("404 raises NotFound", async () => {
  installFetch([
    { status: 404, body: JSON.stringify({ error: { code: "not_found", message: "Resource not found.", fields: {} } }) },
  ]);

  await assert.rejects(client().getInbox("inbox_missing"), NotFound);
});

test("waitForOtp 404 raises NotFound immediately", async () => {
  installFetch([
    { status: 404, body: JSON.stringify({ error: { code: "not_found", message: "Resource not found.", fields: {} } }) },
  ]);

  await assert.rejects(client().waitForOtp("inbox_missing", { timeout: 60_000 }), NotFound);
});

test("listMessages maps sender -> from filter and hasOtp", async () => {
  const calls = installFetch([
    {
      status: 200,
      body: JSON.stringify({ data: [OTP_MESSAGE], pagination: { next_cursor: null, has_more: false } }),
    },
  ]);

  const page = await client().listMessages("inbox_abc", {
    hasOtp: true,
    subjectContains: "code",
    sender: "acme.test",
  });

  assert.equal(page.hasMore, false);
  assert.equal(page.nextCursor, null);
  assert.equal(page.data.length, 1);
  assert.equal(page.data[0].id, "msg_123");

  const url = calls[0].url;
  assert.ok(url.includes("has_otp=true"));
  assert.ok(url.includes("subject_contains=code"));
  assert.ok(url.includes("from=acme.test")); // sender mapped to the API's `from`
});

test("from JSON key is preserved on message payloads", async () => {
  installFetch([{ status: 200, body: JSON.stringify({ data: OTP_MESSAGE }) }]);

  const msg = await client().getMessage("msg_123");

  assert.equal(msg.from, "Acme <no-reply@acme.test>");
});

test("waitForLink returns link and require=link", async () => {
  const calls = installFetch([{ status: 200, body: JSON.stringify({ data: LINK_MESSAGE }) }]);

  const result = await client().waitForLink("inbox_abc");

  assert.equal(result.link, "https://example.test/go?token=abc");
  assert.equal(result.magicLink, result.link);
  assert.equal(result.otp, null);
  assert.ok(calls[0].url.includes("require=link"));
});

test("wait returns otp with require=any", async () => {
  const calls = installFetch([{ status: 200, body: JSON.stringify({ data: OTP_MESSAGE }) }]);

  const result = await client().wait("inbox_abc");

  assert.equal(result.otp, "123456");
  assert.ok(calls[0].url.includes("require=any"));
});

test("createInbox posts label and parses data", async () => {
  const inbox = {
    id: "inbox_new",
    label: "signup",
    address: "x@in.inboxpipe.net",
    is_enabled: true,
    created_at: "2026-09-23T12:00:00Z",
    updated_at: "2026-09-23T12:00:00Z",
  };
  const calls = installFetch([{ status: 201, body: JSON.stringify({ data: inbox }) }]);

  const result = await client().createInbox("signup");

  assert.equal(result.id, "inbox_new");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(JSON.parse(calls[0].init.body).label, "signup");
});

test("error classes inherit MailsocketError", () => {
  assert.ok(AuthError.prototype instanceof MailsocketError);
  assert.ok(NotFound.prototype instanceof MailsocketError);
  assert.ok(RateLimited.prototype instanceof MailsocketError);
  assert.ok(WaitTimeout.prototype instanceof MailsocketError);
});


test("network failure / socket abort is wrapped as MailsocketError (ocr HIGH)", async () => {
  // fetch rejecting (DNS/connection error, or AbortSignal.timeout firing) must
  // surface as a clean MailsocketError, never a raw DOMException/TypeError.
  globalThis.fetch = async () => {
    const e = new Error("The operation was aborted due to timeout");
    e.name = "TimeoutError";
    throw e;
  };
  await assert.rejects(
    client().getLatest("inbox_abc"),
    (err) => {
      assert.ok(err instanceof MailsocketError, "must be MailsocketError");
      assert.equal(err.code, "network_error");
      assert.match(err.message, /Network error/);
      assert.doesNotMatch(err.message, /\[object/);
      return true;
    },
  );
});

test("metadata consistency: 0.3.1 matches package.json/package-lock/USER_AGENT", async () => {
  const { readFileSync } = await import("node:fs");
  const { fileURLToPath } = await import("node:url");
  const path = await import("node:path");

  const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
  const expected = "0.3.1";

  const pkg = JSON.parse(readFileSync(path.join(root, "package.json"), "utf8"));
  assert.equal(pkg.version, expected);

  const lock = JSON.parse(readFileSync(path.join(root, "package-lock.json"), "utf8"));
  assert.equal(lock.version, expected);
  assert.equal(lock.packages[""].version, expected);

  const clientSrc = readFileSync(path.join(root, "src", "client.ts"), "utf8");
  assert.match(clientSrc, new RegExp(`mailsocket-typescript/${expected}`));
});

// -- sendTestCode (POST /inboxes/{id}/test-code) --------------------------

const TEST_CODE_DATA = { message_id: "msg_test1", sent_at: "2026-10-08T12:00:00Z" };

test("sendTestCode POSTs to /inboxes/{id}/test-code with no body and resolves with data", async () => {
  const calls = installFetch([{ status: 201, body: JSON.stringify({ data: TEST_CODE_DATA }) }]);

  const data = await client().sendTestCode("inbox_abc");

  assert.deepEqual(data, TEST_CODE_DATA);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, `${BASE}/inboxes/inbox_abc/test-code`);
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.body, undefined);
  assert.equal(calls[0].init.headers.Authorization, `Bearer ${API_KEY}`);
});

test("sendTestCode encodes the id as one path segment", async () => {
  const calls = installFetch([{ status: 201, body: JSON.stringify({ data: TEST_CODE_DATA }) }]);

  await client().sendTestCode("a/b?c");

  assert.equal(calls[0].url, `${BASE}/inboxes/a%2Fb%3Fc/test-code`);
});

test("sendTestCode 404 (foreign/missing inbox) throws NotFound", async () => {
  installFetch([
    { status: 404, body: JSON.stringify({ error: { code: "not_found", message: "Resource not found.", fields: {} } }) },
  ]);

  await assert.rejects(client().sendTestCode("inbox_foreign"), (err) => {
    assert.ok(err instanceof NotFound);
    assert.equal(err.status, 404);
    return true;
  });
});

test("sendTestCode 429 throws RateLimited with retryAfter and does not retry", async () => {
  const calls = installFetch([
    {
      status: 429,
      headers: { "Retry-After": "1800" },
      body: JSON.stringify({
        error: { code: "rate_limited", message: "Too many test codes. Try again at 13:00 UTC.", fields: {} },
      }),
    },
  ]);

  await assert.rejects(client().sendTestCode("inbox_abc"), (err) => {
    assert.ok(err instanceof RateLimited);
    assert.equal(err.retryAfter, 1800);
    assert.equal(err.subcode, "rate_limited");
    assert.match(err.message, /Try again at/);
    return true;
  });
  assert.equal(calls.length, 1);
});

test("sendTestCode on a disabled inbox throws MailsocketError code=inbox_disabled status=409", async () => {
  installFetch([
    {
      status: 409,
      body: JSON.stringify({
        error: { code: "inbox_disabled", message: "This inbox is disabled. Enable it to receive a test code.", fields: {} },
      }),
    },
  ]);

  await assert.rejects(client().sendTestCode("inbox_abc"), (err) => {
    assert.ok(err instanceof MailsocketError);
    assert.ok(!(err instanceof NotFound) && !(err instanceof RateLimited));
    assert.equal(err.code, "inbox_disabled");
    assert.equal(err.status, 409);
    return true;
  });
});

test("sendTestCode 401 throws AuthError", async () => {
  installFetch([
    { status: 401, body: JSON.stringify({ error: { code: "authentication_required", message: "Auth required.", fields: {} } }) },
  ]);

  await assert.rejects(client().sendTestCode("inbox_abc"), AuthError);
});

test("since: started = Date.now() / 1000 is sent as unix seconds", async () => {
  const calls = installFetch([{ status: 200, body: JSON.stringify({ data: OTP_MESSAGE }) }]);
  const started = 1_760_000_000.123;

  await client().waitForOtp("inbox_abc", { since: started });

  assert.equal(new URL(calls[0].url).searchParams.get("since"), "1760000000.123");
});

test("README wait samples take a cutoff before the trigger and pass since", async () => {
  const { readFileSync } = await import("node:fs");
  const { fileURLToPath } = await import("node:url");
  const path = await import("node:path");
  const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
  const readme = readFileSync(path.join(root, "README.md"), "utf8");
  const samples = readme.split("```ts").slice(1).map((b) => b.split("```")[0]);
  const waits = samples.filter((s) => /waitFor(Otp|Link)\(|\.wait\(/.test(s));
  assert.ok(waits.length > 0);
  for (const sample of waits) {
    assert.ok(sample.includes("const started = Date.now() / 1000"), sample);
    for (const line of sample.split("\n")) {
      if (/waitFor(Otp|Link)\(|\.wait\(/.test(line)) {
        assert.ok(line.includes("since: started"), line);
        assert.ok(sample.indexOf("const started") < sample.indexOf(line), line);
      }
    }
  }
  assert.ok(readme.includes("client.sendTestCode(inbox.id)"));
});


/** The request path exactly as the SDK hands it to fetch(). */
function requestedPath(url) {
  const prefix = BASE;
  assert.ok(url.startsWith(prefix), url);
  return url.slice(prefix.length).split("?")[0];
}

const HOSTILE_IDS = [
  ["inbox?x=1", "inbox%3Fx%3D1"],
  ["inbox#frag", "inbox%23frag"],
  ["../admin", "..%2Fadmin"],
  ["a/../../b", "a%2F..%2F..%2Fb"],
  ["...", "..."],
  ["in box%2F", "in%20box%252F"],
];

for (const [id, enc] of HOSTILE_IDS) {
  test(`path segments are encoded: ${JSON.stringify(id)} -> ${enc}`, async () => {
    const ok = (data) => ({ status: 200, body: JSON.stringify({ data }) });
    const page = { status: 200, body: JSON.stringify({ data: [], pagination: { next_cursor: null, has_more: false } }) };
    const calls = installFetch([
      ok({ id }), // getInbox
      { status: 204 }, // deleteInbox
      page, // listMessages
      ok(OTP_MESSAGE), // getLatest
      ok(OTP_MESSAGE), // getMessage
      ok(OTP_MESSAGE), // waitForOtp
      ok(LINK_MESSAGE), // waitForLink
      ok(OTP_MESSAGE), // wait
    ]);
    const c = client();
    await c.getInbox(id);
    await c.deleteInbox(id);
    await c.listMessages(id, { hasOtp: true });
    await c.getLatest(id);
    await c.getMessage(id);
    await c.waitForOtp(id);
    await c.waitForLink(id);
    await c.wait(id);

    assert.deepEqual(
      calls.map(({ url }) => requestedPath(url)),
      [
        `/inboxes/${enc}`,
        `/inboxes/${enc}`,
        `/inboxes/${enc}/messages`,
        `/inboxes/${enc}/messages/latest`,
        `/messages/${enc}`,
        `/inboxes/${enc}/messages/wait`,
        `/inboxes/${enc}/messages/wait`,
        `/inboxes/${enc}/messages/wait`,
      ],
    );
    // the id never leaks into the query string or a fragment
    for (const { url } of calls) {
      assert.ok(!url.includes("#"), url);
      const params = new URL(url).searchParams;
      assert.equal(params.get("x"), null, url);
    }
    // wait keeps its own query params intact
    assert.equal(new URL(calls[5].url).searchParams.get("require"), "otp");
  });
}


/**
 * Start a real local HTTP server that records every raw request line path
 * (`req.url`, exactly as received on the socket) and answers like the API.
 */
async function startRecordingServer() {
  const http = await import("node:http");
  const seen = [];
  const server = http.createServer((req, res) => {
    seen.push(req.url);
    const path = req.url.split("?")[0];
    let status = 200;
    let body;
    if (req.method === "DELETE") {
      status = 204;
    } else if (path.endsWith("/messages")) {
      body = { data: [], pagination: { next_cursor: null, has_more: false } };
    } else if (path.startsWith("/api/v1/inboxes/") && !path.includes("/messages")) {
      body = { data: { id: "x" } };
    } else {
      body = { data: OTP_MESSAGE };
    }
    res.writeHead(status, body ? { "Content-Type": "application/json" } : {});
    res.end(body ? JSON.stringify(body) : undefined);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  return {
    seen,
    baseUrl: `http://127.0.0.1:${port}/api/v1`,
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}

/** Run `fn` with the platform fetch (undoing any scripted mock), then restore. */
async function withRealFetch(fn) {
  const previous = globalThis.fetch;
  globalThis.fetch = REAL_FETCH;
  try {
    return await fn();
  } finally {
    globalThis.fetch = previous;
  }
}

/** Every id-taking method, in a fixed order. */
async function callEveryIdMethod(c, id) {
  await c.getInbox(id);
  await c.deleteInbox(id);
  await c.listMessages(id);
  await c.getLatest(id);
  await c.getMessage(id);
  // default 60s timeout: the server answers at once; 25s blocks keep the
  await c.waitForOtp(id);
  await c.waitForLink(id);
  await c.wait(id);
}

const WIRE_IDS = [
  ["?", "%3F"],
  ["#", "%23"],
  ["../x", "..%2Fx"],
  ["a/b", "a%2Fb"],
];

for (const [id, enc] of WIRE_IDS) {
  test(`real HTTP server: ${JSON.stringify(id)} arrives as ONE encoded segment (${enc})`, async () => {
    const srv = await startRecordingServer();
    try {
      await withRealFetch(() =>
        callEveryIdMethod(new MailsocketClient(API_KEY, { baseUrl: srv.baseUrl }), id),
      );
      assert.deepEqual(
        srv.seen.map((u) => u.split("?")[0]),
        [
          `/api/v1/inboxes/${enc}`,
          `/api/v1/inboxes/${enc}`,
          `/api/v1/inboxes/${enc}/messages`,
          `/api/v1/inboxes/${enc}/messages/latest`,
          `/api/v1/messages/${enc}`,
          `/api/v1/inboxes/${enc}/messages/wait`,
          `/api/v1/inboxes/${enc}/messages/wait`,
          `/api/v1/inboxes/${enc}/messages/wait`,
        ],
      );
      // wait's own query survives; the id never became query/fragment
      const waitQuery = new URLSearchParams(srv.seen[5].split("?")[1]);
      assert.equal(waitQuery.get("require"), "otp");
      assert.deepEqual([...waitQuery.keys()].sort(), ["require", "timeout"]);
    } finally {
      await srv.close();
    }
  });
}

for (const id of [".", "..", ""]) {
  test(`real HTTP server: ${JSON.stringify(id)} is rejected client-side and never reaches the network`, async () => {
    const srv = await startRecordingServer();
    try {
      const c = new MailsocketClient(API_KEY, { baseUrl: srv.baseUrl });
      const calls = [
        () => c.getInbox(id),
        () => c.deleteInbox(id),
        () => c.listMessages(id),
        () => c.getLatest(id),
        () => c.getMessage(id),
        () => c.waitForOtp(id),
        () => c.waitForLink(id),
        () => c.wait(id),
        () => c.sendTestCode(id),
        () => c.waitForOtp(id, { timeout: 0 }), // fails on the id, not WaitTimeout
      ];
      await withRealFetch(async () => {
        for (const call of calls) {
          await assert.rejects(call, (err) => {
            assert.ok(err instanceof MailsocketError, String(err));
            assert.ok(!(err instanceof WaitTimeout), String(err));
            assert.equal(err.code, "invalid_id");
            return true;
          });
        }
      });
      assert.deepEqual(srv.seen, []);
    } finally {
      await srv.close();
    }
  });
}

test("waitForOtp with a fractional remaining budget reaches the network (real server)", async () => {
  const srv = await startRecordingServer();
  try {
    await withRealFetch(async () => {
      const c = new MailsocketClient(API_KEY, { baseUrl: srv.baseUrl });
      // 12345.6ms -> server timeout 12.3456s; the socket signal must be an integer.
      const result = await c.waitForOtp("inbox_abc", { timeout: 12_345.6 });
      assert.equal(result.otp, OTP_MESSAGE.otp);
    });
    assert.equal(srv.seen.length, 1);
    assert.match(srv.seen[0], /^\/api\/v1\/inboxes\/inbox_abc\/messages\/wait\?/);
  } finally {
    await srv.close();
  }
});

test("a fractional requestTimeoutMs does not throw ERR_OUT_OF_RANGE (real server)", async () => {
  const srv = await startRecordingServer();
  try {
    await withRealFetch(async () => {
      const c = new MailsocketClient(API_KEY, { baseUrl: srv.baseUrl, requestTimeoutMs: 2_500.5 });
      await c.listMessages("inbox_abc");
    });
    assert.equal(srv.seen.length, 1);
  } finally {
    await srv.close();
  }
});
