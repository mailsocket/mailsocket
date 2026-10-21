// otp-signup.spec.ts
//
// A realistic Playwright test that exercises the mailsocket OTP wedge: fill a
// signup form with a mailsocket inbox address, then block on the wait endpoint
// (Playwright's APIRequestContext) until the verification code arrives, and
// assert that it did.
//
// This is a reference artifact — copy it into your own suite and wire the
// selectors to your real signup form. CI only checks that it discovers
// (`playwright test --list`); it does not run against a real signup app.
//
// Required environment variables (never hardcode a secret here):
//   MAILSOCKET_API_KEY       a Bearer API key minted in the dashboard
//   MAILSOCKET_INBOX_ID      the inbox public id (e.g. "inbox_abc123")
//   MAILSOCKET_INBOX_ADDRESS the full inbox address shown in the dashboard
//                            (e.g. "ab12cd34@in.inboxpipe.net")
//
// Run with (from examples/playwright, after `npm ci`):
//   npx playwright test

import { test, expect } from "@playwright/test";

// MAILSOCKET_API_BASE is the dashboard host with NO /api/v1 suffix (this
// example builds the full `/api/v1/...` path itself below). This differs
// from the SDKs' MAILSOCKET_BASE_URL / base_url, which DO include /api/v1 —
// don't mix the two up if you copy env vars between this example and an SDK.
const API_BASE = process.env.MAILSOCKET_API_BASE ?? "https://dash.mailsocket.app";

function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

// The inbox address is `<routing_key>@in.inboxpipe.net`. The public id
// (`inbox_abc123`) is NOT the routing key, so the address cannot be derived
// from it — read it straight from the dashboard via MAILSOCKET_INBOX_ADDRESS.
function inboxAddress(): string {
  const address = process.env.MAILSOCKET_INBOX_ADDRESS;
  if (address) return address;
  throw new Error(
    "Set MAILSOCKET_INBOX_ADDRESS to the full inbox address shown in the dashboard (e.g. ab12cd34@in.inboxpipe.net).",
  );
}

// Submit the signup form with a mailsocket address. Replace the selectors and
// URL with your own signup page — this is the part you wire to your app.
async function submitSignupForm(page: import("@playwright/test").Page, email: string) {
  await page.goto("https://app.example.com/signup");
  await page.fill('input[name="email"]', email);
  await page.click('button[type="submit"]');
  // The signup flow continues on the server; the OTP is sent to our inbox.
}

// Block on the wait endpoint until the OTP arrives (or the window elapses).
// Returns the parsed 200 body, or null on 204 (nothing arrived within
// `timeoutSeconds` — a normal outcome, not an error. This example only makes
// one call; a hardened caller would retry on 204 with its own overall
// deadline, e.g. by looping this call or using the SDK's `wait_for_otp` /
// `waitForOtp`, which already retries internally).
async function waitForOtp(
  request: import("@playwright/test").APIRequestContext,
  inboxId: string,
  apiKey: string,
  since: number,
  timeoutSeconds = 20,
) {
  const url = `${API_BASE}/api/v1/inboxes/${inboxId}/messages/wait`;
  const response = await request.get(url, {
    headers: { Authorization: `Bearer ${apiKey}` },
    // since = the time taken just before the submit, so an email that lands
    // before this call starts is still matched (and an older one is not).
    params: { require: "otp", since: String(since), timeout: String(timeoutSeconds) },
  });

  if (response.status() === 204) {
    return null; // nothing yet — success, not an error
  }
  expect(response.status()).toBe(200);
  return (await response.json()) as { data: { otp: string | null } };
}

test("signup delivers an OTP to the mailsocket inbox", async ({ page, request }) => {
  const apiKey = requiredEnv("MAILSOCKET_API_KEY");
  const inboxId = requiredEnv("MAILSOCKET_INBOX_ID");
  const email = inboxAddress();

  // 1. Note the time BEFORE the submit, so a fast email still matches.
  const started = Date.now() / 1000;

  // 2. Kick off the signup with a mailsocket inbox address.
  await submitSignupForm(page, email);

  // 3. Wait for the OTP in one blocking call (no polling loop).
  const body = await waitForOtp(request, inboxId, apiKey, started);

  // 4. Assert the code arrived.
  expect(body).not.toBeNull();
  expect(body!.data.otp).toMatch(/^\d{4,8}$/);
});
