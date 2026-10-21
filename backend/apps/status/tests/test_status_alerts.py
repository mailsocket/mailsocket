"""tg-notify: component down / recovery alerts from status_probe.

Load-bearing: each test names the regression it catches.
"""
import threading
from datetime import timedelta
from io import StringIO
from unittest import mock

import pytest
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.db import close_old_connections
from django.test import override_settings
from django.utils import timezone

from apps.ops.testing import fake_telegram
from apps.status import alerts
from apps.status.alerts import check_component_transitions
from apps.status.models import ComponentAlertState, ComponentCheck

OPS = "ops@example.test"
SMTP = ComponentCheck.Component.INBOUND_MAIL
COMPONENTS = ComponentCheck.Component.values
T0 = timezone.now().replace(hour=10, minute=0, second=0, microsecond=0) - timedelta(days=1)


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _round(at, failing=()):
    """One probe round at ``at``: every component ok except ``failing``."""
    for c in COMPONENTS:
        ComponentCheck.objects.create(component=c, ok=c not in failing, latency_ms=5, checked_at=at)


def _step(i, failing=()):
    at = T0 + timedelta(minutes=5 * i)
    _round(at, failing)
    return check_component_transitions(now=at)


def _state(component=SMTP):
    row = ComponentAlertState.objects.filter(component=component).first()
    return row.state if row else ComponentAlertState.OPERATIONAL


@pytest.mark.django_db
@override_settings(OPS_ALERT_EMAIL=OPS)
def test_operational_to_down_after_two_fails_alerts_once_with_since_time():
    with fake_telegram() as tg:
        assert _step(0) == 0
        assert _step(1, failing=[SMTP]) == 0  # single fail = degraded: no alert
        assert tg.calls == [] and mail.outbox == []
        assert _step(2, failing=[SMTP]) == 1  # second fail = down
    assert _state() == "down"
    assert len(tg.texts) == 1
    assert tg.texts[0] == (
        "🔴 Inbound mail (SMTP) is DOWN since 10:05 UTC\n\n"
        f"The /status probe failed Inbound mail (SMTP) twice in a row "
        f"(first failure {T0 + timedelta(minutes=5):%Y-%m-%d} 10:05 UTC).\n\n"
        "https://mailsocket.app/status"
    )
    assert len(mail.outbox) == 1
    assert mail.outbox[0].subject == "[mailsocket] Inbound mail (SMTP) is DOWN since 10:05 UTC"


@pytest.mark.django_db
@override_settings(OPS_ALERT_EMAIL=OPS)
def test_single_fail_never_alerts_flapping():
    with fake_telegram() as tg:
        for i in range(8):
            _step(i, failing=[SMTP] if i % 2 else [])
    assert tg.calls == [] and mail.outbox == []
    assert _state() == "operational"


@pytest.mark.django_db
@override_settings(OPS_ALERT_EMAIL=OPS)
def test_no_repeat_while_still_down_then_one_recovery():
    with fake_telegram() as tg:
        _step(0)
        _step(1, failing=[SMTP])
        _step(2, failing=[SMTP])
        for i in range(3, 9):
            assert _step(i, failing=[SMTP]) == 0  # still down: silent
        assert len(tg.calls) == 1
        assert _step(9) == 0  # first ok after outage = degraded: still silent
        assert _step(10) == 1  # second ok = operational: one recovery
        for i in range(11, 14):
            assert _step(i) == 0
    assert _state() == "operational"
    assert len(tg.texts) == 2
    # Down since 10:05 (first failed check), recovered at 10:50 -> 45 min.
    assert tg.texts[1].startswith("🟢 Inbound mail (SMTP) recovered after 45 min")
    assert tg.texts[1].endswith("https://mailsocket.app/status")
    assert [m.subject for m in mail.outbox] == [
        "[mailsocket] Inbound mail (SMTP) is DOWN since 10:05 UTC",
        "[mailsocket] Inbound mail (SMTP) recovered after 45 min",
    ]


@pytest.mark.django_db
@override_settings(OPS_ALERT_EMAIL=OPS)
def test_each_component_tracked_independently():
    api = ComponentCheck.Component.API
    with fake_telegram() as tg:
        _step(0)
        _step(1, failing=[SMTP, api])
        assert _step(2, failing=[SMTP, api]) == 2
        _step(3, failing=[SMTP])
        assert _step(4, failing=[SMTP]) == 1  # only API recovered
    assert _state(SMTP) == "down" and _state(api) == "operational"
    assert len(tg.calls) == 3


@pytest.mark.django_db
@override_settings(OPS_ALERT_EMAIL=OPS)
def test_failed_delivery_releases_claim_and_retries_next_run():
    with fake_telegram(fail=OSError("tg down")), mock.patch(
        "apps.ops.notify.send_mail", side_effect=OSError("smtp down")
    ):
        _step(0)
        _step(1, failing=[SMTP])
        assert _step(2, failing=[SMTP]) == 0
    assert _state() == "operational"  # claim rolled back
    with fake_telegram() as tg:
        assert _step(3, failing=[SMTP]) == 1
    assert len(tg.calls) == 1
    assert "DOWN since 10:05 UTC" in tg.texts[0]


@pytest.mark.django_db
@override_settings(OPS_ALERT_EMAIL=OPS)
def test_one_channel_delivering_keeps_the_claim():
    with fake_telegram(fail=OSError("tg down")):
        _step(0)
        _step(1, failing=[SMTP])
        assert _step(2, failing=[SMTP]) == 1
        assert _step(3, failing=[SMTP]) == 0
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_never_raises_on_internal_error():
    with mock.patch.object(alerts, "_recent_ok", side_effect=RuntimeError("db gone")):
        assert check_component_transitions(now=T0) == 0


@pytest.mark.django_db(transaction=True)
@override_settings(OPS_ALERT_EMAIL=OPS)
def test_concurrent_runs_send_a_single_alert(monkeypatch):
    for i in range(3):
        _round(T0 + timedelta(minutes=5 * i), failing=[SMTP] if i else [])
    now = T0 + timedelta(minutes=10)
    barrier = threading.Barrier(2)
    real_claim = alerts._claim

    def synced_claim(*a, **kw):
        barrier.wait(timeout=10)  # both runs read "operational" before claiming
        return real_claim(*a, **kw)

    monkeypatch.setattr(alerts, "_claim", synced_claim)
    results = []
    with fake_telegram() as tg:

        def worker():
            try:
                results.append(check_component_transitions(now=now))
            finally:
                close_old_connections()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    assert sorted(results) == [0, 1]
    assert len(tg.calls) == 1
    assert len([m for m in mail.outbox if "DOWN since" in m.subject]) == 1


@pytest.mark.django_db
@override_settings(OPS_ALERT_EMAIL=OPS)
def test_status_probe_command_runs_transition_alerts():
    for i in range(2):
        _round(timezone.now() - timedelta(minutes=10 - 5 * i), failing=[SMTP] if i else [])
    with mock.patch(
        "apps.status.management.commands.status_probe.run_probe",
        side_effect=lambda c: (c != SMTP, 3),
    ), fake_telegram() as tg:
        out = StringIO()
        call_command("status_probe", stdout=out)
    assert "status_alerts=1" in out.getvalue()
    assert len(tg.calls) == 1 and "Inbound mail (SMTP) is DOWN" in tg.texts[0]
