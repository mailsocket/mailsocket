"""Official Python SDK for the mailsocket v1 REST API."""

from .client import Client, Page, WaitResult
from .errors import AuthError, MailsocketError, NotFound, RateLimited, WaitTimeout

__version__ = "0.3.1"

__all__ = [
    "Client",
    "Page",
    "WaitResult",
    "MailsocketError",
    "AuthError",
    "NotFound",
    "RateLimited",
    "WaitTimeout",
]
