# mailsocket Playwright example

A realistic Playwright test for the mailsocket OTP wedge. It fills a signup
form with a mailsocket inbox address, then blocks on the wait endpoint until the
verification code arrives — proving "wait for the OTP in one API call" from a
real E2E test.

This is a **reference artifact**. Copy `otp-signup.spec.ts` into your own
suite and point the signup-form selectors at your own app, or run it as-is
from this directory — it's a self-contained npm package with its own
`package.json`, lockfile and Playwright config. CI only checks that it
discovers (`playwright test --list`), since running it needs a real
signup app and a live API key.

## Environment variables

Set these before running (never hardcode the secret):

| Variable                  | Meaning                                                       |
| ------------------------- | ------------------------------------------------------------- |
| `MAILSOCKET_API_KEY`      | Bearer API key minted in the dashboard.                       |
| `MAILSOCKET_INBOX_ID`     | Inbox public id, e.g. `inbox_abc123`.                         |
| `MAILSOCKET_INBOX_ADDRESS`| Full inbox address shown in the dashboard, e.g. `ab12cd34@in.inboxpipe.net`. |

## Run it

From a fresh clone of this repo:

```sh
cd examples/playwright
npm ci
npx playwright install chromium
npx playwright test
```

The test notes the time just before it submits the signup form, then awaits the
OTP on `GET /api/v1/inboxes/{id}/messages/wait?require=otp&since=<that time>&timeout=20`
(so an email that lands before the wait call starts is still matched) and asserts the
returned code matches a 4–8 digit pattern. This example makes one wait call
and treats a `204` (nothing arrived in the window) as a failed assertion; a
production caller should retry on `204` with its own overall deadline — or
just use the SDK's `wait_for_otp` / `waitForOtp`, which already retries
internally.
