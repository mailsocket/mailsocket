# mailsocket — Python SDK

The official Python client for the [mailsocket](https://mailsocket.app) v1 REST API.
Ephemeral email inboxes, message retrieval, and — the whole point — **wait for the OTP in one line**.

```python
import time

from mailsocket import Client

client = Client("ms_live_...")            # your API key from the dashboard
inbox = client.create_inbox(label="signup")  # -> {"id": "inbox_...", "address": "..."}

started = time.time()                         # take the cutoff BEFORE the trigger
# hand inbox["address"] to whatever form sends the code, then:
result = client.wait_for_otp(inbox["id"], since=started)  # blocks up to 60s
print(result.otp)         # "123456"
print(result.confidence)  # 0.95
```

Take `started` just before you trigger the email and pass it as `since`: a
code that lands before `wait_for_otp` starts is still caught, and an older
code in a reused inbox is not.

No email handy? `client.send_test_code(inbox_id)` drops a sample code into
the inbox so you can see the whole loop work:

```python
started = time.time()
client.send_test_code(inbox["id"])            # -> {"message_id": "msg_...", "sent_at": "..."}
result = client.wait_for_otp(inbox["id"], since=started)
print(result.otp)         # the sample code
```

No polling loop. No regex over the email body. `wait_for_otp` long-polls the
server (which already knows how to extract the code) and hands back a
deterministic result with a confidence score.

## Install

```bash
pip install mailsocket
```

Zero runtime dependencies — pure standard library (`urllib`). Python 3.9+.

## API

```python
client = Client(api_key, base_url="https://dash.mailsocket.app/api/v1")
```

| Method | Returns |
| --- | --- |
| `create_inbox(label=None)` | `dict` — the inbox (`id`, `address`, ...) |
| `list_inboxes(limit=None, cursor=None)` | `Page` — `.data` list + `.next_cursor` / `.has_more` |
| `get_inbox(inbox_id)` | `dict` — inbox plus `message_count_month`, `webhook_configured` |
| `delete_inbox(inbox_id)` | `None` |
| `list_messages(inbox_id, has_otp=None, subject_contains=None, sender=None, ...)` | `Page` — `sender` maps to the API's `from` filter |
| `get_latest(inbox_id)` | `dict` — newest message |
| `get_message(message_id)` | `dict` — full message |
| `send_test_code(inbox_id)` | `dict` — `message_id`, `sent_at` of a sample OTP email put straight into the inbox (no mail server; 5 per inbox / 20 per account per hour) |

### The moat

```python
result = client.wait_for_otp("inbox_abc123", timeout=60, min_confidence=0.0, since=None)
result = client.wait_for_link("inbox_abc123", timeout=60, since=None)
result = client.wait("inbox_abc123", timeout=60, min_confidence=0.0, since=None)  # otp OR link
```

With a cutoff taken before the trigger (the usual case):

```python
started = time.time()
# trigger the email here
result = client.wait_for_otp("inbox_abc123", since=started)
```

All three return a `WaitResult` and take keyword-only arguments after
`inbox_id`. `timeout`/`min_confidence`/`since` shown above are the defaults —
call with no extra arguments for the common case.

| Method | Keyword args (all optional, keyword-only) | Returns |
| --- | --- | --- |
| `wait_for_otp(inbox_id, ...)` | `timeout=60`, `min_confidence=0.0`, `since=None` | `WaitResult` |
| `wait_for_link(inbox_id, ...)` | `timeout=60`, `since=None` | `WaitResult` |
| `wait(inbox_id, ...)` | `timeout=60`, `min_confidence=0.0`, `since=None` | `WaitResult` (otp OR link) |

`since=None` (the default) omits the param, so the server uses its own
default: the request start time — a code that arrives during this call is
caught, and a stale code already in a reused inbox is not. Pass `since=0` to
restore the old "any message already in the inbox" behaviour (useful right
after `create_inbox`, where there's nothing stale to match), or a message id
/ timestamp to continue after a specific point.

`WaitResult` exposes `.otp`, `.confidence`, `.magic_link` (and `.link` as an
alias), plus the full `.message` dict. `str(result)` is the OTP (or the link).

Semantics:

- Each HTTP call blocks server-side for up to 25s (`timeout` is clamped to
  `[1, 25]`); the SDK re-calls until the **overall** `timeout` (seconds,
  default 60) elapses.
- `200` → the matching message. `204` → nothing yet, re-call immediately.
- `429` → honours `Retry-After` and retries within the deadline. `404` → raises.
- On overall deadline → `WaitTimeout`.

## Errors

`MailsocketError` (base) with subclasses:

- `AuthError` — HTTP 401 (bad/missing key).
- `NotFound` — HTTP 404 (also an unknown, deleted or foreign inbox in
  `send_test_code`).
- `RateLimited` — HTTP 429, carries `.subcode` (`rate_limited`,
  `too_many_wait_requests`, `wait_capacity`) and `.retry_after`.
- `WaitTimeout` — no matching message within the overall deadline.

`send_test_code` on a disabled inbox raises the base `MailsocketError` with
`code="inbox_disabled"` and `status=409`. Over its hourly quota it raises
`RateLimited`; `.retry_after` says how many seconds until it frees up.

## Develop

```bash
cd python
python -m pytest -q
```

Tests make no external or live-API requests. Most monkeypatch
`urllib.request.urlopen` with a scripted responder, and a few start a real local
`http.server` on `127.0.0.1` to check actual request behaviour, e.g. that an
invalid id never reaches the network.

## Changelog

- **0.3.1** — the `wait*` helpers no longer miss a message that lands between
  two long-poll calls. When you pass no `since`, the first call still omits it
  (the server uses the request start), and every following call sends the
  cutoff the server reported in the `Mailsocket-Wait-Since` response header of
  the previous call. With an older server that sends no such header, behaviour
  is unchanged. An explicit `since` is used as before on every call.
- **0.3.0** — new `Client.send_test_code(inbox_id)` (`POST /inboxes/{id}/test-code`):
  puts one sample OTP email into your own inbox without going through the
  mail server, and returns `{"message_id", "sent_at"}`. README samples now
  take `started = time.time()` before the trigger and pass `since=started`.
- **0.2.0** — BEHAVIOUR CHANGE: `wait_for_otp`/`wait_for_link`/`wait` now
  default `since=None`, which omits the param so the server uses the
  request start time, instead of the old `since=0` ("any message already
  in the inbox"). The old default silently returned a STALE OTP/link from a
  reused inbox. Pass `since=0` explicitly to keep the old behaviour.
- **0.1.3** — `_seg` now rejects an empty id or a bare `.`/`..` id with a `MailsocketError` (`code="invalid_id"`) before any request is made, matching the TypeScript SDK's contract.
