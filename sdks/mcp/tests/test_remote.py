"""Tests for the remote (Streamable HTTP) MCP server — design B5.

The ASGI app is driven in-process (httpx2.ASGITransport) and the REAL SDK talks
over real HTTP to a local fake upstream (ThreadingHTTPServer) that records
every request, so tenant isolation and header hygiene are checked on the wire.
Nothing touches the live API.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import anyio
import httpx2
import pytest

import mailsocket_mcp.remote as remote
import mailsocket_mcp.server as srv
from mailsocket import RateLimited
from mailsocket_mcp.remote import MAX_BODY_BYTES, create_app

KEY_A = "ms_live_AAAAAAAAtenantA_key_0001"
KEY_B = "ms_live_BBBBBBBBtenantB_key_0002"
TENANT = {KEY_A: "tenant-A", KEY_B: "tenant-B"}
HOST = "mcp.mailsocket.app"
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-11-25",
}


# -- fake upstream -----------------------------------------------------------


class Upstream:
    """A real HTTP server standing in for web:8000, recording every request."""

    def __init__(self):
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        self.mode = "ok"  # ok | error_echo_key | unauthorized
        self.delay = 0.0
        self.wait_429_keys: set[str] = set()  # these keys get 429 on /wait
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                headers = {k.lower(): v for k, v in self.headers.items()}
                with upstream.lock:
                    upstream.calls.append(
                        {"method": self.command, "path": self.path, "headers": headers}
                    )
                if upstream.delay:
                    time.sleep(upstream.delay)
                auth = headers.get("authorization", "")
                key = auth.removeprefix("Bearer ")
                if upstream.mode == "unauthorized" or key not in TENANT:
                    return self._send(401, {"error": {"code": "authentication_failed", "message": "Invalid API key."}})
                if upstream.mode == "error_echo_key":
                    # A hostile/buggy upstream echoing the key must never leak it.
                    return self._send(500, {"error": {"code": "boom", "message": f"failed for {key} ({auth})"}})
                tenant = TENANT[key]
                path = self.path.split("?")[0]
                if path.endswith("/messages/wait") and key in upstream.wait_429_keys:
                    return self._send(
                        429,
                        {"error": {"code": "too_many_wait_requests", "message": "Too many concurrent waits."}},
                        {"Retry-After": "1"},
                    )
                if self.command == "DELETE":
                    return self._send(204, None)
                if path.endswith("/test-code") and self.command == "POST":
                    return self._send(201, {"data": {"message_id": f"msg_{tenant}", "sent_at": "2026-10-08T12:00:00Z"}})
                if path.endswith("/messages/latest"):
                    return self._send(200, {"data": {"id": "msg_1", "subject": f"hello {tenant} {path}"}})
                if path.endswith("/messages/wait"):
                    return self._send(200, {"data": {"otp": "123456", "otp_confidence": 0.9, "magic_link": f"https://x.test/{tenant}", "subject": tenant, "from": "a@b.test"}})
                if path.endswith("/messages"):
                    return self._send(200, {"data": [{"id": "msg_1", "subject": f"{tenant} {path}"}], "pagination": {}})
                if path.endswith("/inboxes") and self.command == "POST":
                    return self._send(201, {"data": {"id": "inbox_new", "address": f"{tenant}@in.test"}})
                if path.endswith("/inboxes"):
                    return self._send(200, {"data": [{"id": "inbox_1", "label": tenant}], "pagination": {}})
                return self._send(404, {"error": {"code": "not_found", "message": "nope"}})

            def _send(self, status, payload, extra_headers=None):
                body = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                for name, value in (extra_headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_DELETE = _handle

        class _Server(ThreadingHTTPServer):
            request_queue_size = 128  # default 5 → macOS resets bursts

        self.httpd = _Server(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v1"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def upstream():
    up = Upstream()
    yield up
    up.close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("MAILSOCKET_API_KEY", raising=False)
    srv._Limiters.reset()
    srv._PerKeyInflight.reset()
    yield
    srv._Limiters.reset()
    srv._PerKeyInflight.reset()


def make_app(upstream: Upstream, **extra):
    env = {
        "MCP_ALLOWED_HOSTS": HOST,
        "UPSTREAM_BASE_URL": upstream.base_url,
        "UPSTREAM_HOST": "dash.mailsocket.app",
    }
    env.update(extra)
    return create_app(env)


@contextlib.asynccontextmanager
async def serving(app):
    """Run the app's lifespan (MCP session manager) and yield an HTTP client."""
    async with app.inner.router.lifespan_context(app.inner):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=f"https://{HOST}") as client:
            yield client


def rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


def tool_call(name, arguments, id_=1):
    return rpc("tools/call", {"name": name, "arguments": arguments}, id_)


def bearer(key):
    return {**MCP_HEADERS, "Authorization": f"Bearer {key}"}


def run(coro_fn, *args):
    return anyio.run(coro_fn, *args)


# -- P0: tenant isolation ----------------------------------------------------


def test_p0_concurrent_keys_are_isolated(upstream):
    """16 overlapping calls alternating keys A/B: each upstream call carries its
    own request's key, and each response carries its own tenant's data.
    (8 per key = exactly the per-key non-wait cap, so none is refused.)"""
    upstream.delay = 0.05  # force overlap
    app = make_app(upstream)
    n = 2 * srv.PER_KEY_OTHER_CONCURRENCY

    async def go():
        results = {}
        async with serving(app) as client:

            async def one(i):
                key = KEY_A if i % 2 == 0 else KEY_B
                r = await client.post(
                    "/mcp",
                    headers=bearer(key),
                    json=tool_call("list_messages", {"inbox_id": f"req{i}"}, id_=i),
                )
                results[i] = (key, r)

            async with anyio.create_task_group() as tg:
                for i in range(n):
                    tg.start_soon(one, i)
        return results

    results = run(go)
    assert len(results) == n
    for i, (key, r) in results.items():
        assert r.status_code == 200
        body = r.json()
        assert body["result"]["isError"] is False, body
        text = body["result"]["content"][0]["text"]
        assert TENANT[key] in text
        other = TENANT[KEY_B if key == KEY_A else KEY_A]
        assert other not in text
    by_req = {}
    for call in upstream.calls:
        req = call["path"].split("/inboxes/")[1].split("/")[0]
        by_req.setdefault(req, set()).add(call["headers"]["authorization"])
    assert len(by_req) == n
    for i in range(n):
        expected = KEY_A if i % 2 == 0 else KEY_B
        assert by_req[f"req{i}"] == {f"Bearer {expected}"}
    # Nothing leaks into the ambient context after the requests.
    assert srv._current() is None


def test_p0_context_reset_even_when_inner_app_raises(upstream, monkeypatch):
    app = make_app(upstream)
    seen = []

    async def exploding_inner(scope, receive, send):
        seen.append(srv._current().api_key)
        raise RuntimeError("boom")

    async def go():
        app.inner = exploding_inner
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=f"https://{HOST}") as client:
            with pytest.raises(RuntimeError):
                await client.post("/mcp", headers=bearer(KEY_A), json=rpc("tools/list"))
        return srv._current()

    assert run(go) is None
    assert seen == [KEY_A]


def test_bind_client_resets_on_exception():
    with pytest.raises(ValueError):
        with srv.bind_client(object(), KEY_A):
            assert srv._current().api_key == KEY_A
            raise ValueError
    assert srv._current() is None


def test_clients_are_never_cached_across_requests(upstream, monkeypatch):
    built = []
    real = remote.Client

    def spy(*args, **kwargs):
        client = real(*args, **kwargs)
        built.append(client)
        return client

    monkeypatch.setattr(remote, "Client", spy)
    app = make_app(upstream)

    async def go():
        async with serving(app) as client:
            for key in (KEY_A, KEY_A, KEY_B):
                await client.post("/mcp", headers=bearer(key), json=tool_call("list_inboxes", {}))

    run(go)
    assert len(built) == 3
    assert len({id(c) for c in built}) == 3


# -- P0: no env fallback -----------------------------------------------------


def test_p0_refuses_to_start_with_env_key(monkeypatch, upstream):
    with pytest.raises(RuntimeError, match="MAILSOCKET_API_KEY"):
        create_app({"MAILSOCKET_API_KEY": KEY_A, "UPSTREAM_BASE_URL": upstream.base_url})
    monkeypatch.setenv("MAILSOCKET_API_KEY", KEY_A)
    with pytest.raises(RuntimeError):
        create_app()
    with pytest.raises(SystemExit) as exc_info:
        remote.main()
    assert exc_info.value.code == 2


def test_p0_no_header_is_401_with_zero_upstream_calls(upstream):
    app = make_app(upstream)

    async def go():
        async with serving(app) as client:
            return await client.post("/mcp", headers=MCP_HEADERS, json=tool_call("list_inboxes", {}))

    r = run(go)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == 'Bearer realm="mailsocket"'
    assert upstream.calls == []


# -- 401 / 400 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "auth_value",
    [
        None,
        "",
        "Bearer ",
        f"Basic {KEY_A}",
        "Bearer ms_test_AAAAAAAAAAAA",
        "Bearer ms_live_short",
        "Bearer ms_live_has spaces_in_it_xx",
        "Bearer ms_live_" + "a" * 201,
        KEY_A,  # no scheme
    ],
)
def test_malformed_or_missing_key_is_401_not_403(upstream, auth_value):
    app = make_app(upstream)
    headers = dict(MCP_HEADERS)
    if auth_value is not None:
        headers["Authorization"] = auth_value

    async def go():
        async with serving(app) as client:
            return await client.post("/mcp", headers=headers, json=rpc("tools/list"))

    r = run(go)
    assert r.status_code == 401
    assert "www-authenticate" in r.headers
    assert upstream.calls == []
    assert KEY_A not in r.text


def test_duplicate_authorization_headers_are_401(upstream):
    app = make_app(upstream)

    async def go():
        async with serving(app) as client:
            raw = [(k, v) for k, v in MCP_HEADERS.items()] + [
                ("Authorization", f"Bearer {KEY_A}"),
                ("Authorization", f"Bearer {KEY_B}"),
            ]
            return await client.post("/mcp", headers=raw, json=rpc("tools/list"))

    r = run(go)
    assert r.status_code == 401
    assert upstream.calls == []


@pytest.mark.parametrize("query", [f"api_key={KEY_A}", "api_key=x", "token=abc", f"x={KEY_A}"])
def test_key_in_query_string_is_400(upstream, query):
    app = make_app(upstream)

    async def go():
        async with serving(app) as client:
            return await client.post(f"/mcp?{query}", headers=bearer(KEY_A), json=rpc("tools/list"))

    r = run(go)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "credential_in_query"
    assert KEY_A not in r.text
    assert upstream.calls == []


def test_upstream_401_is_clean_tool_error(upstream):
    upstream.mode = "unauthorized"
    app = make_app(upstream)

    async def go():
        async with serving(app) as client:
            return await client.post("/mcp", headers=bearer(KEY_A), json=tool_call("list_inboxes", {}))

    r = run(go)
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is True
    assert "API key was rejected" in result["content"][0]["text"]
    assert "Authorization: Bearer" in result["content"][0]["text"]
    assert KEY_A not in r.text


# -- the key never leaks ------------------------------------------------------

ALL_TOOL_CALLS = [
    ("create_inbox", {"label": "x"}),
    ("wait_for_otp", {"inbox_id": "i", "timeout": 1}),
    ("wait_for_link", {"inbox_id": "i", "timeout": 1}),
    ("list_inboxes", {}),
    ("list_messages", {"inbox_id": "i"}),
    ("get_latest", {"inbox_id": "i"}),
    ("delete_inbox", {"inbox_id": "i"}),
    ("send_test_code", {"inbox_id": "i"}),
]


@pytest.mark.parametrize("name, args", ALL_TOOL_CALLS)
def test_key_never_in_response_or_logs_on_error_paths(upstream, caplog, name, args):
    upstream.mode = "error_echo_key"
    app = make_app(upstream)
    caplog.set_level(logging.DEBUG)

    async def go():
        async with serving(app) as client:
            return await client.post("/mcp", headers=bearer(KEY_A), json=tool_call(name, args))

    r = run(go)
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is True
    assert KEY_A not in r.text
    assert "ms_live_" not in r.text
    assert KEY_A not in caplog.text
    assert "authorization" not in caplog.text.lower() or "Bearer ms_live" not in caplog.text
    assert upstream.calls, "the tool should have reached the upstream"


# A production-shaped key (ms_live_ + token_urlsafe(32) = 51 chars): long
# enough that pydantic TRUNCATES it in `input_value=` ("ms_live_abc...xyz"),
# which is exactly the case a literal-only replace would miss.
KEY_REAL = "ms_live_z1K3mkwup0aA0IOi5gjBjRyzqQoj_-xEc1nIrLiA69A"
TENANT[KEY_REAL] = "tenant-R"


def _leaks(text: str, key: str) -> list[str]:
    """Every way ``key`` could be visible in ``text`` (empty list = clean)."""
    from urllib.parse import quote

    found = []
    # The documented placeholder in KEY_HINT ("ms_live_" + "...") is not a key.
    text = text.replace("ms_live_" + "...", "<placeholder>")
    if key in text:
        found.append("literal")
    if quote(key, safe="") in text:
        found.append("url-encoded")
    if "ms_live_" in text:
        found.append("ms_live_ token")
    secret = key.removeprefix("ms_live_")
    for i in range(len(secret) - 10 + 1):
        if secret[i : i + 10] in text:
            found.append(f"fragment {secret[i:i + 10]!r}")
            break
    return found


PRE_VALIDATION_CASES = [
    # (a) key as a non-numeric wait_for_otp.timeout -> pydantic input_value echo
    pytest.param(lambda k: ("wait_for_otp", {"inbox_id": "i", "timeout": k}), id="timeout-is-key"),
    # (b) key inside an unknown tool name -> "Unknown tool: ..." + INFO log
    pytest.param(lambda k: (f"nope_{k}", {}), id="unknown-tool-name"),
    # (c) dict for create_inbox.label -> pydantic input_value={...} echo
    pytest.param(lambda k: ("create_inbox", {"label": {"k": k}}), id="label-is-dict"),
    # extra variants of the same class
    pytest.param(lambda k: ("list_messages", {"inbox_id": "i", "has_otp": k}), id="bool-is-key"),
    pytest.param(lambda k: ("get_latest", {"inbox_id": [k, k]}), id="list-for-str"),
]


@pytest.mark.parametrize("key", [KEY_A, KEY_REAL], ids=["short-key", "prod-length-key"])
@pytest.mark.parametrize("build", PRE_VALIDATION_CASES)
def test_key_never_leaks_from_mcp_validation_or_dispatch_errors(upstream, caplog, key, build):
    """sol HIGH: the mcp library rejects these BEFORE our tool wrapper runs and
    interpolates the rejected input; the key must still be absent from the
    HTTP response AND from every log record (root + mcp loggers at DEBUG)."""
    caplog.set_level(logging.DEBUG)
    for name in ("mcp", "mailsocket_mcp", "uvicorn", "httpx2"):
        caplog.set_level(logging.DEBUG, logger=name)
    app = make_app(upstream)
    name, args = build(key)

    async def go():
        async with serving(app) as client:
            return await client.post("/mcp", headers=bearer(key), json=tool_call(name, args))

    r = run(go)
    assert r.status_code == 200
    body = r.json()  # still valid JSON after redaction
    assert body["result"]["isError"] is True
    assert int(r.headers["content-length"]) == len(r.content)
    assert _leaks(r.text, key) == [], r.text
    assert caplog.records, "expected the mcp library to log something"
    for rec in caplog.records:
        rendered = rec.getMessage() + (rec.exc_text or "") + (rec.stack_info or "")
        assert _leaks(rendered, key) == [], (rec.name, rendered)
    assert _leaks(caplog.text, key) == []
    assert upstream.calls == []  # rejected before any upstream call


def test_redaction_keeps_non_key_text_and_json_valid(upstream):
    """Redaction must not mangle a normal, key-free response."""
    app = make_app(upstream)
    r = _post(app, json=tool_call("list_inboxes", {}))
    assert r.json()["result"]["isError"] is False
    assert "tenant-A" in r.text
    assert int(r.headers["content-length"]) == len(r.content)


def test_log_redaction_covers_child_loggers_created_later(caplog):
    """The record factory scrubs loggers that did not exist at install time."""
    srv.install_log_redaction()
    caplog.set_level(logging.DEBUG)
    lg = logging.getLogger("mcp.some.lazy.module.created_after_install")
    with srv.bind_client(object(), KEY_REAL):
        lg.info("peer said %r", f"xx{KEY_REAL[20:]}yy")
        try:
            raise ValueError(KEY_REAL)
        except ValueError:
            lg.exception("boom")
    logging.getLogger("unrelated").warning("token %s", "ms_live_OTHERSECRET123")
    assert _leaks(caplog.text, KEY_REAL) == []
    assert "OTHERSECRET" not in caplog.text


@pytest.mark.parametrize(
    "path",
    [
        f"/mcp/{KEY_REAL}",
        f"/{KEY_REAL}",
        "/mcp/ms_live_%41%41%41%41%41%41%41%41secret",  # percent-encoded
        f"/.well-known/{KEY_REAL}/x",
    ],
)
def test_key_in_url_path_is_400_and_never_logged(upstream, caplog, path):
    "Public API."
    caplog.set_level(logging.DEBUG)
    app = make_app(upstream)

    async def go():
        async with serving(app) as client:
            return await client.post(path, headers=bearer(KEY_REAL), json=rpc("tools/list"))

    r = run(go)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "credential_in_url"
    assert upstream.calls == []
    assert _leaks(r.text, KEY_REAL) == []
    assert "AAAAAAAAsecret" not in caplog.text
    assert _leaks(caplog.text, KEY_REAL) == []
    access = [rec.getMessage() for rec in caplog.records if rec.name == "mailsocket_mcp.access"]
    assert len(access) == 1 and " 400 " in access[0]
    assert "ms_live_" not in access[0]
    assert access[0].startswith("POST /mcp/<redacted> 400 ")


PATH_BODY = "F1PathBodySecret0123456789"


@pytest.mark.parametrize(
    "path",
    [
        f"/mcp/MS_LIVE_{PATH_BODY}",  # upper case
        f"/mcp/Ms_LiVe_{PATH_BODY}",  # mixed case
        f"/mcp/%256Ds_live_{PATH_BODY}",  # double-encoded "m"
        f"/mcp/%25256D%2573_live_{PATH_BODY}",  # triple-encoded "m", double "s"
        f"/mcp/%6D%53_LIVE_{PATH_BODY}",  # single-encoded, upper case
    ],
)
def test_f1_case_and_multi_encoded_key_in_path_is_400_and_log_is_placeholder(upstream, caplog, path):
    "Public API."
    caplog.set_level(logging.DEBUG)
    app = make_app(upstream)

    async def go():
        async with serving(app) as client:
            return await client.post(path, headers=bearer(KEY_REAL), json=rpc("tools/list"))

    r = run(go)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "credential_in_url"
    assert upstream.calls == []
    assert PATH_BODY not in r.text
    # every SERVER-side record (the in-process test client's own "httpx2"
    # request log is excluded — it is not part of the deployed server)
    server_side = [rec.getMessage() for rec in caplog.records if not rec.name.startswith("httpx")]
    assert server_side, "expected server-side log records"
    assert [line for line in server_side if PATH_BODY in line] == []
    access = [rec.getMessage() for rec in caplog.records if rec.name == "mailsocket_mcp.access"]
    assert len(access) == 1
    assert access[0].startswith("POST /mcp/<redacted> 400 ")


@pytest.mark.parametrize(
    "path,expected",
    [
        (f"/mcp/{KEY_REAL}", "/mcp/<redacted>"),
        (f"/mcp/MS_LIVE_{PATH_BODY}", "/mcp/<redacted>"),
        (f"/x/%256Ds_live_{PATH_BODY}", "/mcp/<redacted>"),  # 2 decodes
        (f"/x/%25256Ds_live_{PATH_BODY}", "/mcp/<redacted>"),  # 3 decodes
        (f"/x/%2525256Ds_live_{PATH_BODY}", f"/x/%2525256Ds_live_{PATH_BODY}"),  # 4: beyond the cap
        ("/mcp", "/mcp"),
        ("/healthz", "/healthz"),
    ],
)
def test_access_log_path_is_placeholder_whenever_path_has_a_key(path, expected):
    """Belt and braces: the access-log path helper itself (case-insensitive,
    up to 3 extra percent-decoding rounds)."""
    assert remote._log_path(path) == expected


def test_access_log_has_no_headers_or_query(upstream, caplog):
    app = make_app(upstream)
    caplog.set_level(logging.INFO, logger="mailsocket_mcp.access")

    async def go():
        async with serving(app) as client:
            await client.post("/mcp", headers=bearer(KEY_A), json=rpc("tools/list"))
            await client.post(f"/mcp?api_key={KEY_B}", headers=bearer(KEY_A), json=rpc("tools/list"))

    run(go)
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "mailsocket_mcp.access"]
    assert lines == [lines[0], lines[1]] and len(lines) == 2
    assert lines[0].startswith("POST /mcp 200")
    assert lines[1].startswith("POST /mcp 400")
    for line in lines:
        assert "ms_live_" not in line
        assert "?" not in line


# -- upstream header hygiene -------------------------------------------------


def test_upstream_gets_fresh_fixed_headers_only(upstream):
    app = make_app(upstream)
    hostile = {
        **bearer(KEY_A),
        "X-Forwarded-For": "6.6.6.6",
        "CF-Connecting-IP": "6.6.6.6",
        "Cookie": "sessionid=stolen",
        "X-Evil": "1",
        "X-Forwarded-Proto": "http",
    }

    async def go():
        async with serving(app) as client:
            return await client.post("/mcp", headers=hostile, json=tool_call("list_inboxes", {}))

    r = run(go)
    assert r.json()["result"]["isError"] is False
    (call,) = upstream.calls
    h = call["headers"]
    assert h["authorization"] == f"Bearer {KEY_A}"
    assert h["host"] == "dash.mailsocket.app"
    assert h["x-forwarded-proto"] == "https"
    for leaked in ("cf-connecting-ip", "x-forwarded-for", "cookie", "x-evil"):
        assert leaked not in h


def test_upstream_url_is_env_only(upstream):
    """The upstream can't be steered by the request (Host / tool args)."""
    app = make_app(upstream)

    async def go():
        async with serving(app) as client:
            return await client.post(
                "/mcp",
                headers=bearer(KEY_A),
                json=tool_call("get_latest", {"inbox_id": "../../../evil"}),
            )

    run(go)
    assert all(c["headers"]["host"] == "dash.mailsocket.app" for c in upstream.calls)
    assert app.config.upstream_base_url == upstream.base_url


@pytest.mark.parametrize(
    "inbox_id, encoded",
    [("x?y=1", "x%3Fy%3D1"), ("x#f", "x%23f"), ("../../admin", "..%2F..%2Fadmin")],
)
def test_hostile_inbox_id_stays_one_segment_on_the_wire(upstream, inbox_id, encoded):
    "Public API."
    app = make_app(upstream)
    r = _post(app, json=tool_call("get_latest", {"inbox_id": inbox_id}))
    assert r.status_code == 200
    (call,) = upstream.calls
    assert call["path"] == f"/api/v1/inboxes/{encoded}/messages/latest"


# -- host / origin / size / method -------------------------------------------


def _post(app, path="/mcp", headers=None, **kwargs):
    async def go():
        async with serving(app) as client:
            return await client.post(path, headers=headers or bearer(KEY_A), **kwargs)

    return run(go)


def test_foreign_host_rejected(upstream):
    app = make_app(upstream)
    r = _post(app, headers={**bearer(KEY_A), "Host": "evil.test"}, json=rpc("tools/list"))
    assert r.status_code == 421
    assert upstream.calls == []


def test_foreign_origin_rejected_allowed_origin_ok(upstream):
    app = make_app(upstream)

    async def go():
        async with serving(app) as client:
            bad = await client.post(
                "/mcp", headers={**bearer(KEY_A), "Origin": "https://evil.test"}, json=rpc("tools/list")
            )
            good = await client.post(
                "/mcp", headers={**bearer(KEY_A), "Origin": f"https://{HOST}"}, json=rpc("tools/list")
            )
            return bad, good

    bad, good = run(go)
    assert bad.status_code == 403
    assert good.status_code == 200


def test_body_over_64kb_is_413_by_content_length(upstream):
    app = make_app(upstream)
    big = json.dumps(tool_call("create_inbox", {"label": "x" * (MAX_BODY_BYTES + 10)}))
    r = _post(app, content=big)
    assert r.status_code == 413
    assert upstream.calls == []


def test_body_over_64kb_is_413_when_streamed_without_length(upstream):
    app = make_app(upstream)
    chunk = b"x" * 8192

    async def body():
        for _ in range(10):
            yield chunk

    r = _post(app, content=body())
    assert r.status_code == 413


def test_get_mcp_is_405(upstream):
    app = make_app(upstream)

    async def go():
        async with serving(app) as client:
            return await client.get("/mcp", headers=bearer(KEY_A))

    r = run(go)
    assert r.status_code == 405
    assert r.headers["allow"] == "POST"


def test_stateless_no_session_id_needed(upstream):
    app = make_app(upstream)

    async def go():
        async with serving(app) as client:
            r1 = await client.post("/mcp", headers=bearer(KEY_A), json=rpc("tools/list"))
            # A brand-new request with no initialize and no session id still works.
            r2 = await client.post("/mcp", headers=bearer(KEY_B), json=tool_call("list_inboxes", {}))
            return r1, r2

    r1, r2 = run(go)
    assert r1.status_code == 200 and "mcp-session-id" not in r1.headers
    assert len(r1.json()["result"]["tools"]) == 8
    assert r2.status_code == 200 and r2.json()["result"]["isError"] is False
    assert "tenant-B" in r2.json()["result"]["content"][0]["text"]


def test_unknown_path_404_and_healthz_and_card(upstream):
    app = make_app(upstream)

    async def go():
        async with serving(app) as client:
            nf = await client.get("/admin", headers=bearer(KEY_A))
            hz = await client.get("/healthz", headers={"Host": "anything"})
            card = await client.get("/.well-known/mcp/server-card.json")
            return nf, hz, card

    nf, hz, card = run(go)
    assert nf.status_code == 404
    assert hz.status_code == 200 and hz.json() == {"status": "ok"}
    assert card.status_code == 200
    data = card.json()
    assert data["authentication"] == {"required": True, "schemes": ["bearer"]}
    assert data["serverInfo"]["version"] == srv.__version__
    assert {t["name"] for t in data["tools"]} == {n for n, _ in ALL_TOOL_CALLS}
    assert upstream.calls == []


# -- clamp / 429 / limiter (fake client, no network) ------------------------


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.key = args[0]
        FakeClient.instances.append(self)
        self.calls = []

    instances: list = []
    gate: threading.Event | None = None
    inflight = 0
    max_inflight = 0
    lock = threading.Lock()
    raise_on_wait: Exception | None = None
    block_keys: set | None = None  # None = every key blocks on the gate

    def _block(self):
        cls = FakeClient
        if cls.block_keys is not None and self.key not in cls.block_keys:
            return
        with cls.lock:
            cls.inflight += 1
            cls.max_inflight = max(cls.max_inflight, cls.inflight)
        try:
            if cls.gate is not None:
                cls.gate.wait(10)
        finally:
            with cls.lock:
                cls.inflight -= 1

    def wait_for_otp(self, inbox_id, *, timeout, min_confidence=0.0):
        self.calls.append(("wait_for_otp", timeout))
        if FakeClient.raise_on_wait is not None:
            raise FakeClient.raise_on_wait
        self._block()

        class R:
            otp = "1"
            confidence = 1.0
            message = {}

        return R()

    def send_test_code(self, inbox_id):
        self.calls.append(("send_test_code", inbox_id))
        if FakeClient.block_keys is not None and self.key in FakeClient.block_keys:
            self._block()
        return {"message_id": "msg_t", "sent_at": "2026-10-08T12:00:00Z"}

    def list_inboxes(self, limit=None, cursor=None):
        if FakeClient.block_keys is not None and self.key in FakeClient.block_keys:
            self._block()

        class P:
            data = []
            next_cursor = None
            has_more = False

        return P()


@pytest.fixture
def fake_client_cls(monkeypatch):
    FakeClient.instances = []
    FakeClient.gate = None
    FakeClient.inflight = FakeClient.max_inflight = 0
    FakeClient.raise_on_wait = None
    FakeClient.block_keys = None
    monkeypatch.setattr(remote, "Client", FakeClient)
    yield FakeClient
    if FakeClient.gate is not None:
        FakeClient.gate.set()


def test_remote_wait_clamp_is_55(upstream, fake_client_cls):
    app = make_app(upstream)
    r = _post(app, json=tool_call("wait_for_otp", {"inbox_id": "i", "timeout": 9999}))
    assert r.json()["result"]["isError"] is False
    assert fake_client_cls.instances[0].calls == [("wait_for_otp", 55.0)]
    assert srv.REMOTE_MAX_WAIT_TIMEOUT == 55.0


def test_wait_capacity_429_is_clean_error_with_retry_hint(upstream, fake_client_cls):
    fake_client_cls.raise_on_wait = RateLimited(
        "Wait capacity reached.", code="wait_capacity", subcode="wait_capacity", retry_after=3.0
    )
    app = make_app(upstream)
    r = _post(app, json=tool_call("wait_for_otp", {"inbox_id": "i"}))
    result = r.json()["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "Rate limited (HTTP 429)" in text and "Retry after 3s" in text
    assert "Traceback" not in text


def test_limiter_caps_waits_at_wait_concurrency_and_other_tools_still_served(
    upstream, fake_client_cls, monkeypatch
):
    """WAIT_CONCURRENCY + 4 concurrent waits: at most WAIT_CONCURRENCY in
    flight, the 4 extra get a clean busy error after the queue timeout, and
    list_inboxes is served meanwhile."""
    cap = srv.WAIT_CONCURRENCY
    excess = 4
    monkeypatch.setattr(srv, "LIMITER_QUEUE_TIMEOUT", 0.5)
    fake_client_cls.gate = threading.Event()
    app = make_app(upstream)

    async def go():
        waits = []
        async with serving(app) as client:

            async def wait_one(i):
                r = await client.post(
                    "/mcp",
                    # distinct keys: this test is about the GLOBAL pool, not
                    # the per-key cap (covered separately below)
                    headers=bearer(f"ms_live_distinct_key_{i:04d}"),
                    json=tool_call("wait_for_otp", {"inbox_id": f"i{i}"}, id_=i),
                )
                waits.append(r.json()["result"])

            async with anyio.create_task_group() as tg:
                for i in range(cap + excess):
                    tg.start_soon(wait_one, i)
                # Let the waits saturate their pool, then prove "other" is free.
                with anyio.fail_after(5):
                    while FakeClient.inflight < cap:
                        await anyio.sleep(0.01)
                other = await client.post(
                    "/mcp", headers=bearer(KEY_A), json=tool_call("list_inboxes", {}, id_=99)
                )
                other_inflight = FakeClient.inflight
                # Wait until the excess callers have been refused, then release.
                with anyio.fail_after(5):
                    while len(waits) < excess:
                        await anyio.sleep(0.01)
                FakeClient.gate.set()
        return waits, other, other_inflight

    waits, other, other_inflight = run(go)
    assert FakeClient.max_inflight == cap
    assert other.json()["result"]["isError"] is False
    assert other_inflight == cap  # served while every wait slot was busy
    busy = [w for w in waits if w["isError"]]
    ok = [w for w in waits if not w["isError"]]
    assert len(busy) == excess and len(ok) == cap
    assert all("Retry in a few seconds" in w["content"][0]["text"] for w in busy)


@pytest.mark.parametrize("path", ["docker-compose.yml", "deploy/mcp/Dockerfile"])
def test_uvicorn_limit_concurrency_covers_both_pools(path):
    """uvicorn answers 503 above --limit-concurrency before our limiters ever
    see the request, so it must admit every wait + other slot (and the wait
    pool must stay below the backend's global 64 so MCP alone cannot fill it)."""
    target = Path(__file__).resolve().parents[3] / path  # absent in the sdks/-only mirror (H4)
    text = target.read_text() if target.is_file() else pytest.skip(f"{path} not in this checkout")
    m = re.search(r'"--limit-concurrency",\s*"(\d+)"', text)
    assert m, path
    assert int(m.group(1)) >= srv.WAIT_CONCURRENCY + srv.OTHER_CONCURRENCY
    assert srv.WAIT_CONCURRENCY < 64





def test_p2_one_key_cannot_starve_another_real_upstream_429(upstream, monkeypatch):
    "Public API."
    monkeypatch.setattr(srv, "LIMITER_QUEUE_TIMEOUT", 1.5)
    n_attack = srv.PER_KEY_WAIT_CONCURRENCY + 6
    upstream.wait_429_keys = {KEY_A}
    app = make_app(upstream)

    async def go():
        a_results = []
        async with serving(app) as client:

            async def attacker(i):
                r = await client.post(
                    "/mcp",
                    headers=bearer(KEY_A),
                    json=tool_call("wait_for_otp", {"inbox_id": f"a{i}", "timeout": 5}, id_=i),
                )
                a_results.append(r.json()["result"])

            async with anyio.create_task_group() as tg:
                for i in range(n_attack):
                    tg.start_soon(attacker, i)
                await anyio.sleep(0.05)  # let A's calls grab whatever they can
                started = time.monotonic()
                victim = await client.post(
                    "/mcp",
                    headers=bearer(KEY_B),
                    json=tool_call("wait_for_otp", {"inbox_id": "b0", "timeout": 5}, id_=99),
                )
                victim_elapsed = time.monotonic() - started
        return a_results, victim.json()["result"], victim_elapsed

    t0 = time.monotonic()
    a_results, victim, victim_elapsed = run(go)
    total = time.monotonic() - t0
    assert victim["isError"] is False, victim
    assert "123456" in victim["content"][0]["text"]
    assert victim_elapsed < 1.0
    b_calls = [c for c in upstream.calls if c["headers"]["authorization"] == f"Bearer {KEY_B}"]
    assert len(b_calls) == 1
    # A: every call is a clean, immediate error — either the per-key cap or
    # the upstream 429 surfaced as-is. No Retry-After sleeping inside a slot.
    assert len(a_results) == n_attack and all(r["isError"] for r in a_results)
    texts = [r["content"][0]["text"] for r in a_results]
    capped = [t for t in texts if "Too many concurrent wait calls for this API key" in t]
    limited = [t for t in texts if t.startswith("Rate limited (HTTP 429)")]
    assert len(capped) + len(limited) == n_attack
    assert len(limited) <= srv.PER_KEY_WAIT_CONCURRENCY
    a_calls = [c for c in upstream.calls if c["headers"]["authorization"] == f"Bearer {KEY_A}"]
    assert len(a_calls) == len(limited)  # exactly one upstream hit each, no retries
    assert total < 3.0
    assert srv._PerKeyInflight.counts == {}  # entries removed at 0


def test_p2_per_key_wait_cap_and_other_key_still_served(upstream, fake_client_cls):
    """Per-key cap alone: A's cap + 6 waits → only PER_KEY_WAIT_CONCURRENCY in
    flight, 6 immediate clean errors; B's wait is served while A's are held."""
    cap = srv.PER_KEY_WAIT_CONCURRENCY
    n = cap + 6
    fake_client_cls.gate = threading.Event()
    fake_client_cls.block_keys = {KEY_A}
    app = make_app(upstream)

    async def go():
        results = []
        async with serving(app) as client:

            async def a(i):
                r = await client.post(
                    "/mcp", headers=bearer(KEY_A),
                    json=tool_call("wait_for_otp", {"inbox_id": f"a{i}"}, id_=i),
                )
                results.append(r.json()["result"])

            async with anyio.create_task_group() as tg:
                for i in range(n):
                    tg.start_soon(a, i)
                with anyio.fail_after(5):
                    while len(results) < n - cap or FakeClient.inflight < cap:
                        await anyio.sleep(0.01)
                held = FakeClient.inflight
                counts_while_held = dict(srv._PerKeyInflight.counts)
                b = await client.post(
                    "/mcp", headers=bearer(KEY_B),
                    json=tool_call("wait_for_otp", {"inbox_id": "b"}, id_=99),
                )
                FakeClient.gate.set()
        return results, b.json()["result"], held, counts_while_held

    results, b, held, counts = run(go)
    assert held == cap and FakeClient.max_inflight == cap
    busy = [r for r in results if r["isError"]]
    assert len(busy) == n - cap and len(results) == n
    assert all(f"max {cap} in flight" in r["content"][0]["text"] for r in busy)
    assert b["isError"] is False
    # keyed by sha256 digest, never by the key itself
    assert all(KEY_A not in k[0] and len(k[0]) == 64 for k in counts)
    assert srv._PerKeyInflight.counts == {}


def test_p2_per_key_other_cap_is_8(upstream, fake_client_cls):
    fake_client_cls.gate = threading.Event()
    fake_client_cls.block_keys = {KEY_A}
    app = make_app(upstream)

    async def go():
        results = []
        async with serving(app) as client:

            async def a(i):
                r = await client.post(
                    "/mcp", headers=bearer(KEY_A), json=tool_call("list_inboxes", {}, id_=i)
                )
                results.append(r.json()["result"])

            async with anyio.create_task_group() as tg:
                for i in range(12):
                    tg.start_soon(a, i)
                with anyio.fail_after(5):
                    while len(results) < 4 or FakeClient.inflight < 8:
                        await anyio.sleep(0.01)
                b = await client.post("/mcp", headers=bearer(KEY_B), json=tool_call("list_inboxes", {}))
                FakeClient.gate.set()
        return results, b.json()["result"]

    results, b = run(go)
    assert FakeClient.max_inflight == 8
    busy = [r for r in results if r["isError"]]
    assert len(busy) == 4
    assert all("non-wait calls for this API key (max 8" in r["content"][0]["text"] for r in busy)
    assert b["isError"] is False
    assert srv._PerKeyInflight.counts == {}


def test_send_test_code_reaches_upstream_as_tenant_post(upstream):
    """Remote: the tool POSTs /inboxes/{id}/test-code upstream with the
    CALLER's key and returns that tenant's result."""
    app = make_app(upstream)
    r = _post(app, json=tool_call("send_test_code", {"inbox_id": "inbox_x"}))
    result = r.json()["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == {
        "message_id": "msg_tenant-A",
        "sent_at": "2026-10-08T12:00:00Z",
    }
    assert [(c["method"], c["path"]) for c in upstream.calls] == [("POST", "/api/v1/inboxes/inbox_x/test-code")]
    assert upstream.calls[0]["headers"]["authorization"] == f"Bearer {KEY_A}"


def test_send_test_code_shares_the_per_key_non_wait_cap(upstream, fake_client_cls):
    """send_test_code is a non-wait tool: the per-key cap of 8 in-flight
    non-wait calls applies to it, and another key is still served."""
    fake_client_cls.gate = threading.Event()
    fake_client_cls.block_keys = {KEY_A}
    app = make_app(upstream)

    async def go():
        results = []
        async with serving(app) as client:

            async def a(i):
                r = await client.post(
                    "/mcp", headers=bearer(KEY_A), json=tool_call("send_test_code", {"inbox_id": "i"}, id_=i)
                )
                results.append(r.json()["result"])

            async with anyio.create_task_group() as tg:
                for i in range(12):
                    tg.start_soon(a, i)
                with anyio.fail_after(5):
                    while len(results) < 4 or FakeClient.inflight < srv.PER_KEY_OTHER_CONCURRENCY:
                        await anyio.sleep(0.01)
                b = await client.post(
                    "/mcp", headers=bearer(KEY_B), json=tool_call("send_test_code", {"inbox_id": "i"})
                )
                FakeClient.gate.set()
        return results, b.json()["result"]

    results, b = run(go)
    assert FakeClient.max_inflight == srv.PER_KEY_OTHER_CONCURRENCY
    busy = [r for r in results if r["isError"]]
    assert len(busy) == 12 - srv.PER_KEY_OTHER_CONCURRENCY
    assert all("non-wait calls for this API key (max 8" in r["content"][0]["text"] for r in busy)
    assert b["isError"] is False
    assert srv._PerKeyInflight.counts == {}


def test_p2_remote_wait_429_returns_immediately_no_sleep(upstream, monkeypatch):
    """Remote mode: an upstream 429 on a wait is surfaced at once (one upstream
    hit), never slept on inside the slot."""
    import mailsocket.client as sdk_client

    slept = []
    monkeypatch.setattr(sdk_client, "_sleep", lambda s: slept.append(s))
    upstream.wait_429_keys = {KEY_A}
    app = make_app(upstream)
    r = _post(app, json=tool_call("wait_for_otp", {"inbox_id": "i", "timeout": 30}))
    result = r.json()["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert text.startswith("Rate limited (HTTP 429)") and "Retry after 1s" in text
    assert slept == []
    assert len(upstream.calls) == 1


def test_p2_per_key_counter_released_on_error(upstream, fake_client_cls):
    fake_client_cls.raise_on_wait = RuntimeError("boom")
    app = make_app(upstream)

    async def go():
        texts = []
        async with serving(app) as client:
            for i in range(srv.PER_KEY_WAIT_CONCURRENCY + 2):  # would hit the cap if slots leaked
                r = await client.post(
                    "/mcp", headers=bearer(KEY_A),
                    json=tool_call("wait_for_otp", {"inbox_id": "i"}, id_=i),
                )
                texts.append(r.json()["result"]["content"][0]["text"])
        return texts

    texts = run(go)
    assert len(texts) == srv.PER_KEY_WAIT_CONCURRENCY + 2
    assert all("Too many concurrent" not in t for t in texts)
    assert all(t == "boom" for t in texts)
    assert srv._PerKeyInflight.counts == {}


def test_stdio_binding_has_no_per_key_cap_and_sdk_still_retries():
    """stdio unchanged: bind_client default is per_key_limits=False, and the
    remote-only SDK flag isn't used by the stdio env client."""
    assert srv.ToolContext(client=None, api_key="k").per_key_limits is False
    import inspect

    assert "per_key_limits" not in inspect.getsource(srv.main)
    assert "retry_wait_on_429" not in inspect.getsource(srv._build_client_from_env)

    class Gate:
        inflight = 0
        peak = 0

    n = srv.PER_KEY_WAIT_CONCURRENCY + 3

    async def go():
        ev = anyio.Event()

        @srv._guarded("wait")
        def body():
            Gate.inflight += 1
            Gate.peak = max(Gate.peak, Gate.inflight)
            anyio.from_thread.run(ev.wait)
            Gate.inflight -= 1
            return {}

        with srv.bind_client(object(), KEY_A):
            async with anyio.create_task_group() as tg:
                for _ in range(n):
                    tg.start_soon(body)
                with anyio.fail_after(5):
                    while Gate.inflight < n:
                        await anyio.sleep(0.01)
                ev.set()

    run(go)
    # > the remote per-key wait cap: stdio is single-tenant, uncapped
    assert Gate.peak == n > srv.PER_KEY_WAIT_CONCURRENCY
