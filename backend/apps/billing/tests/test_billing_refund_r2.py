"Public API."
import json
import threading
import time
from datetime import timedelta

import pytest
from django.core import mail
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.models import AccountPlan
from apps.billing import views as billing_views
from apps.billing.models import BillingPayment
from apps.billing.tests.test_billing_ls import event_body, post_event
from apps.billing.tests.test_billing_refund import (
    ALERT_SETTINGS,
    BILLING_URL,
    SCALE_VARIANT,
    invoice_attrs,
    only_refund_alert,
    paid_user,
    payment,
    refund_alerts,
    refund_body,
    seed_payment,
    snapshot,
)

pytestmark = pytest.mark.django_db(transaction=True)

UNMATCHED_LINE = (
    "A refund was received but could not be matched to an account — check Lemon Squeezy; no plan was changed"
)


def _money_alerts():
    """Recorded-refund (money) alerts only; problem alerts also say "refund"."""
    return [m for m in refund_alerts() if "Billing webhook problem" not in m.subject]


def _problems():
    return [m for m in mail.outbox if m.subject.startswith("[mailsocket] Billing webhook problem")]


# --- 1. concurrency ----------------------------------------------------------------


def _race(monkeypatch, bodies, *, slow_amounts=()):
    """POST ``bodies`` concurrently, one thread (= one DB connection) each.

    A barrier sits INSIDE the refund recorder (wrapping
    ``_apply_refund_to_row``, i.e. AFTER the row has been read, BEFORE the
    max() write). If nothing serializes the two webhooks, both threads read
    the same stale row and meet at the barrier -> lost update / duplicate
    alert. With the locks in place the second thread is blocked on a DB
    lock, so the first one's barrier wait times out (BrokenBarrierError,
    swallowed) and the deliveries run one after the other.
    ``slow_amounts``: payload amounts whose thread sleeps after the barrier,
    so the OTHER thread commits first (deterministic lost-update order)."""
    barrier = threading.Barrier(len(bodies), timeout=2.0)
    original = billing_views._apply_refund_to_row
    seen = {"pids": set(), "met": 0}
    lock = threading.Lock()

    def racing(payment_row, refunded_cents, payload_fully, refunded_at):
        with connection.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            pid = cur.fetchone()[0]
        with lock:
            seen["pids"].add(pid)
        try:
            barrier.wait()
            with lock:
                seen["met"] += 1
        except threading.BrokenBarrierError:
            pass
        if refunded_cents in slow_amounts:
            time.sleep(0.5)
        return original(payment_row, refunded_cents, payload_fully, refunded_at)

    monkeypatch.setattr(billing_views, "_apply_refund_to_row", racing)
    statuses, errors = [], []

    def worker(body):
        try:
            statuses.append(post_event(Client(), body).status_code)
        except Exception as exc:  # surfaced below
            errors.append(repr(exc))
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(b,)) for b in bodies]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    assert statuses == [200] * len(bodies)
    return seen


@override_settings(**ALERT_SETTINGS)
def test_concurrent_same_amount_refunds_converge_one_alert(monkeypatch):
    user = paid_user("rf_conc_same")
    seed_payment(Client(), user, "inv_conc_same")
    before = snapshot(user)
    bodies = [
        refund_body(user, "inv_conc_same", webhook_id=f"wh_cs_{i}", refunded_amount=900) for i in (1, 2)
    ]

    seen = _race(monkeypatch, bodies)

    assert len(seen["pids"]) == 2  # really two DB connections
    p = payment("inv_conc_same")
    assert (p.refunded_amount_cents, p.refunded) == (900, False)
    msg = only_refund_alert()  # NOT two alerts for the same $9.00
    assert "Partial refund: $9.00 of $12.00" in msg.subject
    assert snapshot(user) == before


@override_settings(**ALERT_SETTINGS)
def test_concurrent_different_amount_refunds_keep_max_one_alert_per_amount(monkeypatch):
    user = paid_user("rf_conc_diff")
    seed_payment(Client(), user, "inv_conc_diff")
    before = snapshot(user)
    bodies = [
        refund_body(user, "inv_conc_diff", webhook_id="wh_cd_900", refunded_amount=900),
        refund_body(user, "inv_conc_diff", webhook_id="wh_cd_500", refunded_amount=500),
    ]

    # The 500 thread writes LAST: without serialization it would overwrite
    # the committed 900 with 500 (lost update).
    seen = _race(monkeypatch, bodies, slow_amounts=(500,))

    assert len(seen["pids"]) == 2
    assert payment("inv_conc_diff").refunded_amount_cents == 900  # max, never 500, never 1400
    subjects = [m.subject for m in refund_alerts()]
    amounts = [s.split(":")[1].split(" of ")[0].strip() for s in subjects]
    assert len(amounts) == len(set(amounts)), subjects  # one alert per amount
    assert "$9.00" in amounts, subjects
    assert set(amounts) <= {"$5.00", "$9.00"}, subjects
    assert snapshot(user) == before


@pytest.mark.parametrize("unmatched_amount,matched_amount", [(900, 500), (500, 900)])
@override_settings(**ALERT_SETTINGS)
def test_concurrent_unmatched_and_matched_refund_same_invoice_keep_max(
    monkeypatch, unmatched_amount, matched_amount
):
    """The unmatched path (no user_pk -> no AccountPlan lock) and the normal
    path both write the same invoice row: only the BillingPayment row locks
    serialize them. The LOWER amount is written last (slow), so a missing
    row lock on either side would overwrite the max with the lower amount.
    Both orders run, so the row lock in _record_refund AND the one in
    _record_unmatched_refund are each pinned by one case."""
    user = paid_user(f"rf_conc_um_{unmatched_amount}")
    invoice = f"inv_conc_um_{unmatched_amount}"
    seed_payment(Client(), user, invoice)
    before = snapshot(user)
    unmatched = json.loads(refund_body(user, invoice, webhook_id=f"wh_cu_u{unmatched_amount}",
                                       refunded_amount=unmatched_amount))
    del unmatched["meta"]["custom_data"]
    bodies = [
        json.dumps(unmatched).encode(),
        refund_body(user, invoice, webhook_id=f"wh_cu_m{matched_amount}", refunded_amount=matched_amount),
    ]

    seen = _race(monkeypatch, bodies, slow_amounts=(500,))

    assert len(seen["pids"]) == 2
    assert payment(invoice).refunded_amount_cents == 900
    assert snapshot(user) == before


# --- 2. parse_datetime never 500s ------------------------------------------------------


@override_settings(**ALERT_SETTINGS)
def test_refund_with_invalid_refunded_at_falls_back_to_now():
    user = paid_user("rf_baddate")
    client = Client()
    seed_payment(client, user, "inv_bd")
    body = json.loads(refund_body(user, "inv_bd", webhook_id="wh_bd", refunded_amount=1200))
    body["data"]["attributes"]["refunded_at"] = "2026-13-45T10:00:00Z"  # well-formed, invalid
    body["data"]["attributes"]["updated_at"] = "2026-02-30T10:00:00Z"
    t0 = timezone.now()

    assert post_event(client, json.dumps(body).encode()).status_code == 200

    p = payment("inv_bd")
    assert (p.refunded, p.refunded_amount_cents) == (True, 1200)
    assert t0 - timedelta(seconds=5) <= p.refunded_at <= timezone.now() + timedelta(seconds=5)
    only_refund_alert()


@override_settings(**ALERT_SETTINGS)
def test_refund_creating_row_with_invalid_created_at_does_not_500():
    user = paid_user("rf_badpaid")
    client = Client()
    body = json.loads(refund_body(user, "inv_bp", webhook_id="wh_bp", refunded_amount=500))
    body["data"]["attributes"]["created_at"] = "2026-10-32T10:00:00Z"
    assert post_event(client, json.dumps(body).encode()).status_code == 200
    p = payment("inv_bp")
    assert p.paid_at is not None and p.refunded_amount_cents == 500


@override_settings(**ALERT_SETTINGS)
def test_invalid_updated_at_on_plan_event_is_none_no_stale_skip():
    """views.py updated_at parse: invalid -> None -> no stale-skip and no
    billing_synced_at write; the event still applies."""
    future_sync = timezone.now() + timedelta(days=1)
    user = paid_user("rf_badupd", synced_at=future_sync)
    client = Client()
    upd = event_body("subscription_updated", user_pk=user.pk, sub_id="sub_9001", webhook_id="wh_badupd",
                     variant_id=SCALE_VARIANT, status="active", updated_at="2026-13-45T10:00:00Z")

    assert post_event(client, upd).status_code == 200

    plan = AccountPlan.objects.get(user=user)
    assert plan.plan == AccountPlan.Plan.SCALE  # applied, not stale-skipped
    assert plan.billing_synced_at == future_sync  # not advanced from a bad date


@override_settings(**ALERT_SETTINGS)
def test_payment_success_with_invalid_created_at_does_not_500():
    user = paid_user("rf_badpay")
    client = Client()
    body = event_body(
        "subscription_payment_success", user_pk=user.pk, sub_id="inv_badpay", webhook_id="wh_badpay",
        updated_at=timezone.now().isoformat(),
        extra_attrs=invoice_attrs(created_at="2026-99-01T00:00:00Z"),
    )
    assert post_event(client, body).status_code == 200
    assert payment("inv_badpay").paid_at is not None


# --- 3. amounts --------------------------------------------------------------------------


@override_settings(**ALERT_SETTINGS)
def test_bool_refunded_amount_rejected_not_one_cent():
    user = paid_user("rf_bool")
    client = Client()
    seed_payment(client, user, "inv_bool")
    before = snapshot(user)
    body = json.loads(refund_body(user, "inv_bool", webhook_id="wh_bool", refunded_amount=100))
    body["data"]["attributes"]["refunded_amount"] = True
    body["data"]["attributes"]["refunded"] = False

    assert post_event(client, json.dumps(body).encode()).status_code == 200

    assert payment("inv_bool").refunded_amount_cents == 0
    assert _money_alerts() == []
    assert ["refund amount/invoice missing" in m.subject for m in _problems()] == [True]
    assert snapshot(user) == before


@override_settings(**ALERT_SETTINGS)
def test_bool_total_rejected():
    # BL-19: billing-lifecycle scenario (skill billing-lifecycle)
    user = paid_user("rf_booltot")
    client = Client()
    body = json.loads(refund_body(user, "inv_booltot", webhook_id="wh_booltot", refunded_amount=500))
    body["data"]["attributes"]["total"] = True
    body["data"]["attributes"]["total_usd"] = True
    assert post_event(client, json.dumps(body).encode()).status_code == 200
    assert not BillingPayment.objects.filter(ls_invoice_id="inv_booltot").exists()
    assert ["refund invoice total missing" in m.subject for m in _problems()] == [True]


@pytest.mark.parametrize("amount", [0, None])
@override_settings(**ALERT_SETTINGS)
def test_refunded_true_with_zero_or_missing_amount_is_full_invoice_total(amount):
    user = paid_user(f"rf_zero_{'none' if amount is None else amount}")
    client = Client()
    seed_payment(client, user, "inv_zero")
    body = json.loads(refund_body(user, "inv_zero", webhook_id="wh_zero", refunded_amount=1200))
    if amount is None:
        del body["data"]["attributes"]["refunded_amount"]
    else:
        body["data"]["attributes"]["refunded_amount"] = amount
    body["data"]["attributes"]["refunded"] = True

    assert post_event(client, json.dumps(body).encode()).status_code == 200

    p = payment("inv_zero")
    assert (p.refunded, p.refunded_amount_cents) == (True, 1200)
    assert "Full refund: $12.00 of $12.00" in only_refund_alert().subject  # never "$0.00"


def test_refund_display_says_refunded_whenever_refunded_true():
    row = BillingPayment(amount_cents=1200, currency="USD", refunded=True, refunded_amount_cents=0)
    assert row.refund_display == "Refunded"
    row.refunded_amount_cents = 1200
    assert row.refund_display == "Refunded $12.00"
    row.refunded, row.refunded_amount_cents = False, 0
    assert row.refund_display == ""
    row.refunded_amount_cents = 500
    assert row.refund_display == "Partially refunded $5.00 of $12.00"


@override_settings(**ALERT_SETTINGS)
def test_billing_page_shows_refunded_for_flag_without_amount():
    user = paid_user("rf_flag")
    client = Client()
    seed_payment(client, user, "inv_flag")
    BillingPayment.objects.filter(ls_invoice_id="inv_flag").update(refunded=True, refunded_amount_cents=0)
    html = Client()
    html.force_login(user)
    assert "Refunded" in html.get(BILLING_URL).content.decode()


# --- 4. refund with missing / deleted user -------------------------------------------------


@override_settings(**ALERT_SETTINGS)
def test_refund_missing_user_pk_recorded_on_existing_row_refund_footer():
    user = paid_user("rf_nouser")
    client = Client()
    seed_payment(client, user, "inv_nouser")
    before = snapshot(user)
    body = json.loads(refund_body(user, "inv_nouser", webhook_id="wh_nouser", refunded_amount=1200))
    del body["meta"]["custom_data"]

    assert post_event(client, json.dumps(body).encode()).status_code == 200

    p = payment("inv_nouser")
    assert (p.refunded, p.refunded_amount_cents) == (True, 1200)
    assert snapshot(user) == before
    (msg,) = _problems()
    assert "missing user_pk" in msg.subject
    assert UNMATCHED_LINE in msg.body
    assert "Recorded on the existing payment row for invoice inv_nouser: Refunded $12.00" in msg.body
    assert "may have paid and NOT got their plan" not in msg.body  # the payment footer is wrong here


@override_settings(**ALERT_SETTINGS)
def test_refund_for_deleted_user_recorded_on_orphan_row_refund_footer():
    # BL-12: billing-lifecycle scenario (skill billing-lifecycle)
    user = paid_user("rf_gone")
    client = Client()
    seed_payment(client, user, "inv_gone")
    user_pk = user.pk
    body = refund_body(user, "inv_gone", webhook_id="wh_gone", refunded_amount=400)
    # Account / AccountPlan are PROTECT FKs: an account deletion removes them
    # first, then the user; BillingPayment.user -> NULL (SET_NULL).
    from apps.accounts.models import Account

    AccountPlan.objects.filter(user=user).delete()
    Account.objects.filter(user=user).delete()
    user.delete()
    assert payment("inv_gone").user_id is None

    assert post_event(client, body).status_code == 200

    p = payment("inv_gone")
    assert (p.refunded, p.refunded_amount_cents) == (False, 400)
    (msg,) = _problems()
    assert "no plan for user" in msg.subject
    assert f"User pk: {user_pk}" in msg.body
    assert UNMATCHED_LINE in msg.body
    assert "Partially refunded $4.00 of $12.00" in msg.body
    assert "none (deleted)" in msg.body


@override_settings(**ALERT_SETTINGS)
def test_refund_missing_user_without_row_not_invented_refund_footer():
    user = paid_user("rf_norow")
    client = Client()
    body = json.loads(refund_body(user, "inv_norow", webhook_id="wh_norow", refunded_amount=500))
    body["meta"]["custom_data"] = {"user_pk": "999999999"}

    assert post_event(client, json.dumps(body).encode()).status_code == 200

    assert not BillingPayment.objects.filter(ls_invoice_id="inv_norow").exists()
    (msg,) = _problems()
    assert UNMATCHED_LINE in msg.body
    assert "NOT recorded (no payment row for this invoice)" in msg.body


@override_settings(**ALERT_SETTINGS)
def test_order_refund_missing_user_refund_footer():
    client = Client()
    body = event_body(
        "order_refunded", sub_id="ord_77", webhook_id="wh_ord_nouser", status="refunded",
        extra_attrs={"total": 1200, "refunded_amount": 1200, "refunded": True, "currency": "USD"},
    )
    assert post_event(client, body).status_code == 200
    (msg,) = _problems()
    assert UNMATCHED_LINE in msg.body
    assert "may have paid and NOT got their plan" not in msg.body


# --- 5. order_refunded subscription label ----------------------------------------------------


@override_settings(**ALERT_SETTINGS)
def test_order_refunded_labels_current_subscription_as_ours():
    user = paid_user("rf_ordlbl")
    client = Client()
    body = event_body(
        "order_refunded", user_pk=user.pk, sub_id="ord_6001", webhook_id="wh_ordlbl",
        updated_at=timezone.now().isoformat(), status="refunded",
        extra_attrs={"total": 1200, "refunded_amount": 600, "refunded": False, "currency": "USD"},
    )
    assert post_event(client, body).status_code == 200
    msg = only_refund_alert()
    assert "Current LS subscription (ours): https://app.lemonsqueezy.com/subscriptions/sub_9001" in msg.body
    assert "Current LS subscription id (ours): sub_9001" in msg.body
    assert "\nLS subscription:" not in msg.body
    assert "\nLS subscription id:" not in msg.body
