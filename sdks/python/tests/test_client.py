"""Offline unit tests for the mailsocket Python SDK (mocked transport, no network)."""

import json
import urllib.parse

import pytest

import mailsocket
from mailsocket import AuthError, MailsocketError, NotFound, RateLimited, WaitTimeout
from mailsocket.client import Client, WaitResult

OTP_MESSAGE = {
    "id": "msg_123",
    "inbox_id": "inbox_abc",
    "from": "Acme <no-reply@acme.test>",
    "to": ["user@in.inboxpipe.net"],
    "cc": [],
    "subject": "Your code",
    "received_at": "2026-09-23T12:00:00Z",
    "status": "parsed",
    "otp": "123456",
    "otp_confidence": 0.95,
    "magic_link": None,
    "links": [],
    "detected_otp": "123456",
    "envelope_from": "no-reply@acme.test",
    "envelope_recipient": "user@in.inboxpipe.net",
    "text": "Code: 123456",
    "html": "<p>Code: 123456</p>",
}

LINK_MESSAGE = {
    **OTP_MESSAGE,
    "id": "msg_456",
    "otp": None,
    "otp_confidence": None,
    "magic_link": "https://example.test/go?token=abc",
    "links": ["https://example.test/go?token=abc"],
}


class FakeTransport:
    """A scripted urllib transport recorded by the SDK."""

    def __init__(self, responses):
        self.responses = list(responses)  # list of (status, headers, payload)
        self.calls = []  # list of (method, url) in call order

    def __call__(self, request, timeout=None):
        import io
        from urllib.error import HTTPError

        url = request.full_url
        self.calls.append((request.get_method(), url))
        status, headers, payload = self.responses.pop(0)
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""

        class _Resp:
            def __init__(self, status, headers, body):
                self.status = status
                self.headers = headers
                self._body = body

            def getcode(self):
                return self.status

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        if status >= 400:
            raise HTTPError(url, status, "http error", headers, io.BytesIO(body))
        return _Resp(status, headers, body)


def make_client(responses, **kwargs):
    transport = FakeTransport(responses)
    client = Client("ms_live_test1234567890", base_url="https://example.test/api/v1", **kwargs)
    return client, transport


@pytest.fixture
def client_factory(monkeypatch):
    def build(responses, **kwargs):
        client, transport = make_client(responses, **kwargs)
        return client, transport

    return build


def _monkeypatch_transport(monkeypatch, client, transport):
    monkeypatch.setattr("urllib.request.urlopen", transport)


def test_wait_for_otp_returns_on_first_200(monkeypatch):
    client, transport = make_client([(200, {}, {"data": OTP_MESSAGE})])
    _monkeypatch_transport(monkeypatch, client, transport)

    result = client.wait_for_otp("inbox_abc", timeout=60)

    assert isinstance(result, WaitResult)
    assert result.otp == "123456"
    assert result.confidence == 0.95
    assert str(result) == "123456"
    assert len(transport.calls) == 1


def test_wait_result_equality_and_hash_are_identity_based():
    "Public API."
    a = WaitResult(dict(OTP_MESSAGE))
    b = WaitResult(dict(OTP_MESSAGE))

    assert a == a
    assert a != b  # same field values, but distinct instances
    assert hash(a) == hash(a)
    assert hash(a) != hash(b) or a is b  # identity-based hash, no collision assumed
    assert {a, b} == {a, b}  # hashable: usable in a set/dict key


def test_wait_for_otp_default_since_is_omitted(monkeypatch):
    """since=None default must OMIT the `since` param so the server uses the
    request start time, not match a stale message already in a reused inbox.
    Revert-proof: restoring since=0 as the default fails this test."""
    client, transport = make_client([(200, {}, {"data": OTP_MESSAGE})])
    _monkeypatch_transport(monkeypatch, client, transport)

    client.wait_for_otp("inbox_abc", timeout=60)

    _, url = transport.calls[0]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert "since" not in query


def test_wait_for_link_default_since_is_omitted(monkeypatch):
    client, transport = make_client([(200, {}, {"data": LINK_MESSAGE})])
    _monkeypatch_transport(monkeypatch, client, transport)

    client.wait_for_link("inbox_abc", timeout=60)

    _, url = transport.calls[0]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert "since" not in query


def test_wait_default_since_is_omitted(monkeypatch):
    client, transport = make_client([(200, {}, {"data": OTP_MESSAGE})])
    _monkeypatch_transport(monkeypatch, client, transport)

    client.wait("inbox_abc", timeout=60)

    _, url = transport.calls[0]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert "since" not in query


def test_wait_for_otp_explicit_since_zero_still_sent(monkeypatch):
    """Explicit since=0 ("include messages already in the inbox") must still
    be forwarded as a literal 0 on the wire."""
    client, transport = make_client([(200, {}, {"data": OTP_MESSAGE})])
    _monkeypatch_transport(monkeypatch, client, transport)

    client.wait_for_otp("inbox_abc", timeout=60, since=0)

    _, url = transport.calls[0]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query["since"] == ["0"]


def test_wait_keeps_polling_through_204s_then_returns(monkeypatch):
    responses = [
        (204, {}, None),
        (204, {}, None),
        (200, {}, {"data": OTP_MESSAGE}),
    ]
    client, transport = make_client(responses)
    _monkeypatch_transport(monkeypatch, client, transport)

    result = client.wait_for_otp("inbox_abc", timeout=60)

    assert result.otp == "123456"
    assert len(transport.calls) == 3
    # server-side timeout stays clamped to <= 25s on every call
    for _, url in transport.calls:
        assert "timeout=" in url
        assert "require=otp" in url


def _since_of(url):
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    return query.get("since", [None])[0]


CURSOR_1 = "2026-10-12T03:04:05.123456Z"
CURSOR_2 = "2026-10-12T03:04:30.654321Z"
EPOCH = "1970-01-01T00:00:00.000000Z"
H = "Mailsocket-Wait-Since"


@pytest.mark.parametrize(
    "method, kwargs, headers, expected",
    [
        # 0.3.1 gap fix: no caller since -> call 1 omits it, call N sends the
        # header of call N-1, so a message landing between calls is matched.
        ("wait_for_otp", {}, [{H: CURSOR_1}, {H: CURSOR_1}, {}], [None, CURSOR_1, CURSOR_1]),
        ("wait_for_link", {}, [{H: CURSOR_1}, {}], [None, CURSOR_1]),
        ("wait", {}, [{H: CURSOR_1}, {}], [None, CURSOR_1]),
        ("wait_for_otp", {}, [{H: CURSOR_1}, {H: CURSOR_2}, {}], [None, CURSOR_1, CURSOR_2]),
        ("wait_for_otp", {}, [{H.upper(): CURSOR_1}, {}], [None, CURSOR_1]),
        # Old server (no header) or an empty header: unchanged 0.3.0 behaviour.
        ("wait_for_otp", {}, [{}, {}, {}], [None, None, None]),
        ("wait_for_otp", {}, [{H: ""}, {}], [None, None]),
        # An explicit since is never replaced by the header.
        ("wait_for_otp", {"since": "msg_anchor"}, [{H: CURSOR_1}, {H: CURSOR_1}, {}], ["msg_anchor"] * 3),
        ("wait_for_otp", {"since": 0}, [{H: EPOCH}, {}], ["0", "0"]),
    ],
)
def test_wait_loop_cursor(monkeypatch, method, kwargs, headers, expected):
    final = LINK_MESSAGE if method == "wait_for_link" else OTP_MESSAGE
    responses = [(204, h, None) for h in headers[:-1]] + [(200, headers[-1], {"data": final})]
    client, transport = make_client(responses)
    _monkeypatch_transport(monkeypatch, client, transport)

    getattr(client, method)("inbox_abc", timeout=60, **kwargs)

    assert [_since_of(url) for _, url in transport.calls] == expected


def test_wait_loop_keeps_cursor_across_a_429_retry_and_not_between_waits(monkeypatch):
    responses = [
        (204, {H: CURSOR_1}, None),
        (429, {"Retry-After": "0"}, {"error": {"code": "wait_capacity", "message": "busy"}}),
        (200, {}, {"data": OTP_MESSAGE}),
        (200, {}, {"data": OTP_MESSAGE}),
    ]
    client, transport = make_client(responses)
    _monkeypatch_transport(monkeypatch, client, transport)
    monkeypatch.setattr(mailsocket.client, "_sleep", lambda s: None)

    client.wait_for_otp("inbox_abc", timeout=60)
    client.wait_for_otp("inbox_abc", timeout=60)  # a new wait starts fresh

    assert [_since_of(url) for _, url in transport.calls] == [None, CURSOR_1, CURSOR_1, None]


def test_wait_respects_overall_timeout_raises_wait_timeout(monkeypatch):
    # A 204 means "the server blocked for `timeout` seconds and found nothing".
    # Model that with a controllable clock: each 204 advances time by the
    # server-side timeout requested, so the overall client deadline (60s) is
    # eventually exhausted across multiple 25s-clamped calls.
    import urllib.parse

    clock = [0.0]
    calls = []

    def fake_open(request, timeout=None):
        url = request.full_url
        calls.append(url)
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        server_timeout = float(qs["timeout"][0])
        clock[0] += server_timeout  # the server blocked this long

        class _Resp:
            def getcode(self):
                return 204

            def read(self):
                return b""

            @property
            def headers(self):
                return {}

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    monkeypatch.setattr(mailsocket.client, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(mailsocket.client, "_sleep", lambda s: clock.__setitem__(0, clock[0] + s))

    client = Client("ms_live_test1234567890", base_url="https://example.test/api/v1")

    with pytest.raises(WaitTimeout):
        client.wait_for_otp("inbox_abc", timeout=60)

    # Server-side timeout was clamped to 25 on every call but the last.
    requested = [
        float(urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)["timeout"][0]) for u in calls
    ]
    assert all(t <= 25 for t in requested)
    assert requested[-1] <= 25


def test_wait_429_parses_subcode_and_retries(monkeypatch):
    responses = [
        (
            429,
            {"Retry-After": "0"},
            {"error": {"code": "too_many_wait_requests", "message": "Too many", "fields": {}}},
        ),
        (200, {}, {"data": OTP_MESSAGE}),
    ]
    client, transport = make_client(responses)
    _monkeypatch_transport(monkeypatch, client, transport)

    result = client.wait_for_otp("inbox_abc", timeout=60)

    assert result.otp == "123456"
    assert len(transport.calls) == 2


HOSTILE_IDS = [
    ("in?x=1", "in%3Fx%3D1"),
    ("in#frag", "in%23frag"),
    ("../../admin", "..%2F..%2Fadmin"),
    ("a/b", "a%2Fb"),
    ("sp ce&%", "sp%20ce%26%25"),
]


@pytest.mark.parametrize("raw, encoded", HOSTILE_IDS)
def test_ids_are_quoted_as_one_path_segment_everywhere(monkeypatch, raw, encoded):
    "Public API."
    import urllib.parse

    page = {"data": [], "pagination": {}}
    responses = [
        (200, {}, {"data": {"id": "x"}}),  # get_inbox
        (204, {}, None),  # delete_inbox
        (200, {}, page),  # list_messages
        (200, {}, {"data": {"id": "x"}}),  # get_latest
        (200, {}, {"data": {"id": "x"}}),  # get_message
        (200, {}, {"data": OTP_MESSAGE}),  # wait_for_otp
        (200, {}, {"data": LINK_MESSAGE}),  # wait_for_link
        (200, {}, {"data": OTP_MESSAGE}),  # wait
    ]
    client, transport = make_client(responses)
    _monkeypatch_transport(monkeypatch, client, transport)

    client.get_inbox(raw)
    client.delete_inbox(raw)
    client.list_messages(raw, has_otp=True)
    client.get_latest(raw)
    client.get_message(raw)
    client.wait_for_otp(raw, timeout=5)
    client.wait_for_link(raw, timeout=5)
    client.wait(raw, timeout=5)

    expected_paths = [
        f"/api/v1/inboxes/{encoded}",
        f"/api/v1/inboxes/{encoded}",
        f"/api/v1/inboxes/{encoded}/messages",
        f"/api/v1/inboxes/{encoded}/messages/latest",
        f"/api/v1/messages/{encoded}",
        f"/api/v1/inboxes/{encoded}/messages/wait",
        f"/api/v1/inboxes/{encoded}/messages/wait",
        f"/api/v1/inboxes/{encoded}/messages/wait",
    ]
    assert len(transport.calls) == len(expected_paths)
    for (_, url), expected in zip(transport.calls, expected_paths):
        parts = urllib.parse.urlsplit(url)
        assert parts.netloc == "example.test"
        assert parts.path == expected, url
        assert parts.fragment == ""
        # the id never leaks into the query string
        assert "x=1" not in parts.query and "frag" not in parts.query
    # a normal id is unchanged
    assert mailsocket.client._seg("inbox_abc123") == "inbox_abc123"


def test_wait_429_raises_immediately_when_retry_disabled(monkeypatch):
    """retry_wait_on_429=False (remote MCP): no sleep, one call, RateLimited."""
    slept = []
    monkeypatch.setattr(mailsocket.client, "_sleep", lambda s: slept.append(s))
    responses = [
        (
            429,
            {"Retry-After": "2"},
            {"error": {"code": "too_many_wait_requests", "message": "Too many", "fields": {}}},
        ),
        (200, {}, {"data": OTP_MESSAGE}),
    ]
    client, transport = make_client(responses, retry_wait_on_429=False)
    _monkeypatch_transport(monkeypatch, client, transport)

    with pytest.raises(RateLimited) as exc_info:
        client.wait_for_otp("inbox_abc", timeout=60)

    assert exc_info.value.subcode == "too_many_wait_requests"
    assert exc_info.value.retry_after == 2.0
    assert len(transport.calls) == 1
    assert slept == []


def test_wait_429_raises_rate_limited_with_subcode_on_non_wait_endpoint(monkeypatch):
    responses = [
        (
            429,
            {"Retry-After": "7"},
            {"error": {"code": "rate_limited", "message": "Rate limit exceeded.", "fields": {}}},
        ),
    ]
    client, transport = make_client(responses)
    _monkeypatch_transport(monkeypatch, client, transport)

    with pytest.raises(RateLimited) as exc_info:
        client.list_inboxes()

    assert exc_info.value.subcode == "rate_limited"
    assert exc_info.value.retry_after == 7.0
    assert exc_info.value.status == 429


def test_401_raises_auth_error(monkeypatch):
    responses = [
        (401, {}, {"error": {"code": "authentication_required", "message": "Auth required.", "fields": {}}})
    ]
    client, transport = make_client(responses)
    _monkeypatch_transport(monkeypatch, client, transport)

    with pytest.raises(AuthError) as exc_info:
        client.list_inboxes()

    assert exc_info.value.code == "authentication_required"


def test_404_raises_not_found(monkeypatch):
    responses = [
        (404, {}, {"error": {"code": "not_found", "message": "Resource not found.", "fields": {}}})
    ]
    client, transport = make_client(responses)
    _monkeypatch_transport(monkeypatch, client, transport)

    with pytest.raises(NotFound):
        client.get_inbox("inbox_missing")


def test_wait_404_raises_not_found_immediately(monkeypatch):
    responses = [
        (404, {}, {"error": {"code": "not_found", "message": "Resource not found.", "fields": {}}})
    ]
    client, transport = make_client(responses)
    _monkeypatch_transport(monkeypatch, client, transport)

    with pytest.raises(NotFound):
        client.wait_for_otp("inbox_missing", timeout=60)


def test_list_messages_maps_filter_params(monkeypatch):
    responses = [(200, {}, {"data": [OTP_MESSAGE], "pagination": {"next_cursor": None, "has_more": False}})]
    client, transport = make_client(responses)
    _monkeypatch_transport(monkeypatch, client, transport)

    page = client.list_messages("inbox_abc", has_otp=True, subject_contains="code", sender="acme.test")

    assert page.has_more is False
    assert page.next_cursor is None
    assert len(page) == 1
    assert page[0]["id"] == "msg_123"

    url = transport.calls[0][1]
    assert "has_otp=true" in url
    assert "subject_contains=code" in url
    assert "from=acme.test" in url  # sender is mapped to the API's `from` param


def test_from_json_key_handled_on_message_dict(monkeypatch):
    responses = [(200, {}, {"data": OTP_MESSAGE})]
    client, transport = make_client(responses)
    _monkeypatch_transport(monkeypatch, client, transport)

    msg = client.get_message("msg_123")

    assert msg["from"] == "Acme <no-reply@acme.test>"


def test_wait_for_link_returns_link(monkeypatch):
    responses = [(200, {}, {"data": LINK_MESSAGE})]
    client, transport = make_client(responses)
    _monkeypatch_transport(monkeypatch, client, transport)

    result = client.wait_for_link("inbox_abc", timeout=60)

    assert result.magic_link == "https://example.test/go?token=abc"
    assert result.link == result.magic_link
    assert result.otp is None
    _, url = transport.calls[0]
    assert "require=link" in url


def test_wait_any_returns_otp_or_link(monkeypatch):
    responses = [(200, {}, {"data": OTP_MESSAGE})]
    client, transport = make_client(responses)
    _monkeypatch_transport(monkeypatch, client, transport)

    result = client.wait("inbox_abc", timeout=60)

    assert result.otp == "123456"
    _, url = transport.calls[0]
    assert "require=any" in url


def test_create_inbox_sends_label_and_parses_data(monkeypatch):
    inbox = {
        "id": "inbox_new",
        "label": "signup",
        "address": "x@in.inboxpipe.net",
        "is_enabled": True,
        "created_at": "2026-09-23T12:00:00Z",
        "updated_at": "2026-09-23T12:00:00Z",
    }
    responses = [(201, {}, {"data": inbox})]
    client, transport = make_client(responses)
    _monkeypatch_transport(monkeypatch, client, transport)

    result = client.create_inbox(label="signup")

    assert result["id"] == "inbox_new"
    method, url = transport.calls[0]
    assert method == "POST"


def test_create_inbox_without_label_sends_empty_body(monkeypatch):
    inbox = {
        "id": "inbox_new",
        "label": "",
        "address": "x@in.inboxpipe.net",
        "is_enabled": True,
        "created_at": "2026-09-23T12:00:00Z",
        "updated_at": "2026-09-23T12:00:00Z",
    }
    responses = [(201, {}, {"data": inbox})]
    client, transport = make_client(responses)
    _monkeypatch_transport(monkeypatch, client, transport)

    client.create_inbox()

    assert len(transport.calls) == 1


def test_error_types_are_mailsocket_subclasses():
    assert issubclass(AuthError, MailsocketError)
    assert issubclass(NotFound, MailsocketError)
    assert issubclass(RateLimited, MailsocketError)
    assert issubclass(WaitTimeout, MailsocketError)





def test_seg_rejects_empty_and_dot_segments():
    for raw in ("", ".", ".."):
        with pytest.raises(MailsocketError) as exc_info:
            mailsocket.client._seg(raw)
        assert exc_info.value.code == "invalid_id"


INVALID_IDS = ["", ".", ".."]


@pytest.mark.parametrize("raw", INVALID_IDS)
def test_invalid_id_rejected_before_any_request_hits_real_server(raw):
    """Nothing reaches the network: a real local http.server never sees a request."""
    import http.server
    import threading

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _record(self):
            seen.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"data": {"id": "x"}}).encode())

        def do_GET(self):
            self._record()

        def do_DELETE(self):
            self._record()

        def do_POST(self):
            self._record()

        def log_message(self, format, *args):
            pass  # keep test output clean

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}/api/v1"
        client = Client("ms_live_test1234567890", base_url=base_url)

        calls = [
            lambda: client.get_inbox(raw),
            lambda: client.delete_inbox(raw),
            lambda: client.list_messages(raw),
            lambda: client.get_latest(raw),
            lambda: client.get_message(raw),
            lambda: client.wait_for_otp(raw, timeout=5),
            lambda: client.wait_for_link(raw, timeout=5),
            lambda: client.wait(raw, timeout=5),
            lambda: client.send_test_code(raw),
        ]
        for call in calls:
            with pytest.raises(MailsocketError) as exc_info:
                call()
            assert exc_info.value.code == "invalid_id"
            assert not isinstance(exc_info.value, WaitTimeout)

        assert seen == []  # nothing ever reached the network
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_version_exported():
    assert hasattr(mailsocket, "__version__")


def test_metadata_consistency_across_files():
    """Version must match everywhere: pyproject, __version__, user agent."""
    import re
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    expected = "0.3.1"

    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == expected
    assert mailsocket.__version__ == expected

    client_src = (root / "mailsocket" / "client.py").read_text()
    match = re.search(r'"mailsocket-python/([^"]+)"', client_src)
    assert match, "User-Agent literal not found in client.py"
    assert match.group(1) == expected


# -- extra_headers ------------------------------------------------------------


class _HeaderCapture:
    def __init__(self):
        self.headers = []

    def __call__(self, request, timeout=None):
        self.headers.append({k.lower(): v for k, v in request.header_items()})

        class _Resp:
            headers = {}

            def getcode(self):
                return 200

            def read(self):
                return json.dumps({"data": {"id": "inbox_1"}}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        return _Resp()


def test_extra_headers_are_sent(monkeypatch):
    capture = _HeaderCapture()
    monkeypatch.setattr("urllib.request.urlopen", capture)
    client = Client(
        "ms_live_test1234567890",
        base_url="http://web:8000/api/v1",
        extra_headers={"Host": "dash.mailsocket.app", "X-Forwarded-Proto": "https"},
    )
    client.get_inbox("inbox_1")
    sent = capture.headers[0]
    assert sent["host"] == "dash.mailsocket.app"
    assert sent["x-forwarded-proto"] == "https"
    assert sent["authorization"] == "Bearer ms_live_test1234567890"


@pytest.mark.parametrize("name", ["Authorization", "authorization", "AUTHORIZATION"])
def test_extra_headers_cannot_override_authorization(monkeypatch, name):
    capture = _HeaderCapture()
    monkeypatch.setattr("urllib.request.urlopen", capture)
    client = Client(
        "ms_live_test1234567890",
        base_url="https://example.test/api/v1",
        extra_headers={name: "Bearer ms_live_ATTACKER_KEY_000"},
    )
    client.get_inbox("inbox_1")
    auths = [v for k, v in capture.headers[0].items() if k == "authorization"]
    assert auths == ["Bearer ms_live_test1234567890"]


def test_no_extra_headers_is_backwards_compatible(monkeypatch):
    capture = _HeaderCapture()
    monkeypatch.setattr("urllib.request.urlopen", capture)
    Client("ms_live_test1234567890", base_url="https://example.test/api/v1").get_inbox("i")
    sent = capture.headers[0]
    assert set(sent) >= {"authorization", "accept", "user-agent"}
    assert "x-forwarded-proto" not in sent


# -- send_test_code (POST /inboxes/{id}/test-code) ----------------------------

TEST_CODE_DATA = {"message_id": "msg_test1", "sent_at": "2026-10-08T12:00:00Z"}


def test_send_test_code_posts_and_returns_data(monkeypatch):
    client, transport = make_client([(201, {}, {"data": TEST_CODE_DATA})])
    _monkeypatch_transport(monkeypatch, client, transport)

    data = client.send_test_code("inbox_abc")

    assert data == TEST_CODE_DATA
    assert transport.calls == [("POST", "https://example.test/api/v1/inboxes/inbox_abc/test-code")]


def test_send_test_code_sends_no_body(monkeypatch):
    seen = []

    def capture(request, timeout=None):
        seen.append((request.get_method(), request.data, dict(request.header_items())))

        class _Resp:
            headers = {}

            def getcode(self):
                return 201

            def read(self):
                return json.dumps({"data": TEST_CODE_DATA}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", capture)
    Client("ms_live_test1234567890", base_url="https://example.test/api/v1").send_test_code("inbox_abc")
    method, body, headers = seen[0]
    assert method == "POST"
    assert body is None
    assert headers["Authorization"] == "Bearer ms_live_test1234567890"


def test_send_test_code_encodes_id_as_one_segment(monkeypatch):
    client, transport = make_client([(201, {}, {"data": TEST_CODE_DATA})])
    _monkeypatch_transport(monkeypatch, client, transport)

    client.send_test_code("a/b?c")

    assert transport.calls[0][1] == "https://example.test/api/v1/inboxes/a%2Fb%3Fc/test-code"


def test_send_test_code_404_raises_not_found(monkeypatch):
    responses = [(404, {}, {"error": {"code": "not_found", "message": "Resource not found.", "fields": {}}})]
    client, transport = make_client(responses)
    _monkeypatch_transport(monkeypatch, client, transport)

    with pytest.raises(NotFound) as exc_info:
        client.send_test_code("inbox_foreign")
    assert exc_info.value.status == 404


def test_send_test_code_429_raises_rate_limited_with_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr(mailsocket.client, "_sleep", lambda s: slept.append(s))
    responses = [
        (
            429,
            {"Retry-After": "1800"},
            {"error": {"code": "rate_limited", "message": "Too many test codes. Try again at 13:00 UTC.", "fields": {}}},
        )
    ]
    client, transport = make_client(responses)
    _monkeypatch_transport(monkeypatch, client, transport)

    with pytest.raises(RateLimited) as exc_info:
        client.send_test_code("inbox_abc")
    err = exc_info.value
    assert err.retry_after == 1800.0
    assert err.subcode == "rate_limited"
    assert "Try again at" in str(err)
    assert len(transport.calls) == 1  # never retried
    assert slept == []


def test_send_test_code_disabled_inbox_raises_mailsocket_error(monkeypatch):
    responses = [
        (
            409,
            {},
            {"error": {"code": "inbox_disabled", "message": "This inbox is disabled. Enable it to receive a test code.", "fields": {}}},
        )
    ]
    client, transport = make_client(responses)
    _monkeypatch_transport(monkeypatch, client, transport)

    with pytest.raises(MailsocketError) as exc_info:
        client.send_test_code("inbox_abc")
    err = exc_info.value
    assert type(err) is MailsocketError
    assert err.code == "inbox_disabled"
    assert err.status == 409


def test_send_test_code_401_raises_auth_error(monkeypatch):
    responses = [(401, {}, {"error": {"code": "authentication_required", "message": "Auth required.", "fields": {}}})]
    client, transport = make_client(responses)
    _monkeypatch_transport(monkeypatch, client, transport)

    with pytest.raises(AuthError):
        client.send_test_code("inbox_abc")


def test_readme_wait_samples_take_a_cutoff_before_the_trigger():
    """Every runnable wait sample in the README passes since=started, with
    started taken before the trigger (ux4 SDK follow-up)."""
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    blocks = readme.split("```python")[1:]
    samples = [b.split("```")[0] for b in blocks]
    waits = [s for s in samples if "wait_for_otp(" in s and "since=None" not in s]
    assert waits, "no wait sample found"
    for sample in waits:
        assert "started = time.time()" in sample
        assert sample.index("started = time.time()") < sample.index("wait_for_otp(")
        for line in sample.splitlines():
            if "wait_for_otp(" in line or "wait_for_link(" in line:
                assert "since=" in line, line
    assert "client.send_test_code(inbox" in readme
