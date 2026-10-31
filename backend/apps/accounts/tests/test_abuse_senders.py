"""Acceptance tests for alert-only verification sender diversity."""

import hashlib
import json
import os
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core import mail as django_mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from apps.accounts import abuse_alerts, abuse_senders
from apps.accounts.models import AbuseAlert
from apps.accounts.services import create_product_user, get_demo_user
from apps.domains.models import Domain
from apps.inboxes.models import Inbox, Message

pytestmark = pytest.mark.django_db


def receive(inbox, sender, *, header="", otp="123456", link=None, ago=timedelta(), subject="SECRET CODE"):
    raw = f"{inbox.pk}:{sender}:{header}:{Message.objects.count()}".encode()
    sha = hashlib.sha256(raw).hexdigest()
    msg = Message(inbox=inbox, envelope_from=sender, header_from=header, subject=subject)
    msg.otp, msg.magic_link, msg.envelope_recipient = otp, link, inbox.address
    msg.raw_bytes, msg.raw_sha256, msg.dedupe_key, msg.size_bytes = raw, sha, sha, len(raw)
    msg.save()
    Message.objects.filter(pk=msg.pk).update(received_at=timezone.now() - ago)
    return msg


def setup(name="user"):
    user = create_product_user(f"{name}@example.test", "password-unused-123456")
    return user, Inbox.objects.create(user=user, routing_key=f"abuse{name}")


def test_domain_extraction():
    extract = abuse_senders.sender_domain
    assert extract('"Sender" <Hi@A.B.Example.CO.UK>') == "example.co.uk"
    assert extract("qa@auth.example.com.au") == "example.com.au"
    assert extract("qa@a.example.com.vn") == "example.com.vn"
    assert extract("qa@bücher.de") == "xn--bcher-kva.de"
    assert extract("qa@xn--bcher-kva.de") == "xn--bcher-kva.de"
    assert extract("broken-from") is None
    assert extract('"oops" <not-an-address>') is None
    assert extract("@example.com") is None
    assert extract("qa@localhost") is None


def test_invalid_threshold_env_fails_closed():
    from django.core.exceptions import ImproperlyConfigured

    from config.settings.base import _positive_abuse_int

    with patch.dict("os.environ", {"ABUSE_DISTINCT_SENDERS_WARN": "8junk"}):
        with pytest.raises(ImproperlyConfigured):
            _positive_abuse_int("ABUSE_DISTINCT_SENDERS_WARN", os.environ["ABUSE_DISTINCT_SENDERS_WARN"])
    with patch.dict("os.environ", {"ABUSE_DISTINCT_SENDERS_WARN": "0"}):
        with pytest.raises(ImproperlyConfigured):
            _positive_abuse_int("ABUSE_DISTINCT_SENDERS_WARN", os.environ["ABUSE_DISTINCT_SENDERS_WARN"])


@override_settings(ABUSE_DISTINCT_SENDERS_WARN=3, ABUSE_WATCHED_SENDERS_WARN=2)
def test_exclusions_and_window_and_distinct():
    user, inbox = setup()
    Domain.objects.create(user=user, name="qa.own.example.com", status=Domain.Status.VERIFIED)
    receive(inbox, "a@qa.own.example.com")
    receive(inbox, "a@sub.qa.own.example.com")
    receive(inbox, "a@mailsocket.app")
    receive(inbox, "a@in.inboxpipe.net")
    receive(inbox, "a@allowed.example.org")
    receive(inbox, "a@old.example.net", ago=timedelta(hours=25))
    receive(inbox, "a@no-otp.example.net", otp=None)
    receive(inbox, "a@google.com", header='"Google" <noreply@accounts.google.com>')
    receive(inbox, "b@google.com")
    receive(inbox, "a@discord.com", otp=None, link="https://example.test/secret")
    with override_settings(ABUSE_SENDER_ALLOWLIST=["example.org"]):
        (row,) = abuse_senders.report(timezone.now())
    assert row["account_id"] == user.pk
    assert row["distinct_senders"] == 2
    assert row["watched_domains"] == 2
    assert row["top_domains"] == ["google.com", "discord.com"]
    assert row["tripped"] is True


@override_settings(ABUSE_DISTINCT_SENDERS_WARN=2, ABUSE_WATCHED_SENDERS_WARN=2)
def test_own_domain_excluded_even_without_other_sender():
    user, inbox = setup()
    Domain.objects.create(user=user, name="my.example.com", status=Domain.Status.VERIFIED)
    receive(inbox, "a@my.example.com")
    receive(inbox, "b@sub.my.example.com")
    assert abuse_senders.report(timezone.now()) == []


@override_settings(ABUSE_DISTINCT_SENDERS_WARN=2, ABUSE_WATCHED_SENDERS_WARN=2)
def test_thresholds_and_excluded_accounts():
    _, inbox = setup()
    receive(inbox, "a@one.example")
    receive(inbox, "a@two.example")
    staff, staff_inbox = setup("staff")
    staff.is_staff = True
    staff.save(update_fields=["is_staff"])
    receive(staff_inbox, "a@google.com")
    receive(staff_inbox, "a@discord.com")
    demo = get_demo_user()
    demo_inbox = Inbox.objects.create(user=demo, routing_key="abusedemo")
    receive(demo_inbox, "a@google.com")
    receive(demo_inbox, "a@discord.com")
    with override_settings(FUNNEL_EXCLUDED_EMAILS=["qa@example.test"]):
        qa, qa_inbox = setup("qa")
        receive(qa_inbox, "a@google.com")
        receive(qa_inbox, "a@discord.com")
        rows = abuse_senders.report(timezone.now())
    assert len(rows) == 1
    assert rows[0]["distinct_senders"] == 2
    assert rows[0]["watched_domains"] == 0
    assert rows[0]["tripped"] is True


@override_settings(
    ABUSE_DISTINCT_SENDERS_WARN=2, ABUSE_WATCHED_SENDERS_WARN=2, OPS_ALERT_EMAIL="ops@example.test"
)
def test_alert_once_unclaim_failure_and_privacy():
    user, inbox = setup()
    receive(inbox, "person@google.com", subject="secret subject 928471")
    receive(inbox, "person@discord.com")
    with patch.object(
        abuse_alerts.ops_notify, "notify", return_value={"email": "error", "telegram": "disabled"}
    ) as notify:
        assert abuse_alerts.send_due_abuse_alerts() == 0
        body = notify.call_args.args[2]
    assert "person@" not in body and "928471" not in body and "secret subject" not in body
    assert "google.com" not in body and str(user.pk) in body
    assert "google.com" in notify.call_args.kwargs["email_body"]
    assert abuse_alerts.send_due_abuse_alerts() == 1
    assert abuse_alerts.send_due_abuse_alerts() == 0
    assert AbuseAlert.objects.filter(kind=f"many_senders:{user.pk}", last_alert_at__isnull=False).exists()
    assert len([m for m in django_mail.outbox if "verification senders" in m.subject]) == 1


@override_settings(
    ABUSE_DISTINCT_SENDERS_WARN=1, ABUSE_WATCHED_SENDERS_WARN=3, OPS_ALERT_EMAIL="ops@example.test"
)
def test_summary_for_more_than_ten_accounts():
    for i in range(11):
        _, inbox = setup(f"user{i}")
        receive(inbox, "a@other.example")
    with patch.object(
        abuse_alerts.ops_notify, "notify", return_value={"email": "sent", "telegram": "disabled"}
    ) as notify:
        assert abuse_alerts.send_due_abuse_alerts() == 1
        assert notify.call_count == 1
        assert "11 accounts" in notify.call_args.args[2]
        assert abuse_alerts.send_due_abuse_alerts() == 0
    assert AbuseAlert.objects.filter(kind__startswith="many_senders:").count() == 11


@override_settings(ABUSE_DISTINCT_SENDERS_WARN=1)
def test_staff_fleet_and_command(client):
    user, inbox = setup()
    receive(inbox, "a@google.com")
    response = client.get("/app/fleet/")
    assert response.status_code == 404
    staff = get_user_model().objects.create_user(username="fleet-staff", password="test")
    staff.is_staff = True
    staff.save(update_fields=["is_staff"])
    client.force_login(staff)
    response = client.get("/app/fleet/")
    assert response.status_code == 200
    assert b"Accounts with many verification senders (24 h)" in response.content
    assert b"google.com" in response.content
    output = StringIO()
    call_command("abuse_senders_report", "--hours", "24", "--json", stdout=output)
    (row,) = json.loads(output.getvalue())
    assert row["account_id"] == user.pk
    assert row["top_domains"] == ["google.com"]
    output = StringIO()
    call_command("abuse_senders_report", stdout=output)
    assert "google.com" in output.getvalue()
    with pytest.raises(CommandError):
        call_command("abuse_senders_report", "--hours", "0")
