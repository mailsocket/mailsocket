# mailsocket — MCP server

An [MCP](https://modelcontextprotocol.io) server that exposes
[mailsocket](https://mailsocket.app) to AI coding agents as native tools, so an
agent can create a throwaway inbox and **block on the OTP or magic link**
without writing any polling code. It wraps the
[Python SDK](https://github.com/mailsocket/mailsocket-sdk/tree/main/python) —
no second HTTP client, no re-implemented polling.

## What it does

| Tool | What the agent gets |
| --- | --- |
| `create_inbox(label?)` | a fresh inbox `id` + `address` |
| `wait_for_otp(inbox_id, timeout?=60, min_confidence?=0, since?)` | **the headline** — blocks (bounded: requested deadline clamped to 120s locally, 55s on the remote server) and returns the OTP string + confidence + subject/from/id/received_at. Default `since` omitted: only messages arriving after this call are matched. |
| `wait_for_link(inbox_id, timeout?=60, since?)` | the extracted magic link (returned, *not* followed) + subject/from/id/received_at. Default `since` omitted: only messages arriving after this call are matched. |
| `list_inboxes(limit?, cursor?)` | one page of inboxes owned by the key (server default page size 25) + `next_cursor`/`has_more` |
| `list_messages(inbox_id, has_otp?, subject_contains?, sender?, limit?, cursor?)` | one page of messages, optionally filtered (server default page size 25) + `next_cursor`/`has_more` |
| `get_latest(inbox_id)` | the newest message, without blocking |
| `send_test_code(inbox_id)` | puts one sample OTP email straight into the inbox (no mail server) and returns its `message_id` + `sent_at`, so you can check the wait loop without real email. 5 per inbox / 20 per account per hour. |
| `delete_inbox(inbox_id)` | remove an inbox when done |

Errors from the SDK (`AuthError` / `NotFound` / `RateLimited` / `WaitTimeout`)
are turned into clean MCP tool errors — no stack traces, and the API key is
never echoed back.

### Reusing an inbox

`create_inbox` needs no `since` — a fresh inbox only ever has new messages.
By default, `wait_for_otp`/`wait_for_link` omit `since` entirely, so the
server matches only messages arriving AFTER the call (the request start
time). But if you reuse an existing inbox (e.g. to request a second
OTP/link), pass `since` explicitly: otherwise a call with no `since` will
correctly ignore anything already sitting in the inbox, which is right for
most reuse, but if you actually WANT to catch a code that already arrived
before you called the tool, pass either the time right before you triggered
the new email (ISO8601 or unix seconds), or the `id` of the last message you
saw (`msg_...`, meaning "only messages after that one") — both tools return
`id`/`received_at` on the matched message so you can chain `since=<id>` into
the next call on that same inbox.

### Trying the loop without real email

`send_test_code` does not go through the mail server, so the code is
already in the inbox when the tool returns. Take a cutoff first: the `id`
from `get_latest` (or `since=0` if the inbox is empty), or the current time.
Then call `send_test_code(inbox_id)`, then
`wait_for_otp(inbox_id, since=<that cutoff>)`. Without `since` the wait only
sees mail that arrives during the wait, so it would miss the test code.

## Requirements

- Python 3.10+
- A mailsocket API key — from the [dashboard](https://dash.mailsocket.app) or
  `POST /api/v1/agents/register`.

## Install

No install needed with [uv](https://docs.uv.dev/):

```bash
uvx mailsocket-mcp
```

Or install with pip:

```bash
pip install mailsocket-mcp
mailsocket-mcp
```

Either way, it reads `MAILSOCKET_API_KEY` (and optional
`MAILSOCKET_BASE_URL`) from the environment. If the key is missing it exits
with a clear message and status 2; the key is never logged.

## Register with an MCP client

### Claude Desktop / Cursor

Add this to your client's MCP config file — `claude_desktop_config.json` for
Claude Desktop, `~/.cursor/mcp.json` for Cursor:

```json
{
  "mcpServers": {
    "mailsocket": {
      "command": "uvx",
      "args": ["mailsocket-mcp"],
      "env": {
        "MAILSOCKET_API_KEY": "ms_live_..."
      }
    }
  }
}
```

### Claude Code CLI

```bash
claude mcp add mailsocket -e MAILSOCKET_API_KEY=ms_live_... -- uvx mailsocket-mcp
```

Any other MCP-compatible client works the same way: point it at the
`mailsocket-mcp` executable (or `uvx mailsocket-mcp`) over stdio and pass the
key through the environment.

## Remote server (no install)

The same tools are hosted at **`https://mcp.mailsocket.app/mcp`** over
Streamable HTTP. Send your key on every request as a header:
`Authorization: Bearer ms_live_...`. Never put the key in the URL; a
`?api_key=` request is rejected.

### Claude Code CLI

```bash
claude mcp add --transport http mailsocket https://mcp.mailsocket.app/mcp \
  --header "Authorization: Bearer ms_live_..."
```

### Cursor (`~/.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "mailsocket": {
      "url": "https://mcp.mailsocket.app/mcp",
      "headers": { "Authorization": "Bearer ms_live_..." }
    }
  }
}
```

### VS Code (`.vscode/mcp.json`)

```json
{
  "servers": {
    "mailsocket": {
      "type": "http",
      "url": "https://mcp.mailsocket.app/mcp",
      "headers": { "Authorization": "Bearer ms_live_..." }
    }
  }
}
```

Any client that can set a custom HTTP header works the same way (OpenAI Agents
SDK, the n8n MCP Client node, your own code).

Things to know:

- **Claude.ai, Claude Desktop connectors and Smithery need OAuth**, which is
  planned but not available yet. Until then, use the local stdio server above
  in those clients (Claude Desktop), or use a header-capable client.
- On the remote server, `wait_for_otp` / `wait_for_link` wait at most **55s** per
  call, which keeps each response under proxy timeouts. On a timeout, call the
  tool again.
- The server is stateless: no session to keep, and every request is
  authenticated on its own. It has no key of its own and only acts with yours,
  under your normal API rate limits.
- Self-hosting: `mailsocket-mcp-http` runs the same server (see
  `mailsocket_mcp/remote.py` for its env vars).

## Env vars

| Variable | Required | Description |
| --- | --- | --- |
| `MAILSOCKET_API_KEY` | yes (secret) | Your mailsocket API key (`ms_live_...`). |
| `MAILSOCKET_BASE_URL` | no | Override the API base URL. Defaults to `https://dash.mailsocket.app/api/v1`. |

## Develop

```bash
pip install -e ".[dev]"
pytest
```

Tests make no external or live-API requests. The SDK client is faked, and the
remote-transport tests talk to a local fake upstream on `127.0.0.1`.

## Changelog

- **0.4.1** — requires `mailsocket>=0.3.1`: `wait_for_otp` / `wait_for_link`
  called without `since` no longer miss a message that lands between the
  SDK's internal long-poll calls (each follow-up call sends the cutoff the
  server reported in `Mailsocket-Wait-Since`). No tool signature changes.
- **0.4.0** — new `send_test_code(inbox_id)` tool (stdio and the hosted
  remote server, under the same per-key caps as the other non-wait tools):
  puts one sample OTP email into your own inbox without going through the
  mail server. Requires `mailsocket>=0.3.0` (`Client.send_test_code`).
- **0.3.0** — BEHAVIOUR CHANGE (via `mailsocket>=0.2.0`): `wait_for_otp` /
  `wait_for_link` now omit `since` by default instead of forwarding `0`
  ("any message already in the inbox"). The old default could return a
  STALE OTP/link already sitting in a reused inbox; the new default matches
  only messages arriving after the call. Pass `since=0` explicitly to keep
  the old behaviour.
  Hosted server (mcp.mailsocket.app, deployed from the repo, no new PyPI
  release): per-key concurrent waits raised 3 → 10 (= the Pro plan cap; the
  API still enforces each plan's own cap) and the shared wait pool 16 → 32,
  following the backend's raised wait capacity (global 64).
- **0.2.3** — `wait_for_otp` / `wait_for_link` accept an optional `since` (ISO8601, unix seconds or a message id) so a reused inbox returns the NEW code; results include the message `id` and `received_at`.
- **0.2.2** — depends on `mailsocket>=0.1.3` (stricter `_seg` id validation in the underlying Python SDK).

<!-- mcp-name: app.mailsocket/mailsocket-mcp -->
