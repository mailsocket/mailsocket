"Public API."

import os
import subprocess
import sys
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone

from apps.accounts import abuse_alerts, abuse_senders
from apps.accounts.models import AbuseAlert
from apps.accounts.tests.test_abuse_senders import receive, setup
from apps.domains.models import Domain

pytestmark = pytest.mark.django_db

WARN, WATCHED_WARN = 8, 3
REALISTIC = {
    "ABUSE_DISTINCT_SENDERS_WARN": WARN,
    "ABUSE_WATCHED_SENDERS_WARN": WATCHED_WARN,
    "OPS_ALERT_EMAIL": "ops@example.test",
}


def fill(inbox, count, prefix, watched=()):
    """Receive `count` distinct sender domains; `watched` ones count toward the total."""
    for name in watched:
        receive(inbox, f"a@{name}")
    for i in range(count - len(watched)):
        receive(inbox, f"a@{prefix}{i}.example")


def staff_client(client):
    staff = get_user_model().objects.create_user(username="gap-staff", password="test")
    staff.is_staff = True
    staff.save(update_fields=["is_staff"])
    client.force_login(staff)
    return client


@override_settings(**REALISTIC)
def test_below_both_thresholds_is_not_tripped_sends_nothing_and_not_in_fleet(client):
    user, inbox = setup()
    fill(inbox, WARN - 1, "below", watched=["google.com", "discord.com"])
    assert WATCHED_WARN - 1 == 2
    (row,) = abuse_senders.report(timezone.now())
    assert row["distinct_senders"] == WARN - 1
    assert row["watched_domains"] == WATCHED_WARN - 1
    assert row["tripped"] is False
    with patch.object(abuse_alerts.ops_notify, "notify") as notify:
        assert abuse_alerts.send_due_abuse_alerts() == 0
        notify.assert_not_called()
    assert not AbuseAlert.objects.filter(kind__startswith="many_senders:").exists()
    response = staff_client(client).get("/app/fleet/")
    assert response.status_code == 200
    assert b"below0.example" not in response.content
    assert f"/admin/accounts/account/{user.pk}/".encode() not in response.content


@override_settings(**REALISTIC)
def test_fleet_lists_only_tripped_accounts(client):
    tripped_user, tripped_inbox = setup("tripped")
    fill(tripped_inbox, WARN, "hot")
    quiet_user, quiet_inbox = setup("quiet")
    fill(quiet_inbox, WARN - 1, "cold")
    rows = {row["account_id"]: row for row in abuse_senders.report(timezone.now())}
    assert rows[tripped_user.pk]["tripped"] is True
    assert rows[quiet_user.pk]["tripped"] is False
    content = staff_client(client).get("/app/fleet/").content
    assert f"/admin/accounts/account/{tripped_user.pk}/".encode() in content
    assert f"/admin/accounts/account/{quiet_user.pk}/".encode() not in content
    assert b"hot0.example" in content
    assert b"cold0.example" not in content


@override_settings(**REALISTIC)
def test_header_from_preferred_over_envelope_from():
    _, inbox = setup()
    receive(inbox, "a@evil.example", header="x@google.com")
    (row,) = abuse_senders.report(timezone.now())
    assert row["top_domains"] == ["google.com"]


@override_settings(**REALISTIC)
def test_malformed_header_from_falls_back_to_envelope_from():
    _, inbox = setup()
    receive(inbox, "a@evil.example", header="not an address")
    receive(inbox, "a@fallback.example", header="")
    (row,) = abuse_senders.report(timezone.now())
    assert sorted(row["top_domains"]) == ["evil.example", "fallback.example"]


@override_settings(**REALISTIC)
def test_unverified_own_domain_is_still_counted():
    user, inbox = setup()
    Domain.objects.create(user=user, name="pending.example.com", status=Domain.Status.PENDING_DNS)
    receive(inbox, "a@pending.example.com")
    (row,) = abuse_senders.report(timezone.now())
    assert row["top_domains"] == ["example.com"]
    assert row["distinct_senders"] == 1


@override_settings(**REALISTIC)
def test_send_alerts_crash_does_not_escape_send_due_abuse_alerts():
    with patch.object(abuse_senders, "send_alerts", side_effect=RuntimeError("boom")):
        assert abuse_alerts.send_due_abuse_alerts() == 0
    with patch.object(abuse_senders, "report", side_effect=RuntimeError("boom")):
        assert abuse_alerts.send_due_abuse_alerts() == 0


def test_settings_module_fails_on_invalid_abuse_env():
    backend = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    for name, value in (("ABUSE_DISTINCT_SENDERS_WARN", "8junk"), ("ABUSE_WATCHED_SENDERS_WARN", "0")):
        env = {**os.environ, name: value, "DJANGO_SETTINGS_MODULE": "config.settings.base"}
        result = subprocess.run(
            [sys.executable, "-c", "import config.settings.base"],
            cwd=backend,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode != 0
        assert "ImproperlyConfigured" in result.stderr and name in result.stderr
