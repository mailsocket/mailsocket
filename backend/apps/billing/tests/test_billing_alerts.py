"Public API."
import json
import smtplib
from unittest.mock import patch

import pytest
from django.core import mail
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.models import AccountPlan
from apps.billing.models import BillingEvent
from apps.billing.tests.test_billing_ls import SECRET, event_body, make_user, post_event

pytestmark = pytest.mark.django_db(transaction=True)

OPS = "ops@example.test"
ALERT_SETTINGS = dict(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET, OPS_ALERT_EMAIL=OPS)


def _only_alert():
    assert len(mail.outbox) == 1, [m.subject for m in mail.outbox]
    msg = mail.outbox[0]
    assert msg.to == [OPS]
    return msg


def _admin_link(user):
    return f"https://dash.mailsocket.app/admin/accounts/accountplan/{user.pk}/change/"


def _subscribe(client, user, *, webhook_id="wh_seed", sub_id="sub_1"):
    body = event_body(
        "subscription_created", user_pk=user.pk, sub_id=sub_id, webhook_id=webhook_id,
        status="active", updated_at=timezone.now().isoformat(),
    )
    assert post_event(client, body).status_code == 200


# --- applied events -----------------------------------------------------------


@override_settings(**ALERT_SETTINGS)
def test_subscription_created_sends_new_subscription_alert():
    user = make_user("al_new", email="buyer@example.test")
    client = Client()
    body = event_body(
        "subscription_created", user_pk=user.pk, sub_id="sub_new", status="active",
        updated_at=timezone.now().isoformat(), extra_attrs={"card_last_four": "4242"},
    )
    resp = post_event(client, body)
    assert resp.status_code == 200
    assert AccountPlan.objects.get(user=user).plan == AccountPlan.Plan.PAID

    msg = _only_alert()
    assert msg.subject == "[mailsocket] New subscription: Pro (paid) — buyer@example.test"
    assert "Event: subscription_created" in msg.body
    assert "Account: buyer@example.test" in msg.body
    assert "Plan: Free (free) -> Pro (paid)" in msg.body
    assert "Status after: active" in msg.body
    assert "LS subscription id: sub_new" in msg.body
    assert _admin_link(user) in msg.body
    # No raw payload / card data / secret in the mail.
    assert "4242" not in msg.body
    assert "card_last_four" not in msg.body
    assert SECRET not in msg.body
    assert "cust_1" not in msg.body


@pytest.mark.parametrize(
    "event_name, extra, plan_after, status_after",
    [
        ("subscription_payment_success", {}, "Pro (paid)", "active"),
        ("subscription_payment_failed", {}, "Pro (paid)", "past_due"),
        ("subscription_cancelled", {"status": "cancelled"}, "Pro (paid)", "active"),
        ("subscription_expired", {"status": "expired"}, "Free (free)", "active"),
    ],
)
def test_lifecycle_event_alert(event_name, extra, plan_after, status_after):
    with override_settings(**ALERT_SETTINGS):
        user = make_user(f"al_{event_name}"[:30], email=f"{event_name}@example.test")
        client = Client()
        _subscribe(client, user)
        mail.outbox.clear()

        body = event_body(
            event_name, user_pk=user.pk, webhook_id=f"wh_{event_name}",
            updated_at=timezone.now().isoformat(), **extra,
        )
        resp = post_event(client, body)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        msg = _only_alert()
        assert msg.subject == (
            f"[mailsocket] Billing event {event_name}: {plan_after} — {event_name}@example.test"
        )
        assert f"Event: {event_name}" in msg.body
        assert f"Plan: Pro (paid) -> {plan_after}" in msg.body
        assert f"Status after: {status_after}" in msg.body
        assert "LS subscription id: sub_1" in msg.body
        assert _admin_link(user) in msg.body


@override_settings(**ALERT_SETTINGS)
def test_payment_event_uses_attributes_subscription_id():
    user = make_user("al_inv")
    client = Client()
    _subscribe(client, user, sub_id="sub_77")
    mail.outbox.clear()
    body = event_body(
        "subscription_payment_success", user_pk=user.pk, sub_id="inv_1", webhook_id="wh_inv",
        updated_at=timezone.now().isoformat(), extra_attrs={"subscription_id": 77},
    )
    assert post_event(client, body).status_code == 200
    assert "LS subscription id: 77" in _only_alert().body


@override_settings(**ALERT_SETTINGS, ENABLE_MULTI_ACCOUNT=True)
def test_workspace_account_alert_names_human_owner():
    from apps.accounts.multi_account_services import create_owned_account

    owner = make_user("al_owner", email="human@example.test", plan=AccountPlan.Plan.PAID)
    ws_user = create_owned_account(owner, "Side project").account_user
    client = Client()
    body = event_body(
        "subscription_created", user_pk=ws_user.pk, sub_id="sub_ws", status="active",
        updated_at=timezone.now().isoformat(),
    )
    assert post_event(client, body).status_code == 200
    assert "(workspace of human@example.test)" in _only_alert().subject


# --- no alert -----------------------------------------------------------------


@override_settings(**ALERT_SETTINGS)
def test_duplicate_delivery_no_second_alert():
    user = make_user("al_dup")
    client = Client()
    body = event_body(
        "subscription_created", user_pk=user.pk, status="active", webhook_id="wh_dup",
        updated_at=timezone.now().isoformat(),
    )
    assert post_event(client, body).status_code == 200
    assert len(mail.outbox) == 1  # first delivery alerts (load-bearing control)
    resp = post_event(client, body)
    assert resp.json() == {"duplicate": True}
    assert len(mail.outbox) == 1


@override_settings(**ALERT_SETTINGS)
def test_bad_signature_no_alert_even_for_problem_payload():
    user = make_user("al_sig")
    client = Client()
    good = event_body("subscription_created", user_pk=user.pk, status="active", webhook_id="wh_s1")
    assert post_event(client, good, sig="0" * 64).status_code == 401
    garbage = event_body("subscription_created", custom_data={}, webhook_id="wh_s2")
    assert post_event(client, garbage, sig="0" * 64).status_code == 401
    assert mail.outbox == []
    assert BillingEvent.objects.count() == 0


@override_settings(**ALERT_SETTINGS)
def test_400_paths_no_alert():
    client = Client()
    for body in (b"{not json", json.dumps({"no_meta": 1}).encode()):
        assert post_event(client, body).status_code == 400
    assert mail.outbox == []


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET, OPS_ALERT_EMAIL="")
def test_ops_alert_email_empty_skips_silently():
    user = make_user("al_empty")
    client = Client()
    body = event_body(
        "subscription_created", user_pk=user.pk, status="active",
        updated_at=timezone.now().isoformat(),
    )
    assert post_event(client, body).status_code == 200
    assert AccountPlan.objects.get(user=user).plan == AccountPlan.Plan.PAID
    missing = event_body("subscription_created", custom_data={}, webhook_id="wh_empty2")
    assert post_event(client, missing).status_code == 200
    assert mail.outbox == []


@override_settings(**ALERT_SETTINGS)
def test_rolled_back_webhook_sends_no_alert():
    user = make_user("al_rb")
    client = Client()
    body = event_body(
        "subscription_created", user_pk=user.pk, status="active", webhook_id="wh_rb",
        updated_at=timezone.now().isoformat(),
    )
    # Crash AFTER the plan.save + alert queue, still inside the atomic block.
    with patch("apps.billing.views.queue_event_alert", side_effect=_queue_then_raise):
        with pytest.raises(RuntimeError):
            post_event(client, body)
    assert mail.outbox == []
    assert AccountPlan.objects.get(user=user).plan == AccountPlan.Plan.FREE


def _queue_then_raise(*args, **kwargs):
    from apps.billing.alerts import queue_event_alert

    queue_event_alert(*args, **kwargs)
    raise RuntimeError("boom after queue")


@override_settings(**ALERT_SETTINGS)
def test_unalerted_events_send_nothing():
    user = make_user("al_upd")
    client = Client()
    _subscribe(client, user)
    mail.outbox.clear()
    body = event_body(
        "subscription_updated", user_pk=user.pk, status="active", variant_id="2176478",
        webhook_id="wh_upd", updated_at=timezone.now().isoformat(),
    )
    assert post_event(client, body).status_code == 200
    assert AccountPlan.objects.get(user=user).plan == AccountPlan.Plan.SCALE
    assert mail.outbox == []


# --- mail failure never affects the webhook -----------------------------------


@override_settings(**ALERT_SETTINGS)
def test_smtp_failure_webhook_still_200_and_plan_mutated():
    user = make_user("al_smtp")
    client = Client()
    body = event_body(
        "subscription_created", user_pk=user.pk, status="active", sub_id="sub_smtp",
        updated_at=timezone.now().isoformat(),
    )
    with patch(
        "apps.ops.notify.send_mail", side_effect=smtplib.SMTPException("relay down")
    ) as sender:
        resp = post_event(client, body)
    assert sender.call_count == 1  # the send really was attempted
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    plan = AccountPlan.objects.get(user=user)
    assert plan.plan == AccountPlan.Plan.PAID
    assert plan.stripe_subscription_id == "sub_smtp"
    assert BillingEvent.objects.filter(event_key="wh_1").count() == 1


@override_settings(**ALERT_SETTINGS)
def test_smtp_failure_on_problem_path_still_200():
    client = Client()
    body = event_body("subscription_created", custom_data={}, webhook_id="wh_smtp_p")
    with patch("apps.ops.notify.send_mail", side_effect=OSError("conn refused")) as sender:
        resp = post_event(client, body)
    assert sender.call_count == 1
    assert resp.status_code == 200
    assert BillingEvent.objects.filter(event_key="wh_smtp_p").count() == 1


# --- problem paths ------------------------------------------------------------


def _problem(reason):
    msg = _only_alert()
    assert msg.subject == f"[mailsocket] Billing webhook problem: {reason}"
    assert "NOT got their plan" in msg.body
    return msg


@override_settings(**ALERT_SETTINGS)
def test_problem_missing_user_pk():
    client = Client()
    body = event_body(
        "subscription_created", custom_data={}, webhook_id="wh_p1", sub_id="sub_p1",
        extra_attrs={"user_email": "payer@example.test"},
    )
    assert post_event(client, body).status_code == 200
    msg = _problem("missing user_pk")
    assert "Event: subscription_created" in msg.body
    assert "LS webhook id: wh_p1" in msg.body
    assert "LS subscription id: sub_p1" in msg.body
    assert "LS customer email: payer@example.test" in msg.body


@override_settings(**ALERT_SETTINGS)
def test_problem_no_plan_for_user():
    user = make_user("al_noplan")
    AccountPlan.objects.filter(user=user).delete()
    client = Client()
    body = event_body("subscription_created", user_pk=user.pk, webhook_id="wh_p2")
    assert post_event(client, body).status_code == 200
    msg = _problem("no plan for user")
    assert f"User pk: {user.pk}" in msg.body


@override_settings(**ALERT_SETTINGS)
def test_problem_unknown_variant():
    # BL-17: billing-lifecycle scenario (skill billing-lifecycle)
    user = make_user("al_var")
    client = Client()
    body = event_body(
        "subscription_created", user_pk=user.pk, variant_id="9999999", webhook_id="wh_p3",
        updated_at=timezone.now().isoformat(),
    )
    assert post_event(client, body).status_code == 200
    assert AccountPlan.objects.get(user=user).plan == AccountPlan.Plan.FREE
    msg = _problem("unknown variant")
    assert "Variant id: 9999999" in msg.body
    assert f"User pk: {user.pk}" in msg.body


@override_settings(**ALERT_SETTINGS)
def test_problem_malformed_data_and_attributes():
    user = make_user("al_mal")
    client = Client()
    base_meta = {"event_name": "subscription_created", "custom_data": {"user_pk": str(user.pk)}}

    body_data = json.dumps({"meta": {**base_meta, "webhook_id": "wh_p4"}, "data": "x"}).encode()
    assert post_event(client, body_data).status_code == 200
    _problem("malformed data")
    mail.outbox.clear()

    body_attrs = json.dumps(
        {"meta": {**base_meta, "webhook_id": "wh_p5"}, "data": {"id": "s", "attributes": ["x"]}}
    ).encode()
    assert post_event(client, body_attrs).status_code == 200
    _problem("malformed attributes")


@override_settings(**ALERT_SETTINGS)
def test_problem_alert_is_deferred_to_commit():
    """The alert must not be sent from inside the webhook transaction."""
    from django.db import connection

    sent_in_atomic = []
    real_send = __import__("apps.ops.notify", fromlist=["send_mail"]).send_mail

    def spy(*args, **kwargs):
        sent_in_atomic.append(connection.in_atomic_block)
        return real_send(*args, **kwargs)

    client = Client()
    with patch("apps.ops.notify.send_mail", side_effect=spy):
        body = event_body("subscription_created", custom_data={}, webhook_id="wh_p6")
        assert post_event(client, body).status_code == 200
    assert sent_in_atomic == [False]
    _problem("missing user_pk")


# --- tg-notify: Telegram channel (fake bot, no network) -------------------------

from apps.ops.testing import FAKE_TOKEN, fake_telegram  # noqa: E402


@override_settings(**ALERT_SETTINGS)
def test_event_alert_also_goes_to_telegram_without_customer_email():
    user = make_user("al_tg", email="tg-buyer@example.test")
    client = Client()
    body = event_body(
        "subscription_created", user_pk=user.pk, sub_id="sub_tg", status="active",
        updated_at=timezone.now().isoformat(),
    )
    with fake_telegram() as tg:
        assert post_event(client, body).status_code == 200
    # Email unchanged: still names the account.
    msg = _only_alert()
    assert "tg-buyer@example.test" in msg.subject and "Account: tg-buyer@example.test" in msg.body
    # Telegram: money level, plan change + admin link, NO email / ids / token.
    assert len(tg.texts) == 1
    text = tg.texts[0]
    assert text.startswith("💰 New subscription: Pro (paid)")
    assert "Plan: Free (free) -> Pro (paid)" in text
    assert _admin_link(user) in text
    assert "tg-buyer" not in text and "@" not in text
    assert "sub_tg" not in text and SECRET not in text and FAKE_TOKEN not in text


@override_settings(**ALERT_SETTINGS)
def test_problem_alert_telegram_drops_customer_email_line():
    client = Client()
    body = event_body(
        "subscription_created", custom_data={}, webhook_id="wh_tg_p",
        extra_attrs={"user_email": "lost-payer@example.test"},
    )
    with fake_telegram() as tg:
        assert post_event(client, body).status_code == 200
    msg = _problem("missing user_pk")
    assert "lost-payer@example.test" in msg.body  
    assert len(tg.texts) == 1
    text = tg.texts[0]
    assert text.startswith("🟠 Billing webhook problem: missing user_pk")
    assert "lost-payer" not in text and "customer email" not in text.lower()
    assert "LS webhook id: wh_tg_p" in text


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET, OPS_ALERT_EMAIL="")
def test_telegram_only_config_still_alerts():
    user = make_user("al_tg_only")
    client = Client()
    body = event_body(
        "subscription_created", user_pk=user.pk, sub_id="sub_tgo", status="active",
        updated_at=timezone.now().isoformat(),
    )
    with fake_telegram() as tg:
        assert post_event(client, body).status_code == 200
    assert mail.outbox == []
    assert len(tg.calls) == 1


@override_settings(**ALERT_SETTINGS)
def test_telegram_failure_webhook_still_200_and_email_sent():
    user = make_user("al_tg_fail")
    client = Client()
    body = event_body(
        "subscription_created", user_pk=user.pk, sub_id="sub_tgf", status="active",
        updated_at=timezone.now().isoformat(),
    )
    with fake_telegram(fail=OSError("tg down")) as tg:
        resp = post_event(client, body)
    assert len(tg.calls) == 1
    assert resp.status_code == 200
    assert AccountPlan.objects.get(user=user).plan == AccountPlan.Plan.PAID
    _only_alert()
