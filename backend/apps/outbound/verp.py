"""VERP bounce-forgery hardening ( Part A).

A keyed MAC over the OutboundEmail public_id, appended to the VERP
localpart, so a bounce is only honored if it carries a MAC only mailsocket
could have produced. This stops a recipient from forging a DSN for a
public_id they merely learned about (e.g. via the API); it does NOT (and
per the design cannot, with a single shared envelope sender) stop a
recipient forging a bounce for mail they actually received — that is
addressed separately in apps.outbound.services.record_hard_bounce's
single-recipient suppression rule (design §D2 Part B).
"""
import hmac
import re
from hashlib import sha256

from django.conf import settings

# public_id is at most 32 chars (model max_length=32; the real value is 28 =
# "obe_" + token_urlsafe(18)). The base64url alphabet has no ".", so "." is
# an unambiguous separator — split on the LAST "." to be safe even if a
# future public_id shape ever contained one. "." is RFC 5321-safe
# mid-localpart.
_MAC_HEX_LENGTH = 20  # 80 bits, lowercase hex — survives case-folding MTAs.
_HEX_MAC_RE = re.compile(r"[0-9a-fA-F]{20}")
MAX_LOCALPART_LENGTH = 64


def _verp_key() -> bytes:
    # Domain-separated from the Fernet derivation in dkim_keys.py (a plain
    # sha256 of the master) via a distinct context string.
    return hmac.new(
        settings.OUTBOUND_DKIM_MASTER_KEY.encode("utf-8"),
        b"mailsocket-verp-key-v1",
        sha256,
    ).digest()


def _mac_for(public_id: str) -> str:
    return hmac.new(
        _verp_key(), b"mailsocket-verp-v1\0" + public_id.encode("utf-8"), sha256
    ).hexdigest()[:_MAC_HEX_LENGTH]


def build_verp_localpart(public_id: str) -> str:
    """Build the localpart ``{public_id}.{mac}`` for the VERP envelope
    sender. Asserts the result fits the RFC 5321 localpart length ceiling."""
    localpart = f"{public_id}.{_mac_for(public_id)}"
    assert len(localpart) <= MAX_LOCALPART_LENGTH
    return localpart


def verify_verp_localpart(localpart: str) -> str | None:
    """Return the public_id if ``localpart`` carries a valid MAC for it,
    else None. Never raises — the caller treats None as "not a genuine
    bounce for anything we sent" and routes to the UNPARSED/replay path."""
    if "." not in localpart:
        return None
    public_id, _, mac = localpart.rpartition(".")
    if not public_id or not mac:
        return None
    if len(mac) != _MAC_HEX_LENGTH:
        return None
    if not _HEX_MAC_RE.fullmatch(mac):
        return None
    expected = _mac_for(public_id)
    if not hmac.compare_digest(expected, mac.lower()):
        return None
    return public_id
