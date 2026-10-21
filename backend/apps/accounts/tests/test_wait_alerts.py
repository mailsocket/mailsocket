"Public API."
import threading
from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.db import DatabaseError, close_old_connections, connection
from django.test import Client, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts import services, wait_alerts
from apps.accounts.models import (
    Account,
    AccountPlan,
    WaitCapacity,
    WaitCapacityAlert,
    WaitCapacityDaily,
)
from apps.accounts.services import (
    WAIT_CAPACITY_FREE_KEY,
    WAIT_CAPACITY_KEY,
    WaitSlotError,
    acquire_wait_capacity,
    create_api_key,
    release_wait_capacity,
    wait_capacity_warn_threshold,
    wait_slot,
)
from apps.inboxes.models import Inbox

OPS = "ops@example.test"


def _make_user(name, *, plan=AccountPlan.Plan.PAID):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@customer.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=f"{name}@customer.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user, plan=plan)
    return user


def _day(pool):
    return WaitCapacityDaily.objects.filter(date=timezone.now().date(), pool=pool).first()


def _alert_mails(level, pool=None):
    return [
        m for m in mail.outbox
        if f"Wait capacity {level.upper()}" in m.subject and (pool is None or f": {pool} pool" in m.subject)
    ]


# --- recording -----------------------------------------------------------------


@pytest.mark.django_db
def test_warn_threshold_is_80_percent_of_real_limits():
    assert wait_capacity_warn_threshold(services.WAIT_CAPACITY_LIMIT) == 52
    assert wait_capacity_warn_threshold(services.WAIT_CAPACITY_FREE_LIMIT) == 10


@pytest.mark.django_db
def test_global_refusal_counted_and_marked_saturated(monkeypatch):
    monkeypatch.setattr(services, "WAIT_CAPACITY_LIMIT", 2)
    assert acquire_wait_capacity() is True
    assert acquire_wait_capacity() is True
    for _ in range(3):
        assert acquire_wait_capacity() is False
    g = _day(WAIT_CAPACITY_KEY)
    assert g.refusals == 3
    assert g.peak == 2
    assert g.saturated_at is not None
    assert _day(WAIT_CAPACITY_FREE_KEY) is None  # paid path never touches the free pool


@pytest.mark.django_db
def test_free_pool_refusal_counted_separately():
    for _ in range(services.WAIT_CAPACITY_FREE_LIMIT):
        assert acquire_wait_capacity(free=True) is True
    assert acquire_wait_capacity(free=True) is False
    assert acquire_wait_capacity(free=True) is False
    f = _day(WAIT_CAPACITY_FREE_KEY)
    g = _day(WAIT_CAPACITY_KEY)
    assert f.refusals == 2 and f.saturated_at is not None
    assert f.peak == services.WAIT_CAPACITY_FREE_LIMIT
    assert g.refusals == 0 and g.saturated_at is None  # global was never full
    assert g.peak == services.WAIT_CAPACITY_FREE_LIMIT


@pytest.mark.django_db
def test_free_caller_refused_by_full_global_pool_counts_global(monkeypatch):
    monkeypatch.setattr(services, "WAIT_CAPACITY_LIMIT", 1)
    assert acquire_wait_capacity() is True
    assert acquire_wait_capacity(free=True) is False
    assert _day(WAIT_CAPACITY_KEY).refusals == 1
    # The free slot was taken then given back: not a free-pool refusal.
    assert _day(WAIT_CAPACITY_FREE_KEY) is None
    assert WaitCapacity.objects.get(key=WAIT_CAPACITY_FREE_KEY).count == 0


@pytest.mark.django_db
def test_per_account_refusal_is_not_counted():
    user = _make_user("walert_peracct", plan=AccountPlan.Plan.FREE)
    limit = services.wait_lease_limit_for(user.pk)
    for _ in range(limit):
        assert services.acquire_wait_lease(user.pk) is True
    with pytest.raises(WaitSlotError) as exc:
        with wait_slot(user.pk):
            pass
    assert exc.value.reason == "too_many_wait_requests"
    assert not WaitCapacityDaily.objects.filter(refusals__gt=0).exists()
    assert not WaitCapacityDaily.objects.filter(saturated_at__isnull=False).exists()


@pytest.mark.django_db
def test_peak_tracks_highest_in_flight_not_latest():
    for _ in range(3):
        assert acquire_wait_capacity() is True
    release_wait_capacity()
    release_wait_capacity()
    assert acquire_wait_capacity() is True  # in flight now 2
    g = _day(WAIT_CAPACITY_KEY)
    assert g.peak == 3
    assert g.refusals == 0
    assert g.warned_at is None  # 3 of 64 is far below 80%


@pytest.mark.django_db
def test_warning_marked_exactly_at_80_percent_of_real_global_limit():
    for _ in range(51):
        assert acquire_wait_capacity() is True
    assert _day(WAIT_CAPACITY_KEY).warned_at is None
    assert acquire_wait_capacity() is True  # 52 / 64
    assert _day(WAIT_CAPACITY_KEY).warned_at is not None


@pytest.mark.django_db
def test_stats_write_is_outside_the_counter_lock(monkeypatch):
    """The upsert must run after the counter-row atomic (FOR UPDATE lock) has
    exited: its savepoint depth is exactly caller depth + 1 (its own private
    atomic), not + 2 (nested inside the counter transaction)."""
    seen = []

    def spy(**kwargs):
        seen.append(len(connection.savepoint_ids))

    monkeypatch.setattr(services, "_upsert_wait_capacity_daily", spy)
    baseline = len(connection.savepoint_ids)
    assert acquire_wait_capacity() is True
    assert acquire_wait_capacity(free=True) is True
    assert seen == [baseline + 1] * 3


# --- stats failures never break a wait -------------------------------------------


@pytest.mark.django_db
def test_stats_failure_does_not_break_acquire_or_release(monkeypatch):
    def boom(**kwargs):
        raise DatabaseError("stats down")

    monkeypatch.setattr(services, "_upsert_wait_capacity_daily", boom)
    monkeypatch.setattr(services, "WAIT_CAPACITY_LIMIT", 1)
    assert acquire_wait_capacity() is True
    assert acquire_wait_capacity() is False  # refusal path also survives
    release_wait_capacity()
    assert WaitCapacity.objects.get(key=WAIT_CAPACITY_KEY).count == 0
    assert acquire_wait_capacity(free=True) is True
    release_wait_capacity(free=True)
    assert not WaitCapacityDaily.objects.exists()


@pytest.mark.django_db
def test_failed_stats_sql_does_not_poison_callers_transaction(monkeypatch):
    """A real SQL error in the stats write must be contained by its savepoint:
    without it, every later query in the request transaction would fail."""

    def bad_sql(**kwargs):
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM walert_table_that_does_not_exist")

    monkeypatch.setattr(services, "_upsert_wait_capacity_daily", bad_sql)
    assert acquire_wait_capacity() is True
    assert WaitCapacity.objects.get(key=WAIT_CAPACITY_KEY).count == 1


@pytest.mark.django_db(transaction=True)
def test_stats_failure_does_not_break_wait_endpoint(monkeypatch):
    def boom(**kwargs):
        raise DatabaseError("stats down")

    monkeypatch.setattr(services, "_upsert_wait_capacity_daily", boom)
    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    user = _make_user("walert_endpoint")
    inbox = Inbox.objects.create(user=user, routing_key="walert_endpoint_box")
    _, token = create_api_key(user, "test")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    response = client.get(f"/api/v1/inboxes/{inbox.public_id}/messages/wait?timeout=1")
    assert response.status_code == 204
    assert WaitCapacity.objects.get(key=WAIT_CAPACITY_KEY).count == 0  # released


# --- alert emails ----------------------------------------------------------------


@pytest.mark.django_db
@override_settings(OPS_ALERT_EMAIL=OPS)
def test_warning_sends_exactly_one_email_per_hour():
    for _ in range(52):
        assert acquire_wait_capacity() is True
    assert wait_alerts.send_due_wait_capacity_alerts() == 1
    # Re-triggered many times within the hour: still one email.
    for _ in range(5):
        release_wait_capacity()
        assert acquire_wait_capacity() is True
        wait_alerts.send_due_wait_capacity_alerts()
    msgs = _alert_mails("warning", WAIT_CAPACITY_KEY)
    assert len(msgs) == 1
    assert len(mail.outbox) == 1
    body = msgs[0].body
    assert msgs[0].to == [OPS]
    assert "In flight now: 52 / 64" in body
    assert "Today's peak (UTC): 52 / 64" in body
    assert "429" in body and "Retry-After 2s" in body and "SDKs retry" in body
    assert "WAIT_CAPACITY_LIMIT" in body and "GUNICORN_THREADS" in body
    assert "Long-poll wait endpoint" in body
    # After the hour, a NEW event alerts again.
    later = timezone.now() + timedelta(minutes=61)
    WaitCapacityDaily.objects.filter(pool=WAIT_CAPACITY_KEY).update(warned_at=later)
    assert wait_alerts.send_due_wait_capacity_alerts(now=later + timedelta(seconds=1)) == 1
    assert len(_alert_mails("warning", WAIT_CAPACITY_KEY)) == 2


@pytest.mark.django_db
@override_settings(OPS_ALERT_EMAIL=OPS)
def test_no_repeat_email_for_an_old_event_after_the_hour():
    for _ in range(52):
        acquire_wait_capacity()
    assert wait_alerts.send_due_wait_capacity_alerts() == 1
    # Two hours later, no new warning event happened: nothing to send.
    assert wait_alerts.send_due_wait_capacity_alerts(now=timezone.now() + timedelta(hours=2)) == 0


@pytest.mark.django_db
@override_settings(OPS_ALERT_EMAIL=OPS)
def test_saturation_sends_exactly_one_email_per_hour_even_when_refused_many_times(monkeypatch):
    for _ in range(services.WAIT_CAPACITY_FREE_LIMIT):
        assert acquire_wait_capacity(free=True) is True
    for _ in range(20):
        assert acquire_wait_capacity(free=True) is False
        wait_alerts.send_due_wait_capacity_alerts()
    sat = _alert_mails("saturated", WAIT_CAPACITY_FREE_KEY)
    assert len(sat) == 1
    assert "Today's refusals (UTC): 1" in sat[0].body  # first run, right after the 1st refusal
    assert "12 / 12" in sat[0].body
    assert _day(WAIT_CAPACITY_FREE_KEY).refusals == 20
    assert _alert_mails("saturated", WAIT_CAPACITY_KEY) == []  # global never refused
    # Free pool also crossed 80% -> one warning for it; no customer data in any mail.
    assert len(_alert_mails("warning", WAIT_CAPACITY_FREE_KEY)) == 1
    for m in mail.outbox:
        assert "@customer.test" not in m.body


@pytest.mark.django_db
def test_claim_is_db_enforced_two_workers_one_winner():
    """Simulates two workers that both decided an alert is due: the
    conditional UPDATE lets exactly one of them claim the hourly slot."""
    now = timezone.now()
    first = wait_alerts._claim(WAIT_CAPACITY_KEY, wait_alerts.SATURATED, now)
    second = wait_alerts._claim(WAIT_CAPACITY_KEY, wait_alerts.SATURATED, now)
    assert first is not None
    assert second is None
    # Another (pool, level) has its own slot.
    assert wait_alerts._claim(WAIT_CAPACITY_KEY, wait_alerts.WARNING, now) is not None
    assert wait_alerts._claim(WAIT_CAPACITY_KEY, wait_alerts.SATURATED, now + timedelta(minutes=59)) is None
    assert wait_alerts._claim(WAIT_CAPACITY_KEY, wait_alerts.SATURATED, now + timedelta(minutes=61)) is not None


@pytest.mark.django_db(transaction=True)
@override_settings(OPS_ALERT_EMAIL=OPS)
def test_two_concurrent_senders_send_one_email(monkeypatch):
    monkeypatch.setattr(services, "WAIT_CAPACITY_LIMIT", 1)
    assert acquire_wait_capacity() is True
    assert acquire_wait_capacity() is False
    barrier = threading.Barrier(2)
    real_claim = wait_alerts._claim

    def synced_claim(*a, **kw):
        barrier.wait(timeout=10)  # both passed the "already alerted?" pre-check
        return real_claim(*a, **kw)

    monkeypatch.setattr(wait_alerts, "_claim", synced_claim)
    monkeypatch.setattr(wait_alerts, "LEVELS", (wait_alerts.SATURATED,))
    monkeypatch.setattr(wait_alerts, "pool_limits", lambda: {WAIT_CAPACITY_KEY: 1})
    results = []

    def worker():
        try:
            results.append(wait_alerts.send_due_wait_capacity_alerts())
        finally:
            close_old_connections()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(results) == [0, 1]
    assert len(_alert_mails("saturated", WAIT_CAPACITY_KEY)) == 1


@pytest.mark.django_db
@override_settings(OPS_ALERT_EMAIL="")
def test_no_email_when_ops_alert_email_empty(monkeypatch):
    monkeypatch.setattr(services, "WAIT_CAPACITY_LIMIT", 1)
    acquire_wait_capacity()
    acquire_wait_capacity()
    assert wait_alerts.send_due_wait_capacity_alerts() == 0
    assert mail.outbox == []
    assert not WaitCapacityAlert.objects.filter(last_alert_at__isnull=False).exists()
    # Recording still happened.
    assert _day(WAIT_CAPACITY_KEY).refusals == 1


@pytest.mark.django_db
@override_settings(OPS_ALERT_EMAIL=OPS)
def test_failed_send_never_raises_and_releases_claim(monkeypatch):
    monkeypatch.setattr(services, "WAIT_CAPACITY_LIMIT", 1)
    acquire_wait_capacity()
    acquire_wait_capacity()

    def smtp_down(*a, **kw):
        raise OSError("smtp down")

    monkeypatch.setattr("apps.ops.notify.send_mail", smtp_down)
    assert wait_alerts.send_due_wait_capacity_alerts() == 0
    assert not WaitCapacityAlert.objects.filter(last_alert_at__isnull=False).exists()
    monkeypatch.undo()
    monkeypatch.setattr(wait_alerts, "pool_limits", lambda: {WAIT_CAPACITY_KEY: 1})
    assert wait_alerts.send_due_wait_capacity_alerts() >= 1  # retried next run
    assert len(_alert_mails("saturated", WAIT_CAPACITY_KEY)) == 1


@pytest.mark.django_db
@override_settings(OPS_ALERT_EMAIL=OPS)
def test_status_probe_cron_sends_due_alerts(monkeypatch):
    import socket
    import urllib.request

    monkeypatch.setattr(services, "WAIT_CAPACITY_LIMIT", 1)
    acquire_wait_capacity()
    acquire_wait_capacity()
    assert mail.outbox == []  # the wait path itself never sends

    def no_net(*a, **kw):
        raise OSError("no network in tests")

    monkeypatch.setattr(socket, "create_connection", no_net)
    monkeypatch.setattr(urllib.request, "urlopen", no_net)
    out = StringIO()
    call_command("status_probe", stdout=out)
    assert len(_alert_mails("saturated", WAIT_CAPACITY_KEY)) == 1
    assert "wait_alerts=" in out.getvalue()





@pytest.mark.django_db
def test_fleet_wait_capacity_panel_staff_only():
    for _ in range(3):
        acquire_wait_capacity()
    acquire_wait_capacity(free=True)
    yesterday = timezone.now().date() - timedelta(days=1)
    WaitCapacityDaily.objects.create(date=yesterday, pool=WAIT_CAPACITY_KEY, peak=47, refusals=9)
    WaitCapacityDaily.objects.create(
        date=timezone.now().date() - timedelta(days=20), pool=WAIT_CAPACITY_KEY, peak=63, refusals=99
    )

    U = get_user_model()
    staff = U.objects.create_user("walert_staff", password="x", is_staff=True)
    c = Client()
    c.force_login(staff)
    r = c.get("/app/fleet/", HTTP_HOST="testserver", secure=True)
    assert r.status_code == 200
    body = r.content.decode()
    assert "Wait capacity" in body
    ctx = r.context
    assert ctx["wait_global_now"] == 4 and ctx["wait_global_limit"] == 64
    assert ctx["wait_free_now"] == 1 and ctx["wait_free_limit"] == 12
    days = ctx["wait_days"]
    assert len(days) == 14
    assert days[0]["date"] == timezone.now().date() and days[0]["global_peak"] == 4
    assert days[0]["free_peak"] == 1
    assert days[1]["global_peak"] == 47 and days[1]["global_refusals"] == 9
    assert ">47<" in body and ">9<" in body
    assert ">63<" not in body and ">99<" not in body  # outside the 14-day window
    panel = body[body.index("Wait capacity"):]
    assert "style=" not in panel and "<script" not in panel  # CSP

    normal = U.objects.create_user("walert_normal", password="x")
    c2 = Client()
    c2.force_login(normal)
    # qafix2 #20: customers get a 404 (no redirect revealing the admin login).
    assert c2.get("/app/fleet/", HTTP_HOST="testserver", secure=True).status_code == 404
    assert Client().get("/app/fleet/", HTTP_HOST="testserver", secure=True).status_code == 404


# --- retention -------------------------------------------------------------------


@pytest.mark.django_db
def test_purge_keeps_400_days_in_bounded_batches():
    today = timezone.now().date()
    for i in range(5):
        WaitCapacityDaily.objects.create(date=today - timedelta(days=401 + i), pool=WAIT_CAPACITY_KEY)
    keep = WaitCapacityDaily.objects.create(date=today - timedelta(days=400), pool=WAIT_CAPACITY_KEY)
    keep2 = WaitCapacityDaily.objects.create(date=today, pool=WAIT_CAPACITY_FREE_KEY)

    deletes = []
    from django.db.models.query import QuerySet

    real_delete = QuerySet.delete

    def counting_delete(self):
        result = real_delete(self)
        if self.model is WaitCapacityDaily:
            deletes.append(result[0])
        return result

    QuerySet.delete = counting_delete
    try:
        assert wait_alerts.purge_old_wait_capacity_daily(batch_size=2) == 5
    finally:
        QuerySet.delete = real_delete
    assert deletes == [2, 2, 1]  # each batch bounded by batch_size
    assert set(WaitCapacityDaily.objects.values_list("pk", flat=True)) == {keep.pk, keep2.pk}

    WaitCapacityDaily.objects.create(date=today - timedelta(days=500), pool=WAIT_CAPACITY_KEY)
    out = StringIO()
    call_command("purge_expired_messages", stdout=out)
    assert "wait_capacity_days=1" in out.getvalue()
    assert WaitCapacityDaily.objects.count() == 2


# --- tg-notify: Telegram channel -------------------------------------------------


@pytest.mark.django_db
@override_settings(OPS_ALERT_EMAIL=OPS)
def test_wait_alerts_go_to_telegram_warn_and_down(monkeypatch):
    from apps.ops.testing import fake_telegram

    monkeypatch.setattr(services, "WAIT_CAPACITY_LIMIT", 1)
    monkeypatch.setattr(wait_alerts, "pool_limits", lambda: {WAIT_CAPACITY_KEY: 1})
    acquire_wait_capacity()
    acquire_wait_capacity()  # refused -> saturated (warned too: 1/1 >= 80%)
    with fake_telegram() as tg:
        assert wait_alerts.send_due_wait_capacity_alerts() == 2
        assert wait_alerts.send_due_wait_capacity_alerts() == 0  # throttled
    texts = sorted(tg.texts)
    assert len(texts) == 2
    assert any(t.startswith(f"🔴 Wait capacity SATURATED: {WAIT_CAPACITY_KEY} pool") for t in texts)
    assert any(t.startswith(f"🟠 Wait capacity WARNING: {WAIT_CAPACITY_KEY} pool") for t in texts)
    assert len(mail.outbox) == 2
    assert all("@" not in t for t in texts)


@pytest.mark.django_db
@override_settings(OPS_ALERT_EMAIL="")
def test_wait_alerts_telegram_only_still_sends(monkeypatch):
    from apps.ops.testing import fake_telegram

    monkeypatch.setattr(services, "WAIT_CAPACITY_LIMIT", 1)
    monkeypatch.setattr(wait_alerts, "LEVELS", (wait_alerts.SATURATED,))
    monkeypatch.setattr(wait_alerts, "pool_limits", lambda: {WAIT_CAPACITY_KEY: 1})
    acquire_wait_capacity()
    acquire_wait_capacity()
    with fake_telegram() as tg:
        assert wait_alerts.send_due_wait_capacity_alerts() == 1
    assert mail.outbox == [] and len(tg.calls) == 1


@pytest.mark.django_db
@override_settings(OPS_ALERT_EMAIL=OPS)
def test_wait_alert_email_down_but_telegram_ok_keeps_claim(monkeypatch):
    from apps.ops.testing import fake_telegram

    monkeypatch.setattr(services, "WAIT_CAPACITY_LIMIT", 1)
    monkeypatch.setattr(wait_alerts, "LEVELS", (wait_alerts.SATURATED,))
    monkeypatch.setattr(wait_alerts, "pool_limits", lambda: {WAIT_CAPACITY_KEY: 1})
    acquire_wait_capacity()
    acquire_wait_capacity()
    monkeypatch.setattr("apps.ops.notify.send_mail", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    with fake_telegram() as tg:
        assert wait_alerts.send_due_wait_capacity_alerts() == 1
        assert wait_alerts.send_due_wait_capacity_alerts() == 0  # delivered: not retried
    assert len(tg.calls) == 1
