"Public API."
import json
from datetime import timedelta

import pytest
from django.core import mail
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AccountPlan
from apps.billing.models import BillingEvent, BillingPayment
from apps.billing.tests.test_billing_ls import SECRET, event_body, make_user, post_event

pytestmark = pytest.mark.django_db(transaction=True)

OPS = "ops@example.test"
ALERT_SETTINGS = dict(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET, OPS_ALERT_EMAIL=OPS)
BILLING_URL = reverse("dashboard:billing-page")
PAID_VARIANT = "2176466"
SCALE_VARIANT = "2176478"


# --- helpers -------------------------------------------------------------------


def paid_user(name, *, sub_id="sub_9001", synced_at=None):
    user = make_user(name, email=f"{name}@example.test")
    AccountPlan.objects.filter(user=user).update(
        plan=AccountPlan.Plan.PAID,
        status=AccountPlan.Status.ACTIVE,
        stripe_subscription_id=sub_id,
        stripe_customer_id="cust_9001",
        stripe_price_id=PAID_VARIANT,
        billing_synced_at=synced_at or timezone.now(),
    )
    return user


def snapshot(user):
    """Every AccountPlan column a refund must leave alone (incl. updated_at:
    the refund branch must not even save() the row)."""
    p = AccountPlan.objects.get(user=user)
    return (
        p.plan, p.status, p.stripe_customer_id, p.stripe_subscription_id, p.stripe_price_id,
        p.current_period_start, p.current_period_end, p.billing_synced_at, p.updated_at,
    )


def invoice_attrs(*, sub_ls_id=9001, total=1200, refunded_amount=None, refunded=False, status="paid",
                  created_at="2026-10-01T10:00:00.000000Z", reason="renewal"):
    attrs = {
        "subscription_id": sub_ls_id, "billing_reason": reason, "total": total, "total_usd": total,
        "total_formatted": f"${total / 100:,.2f}", "currency": "USD", "card_brand": "visa",
        "card_last_four": "4242", "created_at": created_at, "status": status,
        "urls": {"invoice_url": "https://app.lemonsqueezy.com/my-orders/x/subscription-invoice/1?sig=s"},
    }
    if refunded_amount is not None:
        attrs.update(
            refunded_amount=refunded_amount, refunded=refunded,
            refunded_at="2026-10-09T10:00:00.000000Z" if refunded else None,
        )
    return attrs


def payment_body(user, invoice_id, *, webhook_id, total=1200, updated_at=None, **kw):
    return event_body(
        "subscription_payment_success", user_pk=user.pk, sub_id=invoice_id, webhook_id=webhook_id,
        updated_at=updated_at or timezone.now().isoformat(), extra_attrs=invoice_attrs(total=total, **kw),
    )


def refund_body(user, invoice_id, *, webhook_id, refunded_amount, total=1200, updated_at=None, **kw):
    full = refunded_amount >= total
    return event_body(
        "subscription_payment_refunded", user_pk=user.pk, sub_id=invoice_id, webhook_id=webhook_id,
        updated_at=updated_at or timezone.now().isoformat(),
        extra_attrs=invoice_attrs(
            total=total, refunded_amount=refunded_amount, refunded=full,
            status="refunded" if full else "partial_refund", **kw,
        ),
    )


def seed_payment(client, user, invoice_id, *, total=1200, **kw):
    """Record a real payment through the webhook, then clear the outbox so
    tests count refund alerts only."""
    assert post_event(client, payment_body(user, invoice_id, webhook_id=f"wh_pay_{invoice_id}",
                                           total=total, **kw)).status_code == 200
    mail.outbox.clear()


def refund_alerts():
    return [m for m in mail.outbox if "refund" in m.subject.lower()]


def only_refund_alert():
    alerts = refund_alerts()
    assert len(alerts) == 1, [m.subject for m in mail.outbox]
    assert alerts[0].to == [OPS]
    return alerts[0]


def payment(invoice_id):
    return BillingPayment.objects.get(ls_invoice_id=invoice_id)


# --- 1. full refund ---------------------------------------------------------------


@override_settings(**ALERT_SETTINGS)
def test_full_refund_records_and_alerts_plan_unchanged():
    # BL-10: billing-lifecycle scenario (skill billing-lifecycle)
    user = paid_user("rf_full")
    client = Client()
    seed_payment(client, user, "inv_full")
    before = snapshot(user)

    resp = post_event(client, refund_body(user, "inv_full", webhook_id="wh_rf_full", refunded_amount=1200))
    assert resp.status_code == 200

    assert snapshot(user) == before  # plan PAID, ACTIVE, sub_9001, synced_at untouched
    p = payment("inv_full")
    assert (p.refunded, p.refunded_amount_cents) == (True, 1200)
    assert p.refunded_at is not None
    msg = only_refund_alert()
    assert msg.subject.startswith("[mailsocket] Full refund: $12.00 of $12.00 (invoice inv_full)")
    assert "rf_full@example.test" in msg.subject
    for text in (
        "Plan unchanged",
        "Our plan: Pro (paid), status active — STILL ACTIVE (paid); unchanged by this refund",
        "https://app.lemonsqueezy.com/subscriptions/9001",
        "cancel the subscription in Lemon Squeezy",
        "LS invoice id: inv_full",
    ):
        assert text in msg.body, text
    assert "4242" not in msg.body  

    html = Client()
    html.force_login(user)
    assert "Refunded $12.00" in html.get(BILLING_URL).content.decode()


# --- 2. partial refund --------------------------------------------------------------


@override_settings(**ALERT_SETTINGS)
def test_partial_refund_records_amount_shows_partial_plan_unchanged():
    # BL-11: billing-lifecycle scenario (skill billing-lifecycle)
    user = paid_user("rf_part")
    client = Client()
    seed_payment(client, user, "inv_part")
    before = snapshot(user)

    assert post_event(client, refund_body(user, "inv_part", webhook_id="wh_rf_part",
                                          refunded_amount=500)).status_code == 200

    assert snapshot(user) == before
    p = payment("inv_part")
    assert (p.refunded, p.refunded_amount_cents, p.amount_cents) == (False, 500, 1200)
    msg = only_refund_alert()
    assert "Partial refund: $5.00 of $12.00 (invoice inv_part)" in msg.subject
    assert "Partial refund — $5.00 of $12.00 refunded (cumulative)" in msg.body

    html = Client()
    html.force_login(user)
    assert "Partially refunded $5.00 of $12.00" in html.get(BILLING_URL).content.decode()


# --- 3. refund of an OLD invoice ------------------------------------------------------


@override_settings(**ALERT_SETTINGS)
def test_refund_of_old_invoice_marks_only_that_invoice_plan_unchanged():
    user = paid_user("rf_old")
    client = Client()
    seed_payment(client, user, "inv_aug", created_at="2026-08-01T10:00:00.000000Z")
    seed_payment(client, user, "inv_sep", created_at="2026-09-01T10:00:00.000000Z")
    before = snapshot(user)

    old_ts = (timezone.now() - timedelta(days=30)).isoformat()
    assert post_event(client, refund_body(user, "inv_aug", webhook_id="wh_rf_old", refunded_amount=1200,
                                          updated_at=old_ts)).status_code == 200

    assert snapshot(user) == before
    assert (payment("inv_aug").refunded, payment("inv_aug").refunded_amount_cents) == (True, 1200)
    assert (payment("inv_sep").refunded, payment("inv_sep").refunded_amount_cents) == (False, 0)
    assert "invoice inv_aug" in only_refund_alert().subject


# --- 4. duplicate delivery ---------------------------------------------------------------


@override_settings(**ALERT_SETTINGS)
def test_duplicate_refund_delivery_records_once_alerts_once():
    user = paid_user("rf_dup")
    client = Client()
    seed_payment(client, user, "inv_dup")
    before = snapshot(user)
    body = refund_body(user, "inv_dup", webhook_id="wh_rf_dup", refunded_amount=700)

    assert post_event(client, body).status_code == 200
    # Same webhook id: BillingEvent dedupe.
    assert post_event(client, body).json() == {"duplicate": True}
    # LS re-sends the same refund fact under a NEW webhook id: max() no-op.
    assert post_event(client, refund_body(user, "inv_dup", webhook_id="wh_rf_dup_again",
                                          refunded_amount=700)).status_code == 200

    assert snapshot(user) == before
    assert payment("inv_dup").refunded_amount_cents == 700  # never 1400
    assert BillingEvent.objects.filter(event_name="subscription_payment_refunded").count() == 2
    only_refund_alert()


# --- 5. two partial refunds: cumulative, max() not sum, out of order ----------------------


@override_settings(**ALERT_SETTINGS)
def test_two_partials_cumulative_amount_stored_as_max_never_summed():
    # BL-11: billing-lifecycle scenario (skill billing-lifecycle)
    user = paid_user("rf_cum")
    client = Client()
    seed_payment(client, user, "inv_cum")
    before = snapshot(user)

    # Partial #1: $3 refunded so far.
    assert post_event(client, refund_body(user, "inv_cum", webhook_id="wh_c1", refunded_amount=300)).status_code == 200
    assert payment("inv_cum").refunded_amount_cents == 300
    assert len(refund_alerts()) == 1
    mail.outbox.clear()

    # Partial #2: LS sends the CUMULATIVE total ($3 + $5 = $8), not the delta.
    assert post_event(client, refund_body(user, "inv_cum", webhook_id="wh_c2", refunded_amount=800)).status_code == 200
    p = payment("inv_cum")
    assert (p.refunded_amount_cents, p.refunded) == (800, False)  # summing would give 1100
    assert "Partial refund: $8.00 of $12.00" in only_refund_alert().subject
    mail.outbox.clear()

    # A late re-delivery of partial #1 (older, lower cumulative) arrives last.
    assert post_event(client, refund_body(user, "inv_cum", webhook_id="wh_c1_late",
                                          refunded_amount=300)).status_code == 200
    assert payment("inv_cum").refunded_amount_cents == 800  # never regresses to 300
    assert refund_alerts() == []
    assert snapshot(user) == before


@override_settings(**ALERT_SETTINGS)
def test_out_of_order_partials_newest_first_keep_max():
    user = paid_user("rf_ooo")
    client = Client()
    seed_payment(client, user, "inv_ooo")
    before = snapshot(user)

    assert post_event(client, refund_body(user, "inv_ooo", webhook_id="wh_o2", refunded_amount=900)).status_code == 200
    assert post_event(client, refund_body(user, "inv_ooo", webhook_id="wh_o1", refunded_amount=400)).status_code == 200

    assert payment("inv_ooo").refunded_amount_cents == 900
    assert snapshot(user) == before
    only_refund_alert()


# --- 6. unknown invoice: refund BEFORE payment_success -------------------------------------


@override_settings(**ALERT_SETTINGS)
def test_refund_before_payment_creates_row_from_refund_payload_then_payment_keeps_it():
    user = paid_user("rf_early")
    client = Client()
    before = snapshot(user)
    assert not BillingPayment.objects.filter(ls_invoice_id="inv_early").exists()

    assert post_event(client, refund_body(user, "inv_early", webhook_id="wh_rf_early",
                                          refunded_amount=500, total=4900)).status_code == 200

    assert snapshot(user) == before
    p = payment("inv_early")  # created from the refund payload (full invoice object)
    assert (p.user_id, p.amount_cents, p.currency, p.ls_subscription_id) == (user.pk, 4900, "USD", "9001")
    assert (p.refunded, p.refunded_amount_cents) == (False, 500)
    msg = only_refund_alert()
    assert "row created from the refund payload" in msg.body
    assert "Partial refund: $5.00 of $49.00" in msg.subject

    # payment_success for the same invoice arrives late: must NOT overwrite
    # the refunded fields, must not add a second row.
    mail.outbox.clear()
    assert post_event(client, payment_body(user, "inv_early", webhook_id="wh_pay_early",
                                           total=4900)).status_code == 200
    assert BillingPayment.objects.filter(ls_invoice_id="inv_early").count() == 1
    p = payment("inv_early")
    assert (p.refunded, p.refunded_amount_cents, p.amount_cents) == (False, 500, 4900)
    plan = AccountPlan.objects.get(user=user)
    assert (plan.plan, plan.status, plan.stripe_subscription_id) == ("paid", "active", "sub_9001")
    assert refund_alerts() == []  # the payment event is not a refund alert


# --- 7. order_refunded without a matching row -----------------------------------------------


@override_settings(**ALERT_SETTINGS)
def test_order_refunded_without_matching_row_sends_owner_alert_plan_unchanged():
    user = paid_user("rf_order")
    client = Client()
    before = snapshot(user)
    rows_before = BillingPayment.objects.count()
    body = event_body(
        "order_refunded", user_pk=user.pk, sub_id="ord_5551", webhook_id="wh_ord",
        updated_at=timezone.now().isoformat(), status="refunded",
        extra_attrs={"total": 1200, "refunded_amount": 1200, "refunded": True, "currency": "USD",
                     "total_formatted": "$12.00"},
    )
    assert post_event(client, body).status_code == 200

    assert snapshot(user) == before
    assert BillingPayment.objects.count() == rows_before  # nothing matched / invented
    msg = only_refund_alert()
    assert "Refund on LS order ord_5551: $12.00 of $12.00 — no matching payment row" in msg.subject
    for text in ("https://app.lemonsqueezy.com/orders/ord_5551",
                 "https://app.lemonsqueezy.com/subscriptions/sub_9001",
                 "STILL ACTIVE (paid)", "Plan unchanged"):
        assert text in msg.body, text


# --- 8. refund must not advance billing_synced_at --------------------------------------------


@override_settings(**ALERT_SETTINGS)
def test_refund_newer_than_synced_at_then_real_subscription_updated_still_applies():
    t0 = timezone.now() - timedelta(hours=2)
    user = paid_user("rf_sync", synced_at=t0)
    client = Client()
    # Seed payment stamped BEFORE t0 so it does not move billing_synced_at
    # itself (the payment row is still recorded pre-guard).
    seed_payment(client, user, "inv_sync", updated_at=(t0 - timedelta(hours=1)).isoformat())
    synced_before = AccountPlan.objects.get(user=user).billing_synced_at
    before = snapshot(user)

    # Refund stamped NOW (newer than synced_at).
    assert post_event(client, refund_body(user, "inv_sync", webhook_id="wh_rs", refunded_amount=1200,
                                          updated_at=timezone.now().isoformat())).status_code == 200
    assert synced_before == t0
    assert snapshot(user) == before
    assert AccountPlan.objects.get(user=user).billing_synced_at == synced_before
    assert payment("inv_sync").refunded_amount_cents == 1200
    only_refund_alert()

    # A real subscription_updated stamped BETWEEN the old sync and the refund
    # (upgrade to Scale) must still apply — it would be stale-skipped if the
    # refund had advanced billing_synced_at.
    between = (timezone.now() - timedelta(minutes=30)).isoformat()
    upd = event_body("subscription_updated", user_pk=user.pk, sub_id="sub_9001", webhook_id="wh_upd",
                     variant_id=SCALE_VARIANT, status="active", updated_at=between)
    assert post_event(client, upd).status_code == 200
    plan = AccountPlan.objects.get(user=user)
    assert (plan.plan, plan.stripe_price_id) == (AccountPlan.Plan.SCALE, SCALE_VARIANT)


# --- 9. data.id (invoice id) != stored subscription id ------------------------------------------


@override_settings(**ALERT_SETTINGS)
def test_refund_with_invoice_id_not_subscription_id_still_recorded():
    """The mismatch guard compares data.id to stripe_subscription_id; for a
    refund data.id is the INVOICE id. A refund payload without updated_at
    passes the stale-skip and lands exactly on that guard (``newer`` False),
    which used to swallow it."""
    user = paid_user("rf_mism", sub_id="sub_9001")
    client = Client()
    before = snapshot(user)
    body = json.loads(refund_body(user, "inv_77", webhook_id="wh_mism", refunded_amount=600))
    del body["data"]["attributes"]["updated_at"]
    assert body["data"]["id"] != AccountPlan.objects.get(user=user).stripe_subscription_id

    assert post_event(client, json.dumps(body).encode()).status_code == 200

    assert snapshot(user) == before
    p = payment("inv_77")
    assert (p.refunded_amount_cents, p.refunded) == (600, False)
    only_refund_alert()





@override_settings(**ALERT_SETTINGS)
def test_refund_without_amount_is_problem_alert_plan_unchanged():
    user = paid_user("rf_bad")
    client = Client()
    before = snapshot(user)
    body = json.loads(refund_body(user, "inv_bad", webhook_id="wh_bad", refunded_amount=100))
    del body["data"]["attributes"]["refunded_amount"]
    body["data"]["attributes"]["refunded"] = False
    assert post_event(client, json.dumps(body).encode()).status_code == 200
    assert snapshot(user) == before
    assert not BillingPayment.objects.filter(ls_invoice_id="inv_bad").exists()
    assert len(mail.outbox) == 1
    assert "Billing webhook problem: refund amount/invoice missing" in mail.outbox[0].subject


@override_settings(**ALERT_SETTINGS)
def test_refund_for_invoice_of_another_account_not_recorded_problem_alert():
    owner = paid_user("rf_own_a", sub_id="sub_a")
    other = paid_user("rf_own_b", sub_id="sub_b")
    client = Client()
    seed_payment(client, owner, "inv_a")
    before_owner, before_other = snapshot(owner), snapshot(other)

    assert post_event(client, refund_body(other, "inv_a", webhook_id="wh_x", refunded_amount=1200)).status_code == 200

    assert (snapshot(owner), snapshot(other)) == (before_owner, before_other)
    assert payment("inv_a").refunded_amount_cents == 0
    assert len(mail.outbox) == 1
    assert "refund invoice belongs to another account" in mail.outbox[0].subject


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET, OPS_ALERT_EMAIL="")
def test_refund_with_no_alert_channel_still_recorded_silently():
    user = paid_user("rf_quiet")
    client = Client()
    before = snapshot(user)
    assert post_event(client, refund_body(user, "inv_q", webhook_id="wh_q", refunded_amount=1200)).status_code == 200
    assert snapshot(user) == before
    assert payment("inv_q").refunded is True
    assert mail.outbox == []


@override_settings(**ALERT_SETTINGS)
def test_subscription_expired_still_downgrades_after_refund():
    # BL-10: billing-lifecycle scenario (skill billing-lifecycle)
    """The documented way to end access after a refund: LS cancel -> expired."""
    user = paid_user("rf_exp")
    client = Client()
    seed_payment(client, user, "inv_exp")
    assert post_event(client, refund_body(user, "inv_exp", webhook_id="wh_rexp", refunded_amount=1200)).status_code == 200
    assert AccountPlan.objects.get(user=user).plan == AccountPlan.Plan.PAID
    exp = event_body("subscription_expired", user_pk=user.pk, sub_id="sub_9001", webhook_id="wh_exp",
                     status="expired", updated_at=(timezone.now() + timedelta(seconds=5)).isoformat())
    assert post_event(client, exp).status_code == 200
    plan = AccountPlan.objects.get(user=user)
    assert (plan.plan, plan.stripe_subscription_id) == (AccountPlan.Plan.FREE, None)
