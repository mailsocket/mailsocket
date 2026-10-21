"Public API."

from datetime import datetime, timedelta
from datetime import timezone as dt_tz

import pytest
from django.test import Client, override_settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.accounts.models import AccountPlan
from apps.billing.intervals import variant_interval
from apps.billing.views import billing_period_start

from .test_billing_ls import SECRET, event_body, make_user, post_event

pytestmark = pytest.mark.django_db

SEEDED_START = datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt_tz.utc)


def _post(user, *, variant_id, created_at, renews_at, event="subscription_created", webhook_id="wh_td07"):
    sub_id = f"sub_{webhook_id}"  # unique per user: stripe_subscription_id is UNIQUE
    body = event_body(
        event,
        user_pk=user.pk,
        variant_id=variant_id,
        status="active",
        created_at=created_at.isoformat(),
        renews_at=renews_at.isoformat(),
        updated_at=timezone.now().isoformat(),
        sub_id=sub_id,
        webhook_id=webhook_id,
    )
    resp = post_event(Client(), body)
    assert resp.status_code == 200
    return AccountPlan.objects.get(user=user)


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_period_start_is_renews_at_minus_one_month_not_created_at():
    user = make_user("td07_old_sub")
    now = timezone.now()
    created_at = now - timedelta(days=122)  # ~4 months old subscription
    renews_at = now + timedelta(days=7)
    plan = _post(user, variant_id="2176466", created_at=created_at, renews_at=renews_at)
    renews = parse_datetime(renews_at.isoformat())
    expected = billing_period_start(renews, "month", 1)
    assert plan.current_period_end == renews
    assert plan.current_period_start == expected
    assert plan.current_period_start != parse_datetime(created_at.isoformat())
    # Calendar check, independent of billing_period_start: one month earlier, same time of day.
    month = renews.month - 1 or 12
    assert (plan.current_period_start.month, plan.current_period_start.time()) == (month, renews.time())
    assert timedelta(days=28) <= renews - plan.current_period_start <= timedelta(days=31)


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_subscription_updated_recomputes_period_start_for_scale_and_business():
    for variant, name in (("2176478", "td07_scale"), ("2187025", "td07_business")):
        user = make_user(name)
        renews_at = datetime(2026, 11, 7, 18, 12, tzinfo=dt_tz.utc)
        plan = _post(
            user,
            variant_id=variant,
            created_at=datetime(2026, 6, 7, 18, 12, tzinfo=dt_tz.utc),
            renews_at=renews_at,
            event="subscription_updated",
            webhook_id=f"wh_{name}",
        )
        assert plan.current_period_start == datetime(2026, 10, 7, 18, 12, tzinfo=dt_tz.utc)


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_missing_interval_leaves_period_start_unchanged(monkeypatch):
    user = make_user("td07_no_interval")
    AccountPlan.objects.filter(user=user).update(current_period_start=SEEDED_START)
    monkeypatch.setattr("apps.billing.intervals.variant_interval", lambda _variant: None)
    now = timezone.now()
    renews_at = now + timedelta(days=7)
    plan = _post(user, variant_id="2176466", created_at=now - timedelta(days=122), renews_at=renews_at)
    assert plan.plan == AccountPlan.Plan.PAID  # the event itself was applied
    assert plan.current_period_end == parse_datetime(renews_at.isoformat())
    assert plan.current_period_start == SEEDED_START  # not created_at, not recomputed


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_missing_renews_at_leaves_period_start_unchanged():
    user = make_user("td07_no_renews")
    AccountPlan.objects.filter(user=user).update(current_period_start=SEEDED_START)
    body = event_body(
        "subscription_created",
        user_pk=user.pk,
        variant_id="2176466",
        status="active",
        created_at=(timezone.now() - timedelta(days=122)).isoformat(),
        updated_at=timezone.now().isoformat(),
        sub_id="sub_td07",
        webhook_id="wh_td07_nr",
    )
    assert post_event(Client(), body).status_code == 200
    plan = AccountPlan.objects.get(user=user)
    assert plan.plan == AccountPlan.Plan.PAID
    assert plan.current_period_start == SEEDED_START


@override_settings(LS_VARIANT_PAID="11", LS_VARIANT_SCALE="22", LS_VARIANT_BUSINESS="33")
def test_variant_interval_known_only_for_plan_variants():
    assert variant_interval("11") == ("month", 1)
    assert variant_interval("22") == ("month", 1)
    assert variant_interval(33) == ("month", 1)
    assert variant_interval("44") is None
    assert variant_interval("") is None
    assert variant_interval(None) is None


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_payment_success_with_unknown_variant_leaves_period_start_untouched():
    "Public API."
    user = make_user("ps_unknown_variant")
    now = timezone.now().replace(microsecond=0)
    start = now - timedelta(days=40)
    AccountPlan.objects.filter(user=user).update(
        plan=AccountPlan.Plan.PAID,
        status=AccountPlan.Status.PAST_DUE,
        stripe_subscription_id="sub_uv",
        stripe_price_id="999999",
        current_period_start=start,
        current_period_end=now - timedelta(days=1),
        billing_synced_at=now - timedelta(days=1),
    )
    body = event_body(
        "subscription_payment_success",
        user_pk=user.pk,
        sub_id="sub_uv",
        variant_id="999999",
        renews_at=(now + timedelta(days=30)).isoformat(),
        updated_at=now.isoformat(),
    )
    assert post_event(Client(), body).status_code == 200
    plan = AccountPlan.objects.get(user=user)
    assert plan.current_period_end == now + timedelta(days=30)
    assert plan.current_period_start == start
