"Public API."
import hashlib
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, AccountPlan, MonthlyUsage
from apps.accounts.services import (
    acquire_wait_capacity,
    acquire_wait_lease,
    create_api_key,
    release_wait_capacity,
)
from apps.inboxes.models import Inbox, Message

pytestmark = pytest.mark.django_db(transaction=True)


def make_user(name, *, plan=AccountPlan.Plan.PAID):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user, plan=plan)
    return user


def auth_client(user):
    _, token = create_api_key(user, "hardening-test")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client





def test_verify_does_not_mis_record_usage_against_api_key_pk(monkeypatch):
    "Public API."
    owner = make_user("verify_usage")
    client = auth_client(owner)

    monkeypatch.setattr(
        "apps.api.verification.dns.resolver.Resolver.resolve",
        lambda self, domain, rdtype: (_ for _ in ()).throw(
            __import__("dns.resolver", fromlist=["NXDOMAIN"]).NXDOMAIN()
        ),
    )

    client.post("/api/v1/verify", {"email": "someone@example.test"}, format="json")

    period_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).date()
    usage = MonthlyUsage.objects.filter(user=owner, period_start=period_start).first()
    assert usage is not None
    # Exactly one increment from BearerAPIView.initial(); no second explicit
    # record_api_request(request.auth) call inflating or mis-keying this.
    assert usage.api_requests == 1





def test_agent_register_global_cap_returns_429(monkeypatch):
    from apps.accounts import services as services_mod

    monkeypatch.setattr(services_mod, "AGENT_REGISTER_GLOBAL_RATE_LIMIT", 1)
    client = APIClient()

    first = client.post("/api/v1/agents/register", {"name": "a"}, format="json")
    assert first.status_code == 201

    second = client.post("/api/v1/agents/register", {"name": "b"}, format="json")
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"





def test_free_plan_wait_capacity_capped_below_global_limit(monkeypatch):
    monkeypatch.setattr("apps.accounts.services.WAIT_CAPACITY_LIMIT", 10)
    monkeypatch.setattr("apps.accounts.services.WAIT_CAPACITY_FREE_LIMIT", 2)

    free_a = make_user("free_slot_a", plan=AccountPlan.Plan.FREE)
    free_b = make_user("free_slot_b", plan=AccountPlan.Plan.FREE)
    free_c = make_user("free_slot_c", plan=AccountPlan.Plan.FREE)

    assert acquire_wait_lease(free_a.pk) is True
    assert acquire_wait_capacity(free=True) is True
    assert acquire_wait_lease(free_b.pk) is True
    assert acquire_wait_capacity(free=True) is True
    # Third FREE waiter is refused even though the global pool (10) has tons
    # of headroom left — the free sub-pool (2) is what's exhausted.
    assert acquire_wait_lease(free_c.pk) is True
    assert acquire_wait_capacity(free=True) is False

    # A PAID account can still get a slot: the free sub-cap never touched the
    # paid headroom inside the shared global pool.
    paid = make_user("paid_after_free_exhaustion", plan=AccountPlan.Plan.PAID)
    assert acquire_wait_lease(paid.pk) is True
    assert acquire_wait_capacity(free=False) is True


def test_free_agent_key_cannot_starve_paid_wait_endpoint(monkeypatch):
    """End-to-end: with the free sub-pool exhausted, a PAID account's real
    wait request still succeeds (not 429), proving free/agent traffic (C1)
    cannot starve the flagship OTP-wait feature for paying customers."""
    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    monkeypatch.setattr("apps.accounts.services.WAIT_CAPACITY_LIMIT", 16)
    monkeypatch.setattr("apps.accounts.services.WAIT_CAPACITY_FREE_LIMIT", 1)

    # Simulate an attacker having minted many agent accounts and exhausting
    # the FREE sub-pool (not the global one).
    attacker = make_user("attacker_free", plan=AccountPlan.Plan.FREE)
    assert acquire_wait_lease(attacker.pk) is True
    assert acquire_wait_capacity(free=True) is True

    paid = make_user("victim_paid", plan=AccountPlan.Plan.PAID)
    inbox = Inbox.objects.create(user=paid, routing_key="paid_victim_box")
    client = auth_client(paid)

    response = client.get(f"/api/v1/inboxes/{inbox.public_id}/messages/wait?timeout=1")

    assert response.status_code == 204  # waited, no 429 — paid was not starved
    release_wait_capacity(free=True)





@pytest.mark.parametrize("reserved", ["postmaster", "abuse", "admin", "noreply", "hostmaster", "mailer-daemon"])
def test_custom_slug_rejects_reserved_localparts(reserved):
    owner = make_user("reserved_slug_owner")
    client = auth_client(owner)

    response = client.post("/api/v1/inboxes", {"routing_key": reserved}, format="json")

    assert response.status_code == 422
    assert not Inbox.objects.filter(routing_key=reserved).exists()





def test_custom_slug_uppercase_is_folded_to_lowercase():
    owner = make_user("case_fold_owner")
    client = auth_client(owner)

    response = client.post("/api/v1/inboxes", {"routing_key": "MyCustomBox"}, format="json")

    assert response.status_code == 201
    assert response.json()["data"]["address"].startswith("mycustombox@")
    assert Inbox.objects.filter(routing_key="mycustombox").exists()





def test_verify_rate_limit_is_enforced_per_account_across_keys(monkeypatch):
    from apps.accounts import services as services_mod

    monkeypatch.setattr(services_mod, "VERIFY_ACCOUNT_RATE_LIMIT", 1)
    monkeypatch.setattr(services_mod, "VERIFY_KEY_RATE_LIMIT", 1000)

    owner = make_user("verify_account_cap")
    _, token_a = create_api_key(owner, "key-a")
    _, token_b = create_api_key(owner, "key-b")

    client_a = APIClient()
    client_a.credentials(HTTP_AUTHORIZATION=f"Bearer {token_a}")
    client_b = APIClient()
    client_b.credentials(HTTP_AUTHORIZATION=f"Bearer {token_b}")

    first = client_a.post("/api/v1/verify", {"email": "one@example.test"}, format="json")
    assert first.status_code in (200, 429)  # DNS network call may fail in CI; status not the point

    # Second call from a DIFFERENT key on the SAME account must be blocked by
    # the per-account cap even though key_b's own per-key bucket is empty.
    second = client_b.post("/api/v1/verify", {"email": "two@example.test"}, format="json")
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"





def make_message(inbox, marker, received_at, **overrides):
    raw = f"Subject: {marker}\r\n\r\nbody {marker}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    values = {
        "inbox": inbox,
        "dedupe_key": digest,
        "envelope_from": "sender@example.test",
        "envelope_recipient": inbox.address,
        "header_from": "Sender <sender@example.test>",
        "header_to": [inbox.address],
        "header_cc": [],
        "subject": marker,
        "text_body": f"text {marker}",
        "html_body": f"<p>{marker}</p>",
        "raw_bytes": raw,
        "raw_sha256": digest,
        "size_bytes": len(raw),
        "ingest_status": Message.IngestStatus.PARSED,
        "received_at": received_at,
        "otp": marker,
        "otp_confidence": 0.9,
    }
    values.update(overrides)
    return Message.objects.create(**values)


def test_sse_composite_cursor_does_not_skip_same_timestamp_messages(monkeypatch):
    """>20 messages sharing one received_at must all eventually be delivered;
    a received_at-only cursor would skip whichever ones fall past the first
    20-row slice at that exact timestamp."""
    monkeypatch.setattr("apps.api.views.SSE_POLL_SECONDS", 0.01)
    monkeypatch.setattr("apps.api.views.SSE_MAX_SECONDS", 0.5)

    owner = make_user("sse_cursor_owner", plan=AccountPlan.Plan.PAID)
    inbox = Inbox.objects.create(user=owner, routing_key="sse_cursor_box")
    same_ts = timezone.now() - timedelta(seconds=5)
    # 25 messages sharing the identical received_at, beyond the [:20] slice.
    ids = []
    for i in range(25):
        msg = make_message(inbox, f"burst-{i}", same_ts)
        ids.append(msg.pk)

    client = auth_client(owner)
    response = client.get(
        f"/api/v1/inboxes/{inbox.public_id}/stream?since=0&require=otp", stream=True
    )
    assert response.status_code == 200
    body = b"".join(response.streaming_content).decode()

    delivered_otps = set()
    for line in body.splitlines():
        if line.startswith("data: ") and '"otp"' in line:
            import json

            payload = json.loads(line[len("data: "):])
            if payload.get("otp"):
                delivered_otps.add(payload["otp"])

    expected = {f"burst-{i}" for i in range(25)}
    assert expected <= delivered_otps


# --- B1: SSE /stream must accept `Accept: text/event-stream` ------------------
# Regression: DRF content negotiation had no renderer for text/event-stream, so
# a browser EventSource client (which always sends this Accept header and
# cannot override it) got a 406 before the view body ever ran. See



@pytest.mark.parametrize(
    "accept_header",
    [
        "text/event-stream",
        "*/*",
        "application/json",
        None,
    ],
    ids=["text_event_stream", "star_star", "application_json", "no_accept_header"],
)
def test_stream_accepts_all_client_accept_header_variants(accept_header):
    owner = make_user(f"sse_accept_{(accept_header or 'none').replace('/', '_').replace('*', 'star')}")
    inbox = Inbox.objects.create(user=owner, routing_key=f"sse_accept_box_{id(accept_header)}")

    client = auth_client(owner)
    extra = {} if accept_header is None else {"HTTP_ACCEPT": accept_header}
    response = client.get(
        f"/api/v1/inboxes/{inbox.public_id}/stream?since=0&require=otp",
        stream=True,
        **extra,
    )
    assert response.status_code == 200, (
        f"Accept: {accept_header!r} got {response.status_code}, expected 200"
    )
    assert response["Content-Type"].startswith("text/event-stream")
    # Drain the streaming body so the test doesn't leave the generator open.
    b"".join(response.streaming_content)


# --- F-A: SSE /stream ERROR responses must stay JSON, not text/event-stream ---
# Regression: EventStreamRenderer.render() unconditionally returned the raw
# dict, so 401/404/429 error Responses on StreamMessagesView (which DO go
# through DRF's normal Response.rendered_content, unlike the happy-path raw
# StreamingHttpResponse) were rendered as text/event-stream with body
# literally "error" (str(dict) truncated) instead of the documented

# F-A. This test must FAIL on the pre-fix EventStreamRenderer and pass after.


def _assert_json_error_envelope(response, *, expected_status):
    assert response.status_code == expected_status
    content_type = response["Content-Type"]
    assert content_type.startswith("application/json"), (
        f"expected JSON error body, got Content-Type: {content_type!r}, "
        f"body: {response.content!r}"
    )
    payload = response.json()
    assert "error" in payload and "code" in payload["error"], payload


@pytest.mark.parametrize("accept_header", ["text/event-stream", "*/*"])
def test_stream_401_returns_json_error_under_event_stream_accept(accept_header):
    client = APIClient()
    response = client.get(
        "/api/v1/inboxes/does-not-matter/stream?since=0&require=otp",
        HTTP_ACCEPT=accept_header,
    )
    _assert_json_error_envelope(response, expected_status=401)


@pytest.mark.parametrize("accept_header", ["text/event-stream", "*/*"])
def test_stream_404_returns_json_error_under_event_stream_accept(accept_header):
    owner = make_user(f"sse_404_{accept_header.replace('/', '_').replace('*', 'star')}")
    other = make_user(f"sse_404_other_{accept_header.replace('/', '_').replace('*', 'star')}")
    inbox = Inbox.objects.create(user=other, routing_key=f"sse_404_box_{id(accept_header)}")

    client = auth_client(owner)
    response = client.get(
        f"/api/v1/inboxes/{inbox.public_id}/stream?since=0&require=otp",
        HTTP_ACCEPT=accept_header,
    )
    _assert_json_error_envelope(response, expected_status=404)


@pytest.mark.parametrize("accept_header", ["text/event-stream", "*/*"])
def test_stream_429_wait_capacity_returns_json_error_under_event_stream_accept(
    accept_header, monkeypatch
):
    "Public API."
    monkeypatch.setattr("apps.accounts.services.WAIT_CAPACITY_LIMIT", 1)
    monkeypatch.setattr("apps.accounts.services.WAIT_CAPACITY_FREE_LIMIT", 1)

    owner = make_user(
        f"sse_429_{accept_header.replace('/', '_').replace('*', 'star')}",
        plan=AccountPlan.Plan.PAID,
    )
    inbox = Inbox.objects.create(user=owner, routing_key=f"sse_429_box_{id(accept_header)}")

    # Fill the sole global slot so the endpoint's own wait_slot() call is the
    # one that trips WaitSlotError("wait_capacity") — matches the pattern
    # used by test_free_plan_wait_capacity_capped_below_global_limit above.
    other = make_user(f"sse_429_filler_{accept_header.replace('/', '_').replace('*', 'star')}")
    assert acquire_wait_lease(other.pk) is True
    assert acquire_wait_capacity(free=False) is True

    client = auth_client(owner)
    response = client.get(
        f"/api/v1/inboxes/{inbox.public_id}/stream?since=0&require=otp",
        HTTP_ACCEPT=accept_header,
    )
    _assert_json_error_envelope(response, expected_status=429)
