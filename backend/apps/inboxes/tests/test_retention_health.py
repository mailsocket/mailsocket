import hashlib
from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import AccountPlan
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


def run_health(*args):
    output = StringIO()
    call_command("check_retention_health", *args, stdout=output)
    return output.getvalue()


@override_settings(RETENTION_DAYS=RETENTION)
def test_no_overdue_exits_zero_prints_ok_and_sends_no_email():
    _, inbox = make_tenant("healthy", AccountPlan.Plan.FREE)
    make_message(inbox, "fresh", timedelta(hours=23))

    output = run_health()

    assert "retention OK" in output
    assert "oldest_free=" in output
    assert len(mail.outbox) == 0


@override_settings(
    RETENTION_DAYS=RETENTION, OPS_ALERT_EMAIL="ops@mailsocket.app"
)
def test_overdue_exits_nonzero_prints_summary_and_emails_ops_without_raw_content():
    _, inbox = make_tenant("overdue", AccountPlan.Plan.FREE)
    make_message(inbox, "overdue", timedelta(days=1, hours=3))

    with pytest.raises(CommandError):
        run_health()

    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == ["ops@mailsocket.app"]
    assert message.subject == "[mailsocket] retention purge overdue"
    assert "overdue_free=1" in message.body
    assert "retention purge overdue" in message.body
    # No raw PII may leak into the alert.
    for forbidden in (b"OTP 482193", b"secret", b"From: sender"):
        assert forbidden not in message.body.encode()


@override_settings(RETENTION_DAYS=RETENTION)
def test_message_older_than_window_but_within_grace_does_not_alarm():
    _, inbox = make_tenant("grace", AccountPlan.Plan.FREE)
    make_message(inbox, "late", timedelta(days=1, hours=1))

    output = run_health()

    assert "retention OK" in output
    assert len(mail.outbox) == 0


@override_settings(RETENTION_DAYS=RETENTION)
def test_overdue_without_ops_email_still_exits_nonzero_and_skips_email():
    _, inbox = make_tenant("no_ops", AccountPlan.Plan.FREE)
    make_message(inbox, "overdue", timedelta(days=2))

    output = StringIO()
    with pytest.raises(CommandError):
        call_command("check_retention_health", stdout=output)

    assert "retention purge overdue" in output.getvalue()
    assert "alert email skipped (OPS_ALERT_EMAIL unset)" in output.getvalue()
    assert len(mail.outbox) == 0


@override_settings(RETENTION_DAYS=RETENTION)
def test_grace_boundary_respected_via_flag():
    _, inbox = make_tenant("boundary", AccountPlan.Plan.FREE)
    make_message(inbox, "boundary", timedelta(days=1, hours=5))

    # Default grace (2h): 29h > 26h -> overdue.
    with pytest.raises(CommandError):
        run_health()

    # --grace-hours 10: cutoff 34h -> not overdue.
    output = run_health("--grace-hours", "10")
    assert "retention OK" in output


@override_settings(RETENTION_DAYS=RETENTION, OPS_ALERT_EMAIL="ops@mailsocket.app")
def test_t9_check_retention_health_counts_a_scale_overdue_message():
    _, scale_inbox = make_tenant("scale_overdue", AccountPlan.Plan.SCALE)
    make_message(scale_inbox, "scale-overdue", timedelta(days=30, hours=3))

    with pytest.raises(CommandError):
        run_health()

    assert len(mail.outbox) == 1
    assert "overdue_scale=1" in mail.outbox[0].body


@override_settings(RETENTION_DAYS=RETENTION, OPS_ALERT_EMAIL="ops@mailsocket.app")
def test_t10_check_retention_health_counts_out_of_enum_plan_value_under_free():
    
    # CharField value (e.g. "legacy") must be counted under overdue_free,
    # not silently dropped from monitoring (the #5 landmine the spec calls
    # out) — same exclusion-against-known-keys logic as the purge predicate.
    user, inbox = make_tenant("legacy_health", AccountPlan.Plan.FREE)
    AccountPlan.objects.filter(user=user).update(plan="legacy")
    make_message(inbox, "legacy-overdue", timedelta(days=1, hours=3))

    with pytest.raises(CommandError):
        run_health()

    assert len(mail.outbox) == 1
    assert "overdue_free=1" in mail.outbox[0].body


# --- tg-notify ------------------------------------------------------------------


@override_settings(RETENTION_DAYS=RETENTION, OPS_ALERT_EMAIL="ops@mailsocket.app")
def test_overdue_alert_also_goes_to_telegram_as_warn_without_content():
    from apps.ops.testing import fake_telegram

    _, inbox = make_tenant("overdue_tg", AccountPlan.Plan.FREE)
    make_message(inbox, "overdue_tg", timedelta(days=1, hours=3))
    with fake_telegram() as tg:
        with pytest.raises(CommandError):
            run_health()
    assert len(mail.outbox) == 1
    assert len(tg.texts) == 1
    text = tg.texts[0]
    assert text.startswith("🟠 retention purge overdue")
    assert "overdue_free=1" in text
    for forbidden in ("OTP 482193", "secret", "From: sender", "@"):
        assert forbidden not in text
