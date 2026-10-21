"""Wait cursor: the `Mailsocket-Wait-Since` response header.

Every 200/204 of ``GET /inboxes/{id}/messages/wait`` (and the 200 of
``/stream``) reports the cutoff the call actually used: the caller's
``since``, or the request start when ``since`` was omitted. A client that
loops on 204 passes it back as ``since`` on the next call, so a message that
lands between two calls (network round trip plus client overhead) is still
matched by the second one instead of falling before a fresh cutoff.
"""

from datetime import datetime
from datetime import timezone as datetime_timezone

from django.db.models import Q

WAIT_SINCE_HEADER = "Mailsocket-Wait-Since"


def utc_cutoff(since):
    """Normalise an aware ISO ``since`` to UTC at parse time, so the query and
    the header use the same instant. A value at the very edge of the datetime
    range that cannot be shifted to UTC (year 1 with a positive offset, year
    9999 with a negative one) becomes the nearest representable UTC instant
    on the same side, and that clamped instant is BOTH the query cutoff and the
    header (so they never disagree). Strictly, a message stamped exactly at
    0001-01-01T00:00:00Z would be excluded although it is after the caller's
    instant; that cannot happen in practice because received_at is assigned by
    the server at ingest (now()), never by the sender (GPT wait-cursor r2 P3)."""
    try:
        return since.astimezone(datetime_timezone.utc)
    except (OverflowError, ValueError):
        edge = datetime.min if since.year <= 1 else datetime.max
        return edge.replace(tzinfo=datetime_timezone.utc)


def parse_iso_since(text):
    """ISO-8601 ``since`` -> aware UTC cutoff, or None when ``text`` is not an
    ISO datetime. Naive values are UTC. A well-formed but impossible value
    (e.g. ``2026-02-30T00:00:00Z``) makes Django raise ValueError; it is
    treated as "not ISO" so the caller's unparseable-since policy (epoch)
    applies instead of a 500 (GPT wait-cursor r2 P3)."""
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime

    try:
        parsed = parse_datetime(text)
    except ValueError:
        return None
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime_timezone.utc)
    return utc_cutoff(parsed)


def after_cursor(received_at, last_id):
    "Public API."
    if last_id is None:
        return Q(received_at__gt=received_at)
    return Q(received_at__gt=received_at) | Q(received_at=received_at, id__gt=last_id)


def format_wait_since(since):
    """ISO-8601 UTC with microseconds, e.g. ``2026-10-12T03:04:05.123456Z``.

    ``since`` is the effective cutoff (already representable in UTC, see
    ``utc_cutoff``); the header round-trips through ``_parse_wait_since`` to
    that same instant.
    """
    utc = since.astimezone(datetime_timezone.utc)
    return utc.replace(tzinfo=None).isoformat(timespec="microseconds") + "Z"


def with_wait_since(response, since):
    """Stamp the cutoff on a successful wait/stream response; errors (4xx)
    carry no cursor. Returns the same response."""
    if response.status_code in (200, 204):
        response[WAIT_SINCE_HEADER] = format_wait_since(since)
    return response
