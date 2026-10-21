"Public API."

import json
import time
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts import funnel, funnel_signals
from apps.accounts.models import AccountActiveDay, AccountIntent, DemoStartDaily
from apps.accounts.tests.test_funnel_report import (
    NOW,
    SINCE,
    T0,
    activated_account,
    make_account,
    make_inbox,
    receive,
    report,
    row,
    store_unmetered,
)
from apps.inboxes.models import Message

pytestmark = pytest.mark.django_db

TRY_CREATE = reverse("dashboard:try-inbox-create")


# --- (a) demo_started ---------------------------------------------------------


def test_demo_start_counts_per_day_and_validated_ref():
    client = Client()
    assert client.post(TRY_CREATE + "?ref=hn").status_code == 201
    assert Client(REMOTE_ADDR="10.0.0.2").post(TRY_CREATE + "?ref=hn").status_code == 201
    assert Client(REMOTE_ADDR="10.0.0.3").post(TRY_CREATE).status_code == 201
    assert Client(REMOTE_ADDR="10.0.0.4").post(TRY_CREATE + "?ref=<x>").status_code == 201
    rows = {(r.ref, r.count) for r in DemoStartDaily.objects.all()}
    assert rows == {("hn", 2), ("", 2)}
    stored = {f.name for f in DemoStartDaily._meta.get_fields()}
    assert stored == {"id", "day", "ref", "count"}  # no IP, no inbox id


def test_refused_demo_start_is_not_counted():
    client = Client(REMOTE_ADDR="10.9.9.9")
    from apps.accounts.services import DEMO_INBOX_RATE_LIMIT

    codes = [client.post(TRY_CREATE).status_code for _ in range(DEMO_INBOX_RATE_LIMIT + 1)]
    assert codes[-1] == 429
    assert DemoStartDaily.objects.get().count == DEMO_INBOX_RATE_LIMIT


def test_demo_counter_failure_never_breaks_the_demo():
    real = funnel_signals._quote

    def broken(model):
        if model is DemoStartDaily:
            raise RuntimeError("counter table unavailable")
        return real(model)

    with patch.object(funnel_signals, "_quote", side_effect=broken):
        response = Client().post(TRY_CREATE)
    assert response.status_code == 201
    assert not DemoStartDaily.objects.exists()


def test_try_page_forwards_the_ref_to_the_create_call():
    page = Client().get(reverse("dashboard:try-it"), {"ref": "free-for-dev"}).content.decode()
    assert f'data-endpoint="{TRY_CREATE}?ref=free-for-dev"' in page
    plain = Client().get(reverse("dashboard:try-it")).content.decode()
    assert f'data-endpoint="{TRY_CREATE}"' in plain


def test_report_demo_started_is_windowed_by_day_and_ref():
    funnel_signals.record_demo_start("hn", now=T0)
    funnel_signals.record_demo_start("hn", now=T0 + timedelta(days=1))
    funnel_signals.record_demo_start(None, now=T0 + timedelta(days=1))
    funnel_signals.record_demo_start("hn", now=T0 + timedelta(days=7))  # outside [since, since+7)
    rep = report()
    assert rep.demo_started == {
        "total": 3,
        "by_day": [{"day": "2026-09-07", "count": 1}, {"day": "2026-09-08", "count": 2}],
        "by_ref": {"hn": 2, "(none)": 1},
    }


# --- (b) retained activation --------------------------------------------------


def test_retained_needs_two_distinct_utc_days_within_seven_days():
    one_day = activated_account("one-day", ref="hn")  # mail on day 1 only
    two_days = activated_account("two-days", ref="hn")
    receive(make_inbox(two_days, at=T0), at=T0 + timedelta(days=3), marker="second")
    late = activated_account("late", ref="hn")
    receive(make_inbox(late, at=T0), at=T0 + timedelta(days=7, minutes=1), marker="late")
    same_day = activated_account("same-day", ref="hn")
    receive(make_inbox(same_day, at=T0), at=T0 + timedelta(days=1, hours=1), marker="same")
    rep = report(tags=["hn"])
    assert row(rep, "hn").activated == 4
    assert row(rep, "hn").retained == 1
    assert one_day and same_day


def test_retained_survives_the_free_message_purge():
    user = activated_account("purged")
    receive(make_inbox(user, at=T0), at=T0 + timedelta(days=2), marker="d2")
    Message.objects.filter(inbox__user=user).delete()  # Free 1-day retention purge
    assert AccountActiveDay.objects.filter(user=user).count() == 2
    assert row(report(), "direct").retained == 1


def test_retained_counts_unmetered_retained_messages_and_backfill():
    user = activated_account("unmetered")
    inbox = make_inbox(user, at=T0)
    store_unmetered(inbox, marker="u", at=T0 + timedelta(days=4))
    assert row(report(), "direct").retained == 1  # read side sees the retained row
    AccountActiveDay.objects.filter(user=user).delete()
    assert funnel_signals.backfill_active_days(T0) == 2
    assert funnel_signals.backfill_active_days(T0) == 0  # idempotent


def test_active_day_ignores_system_mail_and_mail_after_the_window():
    user = make_account("sys")
    inbox = make_inbox(user, at=T0)
    receive(inbox, at=T0 + timedelta(hours=1), sender="welcome@mailsocket.app", marker="w")
    receive(inbox, at=T0 + timedelta(days=8), marker="after")
    assert not AccountActiveDay.objects.filter(user=user).exists()


def test_retained_is_not_counted_without_activation():
    user = make_account("no-inbox-in-window")
    late_inbox = make_inbox(user, at=T0 + timedelta(days=8))
    AccountActiveDay.objects.create(user=user, day=date(2026, 9, 8))
    AccountActiveDay.objects.create(user=user, day=date(2026, 9, 9))
    assert late_inbox and row(report(), "direct").retained == 0


# --- (c) commercial intent ----------------------------------------------------


def _signed_in(name):
    user = make_account(name)
    client = Client()
    client.force_login(user)
    return user, client


@override_settings(ENABLE_BILLING=True)
def test_pricing_and_billing_views_set_the_flag_once():
    user, client = _signed_in("intent-view")
    assert client.get(reverse("dashboard:pricing")).status_code == 200
    first = AccountIntent.objects.get(user=user).viewed_pricing_at
    assert first is not None
    assert client.get(reverse("dashboard:billing-page")).status_code == 200
    assert AccountIntent.objects.get(user=user).viewed_pricing_at == first  # first event wins
    assert AccountIntent.objects.count() == 1
    stored = {f.name for f in AccountIntent._meta.get_fields()}
    assert stored == {"user", "viewed_pricing_at", "started_checkout_at"}  # flags, not a log


def test_anonymous_pricing_view_and_other_pages_record_nothing():
    Client().get(reverse("dashboard:pricing"))
    _user, client = _signed_in("intent-other")
    client.get(reverse("dashboard:overview"))
    client.get(reverse("dashboard:docs"))
    assert not AccountIntent.objects.exists()


@override_settings(ENABLE_BILLING=True)
def test_checkout_start_sets_the_flag_only_when_it_hands_off():
    user, client = _signed_in("intent-checkout")
    with patch("apps.billing.views._ls_post", side_effect=OSError("down")):
        assert client.post(reverse("dashboard:billing-checkout"), {"plan": "paid"}).status_code == 502
    assert not AccountIntent.objects.filter(user=user, started_checkout_at__isnull=False).exists()
    ok = {"data": {"attributes": {"url": "https://mailsocket.lemonsqueezy.com/checkout/x"}}}
    with patch("apps.billing.views._ls_post", return_value=ok):
        assert client.post(reverse("dashboard:billing-checkout"), {"plan": "paid"}).status_code == 303
    assert AccountIntent.objects.get(user=user).started_checkout_at is not None


def test_intent_failure_never_breaks_the_page():
    _user, client = _signed_in("intent-broken")
    with patch.object(funnel_signals, "record_intent", side_effect=RuntimeError("x")):
        assert client.get(reverse("dashboard:pricing")).status_code == 200


@override_settings(ENABLE_BILLING=True)
def test_intent_already_set_makes_zero_write_queries():
    "Public API."
    user, client = _signed_in("intent-set")
    funnel_signals.record_intent(user.pk, funnel_signals.VIEWED_PRICING, now=T0)
    with CaptureQueriesContext(connection) as ctx:
        assert funnel_signals.record_intent(user.pk, funnel_signals.VIEWED_PRICING) == 0
    assert len(ctx.captured_queries) == 1
    assert ctx.captured_queries[0]["sql"].lstrip().upper().startswith("SELECT")
    with CaptureQueriesContext(connection) as ctx:
        assert client.get(reverse("dashboard:pricing")).status_code == 200
    table = AccountIntent._meta.db_table
    writes = [q["sql"] for q in ctx.captured_queries if table in q["sql"] and "INSERT" in q["sql"].upper()]
    assert writes == []
    assert AccountIntent.objects.get(user=user).viewed_pricing_at == T0


def test_intent_write_is_capped_by_statement_timeout(caplog):
    """A slow / locked INSERT is cancelled at INTENT_WRITE_TIMEOUT_MS: the
    page still answers 200, the failure is logged, the session timeout is
    left as it was."""
    assert connection.vendor == "postgresql"
    _user, client = _signed_in("intent-slow")

    def slow(*_args):  # stands in for an INSERT waiting on a row lock
        return "SELECT pg_sleep(5)", []

    with connection.cursor() as cursor:
        cursor.execute("SHOW statement_timeout")
        before = cursor.fetchone()[0]
    started = time.monotonic()
    with patch.object(funnel_signals, "_intent_sql", side_effect=slow), caplog.at_level("ERROR"):
        assert client.get(reverse("dashboard:pricing")).status_code == 200
    assert time.monotonic() - started < 2
    assert "funnel_signal_failed signal=intent" in caplog.text
    assert "statement timeout" in caplog.text
    with connection.cursor() as cursor:
        cursor.execute("SHOW statement_timeout")
        assert cursor.fetchone()[0] == before
    assert not AccountIntent.objects.exists()


def test_intent_timeout_is_reset_after_a_successful_write():
    user = make_account("intent-reset")
    with connection.cursor() as cursor:
        cursor.execute("SHOW statement_timeout")
        before = cursor.fetchone()[0]
    assert funnel_signals.record_intent(user.pk, funnel_signals.STARTED_CHECKOUT) == 1
    with connection.cursor() as cursor:
        cursor.execute("SHOW statement_timeout")
        assert cursor.fetchone()[0] == before


def test_record_intent_rejects_unknown_fields():
    with pytest.raises(ValueError):
        funnel_signals.record_intent(1, "user_id")


def test_report_counts_intent_flags_per_source():
    a = make_account("int-a", ref="hn")
    b = make_account("int-b", ref="hn")
    make_account("int-c", ref="hn")
    funnel_signals.record_intent(a.pk, funnel_signals.VIEWED_PRICING, now=T0)
    funnel_signals.record_intent(b.pk, funnel_signals.VIEWED_PRICING, now=T0)
    funnel_signals.record_intent(b.pk, funnel_signals.STARTED_CHECKOUT, now=T0)
    hn = row(report(tags=["hn"]), "hn")
    assert (hn.signups, hn.pricing_viewed, hn.checkout_started) == (3, 2, 1)


# --- (d) median time to first external mail -----------------------------------


def test_median_hours_signup_to_first_external_mail():
    activated_account("m1", message_after=timedelta(hours=2))
    activated_account("m2", message_after=timedelta(hours=4))
    activated_account("m3", message_after=timedelta(hours=30))
    make_account("m4")  # no mail: not in the median
    late = make_account("m5")
    receive(make_inbox(late, at=T0), at=T0 + timedelta(hours=1), sender="welcome@mailsocket.app")
    assert report().time_to_first_mail == {"accounts": 3, "median_hours": 4.0}
    assert funnel.build_report(date(2020, 1, 6), now=NOW).time_to_first_mail == {
        "accounts": 0,
        "median_hours": None,
    }


# --- output contract ----------------------------------------------------------


def _run(*args):
    out = StringIO()
    with patch("apps.accounts.funnel.timezone.now", return_value=NOW):
        call_command("funnel_report", *args, stdout=out)
    return out.getvalue()


def test_text_and_json_carry_the_new_counters_without_pii():
    user = activated_account("dana.priv", ref="hn")
    receive(make_inbox(user, at=T0), at=T0 + timedelta(days=2), marker="d2")
    funnel_signals.record_intent(user.pk, funnel_signals.VIEWED_PRICING, now=T0)
    funnel_signals.record_demo_start("hn", now=T0)
    text = _run("--since", SINCE.isoformat(), "--tags", "hn")
    assert "@" not in text and "dana" not in text
    header = text.splitlines()[1].split()
    assert header[-3:] == ["retained", "pricing_viewed", "checkout_started"]
    assert any(
        line.split()[:9] == ["hn", "1", "1", "1", "1", "0", "1", "1", "0"] for line in text.splitlines()
    )
    assert "demo_started (zero-signup /docs/try) 1; by ref: hn 1" in text
    assert "median signup -> first external mail: 24.0 h (over 1 accounts)" in text
    data = json.loads(_run("--since", SINCE.isoformat(), "--json"))
    assert data["demo_started"]["total"] == 1 and data["time_to_first_mail"]["median_hours"] == 24.0
    assert str(user.pk) not in json.dumps(data["demo_started"]) + json.dumps(data["time_to_first_mail"])


def test_report_with_new_signals_is_still_read_only():
    activated_account("ro")
    funnel_signals.record_demo_start("hn", now=T0)
    tables = [AccountActiveDay, AccountIntent, DemoStartDaily]
    before = [m.objects.count() for m in tables]
    with connection.execute_wrapper(_refuse_writes):
        report(tags=["hn"], north_star=True)
    assert [m.objects.count() for m in tables] == before


def _refuse_writes(execute, sql, params, many, context):
    head = sql.lstrip().split(None, 1)[0].upper()
    assert head not in {"INSERT", "UPDATE", "DELETE"}, sql
    return execute(sql, params, many, context)


def test_migration_is_additive():
    import importlib

    module = importlib.import_module("apps.accounts.migrations.0023_activation_funnel_signals")
    ops = module.Migration.operations
    assert {type(op).__name__ for op in ops} == {"CreateModel"}  # new tables only
    assert {op.name for op in ops} == {"AccountIntent", "DemoStartDaily", "AccountActiveDay"}


def test_received_at_window_uses_utc_days():
    user = make_account("utc", created=datetime(2026, 9, 7, 23, 0, tzinfo=UTC))
    inbox = make_inbox(user, at=datetime(2026, 9, 7, 23, 5, tzinfo=UTC))
    receive(inbox, at=datetime(2026, 9, 7, 23, 30, tzinfo=UTC), marker="a")
    receive(inbox, at=datetime(2026, 9, 8, 0, 30, tzinfo=UTC), marker="b")  # next UTC day
    assert sorted(AccountActiveDay.objects.filter(user=user).values_list("day", flat=True)) == [
        date(2026, 9, 7),
        date(2026, 9, 8),
    ]


def test_active_day_hook_failure_never_breaks_usage_metering():
    user = make_account("hook-active")
    inbox = make_inbox(user, at=T0)
    with patch("apps.accounts.funnel_signals.record_active_day", side_effect=RuntimeError("boom")):
        receive(inbox, at=T0 + timedelta(hours=1))
    assert user.monthly_usage.get().messages_received == 1
    assert not AccountActiveDay.objects.filter(user=user).exists()


def test_reconcile_backfills_active_days_of_unmetered_messages():
    from django.utils import timezone

    user = make_account("reconcile-days", created=timezone.now() - timedelta(hours=2))
    store_unmetered(make_inbox(user, at=timezone.now()), marker="r")
    out = StringIO()
    call_command("reconcile_monthly_usage", stdout=out)
    assert "active_days_added=1" in out.getvalue()
    assert AccountActiveDay.objects.filter(user=user).count() == 1
