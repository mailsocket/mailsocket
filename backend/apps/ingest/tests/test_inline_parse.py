"""P0 hotfix regressions: /internal/ingest must parse inline (D3).

52420c2 dropped the inline ``parse_after_commit`` call from the ingest view
when it moved usage metering into the parse step; new mail then sat in
``received`` until the per-minute reconcile_ingest cron, so /messages/wait
(clamped at 25 s) returned 204 on most real OTP mail. These tests pin the
inline parse plus the invariants that removal was protecting.
"""
import threading
import time
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import DatabaseError, close_old_connections
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, AccountPlan, MonthlyUsage
from apps.accounts.services import create_api_key
from apps.inboxes.models import Inbox, Message
from apps.webhooks.models import WebhookDelivery
from apps.webhooks.services import create_webhook

pytestmark = pytest.mark.django_db(transaction=True)

SECRET = "test-ingest-secret"
OTP_RAW = (
    b"From: GitHub <noreply@github.example>\r\n"
    b"To: someone@example.test\r\n"
    b"Message-ID: <inline-otp@example.test>\r\n"
    b"Subject: Your verification code\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Your verification code is 481920. It expires in 10 minutes.\r\n"
)

ingest_settings = override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    MAIL_DOMAIN="in.inboxpipe.net",
)


def make_owner(name, *, plan=AccountPlan.Plan.PAID):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user, plan=plan)
    return user


@pytest.fixture
def inbox():
    owner = make_owner("inline_owner")
    return Inbox.objects.create(user=owner, routing_key="inline_box", label="Inline")


def post(inbox, raw=OTP_RAW, client=None):
    return (client or APIClient()).post(
        "/internal/ingest",
        data=raw,
        content_type="application/octet-stream",
        HTTP_X_MAILSOCKET_INGEST_SECRET=SECRET,
        HTTP_X_ENVELOPE_TO=inbox.address,
        HTTP_X_ENVELOPE_FROM="bounce@example.test",
        REMOTE_ADDR="172.30.0.9",
    )


def usage_count(inbox):
    period = timezone.now().date().replace(day=1)
    row = MonthlyUsage.objects.filter(user=inbox.user, period_start=period).first()
    return row.messages_received if row else 0


# (a) parsed + OTP extracted in the same request, no cron.
@ingest_settings
def test_ingest_parses_inline_without_reconciler(inbox):
    with patch("apps.ingest.reconciler.reconcile") as reconcile:
        response = post(inbox)

    assert response.status_code == 201
    assert response.json()["ingest_status"] == Message.IngestStatus.PARSED
    reconcile.assert_not_called()
    message = Message.objects.get()
    assert message.ingest_status == Message.IngestStatus.PARSED
    assert message.otp == "481920"
    assert message.parse_attempts == 1
    assert message.parsed_at is not None


# (b) end-to-end: a wait started BEFORE ingest returns 200 with the OTP.
@ingest_settings
def test_wait_started_before_ingest_returns_otp(inbox, monkeypatch):
    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    _, token = create_api_key(inbox.user, "inline-wait")
    waiter = APIClient()
    waiter.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    ingest_status = {}

    def deliver_mid_wait():
        close_old_connections()
        try:
            time.sleep(0.3)
            ingest_status["code"] = post(inbox).status_code
        finally:
            close_old_connections()

    thread = threading.Thread(target=deliver_mid_wait)
    started = time.monotonic()
    thread.start()
    # No reconcile_ingest runs anywhere in this test: only inline parse can
    # make the message visible to wait before the 5 s timeout.
    response = waiter.get(
        f"/api/v1/inboxes/{inbox.public_id}/messages/wait?require=otp&timeout=5"
    )
    elapsed = time.monotonic() - started
    thread.join(timeout=10)

    assert ingest_status == {"code": 201}
    assert response.status_code == 200
    assert response.json()["data"]["otp"] == "481920"
    assert elapsed < 4


# (c) inline parse failure: ingest still 2xx, row stays received, reconcile parses later.
@ingest_settings
def test_inline_parser_exception_is_2xx_and_reconciler_recovers(inbox):
    with patch("apps.ingest.parser.parse_raw_message", side_effect=RuntimeError("boom")):
        response = post(inbox)

    assert response.status_code == 201
    assert response.json()["ingest_status"] == Message.IngestStatus.RECEIVED
    message = Message.objects.get()
    assert message.ingest_status == Message.IngestStatus.RECEIVED
    assert message.parse_attempts == 1
    assert message.next_parse_at is not None

    # Make the backoff due, then let the crash-safe fallback finish the job.
    Message.objects.filter(pk=message.pk).update(next_parse_at=timezone.now() - timedelta(seconds=1))
    call_command("reconcile_ingest", "--once")
    message.refresh_from_db()
    assert message.ingest_status == Message.IngestStatus.PARSED
    assert message.otp == "481920"
    assert message.parse_attempts == 2


@ingest_settings
@pytest.mark.parametrize("exc", [DatabaseError("db blip"), RuntimeError("unexpected")])
def test_exception_escaping_inline_parse_never_tempfails_postfix(inbox, exc):
    # process_claim re-raises DatabaseError by design; the view must still
    # answer 2xx because the raw bytes are already durable.
    with patch("apps.ingest.views.parse_after_commit", side_effect=exc):
        response = post(inbox)

    assert response.status_code == 201
    body = response.json()
    assert body["ingest_status"] == Message.IngestStatus.RECEIVED
    assert body["deduplicated"] is False
    message = Message.objects.get()
    assert body["public_id"] == message.public_id
    assert message.ingest_status == Message.IngestStatus.RECEIVED

    call_command("reconcile_ingest", "--once")
    message.refresh_from_db()
    assert message.ingest_status == Message.IngestStatus.PARSED
    assert message.otp == "481920"


# (d) usage counted exactly once across inline + reconcile.
@ingest_settings
def test_usage_counted_once_across_inline_and_reconcile(inbox):
    assert usage_count(inbox) == 0

    response = post(inbox)
    assert response.status_code == 201
    message = Message.objects.get()
    assert message.usage_counted is True
    assert usage_count(inbox) == 1

    # Reconcile skips parsed rows and must not recount.
    call_command("reconcile_ingest", "--once")
    message.refresh_from_db()
    assert message.parse_attempts == 1
    assert usage_count(inbox) == 1

    # A replayed parse of an already-parsed message is a no-op.
    from apps.ingest.services import parse_after_commit

    again = parse_after_commit(message.pk)
    assert again.ingest_status == Message.IngestStatus.PARSED
    assert again.parse_attempts == 1
    assert usage_count(inbox) == 1


@ingest_settings
def test_usage_counted_once_when_inline_fails_and_reconcile_succeeds(inbox):
    with patch("apps.ingest.parser.parse_raw_message", side_effect=RuntimeError("boom")):
        assert post(inbox).status_code == 201
    message = Message.objects.get()
    assert message.usage_counted is True
    assert usage_count(inbox) == 1

    Message.objects.filter(pk=message.pk).update(next_parse_at=timezone.now() - timedelta(seconds=1))
    call_command("reconcile_ingest", "--once")
    message.refresh_from_db()
    assert message.ingest_status == Message.IngestStatus.PARSED
    assert usage_count(inbox) == 1


# (e) dedup path does not re-parse (or recount).
@ingest_settings
def test_dedup_retry_does_not_reparse(inbox):
    first = post(inbox)
    assert first.status_code == 201
    message = Message.objects.get()
    parsed_at = message.parsed_at

    with patch("apps.ingest.views.parse_after_commit") as inline_parse:
        duplicate = post(inbox)

    assert duplicate.status_code == 200
    assert duplicate.json() == {
        "public_id": message.public_id,
        "ingest_status": Message.IngestStatus.PARSED,
        "deduplicated": True,
    }
    inline_parse.assert_not_called()
    message.refresh_from_db()
    assert Message.objects.count() == 1
    assert message.parse_attempts == 1
    assert message.parsed_at == parsed_at
    assert usage_count(inbox) == 1


# Sweep: webhook delivery row is enqueued in the ingest request itself.
@ingest_settings
def test_webhook_delivery_enqueued_by_inline_parse(inbox):
    webhook, _ = create_webhook(
        inbox, "https://receiver.example/hook", resolver=lambda _host: ["93.184.216.34"]
    )

    response = post(inbox)

    assert response.status_code == 201
    message = Message.objects.get()
    delivery = WebhookDelivery.objects.get(message=message)
    assert delivery.webhook_id == webhook.pk
    assert delivery.next_attempt_at <= timezone.now()

    # Reconcile afterwards must not create a second delivery.
    call_command("reconcile_ingest", "--once")
    assert WebhookDelivery.objects.filter(message=message).count() == 1
