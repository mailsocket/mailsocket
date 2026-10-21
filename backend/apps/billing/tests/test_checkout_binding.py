"Public API."

import io
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core import mail, signing
from django.core.management import call_command
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.accounts.models import AccountPlan
from apps.billing.models import BillingEvent, BillingPayment
from apps.billing.tests.test_billing_ls import SECRET, event_body, make_user, post_event
from apps.billing.tests.test_billing_reconcile import ON, Edge
from apps.billing.tests.test_billing_reconcile_invoices import ls_inv
from apps.billing.tests.test_billing_refund import invoice_attrs

pytestmark = pytest.mark.django_db(transaction=True)

OPS = "ops@example.test"
ALERTS = dict(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET, OPS_ALERT_EMAIL=OPS)
PAID, SCALE = "2176466", "2176478"
SALT = "mailsocket.ls-checkout.v1"
BUYER_EMAIL = "buyer-secret@attacker.test"
REFUSED_SUBJECT = "[mailsocket] Billing webhook problem: unsigned binding refused"


def sig_for(pk):
    """The documented contract: django.core.signing, salt mailsocket.ls-checkout.v1."""
    return signing.Signer(salt=SALT).sign(str(pk))


def unsigned(pk):
    return {"user_pk": str(pk)}


def signed(pk, *, sig_pk=None):
    return {"user_pk": str(pk), "user_sig": sig_for(pk if sig_pk is None else sig_pk)}


def subscribed(name, sub_id, *, variant=PAID):
    user = make_user(name)
    AccountPlan.objects.filter(user=user).update(
        plan=AccountPlan.Plan.PAID,
        status=AccountPlan.Status.ACTIVE,
        stripe_subscription_id=sub_id,
        stripe_price_id=variant,
        stripe_customer_id="cust_live",
        current_period_end=timezone.now() + timedelta(days=10),
        billing_synced_at=timezone.now() - timedelta(days=1),
    )
    return user


def row(user):
    """The whole AccountPlan row, including updated_at (any save changes it)."""
    return AccountPlan.objects.filter(user=user).values().get()


def now_iso(minutes=0):
    return (timezone.now() + timedelta(minutes=minutes)).isoformat()


def created_body(custom_data, *, sub_id, webhook_id, variant=PAID):
    return event_body(
        "subscription_created",
        custom_data=custom_data,
        sub_id=sub_id,
        variant_id=variant,
        webhook_id=webhook_id,
        updated_at=now_iso(),
        renews_at=now_iso(60 * 24 * 30),
        customer_id="cust_attacker",
        extra_attrs={"user_email": BUYER_EMAIL},
    )


def payment_body(event_name, custom_data, *, invoice_id, sub_ls_id, webhook_id, **attrs_kw):
    attrs = invoice_attrs(sub_ls_id=sub_ls_id, **attrs_kw)
    attrs["user_email"] = BUYER_EMAIL
    attrs["renews_at"] = now_iso(60 * 24 * 30)
    return event_body(
        event_name,
        custom_data=custom_data,
        sub_id=invoice_id,
        webhook_id=webhook_id,
        updated_at=now_iso(),
        extra_attrs=attrs,
    )


def refused_alerts():
    return [m for m in mail.outbox if m.subject == REFUSED_SUBJECT]


def assert_one_refusal_alert(*, victim_pk, webhook_id):
    alerts = refused_alerts()
    assert len(alerts) == 1, [m.subject for m in mail.outbox]
    body = alerts[0].body
    assert f"Claimed user pk: {victim_pk}" in body
    assert f"LS webhook id: {webhook_id}" in body
    # ids only: never the buyer's email, never a signature
    assert "@" not in body.replace(OPS, "")
    assert BUYER_EMAIL not in body
    assert "user_sig" not in body
    return body


# --- 1. forged unsigned subscription_created -----------------------------------------------


@override_settings(**ALERTS)
@pytest.mark.parametrize("victim_sub", [None, "sub_victim_live"])
def test_1_forged_unsigned_created_for_new_sub_leaves_victim_untouched(victim_sub):
    victim = subscribed("cb1_victim", victim_sub) if victim_sub else make_user("cb1_victim")
    before = row(victim)
    mail.outbox.clear()

    response = post_event(
        Client(), created_body(unsigned(victim.pk), sub_id="sub_attacker", webhook_id="wh_cb1")
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert row(victim) == before
    event = BillingEvent.objects.get(event_key="wh_cb1")
    assert event.event_name == "subscription_created"
    assert event.user_id is None  # never attributed to the claimed victim
    assert not BillingPayment.objects.exists()
    body = assert_one_refusal_alert(victim_pk=victim.pk, webhook_id="wh_cb1")
    assert "LS subscription id (payload): sub_attacker" in body
    assert f"LS subscription id (ours): {victim_sub or '-'}" in body
    # LS redelivery of the same event is a duplicate, still 200, still no write.
    again = post_event(
        Client(), created_body(unsigned(victim.pk), sub_id="sub_attacker", webhook_id="wh_cb1")
    )
    assert again.json() == {"duplicate": True}
    assert row(victim) == before


@override_settings(**ALERTS)
@pytest.mark.parametrize(
    "custom_data",
    [
        {"user_pk": "VICTIM", "user_sig": "garbage"},
        {"user_pk": "VICTIM", "user_sig": ""},
        {"user_pk": "VICTIM", "user_sig": {"nested": "dict"}},
        {"user_pk": "VICTIM", "user_sig": None},
        {"user_pk": "VICTIM", "user_sig": "VICTIM:tampered"},
    ],
)
def test_1b_invalid_or_malformed_signature_is_treated_as_unsigned(custom_data):
    victim = make_user("cb1b_victim")
    custom_data = {
        k: (v.replace("VICTIM", str(victim.pk)) if isinstance(v, str) else v) for k, v in custom_data.items()
    }
    before = row(victim)
    mail.outbox.clear()

    response = post_event(Client(), created_body(custom_data, sub_id="sub_x", webhook_id="wh_cb1b"))

    assert response.status_code == 200
    assert row(victim) == before
    assert_one_refusal_alert(victim_pk=victim.pk, webhook_id="wh_cb1b")


# --- 2. signature for the ATTACKER, user_pk = VICTIM -------------------------------------


@override_settings(**ALERTS)
def test_2_attacker_signature_with_victim_user_pk_is_refused():
    attacker = make_user("cb2_attacker")
    victim = make_user("cb2_victim")
    victim_before, attacker_before = row(victim), row(attacker)
    mail.outbox.clear()

    response = post_event(
        Client(), created_body(signed(victim.pk, sig_pk=attacker.pk), sub_id="sub_cb2", webhook_id="wh_cb2")
    )

    assert response.status_code == 200
    assert row(victim) == victim_before
    assert row(attacker) == attacker_before
    assert BillingEvent.objects.get(event_key="wh_cb2").user_id is None
    assert_one_refusal_alert(victim_pk=victim.pk, webhook_id="wh_cb2")


# --- 3. legit signed new checkout ----------------------------------------------------------


@override_settings(**ALERTS)
def test_3_signed_new_checkout_binds_the_plan_as_before():
    user = make_user("cb3")
    mail.outbox.clear()
    body = created_body(signed(user.pk), sub_id="sub_cb3", webhook_id="wh_cb3", variant=SCALE)

    response = post_event(Client(), body)

    assert response.status_code == 200
    plan = AccountPlan.objects.get(user=user)
    assert (plan.plan, plan.status) == (AccountPlan.Plan.SCALE, AccountPlan.Status.ACTIVE)
    assert (plan.stripe_subscription_id, plan.stripe_price_id, plan.stripe_customer_id) == (
        "sub_cb3",
        SCALE,
        "cust_attacker",
    )
    assert BillingEvent.objects.get(event_key="wh_cb3").user_id == user.pk
    assert refused_alerts() == []

    # The first payment of that checkout (also signed) is recorded for the user.
    pay = payment_body(
        "subscription_payment_success",
        signed(user.pk),
        invoice_id="inv_cb3",
        sub_ls_id="sub_cb3",
        webhook_id="wh_cb3_pay",
    )
    assert post_event(Client(), pay).status_code == 200
    assert BillingPayment.objects.get(ls_invoice_id="inv_cb3").user_id == user.pk


@override_settings(**ALERTS)
def test_3b_signed_payment_arriving_before_created_is_recorded():
    # LS does not guarantee order: payment_success may land before
    # subscription_created. A signed payload may bind, so it is recorded.
    user = make_user("cb3b")
    pay = payment_body(
        "subscription_payment_success",
        signed(user.pk),
        invoice_id="inv_cb3b",
        sub_ls_id="sub_cb3b",
        webhook_id="wh_cb3b",
    )
    assert post_event(Client(), pay).status_code == 200
    assert BillingPayment.objects.get(ls_invoice_id="inv_cb3b").user_id == user.pk
    assert refused_alerts() == []


# --- 4. the two LIVE legacy subscriptions (unsigned, already bound) ------------------------

LIVE_SUB = "4242"  # LS ids: string in data.id, integer in attributes.subscription_id


@override_settings(**ALERTS)
def test_4_live_unsigned_subscription_updated_still_applies():
    user = subscribed("cb4_upd", LIVE_SUB)
    body = event_body(
        "subscription_updated",
        custom_data=unsigned(user.pk),
        sub_id=LIVE_SUB,
        variant_id=SCALE,
        webhook_id="wh_cb4_upd",
        updated_at=now_iso(),
        renews_at=now_iso(60 * 24 * 30),
    )
    assert post_event(Client(), body).status_code == 200
    plan = AccountPlan.objects.get(user=user)
    assert (plan.plan, plan.stripe_price_id, plan.stripe_subscription_id) == ("scale", SCALE, LIVE_SUB)
    assert BillingEvent.objects.get(event_key="wh_cb4_upd").user_id == user.pk
    assert refused_alerts() == []


@override_settings(**ALERTS)
def test_4_live_unsigned_payment_success_records_payment_and_extends_period():
    user = subscribed("cb4_pay", LIVE_SUB)
    body = payment_body(
        "subscription_payment_success",
        unsigned(user.pk),
        invoice_id="inv_cb4",
        sub_ls_id=int(LIVE_SUB),
        webhook_id="wh_cb4_pay",
    )
    renews = parse_datetime(__import__("json").loads(body)["data"]["attributes"]["renews_at"])

    assert post_event(Client(), body).status_code == 200

    payment = BillingPayment.objects.get(ls_invoice_id="inv_cb4")
    assert (payment.user_id, payment.amount_cents, payment.ls_subscription_id) == (user.pk, 1200, LIVE_SUB)
    plan = AccountPlan.objects.get(user=user)
    assert (plan.plan, plan.status, plan.current_period_end) == ("paid", "active", renews)
    assert [m for m in mail.outbox if "receipt" in m.subject]  # our customer receipt still goes out
    assert refused_alerts() == []


@override_settings(**ALERTS)
def test_4_live_unsigned_payment_failed_marks_past_due():
    user = subscribed("cb4_fail", LIVE_SUB)
    body = payment_body(
        "subscription_payment_failed",
        unsigned(user.pk),
        invoice_id="inv_cb4_fail",
        sub_ls_id=int(LIVE_SUB),
        webhook_id="wh_cb4_fail",
    )
    assert post_event(Client(), body).status_code == 200
    assert AccountPlan.objects.get(user=user).status == AccountPlan.Status.PAST_DUE
    assert refused_alerts() == []


@override_settings(**ALERTS)
def test_4_live_unsigned_payment_refunded_is_recorded_on_the_payment_row():
    user = subscribed("cb4_ref", LIVE_SUB)
    client = Client()
    pay = payment_body(
        "subscription_payment_success",
        unsigned(user.pk),
        invoice_id="inv_cb4_ref",
        sub_ls_id=int(LIVE_SUB),
        webhook_id="wh_cb4_ref_pay",
    )
    assert post_event(client, pay).status_code == 200
    plan_before = row(user)
    refund = payment_body(
        "subscription_payment_refunded",
        unsigned(user.pk),
        invoice_id="inv_cb4_ref",
        sub_ls_id=int(LIVE_SUB),
        webhook_id="wh_cb4_ref",
        refunded_amount=500,
        status="partial_refund",
    )

    assert post_event(client, refund).status_code == 200

    payment = BillingPayment.objects.get(ls_invoice_id="inv_cb4_ref")
    assert (payment.user_id, payment.refunded_amount_cents, payment.refunded) == (user.pk, 500, False)
    assert row(user) == plan_before  # a refund never touches the plan
    assert refused_alerts() == []


@override_settings(**ALERTS)
def test_4_live_unsigned_cancelled_keeps_plan_to_period_end():
    user = subscribed("cb4_can", LIVE_SUB)
    ends = timezone.now().replace(microsecond=0) + timedelta(days=7)
    body = event_body(
        "subscription_cancelled",
        custom_data=unsigned(user.pk),
        sub_id=LIVE_SUB,
        status="cancelled",
        webhook_id="wh_cb4_can",
        updated_at=now_iso(),
        ends_at=ends.isoformat(),
    )
    assert post_event(Client(), body).status_code == 200
    plan = AccountPlan.objects.get(user=user)
    assert (plan.plan, plan.status, plan.current_period_end, plan.stripe_subscription_id) == (
        "paid",
        "active",
        ends,
        LIVE_SUB,
    )
    assert refused_alerts() == []


@override_settings(**ALERTS)
def test_4_live_unsigned_expired_downgrades_to_free():
    user = subscribed("cb4_exp", LIVE_SUB)
    body = event_body(
        "subscription_expired",
        custom_data=unsigned(user.pk),
        sub_id=LIVE_SUB,
        status="expired",
        webhook_id="wh_cb4_exp",
        updated_at=now_iso(),
    )
    assert post_event(Client(), body).status_code == 200
    plan = AccountPlan.objects.get(user=user)
    assert (plan.plan, plan.status, plan.stripe_subscription_id, plan.stripe_price_id) == (
        "free",
        "active",
        None,
        None,
    )
    assert refused_alerts() == []


@override_settings(**ALERTS)
def test_4_live_unsigned_order_refunded_still_alerts_the_refund():
    # order_refunded writes no row and no plan (alert only): nothing to bind.
    user = subscribed("cb4_ord", LIVE_SUB)
    before = row(user)
    body = event_body(
        "order_refunded",
        custom_data=unsigned(user.pk),
        sub_id="ord_1",
        webhook_id="wh_cb4_ord",
        extra_attrs={"total": 1200, "refunded_amount": 1200, "refunded": True, "currency": "USD"},
    )
    assert post_event(Client(), body).status_code == 200
    assert row(user) == before
    assert any("Refund on LS order ord_1" in m.subject for m in mail.outbox)
    assert refused_alerts() == []


# --- unsigned events for an UNBOUND / different subscription: no money row --------------


@override_settings(**ALERTS)
@pytest.mark.parametrize("victim_sub", [None, "sub_victim_live"])
def test_unsigned_payment_for_unbound_sub_creates_no_payment_and_no_receipt(victim_sub):
    victim = subscribed("cbp_victim", victim_sub) if victim_sub else make_user("cbp_victim")
    before = row(victim)
    mail.outbox.clear()
    body = payment_body(
        "subscription_payment_success",
        unsigned(victim.pk),
        invoice_id="inv_forged",
        sub_ls_id=777,
        webhook_id="wh_cbp",
    )

    assert post_event(Client(), body).status_code == 200

    assert not BillingPayment.objects.exists()
    assert row(victim) == before
    assert not [m for m in mail.outbox if "receipt" in m.subject]
    body = assert_one_refusal_alert(victim_pk=victim.pk, webhook_id="wh_cbp")
    assert "LS subscription id (payload): 777" in body


@override_settings(**ALERTS)
def test_unsigned_refund_for_unbound_sub_creates_no_payment_row():
    victim = make_user("cbr_victim")
    before = row(victim)
    mail.outbox.clear()
    body = payment_body(
        "subscription_payment_refunded",
        unsigned(victim.pk),
        invoice_id="inv_forged_ref",
        sub_ls_id=778,
        webhook_id="wh_cbr",
        refunded_amount=1200,
        refunded=True,
        status="refunded",
    )

    assert post_event(Client(), body).status_code == 200

    assert not BillingPayment.objects.exists()
    assert row(victim) == before
    assert_one_refusal_alert(victim_pk=victim.pk, webhook_id="wh_cbr")


@override_settings(**ALERTS)
@pytest.mark.parametrize(
    ("event_name", "status"),
    [
        ("subscription_updated", "active"),
        ("subscription_cancelled", "cancelled"),
        ("subscription_expired", "expired"),
        ("subscription_paused", "paused"),
    ],
)
def test_unsigned_lifecycle_event_for_a_different_sub_never_touches_the_victim(event_name, status):
    victim = subscribed("cbl_victim", "sub_victim_live")
    before = row(victim)
    mail.outbox.clear()
    body = event_body(
        event_name,
        custom_data=unsigned(victim.pk),
        sub_id="sub_attacker",
        status=status,
        webhook_id="wh_cbl",
        updated_at=now_iso(60),  # "newer": today's mismatch guard would let it through
        renews_at=now_iso(60 * 24 * 30),
    )

    assert post_event(Client(), body).status_code == 200

    assert row(victim) == before
    assert_one_refusal_alert(victim_pk=victim.pk, webhook_id="wh_cbl")


@override_settings(**ALERTS)
def test_unsigned_created_is_refused_even_for_the_already_bound_sub():
    # subscription_created always binds, so it always needs a valid signature.
    user = subscribed("cbc_same", LIVE_SUB)
    before = row(user)
    mail.outbox.clear()
    assert (
        post_event(
            Client(), created_body(unsigned(user.pk), sub_id=LIVE_SUB, webhook_id="wh_cbc")
        ).status_code
        == 200
    )
    assert row(user) == before
    assert_one_refusal_alert(victim_pk=user.pk, webhook_id="wh_cbc")


# --- 5. signed payload, the plan already holds a DIFFERENT subscription ------------------


@override_settings(**ALERTS)
def test_5_signed_created_for_a_different_sub_rebinds_like_today():
    # Decision: a valid signature proves the buyer IS this account (its own
    # checkout), so subscription_created keeps today's rule: it binds the new
    # subscription (the old one is then flagged by the daily reconcile).
    user = subscribed("cb5_created", "sub_old")
    body = created_body(signed(user.pk), sub_id="sub_new", webhook_id="wh_cb5c", variant=SCALE)
    assert post_event(Client(), body).status_code == 200
    plan = AccountPlan.objects.get(user=user)
    assert (plan.plan, plan.stripe_subscription_id) == ("scale", "sub_new")
    assert refused_alerts() == []


@override_settings(**ALERTS)
def test_5_signed_older_update_for_a_different_sub_is_skipped_by_the_mismatch_guard():
    user = subscribed("cb5_upd", "sub_old")
    before = row(user)
    body = event_body(
        "subscription_updated",
        custom_data=signed(user.pk),
        sub_id="sub_other",
        variant_id=SCALE,
        webhook_id="wh_cb5u",
        updated_at=(timezone.now() - timedelta(days=3)).isoformat(),  # older than billing_synced_at
    )
    assert post_event(Client(), body).status_code == 200
    assert row(user) == before
    assert refused_alerts() == []


# --- 6. checkout_create sends user_sig and it verifies ------------------------------------


@override_settings(ENABLE_BILLING=True)
def test_6_checkout_create_sends_user_pk_and_a_verifying_user_sig():
    from apps.billing import checkout_token

    user = make_user("cb6")
    client = Client()
    client.force_login(user)
    captured = {}

    def fake_post(path, body, **kwargs):
        captured["body"] = body
        return {"data": {"attributes": {"url": "https://checkout.example/cb6"}}}

    with patch("apps.billing.views._ls_post", side_effect=fake_post):
        response = client.post(reverse("dashboard:billing-checkout"), {"plan": "paid"})

    assert response.status_code == 303
    checkout_data = captured["body"]["data"]["attributes"]["checkout_data"]
    assert set(checkout_data) == {"email", "custom"}
    assert checkout_data["email"] == user.email
    custom = checkout_data["custom"]
    assert set(custom) == {"user_pk", "user_sig"}
    assert custom["user_pk"] == str(user.pk)
    assert custom["user_sig"] == sig_for(user.pk)
    assert checkout_token.verify(custom) == user.pk
    assert checkout_token.sign_user_pk(user.pk) == custom["user_sig"]


def test_6b_verify_contract():
    from apps.billing import checkout_token

    assert checkout_token.verify({"user_pk": "12", "user_sig": sig_for(12)}) == 12
    assert checkout_token.verify({"user_pk": 12, "user_sig": sig_for(12)}) == 12  # LS may echo an int
    assert checkout_token.verify({"user_pk": "12", "user_sig": sig_for(13)}) is None
    assert checkout_token.verify({"user_pk": "012", "user_sig": sig_for(12)}) is None
    assert checkout_token.verify({"user_sig": sig_for(12)}) is None
    assert checkout_token.verify({"user_pk": "12"}) is None
    assert checkout_token.verify({"user_pk": "12", "user_sig": 12}) is None
    assert checkout_token.verify({"user_pk": "12", "user_sig": "x" * 5000}) is None
    assert checkout_token.verify(None) is None
    assert checkout_token.verify([]) is None
    other_salt = signing.Signer(salt="mailsocket.other").sign("12")
    assert checkout_token.verify({"user_pk": "12", "user_sig": other_salt}) is None
    signed_with_current_key = sig_for(12)
    with override_settings(SECRET_KEY="another-secret-key-for-this-test-only-0123456789"):
        assert checkout_token.verify({"user_pk": "12", "user_sig": signed_with_current_key}) is None


# --- billing_backfill_payments maps by the STORED subscription id -------------------------


@override_settings(**ALERTS)
def test_backfill_never_creates_a_row_for_a_refused_forged_binding():
    victim = make_user("cbb_victim")
    assert (
        post_event(
            Client(), created_body(unsigned(victim.pk), sub_id="5150", webhook_id="wh_cbb")
        ).status_code
        == 200
    )
    assert AccountPlan.objects.get(user=victim).stripe_subscription_id is None
    out, err = io.StringIO(), io.StringIO()
    edge = Edge([[]], invoice_pages=[[ls_inv(5151, sub="5150", created=timezone.now() - timedelta(days=1))]])

    with (
        override_settings(**ON),
        patch("urllib.request.urlopen", side_effect=edge),
        patch("apps.billing.reconcile.time.sleep"),
    ):
        call_command("billing_backfill_payments", "--apply", stdout=out, stderr=err)

    assert err.getvalue().splitlines() == [
        "WARNING skipped invoice=5151 sub=5150: no AccountPlan holds this subscription"
    ]
    assert out.getvalue().splitlines()[-1] == "created=0"
    assert not BillingPayment.objects.exists()
