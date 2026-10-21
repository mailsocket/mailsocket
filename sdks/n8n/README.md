# n8n-nodes-mailsocket

n8n community node for [mailsocket](https://mailsocket.app) — throwaway inboxes for AI agents and E2E tests. Create an inbox, hand its address to any signup form, then block a workflow until the OTP or magic link arrives. No polling code, no IMAP.

This is an unofficial-looking name but is published and maintained by the mailsocket team.

## Installation

Follow the n8n [community nodes installation guide](https://docs.n8n.io/integrations/community-nodes/installation-and-management), using the package name `n8n-nodes-mailsocket`.

## Credentials

Create a **mailsocket API** credential:

| Field | Description |
|---|---|
| API Key | Your mailsocket API key (starts with `ms_live_`). Create one in the [dashboard](https://dash.mailsocket.app). |
| Base URL | Defaults to `https://dash.mailsocket.app/api/v1`. Advanced; must be `https://`. |

The credential's "Test" button calls `GET /inboxes?limit=1` — a 401 means the key is wrong, a 403 means the account's email isn't verified yet.

## Nodes

### mailsocket

An action node (`usableAsTool: true`, so an n8n AI Agent can call it directly).

**Resource: Inbox**
| Operation | Notes |
|---|---|
| Create | `POST /inboxes`. Optional label. Limited to 10/hour per account. |
| Delete | `DELETE /inboxes/{id}`. Soft-delete. |
| List | `GET /inboxes`. "Return All" pages up to a hard cap of 500. |
| Send Test Code | `POST /inboxes/{id}/test-code`. Puts one sample OTP email into the inbox without going through the mail server, and returns `message_id` and `sent_at`. Use it to try a Wait for OTP without real email. Limited to 5 per inbox and 20 per account per hour. |

**Resource: Message**
| Operation | Notes |
|---|---|
| Wait for OTP | Blocks until a one-time passcode arrives, or the timeout elapses. |
| Wait for Link | Blocks until a magic link arrives. Never auto-follows the link. |
| Get Latest | `GET /inboxes/{id}/messages/latest`. On an empty inbox, returns `{"found": false}` by default. |
| List | `GET /inboxes/{id}/messages`, with `has_otp` / `subject_contains` / `from` filters. |

#### The wait loop

The mailsocket API itself only ever holds one HTTP call open for up to 25 seconds (`timeout` is clamped server-side to `[1, 25]`). "Wait for OTP" / "Wait for Link" re-call the endpoint automatically until your own **Timeout (seconds)** deadline (default 60s, max 300s) elapses:

- A `204` response means "nothing yet" — the node calls again immediately.
- A `429` response is a rate limit; the node sleeps for the server's `Retry-After` (or 1s) and retries, unless that would push past your deadline, in which case it reports a timeout instead of oversleeping.
- **`since`** is resolved once, before the loop starts, and reused on every call inside that same wait. This matters: if every call used the server's own default (`since = now`), a message that arrived between two of your calls could be skipped. The default `"0"` means "any message already in the inbox," which is correct right after **Create Inbox**. If you're waiting on a reused inbox, pass the previous message's `message_id` (e.g. `msg_abc123`) as `since` instead. An empty Since field is treated as `"0"`.
- For waits longer than 5 minutes, prefer breaking the workflow up or shortening the wait.

## Example: signup → OTP in one workflow

1. **mailsocket** node → Inbox → Create → gives you `address`.
2. Fill in a signup form (HTTP Request / browser node) using that address.
3. **mailsocket** node → Message → Wait for OTP → `inboxId` from step 1.
4. Use `{{ $json.otp }}` to complete the signup.

No email handy? Replace step 2 with **mailsocket** node → Inbox → Send Test Code (same `inboxId`). Step 3 then returns the sample code. Keep Since at `"0"` on a fresh inbox: the test code is already stored when the wait starts.

## Security notes

- The API key is sent only as an `Authorization: Bearer` header — never in a query string, and the node validates that `baseUrl` is `https://`.
- API errors from mailsocket (401/403/404/429/etc.) are surfaced as `NodeApiError` with the server's message; the key itself never appears in an error, in line with the underlying API's own key redaction.

## Development

```bash
npm install
npm run build
npm run lint
npm test        # vitest, mocked httpRequestWithAuthentication — no network access
npm run dev      # link into a local n8n instance
```

## License

MIT
