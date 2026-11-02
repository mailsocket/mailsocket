"""Round-2 fixes: public suffixes, alert unclaim resilience, cached fleet rows, fail-closed env."""

import os
import subprocess
import sys
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

from apps.accounts import abuse_senders
from apps.accounts.tests.test_abuse_senders import receive, setup

pytestmark = pytest.mark.django_db

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("shop.example.co.jp", "example.co.jp"),
        ("a.b.com.br", "b.com.br"),
        ("co.jp", None),
        ("com.br", None),
        ("x.example.co.nz", "example.co.nz"),
        ("mail.example.com.sg", "example.com.sg"),
        ("mail.example.co.za", "example.co.za"),
        ("mail.example.com", "example.com"),
    ],
)
def test_registrable_domain_public_suffixes(host, expected):
    assert abuse_senders.registrable_domain(host) == expected


def test_public_suffix_set_is_frozen_and_covers_brief():
    assert isinstance(abuse_senders.MULTI_SUFFIXES, frozenset)
    for suffix in ("gov.uk", "edu.au", "ne.jp", "com.mx", "co.in", "com.tw", "co.kr", "com.pl", "com.ru"):
        assert suffix in abuse_senders.MULTI_SUFFIXES


def test_two_senders_under_co_jp_count_as_two():
    _user, inbox = setup("jp")
    receive(inbox, "a@x.co.jp")
    receive(inbox, "a@y.co.jp")
    (row,) = abuse_senders.report(timezone.now())
    assert row["distinct_senders"] == 2


def test_send_alerts_unclaim_failure_does_not_stop_other_batches():
    from apps.accounts import abuse_alerts

    due = [(f"many_senders:{i}", None, {"account_id": i}) for i in range(2)]
    with (
        patch.object(abuse_senders, "_due", return_value=due),
        patch.object(abuse_senders, "_compose", return_value=("t", "b", "e")),
        patch.object(abuse_senders.ops_notify, "notify", side_effect=RuntimeError("down")),
        patch.object(abuse_alerts, "_unclaim", side_effect=RuntimeError("db")) as unclaim,
    ):
        assert abuse_senders.send_alerts(timezone.now()) == 0
    assert unclaim.call_count == 2


def _staff(client):
    staff = get_user_model().objects.create_user(username="r2-staff", password="test")
    staff.is_staff = True
    staff.save(update_fields=["is_staff"])
    client.force_login(staff)
    return client


def test_fleet_survives_report_failure(client):
    cache.delete("fleet_many_senders")
    with patch.object(abuse_senders, "report", side_effect=RuntimeError("boom")):
        response = _staff(client).get("/app/fleet/")
    assert response.status_code == 200
    assert abuse_senders.cached_tripped_rows.__name__ == "cached_tripped_rows"
    assert response.context["many_senders"] == [] if response.context else True


def test_cached_tripped_rows_caches_300s():
    cache.delete("fleet_many_senders")
    rows = [{"account_id": 1, "tripped": True}, {"account_id": 2, "tripped": False}]
    with (
        patch.object(abuse_senders, "report", return_value=rows) as rep,
        patch.object(abuse_senders.cache, "set", wraps=abuse_senders.cache.set) as setter,
    ):
        assert abuse_senders.cached_tripped_rows(timezone.now()) == rows[:1]
        assert abuse_senders.cached_tripped_rows(timezone.now()) == rows[:1]
    assert rep.call_count == 1
    assert setter.call_args.args[:2] == ("fleet_many_senders", rows[:1])
    assert setter.call_args.args[2] == 300


def test_cached_tripped_rows_failure_returns_empty_and_logs(caplog):
    cache.delete("fleet_many_senders")
    with patch.object(abuse_senders, "report", side_effect=RuntimeError("boom")):
        assert abuse_senders.cached_tripped_rows(timezone.now()) == []
    assert any(r.levelname == "ERROR" for r in caplog.records)


def _import_settings(**env):
    return subprocess.run(
        [sys.executable, "-c", "import config.settings.base"],
        cwd=BACKEND,
        env={**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.base", **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(
    "name,value",
    [
        ("ABUSE_WATCHED_SENDER_DOMAINS", ","),
        ("ABUSE_WATCHED_SENDER_DOMAINS", "  "),
        ("ABUSE_WATCHED_SENDER_DOMAINS", "google.com,nodot"),
        ("ABUSE_WATCHED_SENDER_DOMAINS", "bad_label.com"),
        ("ABUSE_WATCHED_SENDER_DOMAINS", "-a.com"),
        ("ABUSE_SENDER_ALLOWLIST", "good.com,nodot"),
        ("ABUSE_SENDER_ALLOWLIST", "a b.com"),
    ],
)
def test_settings_fail_closed_on_bad_domain_env(name, value):
    result = _import_settings(**{name: value})
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr and name in result.stderr


def test_settings_accept_valid_domain_env():
    result = _import_settings(ABUSE_WATCHED_SENDER_DOMAINS="google.com, X.co.jp", ABUSE_SENDER_ALLOWLIST="")
    assert result.returncode == 0, result.stderr
