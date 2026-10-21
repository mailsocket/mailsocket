"""Mailsocket-Wait-Since: the wait endpoints report the cutoff they used.

A client that loops on 204 without `since` used to start a fresh cutoff on
every call, so a message stored in the gap between two calls (network round
trip + client overhead) was never returned. Every 200/204 of
`messages/wait` (and the 200 of `stream`) now carries the effective cutoff;
passing it back as `since` closes the gap.
"""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from urllib.parse import quote

import pytest
from django.utils import timezone

from apps.api.tests.test_wait import auth_client, make_message, make_user, wait_url
from apps.api.views import _parse_wait_since
from apps.api.wait_cursor import WAIT_SINCE_HEADER
from apps.api.wait_cursor import format_wait_since as _format_wait_since
from apps.inboxes.models import Inbox

pytestmark = pytest.mark.django_db(transaction=True)

HEADER = "Mailsocket-Wait-Since"


def _fast(monkeypatch):
    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)


def _parse_header(value):
    assert value.endswith("Z"), value
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt_timezone.utc)


def test_header_name_constant():
    assert WAIT_SINCE_HEADER == HEADER


def test_format_is_utc_with_microseconds_and_round_trips():
    aware = datetime(2026, 10, 12, 10, 4, 5, 123456, tzinfo=dt_timezone(timedelta(hours=7)))
    text = _format_wait_since(aware)
    assert text == "2026-10-12T03:04:05.123456Z"
    assert _parse_wait_since(text) == aware
    # Whole seconds still carry six fractional digits.
    assert _format_wait_since(datetime(2026, 1, 1, tzinfo=dt_timezone.utc)) == "2026-01-01T00:00:00.000000Z"


@pytest.mark.parametrize(
    ("since", "utc"),
    [
        # Unshiftable edges are normalised to UTC at PARSE time ...
        ("0001-01-01T00:00:00+07:00", datetime.min.replace(tzinfo=dt_timezone.utc)),
        ("9999-12-31T23:59:00-07:00", datetime.max.replace(tzinfo=dt_timezone.utc)),
        # ... ordinary offsets are just converted.
        ("0001-01-01T12:00:00+07:00", datetime(1, 1, 1, 5, tzinfo=dt_timezone.utc)),
        ("2026-10-12T10:04:05.123456+07:00", datetime(2026, 10, 12, 3, 4, 5, 123456, tzinfo=dt_timezone.utc)),
    ],
)
def test_iso_since_is_utc_at_parse_and_header_is_that_exact_instant(since, utc):
    """P3 (r2): the query cutoff and the header are the same instant. The
    parsed cutoff is already UTC, and the header parses back to it exactly."""
    parsed = _parse_wait_since(since)
    assert parsed == utc
    assert parsed.utcoffset() == timedelta(0)
    header = _format_wait_since(parsed)
    assert _parse_wait_since(header) == parsed
    assert _parse_header(header) == parsed


def test_pathological_since_header_equals_query_cutoff(monkeypatch):
    """End to end: since=year-1+07:00 matches an existing message (cutoff is
    the minimum instant) and the header reports exactly that cutoff."""
    _fast(monkeypatch)
    owner = make_user("wc_edge_year1")
    inbox = Inbox.objects.create(user=owner, routing_key="wc_edge_year1_box")
    client = auth_client(owner)
    make_message(
        inbox, "old", otp="121212", otp_confidence=0.9, received_at=timezone.now() - timedelta(days=1)
    )

    response = client.get(f"{wait_url(inbox)}?since={quote('0001-01-01T00:00:00+07:00')}&timeout=1")
    assert response.status_code == 200
    assert response[HEADER] == "0001-01-01T00:00:00.000000Z"

    future = client.get(f"{wait_url(inbox)}?since={quote('9999-12-31T23:59:00-07:00')}&timeout=1")
    assert future.status_code == 204
    assert future[HEADER] == "9999-12-31T23:59:59.999999Z"


def test_204_carries_header_equal_to_request_start(monkeypatch):
    _fast(monkeypatch)
    owner = make_user("wc_204")
    inbox = Inbox.objects.create(user=owner, routing_key="wc_204_box")
    client = auth_client(owner)

    before = timezone.now()
    response = client.get(f"{wait_url(inbox)}?timeout=1")
    after = timezone.now()

    assert response.status_code == 204
    cutoff = _parse_header(response[HEADER])
    # Request start: after the call began, before the 1 s block ended.
    assert before <= cutoff <= after
    assert cutoff < after - timedelta(seconds=0.9)


def test_200_carries_header(monkeypatch):
    _fast(monkeypatch)
    owner = make_user("wc_200")
    inbox = Inbox.objects.create(user=owner, routing_key="wc_200_box")
    client = auth_client(owner)
    make_message(inbox, "old", otp="111111", otp_confidence=0.9)

    response = client.get(f"{wait_url(inbox)}?since=0&timeout=1")

    assert response.status_code == 200
    assert response[HEADER] == "1970-01-01T00:00:00.000000Z"


def test_explicit_message_id_header_is_that_messages_received_at(monkeypatch):
    _fast(monkeypatch)
    owner = make_user("wc_msgid")
    inbox = Inbox.objects.create(user=owner, routing_key="wc_msgid_box")
    client = auth_client(owner)
    anchor = make_message(
        inbox, "anchor", received_at=timezone.now() - timedelta(minutes=2, microseconds=1234)
    )
    anchor.refresh_from_db()

    response = client.get(f"{wait_url(inbox)}?since={anchor.public_id}&timeout=1")

    assert response.status_code == 204
    assert response[HEADER] == _format_wait_since(anchor.received_at)
    assert _parse_header(response[HEADER]) == anchor.received_at


def test_explicit_unix_seconds_header(monkeypatch):
    _fast(monkeypatch)
    owner = make_user("wc_unix")
    inbox = Inbox.objects.create(user=owner, routing_key="wc_unix_box")
    client = auth_client(owner)

    response = client.get(f"{wait_url(inbox)}?since=4102444800.25&timeout=1")

    assert response.status_code == 204
    assert response[HEADER] == "2100-01-01T00:00:00.250000Z"


def test_explicit_unix_milliseconds_header(monkeypatch):
    _fast(monkeypatch)
    owner = make_user("wc_unixms")
    inbox = Inbox.objects.create(user=owner, routing_key="wc_unixms_box")
    client = auth_client(owner)

    response = client.get(f"{wait_url(inbox)}?since=4102444800123&timeout=1")

    assert response.status_code == 204
    assert response[HEADER] == "2100-01-01T00:00:00.123000Z"


def test_explicit_iso_with_offset_header_is_normalised_to_utc(monkeypatch):
    _fast(monkeypatch)
    owner = make_user("wc_iso")
    inbox = Inbox.objects.create(user=owner, routing_key="wc_iso_box")
    client = auth_client(owner)

    since = quote("2100-01-01T07:00:00.5+07:00")
    response = client.get(f"{wait_url(inbox)}?since={since}&timeout=1")

    assert response.status_code == 204
    assert response[HEADER] == "2100-01-01T00:00:00.500000Z"


def test_explicit_naive_iso_is_read_as_utc(monkeypatch):
    _fast(monkeypatch)
    owner = make_user("wc_naive")
    inbox = Inbox.objects.create(user=owner, routing_key="wc_naive_box")
    client = auth_client(owner)

    response = client.get(f"{wait_url(inbox)}?since=2100-01-01T00:00:00&timeout=1")

    assert response.status_code == 204
    assert response[HEADER] == "2100-01-01T00:00:00.000000Z"


def test_header_absent_on_errors(monkeypatch):
    _fast(monkeypatch)
    owner = make_user("wc_err")
    other = make_user("wc_err_other")
    foreign = Inbox.objects.create(user=other, routing_key="wc_err_foreign")
    client = auth_client(owner)

    not_found = client.get(f"{wait_url(foreign)}?timeout=1")
    assert not_found.status_code == 404
    assert not not_found.has_header(HEADER)

    own = Inbox.objects.create(user=owner, routing_key="wc_err_own")
    invalid = client.get(f"{wait_url(own)}?require=links&timeout=1")
    assert invalid.status_code == 422
    assert not invalid.has_header(HEADER)


def test_header_absent_on_429(monkeypatch):
    from apps.accounts.models import AccountPlan
    from apps.accounts.services import acquire_wait_lease

    _fast(monkeypatch)
    monkeypatch.setattr("apps.accounts.services.WAIT_LEASE_LIMIT_FREE", 1)
    owner = make_user("wc_429", plan=AccountPlan.Plan.FREE)
    inbox = Inbox.objects.create(user=owner, routing_key="wc_429_box")
    client = auth_client(owner)
    assert acquire_wait_lease(owner.pk) is True

    response = client.get(f"{wait_url(inbox)}?timeout=1")

    assert response.status_code == 429
    assert not response.has_header(HEADER)


def test_gap_message_is_returned_when_call_2_passes_the_header(monkeypatch):
    """The actual gap: call 1 (no since) returns 204 with cutoff C1. A message
    is then stored with received_at just after C1 (it landed after call 1 had
    finished polling, before call 2 started). Call 2 without since starts a
    fresh cutoff later than that message and misses it; call 2 with
    since=<C1 header> returns it."""
    _fast(monkeypatch)
    owner = make_user("wc_gap")
    inbox = Inbox.objects.create(user=owner, routing_key="wc_gap_box")
    client = auth_client(owner)

    call_1 = client.get(f"{wait_url(inbox)}?timeout=1")
    assert call_1.status_code == 204
    cutoff_1 = _parse_header(call_1[HEADER])

    # Stored after call 1 returned, stamped inside the gap: strictly after
    # call 1's cutoff, and (since call 1 blocked ~1 s) before any call 2 start.
    gap_received_at = cutoff_1 + timedelta(microseconds=1)
    assert gap_received_at < timezone.now()
    gap = make_message(inbox, "gap", otp="777777", otp_confidence=0.9, received_at=gap_received_at)

    # Old client behaviour (no since): the gap message is missed.
    missed = client.get(f"{wait_url(inbox)}?timeout=1")
    assert missed.status_code == 204
    assert _parse_header(missed[HEADER]) > gap_received_at

    # New client behaviour: send call 1's header back.
    call_2 = client.get(f"{wait_url(inbox)}?since={quote(call_1[HEADER])}&timeout=1")
    assert call_2.status_code == 200
    assert call_2.json()["data"]["id"] == gap.public_id
    assert call_2.json()["data"]["otp"] == "777777"
    # The cursor stays put (the explicit since is echoed back).
    assert call_2[HEADER] == call_1[HEADER]


def test_message_exactly_at_cutoff_is_not_returned(monkeypatch):
    """`since` is strictly-after; echoing the cutoff never re-delivers a
    message stamped at that exact microsecond (e.g. the `msg_` anchor)."""
    _fast(monkeypatch)
    owner = make_user("wc_edge")
    inbox = Inbox.objects.create(user=owner, routing_key="wc_edge_box")
    client = auth_client(owner)
    at = timezone.now() - timedelta(seconds=10)
    make_message(inbox, "edge", otp="888888", otp_confidence=0.9, received_at=at)

    response = client.get(f"{wait_url(inbox)}?since={quote(_format_wait_since(at))}&timeout=1")

    assert response.status_code == 204


def test_stream_200_carries_header(monkeypatch):
    monkeypatch.setattr("apps.api.views.SSE_POLL_SECONDS", 0.01)
    monkeypatch.setattr("apps.api.views.SSE_MAX_SECONDS", 0.2)
    owner = make_user("wc_sse")
    inbox = Inbox.objects.create(user=owner, routing_key="wc_sse_box")
    client = auth_client(owner)

    response = client.get(f"/api/v1/inboxes/{inbox.public_id}/stream?since=4102444800", stream=True)
    assert response.status_code == 200
    assert response[HEADER] == "2100-01-01T00:00:00.000000Z"
    b"".join(response.streaming_content)

    before = timezone.now()
    default = client.get(f"/api/v1/inboxes/{inbox.public_id}/stream", stream=True)
    assert default.status_code == 200
    assert _parse_header(default[HEADER]) >= before
    b"".join(default.streaming_content)


def test_cors_is_inert_or_exposes_the_header(monkeypatch):
    """The API serves no CORS today (corsheaders installed, no allowed
    origins), so a browser cannot read any response header cross-origin and
    there is nothing to expose. If CORS is ever switched on, this fails until
    the header is listed in CORS_EXPOSE_HEADERS."""
    from corsheaders.conf import conf

    _fast(monkeypatch)
    owner = make_user("wc_cors")
    inbox = Inbox.objects.create(user=owner, routing_key="wc_cors_box")
    client = auth_client(owner)

    response = client.get(f"{wait_url(inbox)}?timeout=1", HTTP_ORIGIN="https://app.example.test")
    assert response.status_code == 204
    if response.has_header("Access-Control-Allow-Origin"):
        assert HEADER in conf.CORS_EXPOSE_HEADERS
        assert HEADER in response["Access-Control-Expose-Headers"]





def _stream_otps(client, inbox, since):
    import json

    response = client.get(f"/api/v1/inboxes/{inbox.public_id}/stream?since={quote(since)}", stream=True)
    assert response.status_code == 200
    body = b"".join(response.streaming_content).decode()
    return [
        json.loads(line[len("data: ") :])["otp"]
        for line in body.splitlines()
        if line.startswith("data: ") and '"otp"' in line
    ]


def _stream_fast(monkeypatch):
    monkeypatch.setattr("apps.api.views.SSE_POLL_SECONDS", 0.01)
    monkeypatch.setattr("apps.api.views.SSE_MAX_SECONDS", 0.2)


def test_stream_since_msg_anchor_does_not_emit_the_anchor(monkeypatch):
    _stream_fast(monkeypatch)
    owner = make_user("wc_sse_anchor")
    inbox = Inbox.objects.create(user=owner, routing_key="wc_sse_anchor_box")
    client = auth_client(owner)
    at = timezone.now() - timedelta(seconds=10)
    anchor = make_message(inbox, "anchor", otp="100001", otp_confidence=0.9, received_at=at)
    make_message(inbox, "after", otp="100002", otp_confidence=0.9, received_at=at + timedelta(microseconds=1))

    assert _stream_otps(client, inbox, anchor.public_id) == ["100002"]


def test_stream_message_exactly_at_iso_cutoff_is_not_emitted(monkeypatch):
    _stream_fast(monkeypatch)
    owner = make_user("wc_sse_iso")
    inbox = Inbox.objects.create(user=owner, routing_key="wc_sse_iso_box")
    client = auth_client(owner)
    at = timezone.now() - timedelta(seconds=10)
    make_message(inbox, "edge", otp="200001", otp_confidence=0.9, received_at=at)
    make_message(
        inbox, "before", otp="200000", otp_confidence=0.9, received_at=at - timedelta(microseconds=1)
    )

    assert _stream_otps(client, inbox, _format_wait_since(at)) == []
    # Control: one microsecond earlier the edge message IS emitted (once).
    assert _stream_otps(client, inbox, _format_wait_since(at - timedelta(microseconds=1))) == ["200001"]


def test_stream_same_timestamp_burst_emitted_exactly_once(monkeypatch):
    "Public API."
    _stream_fast(monkeypatch)
    owner = make_user("wc_sse_burst")
    inbox = Inbox.objects.create(user=owner, routing_key="wc_sse_burst_box")
    client = auth_client(owner)
    at = timezone.now() - timedelta(seconds=10)
    for i in range(25):
        make_message(inbox, f"b{i}", otp=f"3{i:05d}", otp_confidence=0.9, received_at=at)

    otps = _stream_otps(client, inbox, "0")
    assert sorted(otps) == sorted(f"3{i:05d}" for i in range(25))


@pytest.mark.parametrize("since", ["2026-02-30T00:00:00Z", "2026-13-01T00:00:00Z", "2026-10-12T25:00:00Z"])
def test_impossible_calendar_since_is_epoch_not_500(since, monkeypatch):
    """GPT wait-cursor r2 P3: Django's parse_datetime raises ValueError for a
    well-formed but impossible date; that used to surface as HTTP 500. It now
    follows the unparseable-since policy (epoch) and the header says so."""
    assert _parse_wait_since(since) == datetime(1970, 1, 1, tzinfo=dt_timezone.utc)
    _fast(monkeypatch)
    owner = make_user("wc_bad_cal_" + str(abs(hash(since)) % 10_000))
    inbox = Inbox.objects.create(user=owner, routing_key="wc_bad_cal_box_" + str(abs(hash(since)) % 10_000))
    response = auth_client(owner).get(f"{wait_url(inbox)}?since={quote(since)}&timeout=1")
    assert response.status_code == 204
    assert response[HEADER] == "1970-01-01T00:00:00.000000Z"
