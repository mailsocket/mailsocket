"""Synchronous HTTP client for the mailsocket v1 REST API.

Pure standard library (``urllib``) — no runtime dependencies.
"""

from __future__ import annotations

import json
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .errors import AuthError, MailsocketError, NotFound, RateLimited, WaitTimeout

DEFAULT_BASE_URL = "https://dash.mailsocket.app/api/v1"
WAIT_TIMEOUT_MAX = 25  # seconds — the server clamps the per-call wait to [1, 25]
WAIT_SOCKET_BUFFER = 10  # seconds of client-side slack above the server block
DEFAULT_RETRY_AFTER = 1.0  # seconds, when a 429 carries no Retry-After header
# Response header with the cutoff a wait call actually used (server >= 2026-10).
WAIT_SINCE_HEADER = "mailsocket-wait-since"

__all__ = ["Client", "Page", "WaitResult"]


def _monotonic() -> float:
    return _time.monotonic()


def _sleep(seconds: float) -> None:
    _time.sleep(seconds)


def _seg(value) -> str:
    """Percent-encode an id as ONE path segment (``/``, ``?``, ``#``, ``..`` safe).

    An empty id and the bare dot segments ``.`` / ``..`` are rejected with a
    :class:`MailsocketError` before any request is made (same contract as the
    TypeScript SDK's ``seg``): urllib's request machinery would otherwise let
    them collapse into a different path than the caller intended. No real id
    is ever one of these.
    """
    raw = str(value)
    if raw in ("", ".", ".."):
        raise MailsocketError(
            f"Invalid id {raw!r}: ids must be non-empty and not \".\" or \"..\".",
            code="invalid_id",
        )
    return urllib.parse.quote(raw, safe="")


class Page:
    """A page of results plus its pagination metadata.

    Behaves like a list of ``.data`` while exposing ``.pagination``,
    ``.next_cursor`` and ``.has_more``.
    """

    def __init__(self, data: list, pagination: dict | None):
        self.data = data
        self.pagination = pagination or {}

    @property
    def next_cursor(self):
        return self.pagination.get("next_cursor")

    @property
    def has_more(self) -> bool:
        return bool(self.pagination.get("has_more"))

    def __len__(self) -> int:
        return len(self.data)

    def __iter__(self):
        return iter(self.data)

    def __getitem__(self, index):
        return self.data[index]

    def __repr__(self) -> str:
        return f"Page(data={self.data!r}, pagination={self.pagination!r})"


@dataclass(frozen=True, eq=False)
class WaitResult:
    """The outcome of a ``wait_for_otp`` / ``wait_for_link`` / ``wait`` call.

    #31: a frozen dataclass, backward compatible with
    the plain-class shape it replaces — same four public attributes
    (``.message``, ``.otp``, ``.confidence``, ``.magic_link``), the derived
    ``.link`` property, and the same ``repr()``/``str()``. Instances are now
    immutable (``frozen=True``); ``eq=False`` keeps equality/hashing
    identity-based exactly as the old plain class, since ``message`` is a
    dict and value-equality would make ``hash()`` raise ``TypeError``.
    """

    message: dict
    otp: str | None = field(init=False)
    confidence: float | None = field(init=False)
    magic_link: str | None = field(init=False)

    def __post_init__(self):
        # frozen=True disallows plain attribute assignment even inside
        # __post_init__, hence object.__setattr__.
        object.__setattr__(self, "otp", self.message.get("otp"))
        object.__setattr__(self, "confidence", self.message.get("otp_confidence"))
        object.__setattr__(self, "magic_link", self.message.get("magic_link"))

    @property
    def link(self):
        """Alias for ``magic_link``."""
        return self.magic_link

    def __repr__(self) -> str:
        return (
            f"WaitResult(otp={self.otp!r}, confidence={self.confidence!r}, "
            f"magic_link={self.magic_link!r})"
        )

    def __str__(self) -> str:
        return self.otp if self.otp is not None else (self.magic_link or "")


class Client:
    """A thin, synchronous client for the mailsocket v1 REST API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        request_timeout: float = 30.0,
        extra_headers: dict[str, str] | None = None,
        retry_wait_on_429: bool = True,
    ):
        """Create a client.

        ``extra_headers`` (optional) are sent on every request, e.g. a fixed
        ``Host`` when calling the API through an internal address. They can
        never override ``Authorization``; that header always carries
        ``api_key``.

        ``retry_wait_on_429`` (default True): the ``wait*`` helpers sleep for
        ``Retry-After`` and retry on HTTP 429 until their deadline. Pass False
        to raise :class:`RateLimited` immediately instead (used by the shared
        remote MCP server so a rate-limited caller doesn't hold a worker slot).
        """
        if not api_key:
            raise ValueError("api_key is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._request_timeout = request_timeout
        self._retry_wait_on_429 = retry_wait_on_429
        self._extra_headers = {
            str(name): str(value)
            for name, value in (extra_headers or {}).items()
            if str(name).lower() != "authorization"
        }

    # -- public API -----------------------------------------------------------

    def create_inbox(self, label: str | None = None) -> dict:
        body = {} if label is None else {"label": label}
        payload = self._request("POST", "/inboxes", json_body=body)
        return payload["data"]

    def list_inboxes(self, limit: int | None = None, cursor: str | None = None) -> Page:
        payload = self._request("GET", "/inboxes", params=self._page_params(limit, cursor))
        return Page(payload["data"], payload.get("pagination"))

    def get_inbox(self, inbox_id: str) -> dict:
        payload = self._request("GET", f"/inboxes/{_seg(inbox_id)}")
        return payload["data"]

    def delete_inbox(self, inbox_id: str) -> None:
        self._request("DELETE", f"/inboxes/{_seg(inbox_id)}")

    def list_messages(
        self,
        inbox_id: str,
        has_otp: bool | None = None,
        subject_contains: str | None = None,
        sender: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Page:
        params = self._page_params(limit, cursor)
        if has_otp is not None:
            params["has_otp"] = "true" if has_otp else "false"
        if subject_contains:
            params["subject_contains"] = subject_contains
        if sender:
            params["from"] = sender  # the API names this filter ``from``
        payload = self._request("GET", f"/inboxes/{_seg(inbox_id)}/messages", params=params)
        return Page(payload["data"], payload.get("pagination"))

    def get_latest(self, inbox_id: str) -> dict:
        payload = self._request("GET", f"/inboxes/{_seg(inbox_id)}/messages/latest")
        return payload["data"]

    def get_message(self, message_id: str) -> dict:
        payload = self._request("GET", f"/messages/{_seg(message_id)}")
        return payload["data"]

    def send_test_code(self, inbox_id: str) -> dict:
        """Put one sample OTP email (a random 6-digit code) into ``inbox_id``.

        Calls ``POST /inboxes/{id}/test-code`` and returns its ``data`` dict:
        ``{"message_id": "msg_...", "sent_at": "..."}``. The message does NOT
        go through the mail server: it is stored and parsed like real inbound
        mail, so a pending or later wait (with a ``since`` from before this
        call) returns it. Use it to prove the wait loop end to end without
        sending real email.

        Shares the dashboard "Send a test code" quota (5 per inbox, 20 per
        account per hour). Errors: :class:`NotFound` for an unknown, deleted
        or foreign inbox; :class:`RateLimited` (with ``retry_after``) over the
        quota; :class:`MailsocketError` with ``code="inbox_disabled"``
        (HTTP 409) for a disabled inbox.
        """
        payload = self._request("POST", f"/inboxes/{_seg(inbox_id)}/test-code")
        return payload["data"]

    # -- the moat -------------------------------------------------------------

    def wait_for_otp(
        self,
        inbox_id: str,
        *,
        timeout: float = 60.0,
        min_confidence: float = 0.0,
        since=None,
    ) -> WaitResult:
        """Wait for an OTP. Returns a :class:`WaitResult` with ``.otp`` etc.

        ``since`` defaults to ``None``, which omits the param entirely so the
        server's own default applies (the request start time) — a caller
        only sees messages that arrive during this call, never a stale OTP
        already sitting in a reused inbox. Pass ``since=0`` to restore the
        old behaviour: match any message already in the inbox.
        """
        return self._wait(
            inbox_id,
            require="otp",
            timeout=timeout,
            min_confidence=min_confidence,
            since=since,
        )

    def wait_for_link(self, inbox_id: str, *, timeout: float = 60.0, since=None) -> WaitResult:
        """Wait for a magic link. Returns a :class:`WaitResult` with ``.link``.

        ``since`` defaults to ``None`` (omitted -> server uses the request
        start time). Pass ``since=0`` to include messages already in the inbox.
        """
        return self._wait(
            inbox_id, require="link", timeout=timeout, min_confidence=None, since=since
        )

    def wait(
        self,
        inbox_id: str,
        *,
        timeout: float = 60.0,
        min_confidence: float = 0.0,
        since=None,
    ) -> WaitResult:
        """Wait for either an OTP or a magic link.

        ``since`` defaults to ``None`` (omitted -> server uses the request
        start time). Pass ``since=0`` to include messages already in the inbox.
        """
        return self._wait(
            inbox_id,
            require="any",
            timeout=timeout,
            min_confidence=min_confidence,
            since=since,
        )

    # -- internals ------------------------------------------------------------

    def _wait(
        self,
        inbox_id: str,
        *,
        require: str,
        timeout: float,
        min_confidence: float | None,
        since,
    ) -> WaitResult:
        deadline = _monotonic() + float(timeout)
        # No caller ``since``: the first call omits it (server default = the
        # request start), and every later call sends back the cutoff the
        # previous response reported, so a message that lands between two
        # calls is still matched. An older server sends no header; then each
        # call keeps omitting ``since`` exactly as before.
        cursor = since
        while True:
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise WaitTimeout(
                    f"No matching message arrived within {timeout:g}s.", code="wait_timeout"
                )
            server_timeout = max(1.0, min(float(WAIT_TIMEOUT_MAX), remaining))
            params = {"timeout": server_timeout, "require": require}
            if cursor is not None:
                params["since"] = cursor
            if min_confidence is not None:
                params["min_confidence"] = min_confidence

            status, headers, body = self._request_http(
                "GET",
                f"/inboxes/{_seg(inbox_id)}/messages/wait",
                params=params,
                socket_timeout=server_timeout + WAIT_SOCKET_BUFFER,
            )
            payload = self._parse_json(body)
            if status == 200:
                return WaitResult(payload["data"])
            if status == 204:
                cursor = self._next_cursor(since, cursor, headers)
                continue
            if status == 429:
                if not self._retry_wait_on_429:
                    self._raise_for_status(status, headers, payload)
                retry_after = self._retry_after(headers)
                sleep_for = retry_after if retry_after is not None else DEFAULT_RETRY_AFTER
                if _monotonic() + sleep_for > deadline:
                    raise WaitTimeout(
                        f"No matching message arrived within {timeout:g}s.", code="wait_timeout"
                    )
                _sleep(sleep_for)
                continue
            self._raise_for_status(status, headers, payload)

    @staticmethod
    def _next_cursor(since, cursor, headers: dict):
        """The ``since`` for the next wait call: the caller's own value when
        given, else the server-reported cutoff of this call, else unchanged
        (an older server sends no header)."""
        if since is not None:
            return cursor
        return headers.get(WAIT_SINCE_HEADER) or cursor

    def _request(self, method: str, path: str, *, params: dict | None = None, json_body=None):
        status, headers, body = self._request_http(method, path, params=params, json_body=json_body)
        payload = self._parse_json(body)
        if 200 <= status < 300:
            return payload
        self._raise_for_status(status, headers, payload)

    def _request_http(self, method, path, *, params=None, json_body=None, socket_timeout=None):
        url = self._base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        headers = {
            "Accept": "application/json",
            "User-Agent": "mailsocket-python/0.3.1",
        }
        headers.update(self._extra_headers)
        # Set last so nothing in extra_headers can ever replace it.
        headers["Authorization"] = f"Bearer {self._api_key}"
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        timeout = socket_timeout if socket_timeout is not None else self._request_timeout
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.getcode()
                response_headers = {k.lower(): v for k, v in response.headers.items()}
                body = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_headers = {k.lower(): v for k, v in exc.headers.items()}
            body = exc.read()
        except urllib.error.URLError as exc:
            raise MailsocketError(f"Network error: {exc.reason}") from exc
        return status, response_headers, body

    def _raise_for_status(self, status: int, headers: dict, payload):
        code = None
        message = None
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            err = payload["error"]
            code = err.get("code")
            message = err.get("message")
        if status == 401:
            raise AuthError(
                message or "Authentication required.",
                code=code or "authentication_required",
                status=401,
            )
        if status == 404:
            raise NotFound(
                message or "Resource not found.", code=code or "not_found", status=404
            )
        if status == 429:
            raise RateLimited(
                message or "Rate limit exceeded.",
                code=code or "rate_limited",
                subcode=code,
                retry_after=self._retry_after(headers),
            )
        raise MailsocketError(message or f"HTTP {status}", code=code, status=status)

    @staticmethod
    def _parse_json(body: bytes) -> dict:
        if not body:
            return {}
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    @staticmethod
    def _retry_after(headers: dict) -> float | None:
        value = headers.get("retry-after")
        if value is None:
            return None
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return None
        # A negative Retry-After (malformed server/proxy) must never become a
        # negative sleep; clamp to 0 so the loop re-polls immediately instead.
        return max(0.0, seconds)

    @staticmethod
    def _page_params(limit, cursor) -> dict:
        params = {}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        return params
