"""Offline unit tests for the mailsocket MCP server (fake SDK client, no network)."""

import asyncio
import json

import pytest

import mailsocket_mcp
from mailsocket import AuthError, MailsocketError, NotFound, RateLimited, WaitTimeout
from mailsocket_mcp.server import (
    MAX_WAIT_TIMEOUT,
    MISSING_KEY_MESSAGE,
    _build_client_from_env,
    bind_client,
    server,
)

API_KEY = "ms_live_test1234567890"

OTP_MESSAGE = {
    "id": "msg_123",
    "inbox_id": "inbox_abc",
    "from": "Acme <no-reply@acme.test>",
    "subject": "Your code",
    "otp": "123456",
    "otp_confidence": 0.95,
    "magic_link": None,
}

LINK_MESSAGE = {
    **OTP_MESSAGE,
    "id": "msg_456",
    "otp": None,
    "otp_confidence": None,
    "magic_link": "https://example.test/go?token=abc",
}


class FakeWaitResult:
    def __init__(self, otp=None, confidence=None, magic_link=None, message=None):
        self.otp = otp
        self.confidence = confidence
        self.magic_link = magic_link
        self.message = message or {}


class FakeClient:
    """Scripted stand-in for the SDK Client, recording every call."""

    def __init__(self):
        self.created = []
        self.wait_otp_calls = []
        self.wait_link_calls = []
        self.list_messages_calls = []
        self.latest_calls = []
        self.deleted = []
        self.wait_otp = None  # (result | exception) scripted per-test
        self.wait_link = None
        self.test_code_calls = []
        self.test_code = {"message_id": "msg_test1", "sent_at": "2026-10-08T12:00:00Z"}

    def create_inbox(self, label=None):
        self.created.append(label)
        return {"id": "inbox_1", "address": "x@in.inboxpipe.net"}

    def wait_for_otp(self, inbox_id, *, timeout=60.0, min_confidence=0.0, since=None):
        self.wait_otp_calls.append((inbox_id, timeout, min_confidence, since))
        if isinstance(self.wait_otp, Exception):
            raise self.wait_otp
        return self.wait_otp

    def wait_for_link(self, inbox_id, *, timeout=60.0, since=None):
        self.wait_link_calls.append((inbox_id, timeout, since))
        if isinstance(self.wait_link, Exception):
            raise self.wait_link
        return self.wait_link

    def list_inboxes(self, limit=None, cursor=None):
        return SimplePage([{"id": "inbox_1", "address": "x@in.inboxpipe.net"}])

    def list_messages(
        self,
        inbox_id,
        has_otp=None,
        subject_contains=None,
        sender=None,
        limit=None,
        cursor=None,
    ):
        self.list_messages_calls.append((inbox_id, has_otp, subject_contains, sender))
        return SimplePage([OTP_MESSAGE])

    def get_latest(self, inbox_id):
        self.latest_calls.append(inbox_id)
        return OTP_MESSAGE

    def delete_inbox(self, inbox_id):
        self.deleted.append(inbox_id)
        return None

    def send_test_code(self, inbox_id):
        self.test_code_calls.append(inbox_id)
        if isinstance(self.test_code, Exception):
            raise self.test_code
        return self.test_code


class SimplePage:
    def __init__(self, data):
        self.data = data
        self.next_cursor = None
        self.has_more = False


@pytest.fixture
def fake_client(monkeypatch):
    """Bind a FakeClient for tool calls (as stdio ``main()`` does) with the key."""
    monkeypatch.setenv("MAILSOCKET_API_KEY", API_KEY)
    monkeypatch.delenv("MAILSOCKET_BASE_URL", raising=False)
    fake = FakeClient()
    with bind_client(fake, API_KEY, max_wait_timeout=MAX_WAIT_TIMEOUT):
        yield fake


def call_tool(name, arguments):
    return asyncio.run(server.call_tool(name, arguments))


# -- tools/list --------------------------------------------------------------


def test_tools_list_returns_all_tools_with_valid_schemas():
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "create_inbox",
        "wait_for_otp",
        "wait_for_link",
        "list_inboxes",
        "list_messages",
        "get_latest",
        "delete_inbox",
        "send_test_code",
    }
    by_name = {t.name: t for t in tools}
    for name, tool in by_name.items():
        assert tool.description, f"{name} missing a description"
        schema = tool.input_schema
        assert schema.get("type") == "object", f"{name} schema is not an object"
        assert isinstance(schema.get("properties"), dict), f"{name} schema has no properties"

    otp_schema = by_name["wait_for_otp"].input_schema
    assert set(otp_schema["properties"]) >= {"inbox_id", "timeout", "min_confidence"}
    assert "inbox_id" in otp_schema.get("required", [])
    # min_confidence defaults to 0, timeout defaults to 60
    assert otp_schema["properties"]["timeout"].get("default") == 60

    create_schema = by_name["create_inbox"].input_schema
    assert "label" in create_schema["properties"]


def test_tools_list_annotation_values():
    """Guard the exact effect-annotation metadata advertised to MCP clients.

    Asserts all four hint fields (read-only/destructive/idempotent/open-world)
    for every tool, not just the fields each tool happens to set explicitly —
    a wrong destructive/idempotent value on a read-only tool, or a wrong
    read-only/idempotent value on the destructive tool, must fail this test.
    """
    tools = asyncio.run(server.list_tools())
    by_name = {t.name: t for t in tools}

    def ann(name):
        annotations = by_name[name].annotations
        return {
            "readOnlyHint": annotations.read_only_hint,
            "destructiveHint": annotations.destructive_hint,
            "idempotentHint": annotations.idempotent_hint,
            "openWorldHint": annotations.open_world_hint,
        }

    expected = {
        "create_inbox": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
        "wait_for_otp": {
            "readOnlyHint": True,
            "destructiveHint": None,
            "idempotentHint": None,
            "openWorldHint": True,
        },
        "wait_for_link": {
            "readOnlyHint": True,
            "destructiveHint": None,
            "idempotentHint": None,
            "openWorldHint": True,
        },
        "list_inboxes": {
            "readOnlyHint": True,
            "destructiveHint": None,
            "idempotentHint": None,
            "openWorldHint": True,
        },
        "list_messages": {
            "readOnlyHint": True,
            "destructiveHint": None,
            "idempotentHint": None,
            "openWorldHint": True,
        },
        "get_latest": {
            "readOnlyHint": True,
            "destructiveHint": None,
            "idempotentHint": None,
            "openWorldHint": True,
        },
        "delete_inbox": {
            "readOnlyHint": None,
            "destructiveHint": True,
            "idempotentHint": None,
            "openWorldHint": True,
        },
        "send_test_code": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    }

    assert set(expected) == set(by_name)
    for name, expected_ann in expected.items():
        assert ann(name) == expected_ann, name


# -- wait_for_otp ------------------------------------------------------------


def test_wait_for_otp_returns_otp_and_confidence(fake_client):
    fake_client.wait_otp = FakeWaitResult(
        otp="123456", confidence=0.95, message={"subject": "Your code", "from": "Acme <no-reply@acme.test>"}
    )

    result = call_tool("wait_for_otp", {"inbox_id": "inbox_abc"})

    assert result.is_error is False
    text = result.content[0].text
    assert '"123456"' in text
    inbox_id, timeout, min_conf, since = fake_client.wait_otp_calls[0]
    assert inbox_id == "inbox_abc"
    assert timeout == 60
    assert min_conf == 0.0
    assert since is None  # absent -> SDK omits since (server uses request start time)


def test_wait_for_otp_tool_with_real_sdk_sends_server_cursor_on_iteration_2(monkeypatch):
    """0.4.1: the tool runs the real SDK wait loop (mailsocket>=0.3.1). With
    no ``since`` the first HTTP call omits it and the second sends the
    Mailsocket-Wait-Since value from the first 204, so a code landing
    between the SDK's internal calls is not missed."""
    import urllib.parse

    from mailsocket import Client

    cursor = "2026-10-12T03:04:05.123456Z"
    scripted = [
        (204, {"Mailsocket-Wait-Since": cursor}, b""),
        (200, {}, json.dumps({"data": OTP_MESSAGE}).encode()),
    ]
    urls = []

    class _Resp:
        def __init__(self, status, headers, body):
            self.status, self.headers, self._body = status, headers, body

        def getcode(self):
            return self.status

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def fake_urlopen(request, timeout=None):
        urls.append(request.full_url)
        return _Resp(*scripted.pop(0))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = Client(API_KEY, base_url="https://example.test/api/v1")

    with bind_client(client, API_KEY, max_wait_timeout=MAX_WAIT_TIMEOUT):
        result = call_tool("wait_for_otp", {"inbox_id": "inbox_abc"})

    assert result.is_error is False
    assert '"123456"' in result.content[0].text
    since = [urllib.parse.parse_qs(urllib.parse.urlsplit(u).query).get("since") for u in urls]
    assert since == [None, [cursor]]


def test_wait_for_otp_timeout_is_clamped(fake_client):
    fake_client.wait_otp = FakeWaitResult(otp="123456")

    call_tool("wait_for_otp", {"inbox_id": "inbox_abc", "timeout": 9999})

    _, timeout, _, _ = fake_client.wait_otp_calls[0]
    assert timeout == MAX_WAIT_TIMEOUT

    call_tool("wait_for_otp", {"inbox_id": "inbox_abc", "timeout": 0})
    _, timeout, _, _ = fake_client.wait_otp_calls[1]
    assert timeout == 1.0


def test_wait_for_otp_since_passed_through(fake_client):
    fake_client.wait_otp = FakeWaitResult(otp="123456")

    call_tool("wait_for_otp", {"inbox_id": "inbox_abc", "since": "msg_111"})

    inbox_id, _, _, since = fake_client.wait_otp_calls[0]
    assert inbox_id == "inbox_abc"
    assert since == "msg_111"


def test_wait_for_otp_explicit_since_zero_passed_through(fake_client):
    """since=0 (include messages already in the inbox) must reach the SDK as 0,
    not be dropped as falsy and turned into the new request-start default."""
    fake_client.wait_otp = FakeWaitResult(otp="123456")

    call_tool("wait_for_otp", {"inbox_id": "inbox_abc", "since": 0})

    _, _, _, since = fake_client.wait_otp_calls[0]
    assert since == 0


def test_wait_for_otp_result_carries_id_and_received_at(fake_client):
    fake_client.wait_otp = FakeWaitResult(
        otp="123456",
        confidence=0.95,
        message={
            "subject": "Your code",
            "from": "Acme <no-reply@acme.test>",
            "id": "msg_999",
            "received_at": "2024-01-01T00:00:00Z",
        },
    )

    result = call_tool("wait_for_otp", {"inbox_id": "inbox_abc"})

    assert result.is_error is False
    text = result.content[0].text
    assert '"msg_999"' in text
    assert '"2024-01-01T00:00:00Z"' in text


def test_wait_for_otp_wait_timeout_is_clean_error(fake_client):
    fake_client.wait_otp = WaitTimeout("No matching message arrived within 60s.")

    result = call_tool("wait_for_otp", {"inbox_id": "inbox_abc"})

    assert result.is_error is True
    text = result.content[0].text
    assert "Traceback" not in text
    assert API_KEY not in text
    assert "timed out" in text.lower() or "No matching message" in text


# -- wait_for_link -----------------------------------------------------------


def test_wait_for_link_returns_magic_link(fake_client):
    fake_client.wait_link = FakeWaitResult(magic_link="https://example.test/go?token=abc")

    result = call_tool("wait_for_link", {"inbox_id": "inbox_abc"})

    assert result.is_error is False
    assert "https://example.test/go?token=abc" in result.content[0].text
    inbox_id, timeout, since = fake_client.wait_link_calls[0]
    assert inbox_id == "inbox_abc"
    assert since is None  # absent -> SDK omits since (server uses request start time)


def test_wait_for_link_since_passed_through(fake_client):
    fake_client.wait_link = FakeWaitResult(magic_link="https://example.test/go?token=abc")

    call_tool("wait_for_link", {"inbox_id": "inbox_abc", "since": "2024-01-01T00:00:00Z"})

    inbox_id, _, since = fake_client.wait_link_calls[0]
    assert inbox_id == "inbox_abc"
    assert since == "2024-01-01T00:00:00Z"


def test_wait_for_link_result_carries_id_and_received_at(fake_client):
    fake_client.wait_link = FakeWaitResult(
        magic_link="https://example.test/go?token=abc",
        message={
            "subject": "Sign in",
            "from": "Acme <no-reply@acme.test>",
            "id": "msg_777",
            "received_at": "2024-02-02T00:00:00Z",
        },
    )

    result = call_tool("wait_for_link", {"inbox_id": "inbox_abc"})

    assert result.is_error is False
    text = result.content[0].text
    assert '"msg_777"' in text
    assert '"2024-02-02T00:00:00Z"' in text


# -- create_inbox / list_messages / delete -----------------------------------


def test_create_inbox_returns_address_and_id(fake_client):
    result = call_tool("create_inbox", {"label": "signup"})

    assert result.is_error is False
    text = result.content[0].text
    assert '"inbox_1"' in text
    assert "x@in.inboxpipe.net" in text
    assert fake_client.created == ["signup"]


def test_list_messages_passes_filters_through(fake_client):
    result = call_tool(
        "list_messages",
        {"inbox_id": "inbox_abc", "has_otp": True, "subject_contains": "code", "sender": "acme.test"},
    )

    assert result.is_error is False
    inbox_id, has_otp, subject, sender = fake_client.list_messages_calls[0]
    assert inbox_id == "inbox_abc"
    assert has_otp is True
    assert subject == "code"
    assert sender == "acme.test"


def test_get_latest_and_delete_inbox(fake_client):
    call_tool("get_latest", {"inbox_id": "inbox_abc"})
    assert fake_client.latest_calls == ["inbox_abc"]

    result = call_tool("delete_inbox", {"inbox_id": "inbox_abc"})
    assert result.is_error is False
    assert fake_client.deleted == ["inbox_abc"]


# -- send_test_code ----------------------------------------------------------


def test_send_test_code_returns_message_id_and_sent_at(fake_client):
    result = call_tool("send_test_code", {"inbox_id": "inbox_abc"})

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload == {"message_id": "msg_test1", "sent_at": "2026-10-08T12:00:00Z"}
    assert fake_client.test_code_calls == ["inbox_abc"]


def test_send_test_code_description_explains_pairing_with_wait():
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    desc = tools["send_test_code"].description
    assert "does NOT go through the mail server" in desc
    assert "since" in desc and "wait_for_otp" in desc
    assert "BEFORE calling this tool" in desc
    schema = tools["send_test_code"].input_schema
    assert set(schema["properties"]) == {"inbox_id"}
    assert schema.get("required") == ["inbox_id"]


@pytest.mark.parametrize(
    "exc, expected_substring",
    [
        (NotFound("Resource not found.", code="not_found", status=404), "Not found (HTTP 404)"),
        (
            RateLimited("Too many test codes. Try again at 13:00 UTC.", code="rate_limited", retry_after=1800.0),
            "Retry after 1800s",
        ),
        (
            MailsocketError(
                "This inbox is disabled. Enable it to receive a test code.", code="inbox_disabled", status=409
            ),
            "This inbox is disabled",
        ),
        (AuthError("Authentication required.", code="authentication_required"), "Authentication failed"),
    ],
)
def test_send_test_code_errors_are_clean_tool_errors(fake_client, exc, expected_substring):
    fake_client.test_code = exc

    result = call_tool("send_test_code", {"inbox_id": "inbox_abc"})

    assert result.is_error is True
    text = result.content[0].text
    assert expected_substring in text
    assert "Traceback" not in text


def test_send_test_code_error_never_leaks_the_key(fake_client):
    fake_client.test_code = MailsocketError(f"boom for {API_KEY} and ms_live_otherkey123")

    result = call_tool("send_test_code", {"inbox_id": "inbox_abc"})

    assert result.is_error is True
    text = result.content[0].text
    assert API_KEY not in text and "ms_live_" not in text
    assert "***" in text


# -- error mapping -----------------------------------------------------------


@pytest.mark.parametrize(
    "exc, expected_substring",
    [
        (AuthError("Authentication required.", code="authentication_required"), "Authentication failed"),
        (NotFound("Resource not found.", code="not_found"), "Not found"),
        (RateLimited("Rate limit exceeded.", retry_after=7.0), "Rate limited"),
    ],
)
def test_sdk_errors_map_to_clean_tool_errors(fake_client, exc, expected_substring):
    fake_client.wait_otp = exc

    result = call_tool("wait_for_otp", {"inbox_id": "inbox_abc"})

    assert result.is_error is True
    text = result.content[0].text
    assert expected_substring in text
    assert "Traceback" not in text
    assert API_KEY not in text


def test_api_key_never_appears_in_any_error(fake_client):
    # The SDK error message itself embeds the key — the server must redact it.
    fake_client.wait_otp = WaitTimeout(f"failed with key {API_KEY} on inbox")

    result = call_tool("wait_for_otp", {"inbox_id": "inbox_abc"})

    assert result.is_error is True
    text = result.content[0].text
    assert API_KEY not in text
    assert "***" in text


# -- startup / missing key ---------------------------------------------------


def test_missing_api_key_raises_clear_startup_error(monkeypatch):
    monkeypatch.delenv("MAILSOCKET_API_KEY", raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        _build_client_from_env()

    assert MISSING_KEY_MESSAGE in str(exc_info.value)
    assert "MAILSOCKET_API_KEY" in str(exc_info.value)


def test_version_exported():
    assert hasattr(mailsocket_mcp, "__version__")


def test_metadata_consistency_across_files():
    """Versions match everywhere: MCP 0.4.1 (pyproject, server.json, __version__)
    and its Python SDK sibling 0.3.1 (pyproject, __version__, user agent)."""
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    expected = "0.4.1"
    expected_sdk = "0.3.1"

    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == expected

    server_json = json.loads((root / "server.json").read_text())
    assert server_json["version"] == expected
    assert server_json["packages"][0]["version"] == expected
    # The MCP package must require an SDK new enough to have extra_headers.
    assert f"mailsocket>={expected_sdk}" in " ".join(pyproject["project"]["dependencies"])

    assert mailsocket_mcp.__version__ == expected
    assert mailsocket_mcp.server.__version__ == expected

    python_pyproject = tomllib.loads(
        (root.parent / "python" / "pyproject.toml").read_text()
    )
    assert python_pyproject["project"]["version"] == expected_sdk

    from mailsocket import __version__ as python_version

    assert python_version == expected_sdk

    client_src = (root.parent / "python" / "mailsocket" / "client.py").read_text()
    assert f"mailsocket-python/{expected_sdk}" in client_src


# -- ocr-found HIGH: reformatted-key redaction + broadened exception scrub ----


def test_safe_redacts_url_encoded_and_prefixed_keys(fake_client):
    from urllib.parse import quote

    from mailsocket_mcp.server import _safe

    # literal
    assert API_KEY not in _safe(f"boom {API_KEY} boom", API_KEY)
    # url-encoded form (e.g. key appearing inside a URL in an error)
    enc = quote(API_KEY, safe="")
    assert API_KEY not in _safe(f"GET /x?k={enc} failed", API_KEY)
    assert enc not in _safe(f"GET /x?k={enc} failed", API_KEY)
    # a DIFFERENT but ms_live_-shaped token is still scrubbed (defense in depth)
    other = "ms_live_SOMEOTHERSECRET99"
    assert other not in _safe(f"leaked {other} here", API_KEY)
    assert "***" in _safe(f"leaked {other} here", API_KEY)
    # A non-ms_live_-shaped key is only scrubbed when passed explicitly.
    odd = "weird-key/with+chars"
    assert odd not in _safe(f"x {odd} y", odd)
    assert quote(odd, safe="") not in _safe(f"x {quote(odd, safe='')} y", odd)


def test_unexpected_exception_is_scrubbed_not_raw_traceback(fake_client):
    # An unexpected error type (not a MailsocketError/RuntimeError) that embeds
    # the key must still become a clean, key-free tool error — never a raw raise.
    fake_client.wait_otp = KeyError(f"weird {API_KEY} boom")

    result = call_tool("wait_for_otp", {"inbox_id": "inbox_abc"})

    assert result.is_error is True
    text = result.content[0].text
    assert API_KEY not in text
    assert "Traceback" not in text


# -- refactor: per-call binding, no env fallback, configurable clamp ----------


def test_unbound_tool_call_never_falls_back_to_env(monkeypatch):
    """Outside stdio main() a tool must NOT read MAILSOCKET_API_KEY from env."""
    monkeypatch.setenv("MAILSOCKET_API_KEY", API_KEY)
    import mailsocket_mcp.server as srv

    built = []
    monkeypatch.setattr(srv, "_build_client_from_env", lambda: built.append(1))

    result = call_tool("list_inboxes", {})

    assert result.is_error is True
    assert "No mailsocket API key" in result.content[0].text
    assert built == []


def test_clamp_ceiling_follows_the_binding():
    from mailsocket_mcp import REMOTE_MAX_WAIT_TIMEOUT

    fake = FakeClient()
    fake.wait_otp = FakeWaitResult(otp="1")
    with bind_client(fake, API_KEY, max_wait_timeout=REMOTE_MAX_WAIT_TIMEOUT):
        call_tool("wait_for_otp", {"inbox_id": "i", "timeout": 9999})
    assert fake.wait_otp_calls[0][1] == REMOTE_MAX_WAIT_TIMEOUT == 55.0


def test_stdio_main_binds_env_client_once(monkeypatch):
    import mailsocket_mcp.server as srv

    monkeypatch.setenv("MAILSOCKET_API_KEY", API_KEY)
    monkeypatch.delenv("MAILSOCKET_BASE_URL", raising=False)
    seen = {}

    def fake_run(*a, **k):
        ctx = srv._current()
        seen["key"] = ctx.api_key
        seen["ceiling"] = ctx.max_wait_timeout

    monkeypatch.setattr(srv.server, "run", fake_run)
    srv.main()
    assert seen == {"key": API_KEY, "ceiling": MAX_WAIT_TIMEOUT}
    assert srv._current() is None  # binding reset after the server exits


def test_stdio_main_missing_key_exits_2(monkeypatch, capsys):
    import mailsocket_mcp.server as srv

    monkeypatch.delenv("MAILSOCKET_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        srv.main()
    assert exc_info.value.code == 2
    assert "MAILSOCKET_API_KEY" in capsys.readouterr().err
