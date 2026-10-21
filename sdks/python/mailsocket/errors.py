"""Typed exceptions raised by the mailsocket Python SDK."""

from __future__ import annotations


class MailsocketError(Exception):
    """Base class for every error the SDK raises."""

    def __init__(
        self, message: str, *, code: str | None = None, status: int | None = None
    ):
        super().__init__(message)
        self.code = code
        self.status = status


class AuthError(MailsocketError):
    """The API key is missing or invalid (HTTP 401)."""


class NotFound(MailsocketError):
    """The requested resource does not exist or is not owned (HTTP 404)."""


class RateLimited(MailsocketError):
    """A rate limit was hit (HTTP 429).

    Carries ``subcode`` (the server error code, e.g. ``rate_limited``,
    ``too_many_wait_requests`` or ``wait_capacity``) and ``retry_after``
    (seconds to wait, from the ``Retry-After`` header, or ``None``).
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        subcode: str | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message, code=code, status=429)
        self.subcode = subcode
        self.retry_after = retry_after


class WaitTimeout(MailsocketError):
    """No matching message arrived before the overall wait deadline."""
