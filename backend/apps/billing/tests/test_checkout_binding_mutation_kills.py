"Public API."

import json
import logging

import pytest
from django.core import mail, signing
from django.test import Client, override_settings
from django.utils import timezone

from apps.billing.models import BillingPayment
from apps.billing.tests.test_billing_ls import make_user, post_event
from apps.billing.tests.test_checkout_binding import (
    ALERTS,
    PAID,
    SALT,
    assert_one_refusal_alert,
    created_body,
    now_iso,
    payment_body,
    refused_alerts,
    row,
    subscribed,
    unsigned,
)

pytestmark = pytest.mark.django_db(transaction=True)


# --- mutation kills (mutmut 3.8 on checkout_token.py, 2026-10-14) -------------------------


@override_settings(**ALERTS)
def test_unsigned_refund_proven_by_an_own_payment_row_is_recorded():
    # The account does not (or no longer) hold the subscription, but OUR row for
    # that invoice belongs to it: nothing new is bound, the refund is recorded.
    user = make_user("cbk_ownrow")
    BillingPayment.objects.create(
        user=user,
        ls_invoice_id="inv_own",
        ls_subscription_id="sub_gone",
        amount_cents=1200,
        currency="USD",
        paid_at=timezone.now(),
    )
    refund = payment_body(
        "subscription_payment_refunded",
        unsigned(user.pk),
        invoice_id="inv_own",
        sub_ls_id="sub_gone",
        webhook_id="wh_cbk_own",
        refunded_amount=300,
        status="partial_refund",
    )
    assert post_event(Client(), refund).status_code == 200
    assert BillingPayment.objects.get(ls_invoice_id="inv_own").refunded_amount_cents == 300
    assert refused_alerts() == []


@override_settings(**ALERTS)
def test_unsigned_refund_for_another_accounts_row_is_refused_and_row_untouched():
    owner = make_user("cbk_rowowner")
    victim = make_user("cbk_rowvictim")
    BillingPayment.objects.create(
        user=owner,
        ls_invoice_id="inv_other",
        ls_subscription_id="sub_o",
        amount_cents=1200,
        currency="USD",
        paid_at=timezone.now(),
    )
    BillingPayment.objects.create(  # a row of the victim for ANOTHER invoice is no proof either
        user=victim,
        ls_invoice_id="inv_victim_own",
        ls_subscription_id="sub_v",
        amount_cents=900,
        currency="USD",
        paid_at=timezone.now(),
    )
    mail.outbox.clear()
    refund = payment_body(
        "subscription_payment_refunded",
        unsigned(victim.pk),
        invoice_id="inv_other",
        sub_ls_id="sub_o",
        webhook_id="wh_cbk_other",
        refunded_amount=1200,
        refunded=True,
        status="refunded",
    )
    assert post_event(Client(), refund).status_code == 200
    assert BillingPayment.objects.get(ls_invoice_id="inv_other").refunded_amount_cents == 0
    assert BillingPayment.objects.get(ls_invoice_id="inv_victim_own").refunded_amount_cents == 0
    body = assert_one_refusal_alert(victim_pk=victim.pk, webhook_id="wh_cbk_other")
    assert "LS invoice id: inv_other" in body


@override_settings(**ALERTS)
def test_an_own_payment_row_proves_nothing_for_a_non_refund_event():
    # Only a REFUND may lean on an existing row; a payment_success for a new
    # subscription still binds and needs a signature.
    user = make_user("cbk_rowpay")
    BillingPayment.objects.create(
        user=user,
        ls_invoice_id="inv_rowpay",
        ls_subscription_id="sub_x",
        amount_cents=1200,
        currency="USD",
        paid_at=timezone.now(),
    )
    before = row(user)
    mail.outbox.clear()
    pay = payment_body(
        "subscription_payment_failed",
        unsigned(user.pk),
        invoice_id="inv_rowpay",
        sub_ls_id="sub_x",
        webhook_id="wh_cbk_rowpay",
    )
    assert post_event(Client(), pay).status_code == 200
    assert row(user) == before
    assert_one_refusal_alert(victim_pk=user.pk, webhook_id="wh_cbk_rowpay")


@override_settings(**ALERTS)
@pytest.mark.parametrize("event_name", ["subscription_updated", "subscription_payment_failed"])
def test_unsigned_event_without_any_subscription_id_never_matches_an_unbound_plan(event_name):
    # Payload without a subscription id + plan without one: '' must never equal ''.
    user = make_user("cbk_nosub")
    before = row(user)
    mail.outbox.clear()
    attrs = {"status": "past_due", "updated_at": now_iso(), "variant_id": PAID}
    body = (
        __import__("json")
        .dumps(
            {
                "meta": {
                    "event_name": event_name,
                    "webhook_id": "wh_cbk_nosub",
                    "custom_data": unsigned(user.pk),
                },
                "data": {"attributes": attrs},
            }
        )
        .encode()
    )
    assert post_event(Client(), body).status_code == 200
    assert row(user) == before
    alert = assert_one_refusal_alert(victim_pk=user.pk, webhook_id="wh_cbk_nosub")
    assert "LS subscription id (payload): -" in alert


@override_settings(**ALERTS)
def test_refusal_alert_and_log_exact_content(caplog):
    victim = subscribed("cbk_alert", "sub_v_live")
    mail.outbox.clear()
    caplog.set_level(logging.WARNING, logger="apps.billing.checkout_token")
    pay = payment_body(
        "subscription_payment_success",
        {"user_pk": str(victim.pk), "user_sig": "nope"},
        invoice_id="inv_al",
        sub_ls_id=4711,
        webhook_id="wh_cbk_al",
    )
    assert post_event(Client(), pay).status_code == 200
    msg = refused_alerts()[0]
    assert "Event: subscription_payment_success" in msg.body
    assert "Checkout signature: invalid" in msg.body
    assert "LS invoice id: inv_al" in msg.body
    assert "LS subscription id (payload): 4711" in msg.body
    assert "LS subscription id (ours): sub_v_live" in msg.body
    assert "NOTHING was applied (no plan change, no" in msg.body
    assert "NOT got their plan" not in msg.body  # the refusal footer, not the default one
    assert [r.getMessage() for r in caplog.records if r.name == "apps.billing.checkout_token"] == [
        f"billing_webhook_unsigned_binding_refused user_pk={victim.pk} event=subscription_payment_success"
        " signature=invalid"
    ]
    mail.outbox.clear()
    created = created_body(unsigned(victim.pk), sub_id="sub_new", webhook_id="wh_cbk_al2")
    assert post_event(Client(), created).status_code == 200
    body = refused_alerts()[0].body
    assert "Checkout signature: missing" in body
    assert "LS invoice id" not in body  # subscription objects carry no invoice id
    assert "LS subscription id (payload): sub_new" in body


@override_settings(**ALERTS)
def test_malformed_payload_shapes_keep_the_old_path_without_a_refusal():
    # data / attributes of the wrong type: _apply_ls_event's own guards alert
    # "malformed ..." as before; the binding pre-filter stays out of the way.
    user = make_user("cbk_malformed")
    before = row(user)
    for webhook_id, data, reason in (
        ("wh_cbk_m1", "x", "malformed data"),
        ("wh_cbk_m2", {"id": "s", "attributes": ["x"]}, "malformed attributes"),
    ):
        mail.outbox.clear()
        meta = {
            "event_name": "subscription_created",
            "webhook_id": webhook_id,
            "custom_data": unsigned(user.pk),
        }
        body = json.dumps({"meta": meta, "data": data}).encode()
        assert post_event(Client(), body).status_code == 200
        assert [m.subject for m in mail.outbox] == [f"[mailsocket] Billing webhook problem: {reason}"]
    assert row(user) == before


def test_verify_signature_length_boundary():
    from apps.billing import checkout_token

    pk = "1" * (checkout_token.MAX_SIGNATURE_LENGTH - 44)  # signer adds ":" + 43-char b64 digest
    sig = signing.Signer(salt=SALT).sign(pk)
    assert len(sig) == checkout_token.MAX_SIGNATURE_LENGTH
    assert checkout_token.verify({"user_pk": pk, "user_sig": sig}) == int(pk)
    longer = signing.Signer(salt=SALT).sign(pk + "1")
    assert checkout_token.verify({"user_pk": pk + "1", "user_sig": longer}) is None


def test_payload_ids_defaults_are_empty_strings():
    from apps.billing.checkout_token import _payload_ids

    assert _payload_ids("subscription_updated", {}) == ("", "")
    assert _payload_ids("subscription_payment_success", {"data": {"id": "inv"}}) == ("", "inv")
    assert _payload_ids("subscription_payment_success", {"data": "x"}) == ("", "")
    assert _payload_ids("subscription_payment_success", {"data": {"id": 5, "attributes": "x"}}) == ("", "5")
    assert _payload_ids("subscription_payment_success", {"data": {"attributes": {"subscription_id": 7}}}) == (
        "7",
        "",
    )
