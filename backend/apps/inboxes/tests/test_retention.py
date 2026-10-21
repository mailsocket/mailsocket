import hashlib
import re
from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.entitlements import usage_summary
from apps.accounts.models import AccountPlan, MonthlyUsage
from apps.inboxes.models import Inbox, Message

pytestmark = pytest.mark.django_db(transaction=True)
RETENTION = {"free": 1, "paid": 7, "scale": 30, "business": 90}
RAW = b"From: sender@example.test\r\nSubject: secret\r\n\r\nOTP 482193"


def make_tenant(name, plan):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    AccountPlan.objects.create(user=user, plan=plan)
    inbox = Inbox.objects.create(user=user, routing_key=f"{name}_box")
    return user, inbox


def make_message(inbox, marker, age):
    raw = RAW + marker.encode()
    digest = hashlib.sha256(raw).hexdigest()
    message = Message.objects.create(
        inbox=inbox,
        dedupe_key=digest,
        envelope_recipient=inbox.address,
        subject=f"secret {marker}",
        text_body=f"secret text {marker}",
        html_body=f"<p>secret html {marker}</p>",
        detected_otp="482193",
        raw_bytes=raw,
        raw_sha256=digest,
        size_bytes=len(raw),
    )
    Message.objects.filter(pk=message.pk).update(received_at=timezone.now() - age)
    return message


@override_settings(RETENTION_DAYS=RETENTION)
def test_purge_deletes_expired_free_message_and_sensitive_payload():
    _, inbox = make_tenant("expired_free", AccountPlan.Plan.FREE)
    message = make_message(inbox, "expired", timedelta(days=1, seconds=1))

    output = StringIO()
    call_command("purge_expired_messages", stdout=output)

    assert not Message.objects.filter(pk=message.pk).exists()
    assert Message.objects.filter(raw_bytes__contains=b"OTP 482193").count() == 0
    assert "messages=1" in output.getvalue()


@override_settings(RETENTION_DAYS=RETENTION)
def test_usage_summary_remains_stable_after_counted_message_is_purged():
    user, inbox = make_tenant("durable_usage", AccountPlan.Plan.FREE)
    message = make_message(inbox, "counted", timedelta(days=2))
    message.refresh_from_db()
    period_start = message.received_at.date().replace(day=1)
    Message.objects.filter(pk=message.pk).update(usage_counted=True)
    MonthlyUsage.objects.create(
        user=user,
        period_start=period_start,
        messages_received=7,
    )

    assert usage_summary(user, at=message.received_at)["messages_received"] == 7
    call_command("purge_expired_messages")

    assert not Message.objects.filter(pk=message.pk).exists()
    summary = usage_summary(user, at=message.received_at)
    assert summary["messages_received"] == 7
    assert summary["limit_exceeded"] is False


@override_settings(RETENTION_DAYS=RETENTION)
def test_purge_keeps_messages_strictly_inside_each_plan_window():
    _, free_inbox = make_tenant("inside_free", AccountPlan.Plan.FREE)
    _, paid_inbox = make_tenant("inside_paid", AccountPlan.Plan.PAID)
    free_message = make_message(free_inbox, "free-inside", timedelta(hours=23))
    paid_message = make_message(paid_inbox, "paid-inside", timedelta(days=6, hours=23))

    call_command("purge_expired_messages")

    assert set(Message.objects.values_list("pk", flat=True)) == {
        free_message.pk,
        paid_message.pk,
    }
    free_message.refresh_from_db()
    assert bytes(free_message.raw_bytes).startswith(RAW)
    assert free_message.detected_otp == "482193"


@override_settings(RETENTION_DAYS=RETENTION)
def test_different_plans_use_different_retention_windows():
    _, free_inbox = make_tenant("window_free", AccountPlan.Plan.FREE)
    _, paid_inbox = make_tenant("window_paid", AccountPlan.Plan.PAID)
    free_message = make_message(free_inbox, "free-two-days", timedelta(days=2))
    paid_message = make_message(paid_inbox, "paid-two-days", timedelta(days=2))
    paid_expired = make_message(paid_inbox, "paid-eight-days", timedelta(days=8))

    call_command("purge_expired_messages")

    assert not Message.objects.filter(pk__in=[free_message.pk, paid_expired.pk]).exists()
    assert Message.objects.filter(pk=paid_message.pk).exists()


@override_settings(RETENTION_DAYS=RETENTION)
def test_purge_is_idempotent_batched_and_preserves_tenant_rows():
    user, inbox = make_tenant("batched", AccountPlan.Plan.FREE)
    message_ids = [
        make_message(inbox, f"batch-{index}", timedelta(days=2)).pk
        for index in range(5)
    ]
    user_count = get_user_model().objects.count()
    inbox_count = Inbox.objects.count()
    plan_count = AccountPlan.objects.count()

    first = StringIO()
    second = StringIO()
    call_command("purge_expired_messages", "--batch-size", "2", stdout=first)
    call_command("purge_expired_messages", "--batch-size", "2", stdout=second)

    assert not Message.objects.filter(pk__in=message_ids).exists()
    assert "messages=5" in first.getvalue() and "batches=3" in first.getvalue()
    assert "messages=0" in second.getvalue() and "batches=0" in second.getvalue()
    assert get_user_model().objects.filter(pk=user.pk).exists()
    assert Inbox.objects.filter(pk=inbox.pk).exists()
    assert (get_user_model().objects.count(), Inbox.objects.count(), AccountPlan.objects.count()) == (
        user_count,
        inbox_count,
        plan_count,
    )


@override_settings(
    RETENTION_DAYS=RETENTION,
    INGEST_SHARED_SECRET="retention-ingest-secret",
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
)
def test_inbound_ingest_still_works_after_standalone_purge():
    _, inbox = make_tenant("ingest_after", AccountPlan.Plan.FREE)
    old_message = make_message(inbox, "old", timedelta(days=2))
    call_command("purge_expired_messages")

    raw = b"From: new@example.test\r\nMessage-ID: <after-purge@example.test>\r\n\r\nnew"
    response = APIClient().post(
        "/internal/ingest",
        data=raw,
        content_type="application/octet-stream",
        HTTP_X_MAILSOCKET_INGEST_SECRET="retention-ingest-secret",
        HTTP_X_ENVELOPE_TO=inbox.address,
        HTTP_X_ENVELOPE_FROM="new@example.test",
        REMOTE_ADDR="172.30.0.9",
    )

    assert response.status_code == 201
    assert not Message.objects.filter(pk=old_message.pk).exists()
    stored = Message.objects.get(public_id=response.json()["public_id"])
    assert bytes(stored.raw_bytes) == raw


@override_settings(RETENTION_DAYS=RETENTION)
def test_t7_scale_message_over_thirty_days_purged_under_not():
    _, scale_inbox = make_tenant("scale_window", AccountPlan.Plan.SCALE)
    fresh = make_message(scale_inbox, "scale-fresh", timedelta(days=29, hours=23))
    expired = make_message(scale_inbox, "scale-expired", timedelta(days=30, hours=1))

    call_command("purge_expired_messages")

    assert Message.objects.filter(pk=fresh.pk).exists()
    assert not Message.objects.filter(pk=expired.pk).exists()


@override_settings(RETENTION_DAYS=RETENTION)
def test_t8_purge_runs_without_command_error_with_scale_in_retention_days():
    # Regresses the #1 landmine: adding "scale" to RETENTION_DAYS must not
    # trip the Step 3 CommandError guard.
    output = StringIO()
    call_command("purge_expired_messages", stdout=output)
    assert "messages=" in output.getvalue()


@override_settings(RETENTION_DAYS=RETENTION)
def test_t10_none_plan_message_purged_at_free_shortest_window():
    user = get_user_model().objects.create_user(
        username="unknown_plan_tenant", email="unknown_plan_tenant@example.test", password="password"
    )
    # Deliberately no AccountPlan row at all (no-plan case).
    inbox = Inbox.objects.create(user=user, routing_key="unknown_plan_box")
    expired_under_free = make_message(inbox, "unknown-plan", timedelta(days=1, seconds=1))

    call_command("purge_expired_messages")

    assert not Message.objects.filter(pk=expired_under_free.pk).exists()


@override_settings(RETENTION_DAYS=RETENTION)
def test_t10_out_of_enum_plan_value_purged_at_free_shortest_window():
    
    # not present in the AccountPlan.Plan enum at all (e.g. a stale "legacy"
    # value left over from before a plan was renamed/removed) — Django model
    # choices are a validation convenience, not a DB constraint. This must
    # still be purged at the free (shortest) cutoff, not escape forever.
    
    # `set(Plan.values) - known_plans` (removed/future ENUM members), so an
    # out-of-enum string like "legacy" matched none of the per-plan branches
    # nor the unknown-enum branch and was never purged.
    user, inbox = make_tenant("legacy_plan_tenant", AccountPlan.Plan.FREE)
    AccountPlan.objects.filter(user=user).update(plan="legacy")
    expired_under_free = make_message(inbox, "legacy-plan", timedelta(days=1, seconds=1))
    fresh_under_free = make_message(inbox, "legacy-plan-fresh", timedelta(hours=23))

    call_command("purge_expired_messages")

    assert not Message.objects.filter(pk=expired_under_free.pk).exists()
    assert Message.objects.filter(pk=fresh_under_free.pk).exists()


def test_legal_pages_are_public_csp_safe_and_linked_from_landing():
    client = Client()
    expected = {
        "/legal/aup": b"Acceptable Use Policy",
        "/legal/privacy": b"Privacy Policy",
        "/legal/terms": b"Terms of Service",
    }

    for path, heading in expected.items():
        response = client.get(path)
        assert response.status_code == 200
        assert heading in response.content
        assert b"abuse@inboxpipe.net" in response.content
        # CSP-safe: no inline script. The only script allowed is the shared,
        # same-origin external app.js (gate R2: nav hamburger keyboard handling).
        html = response.content.decode().lower()
        scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S)
        tags = re.findall(r"<script\b[^>]*>", html)
        assert all(body.strip() == "" for body in scripts), tags
        assert tags == ['<script defer src="/static/dashboard/app.min.js">'], tags
        assert response.headers["Content-Security-Policy"].startswith("default-src 'self'")

    privacy = client.get("/legal/privacy")
    assert b"1 day for Free" in privacy.content
    assert b"7 days for Pro" in privacy.content  # QA #7: customer-facing name
    assert b"Database backups may retain deleted messages for up to 14 days" in privacy.content
    assert b"mail queue for up to 5 days" in privacy.content
    assert b"Hetzner" in privacy.content and b"Germany" in privacy.content

    landing = client.get("/")
    for href in (b'href="/legal/aup"', b'href="/legal/privacy"', b'href="/legal/terms"'):
        assert href in landing.content
    assert b"mailto:abuse@inboxpipe.net" in landing.content
