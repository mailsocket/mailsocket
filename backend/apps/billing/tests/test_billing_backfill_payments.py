"Public API."

import io
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core import mail
from django.core.management import call_command
from django.db import IntegrityError
from django.test import override_settings

from apps.billing import backfill
from apps.billing.models import BillingPayment
from apps.billing.tests.test_billing_reconcile import CUSTOMER_EMAIL, NOW, ON, Edge, local, plans_snapshot
from apps.billing.tests.test_billing_reconcile_invoices import InvoiceEdge, ls_inv, payment, payments_snapshot

pytestmark = pytest.mark.django_db

PAID_AT = NOW - timedelta(days=40)


def run_backfill(edge, *args, **settings_kw):
    out, err = io.StringIO(), io.StringIO()
    with (
        override_settings(**{**ON, **settings_kw}),
        patch("urllib.request.urlopen", side_effect=edge),
        patch("apps.billing.reconcile.time.sleep"),
    ):
        try:
            call_command("billing_backfill_payments", *args, stdout=out, stderr=err)
            code = 0
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def world():
    """Two subscriptions held locally, one LS invoice already recorded, three missing
    (paid, partial refund, full refund), one pending, one for an unknown subscription."""
    alice = local("bf_alice", "3001")
    bob = local("bf_bob", "3002")
    existing = payment(5001, alice, amount=999)  # differs from LS on purpose: must stay untouched
    BillingPayment.objects.filter(pk=existing.pk).update(receipt_sent_at=PAID_AT)
    invoices = [
        [
            ls_inv(5001, sub="3001", created=PAID_AT),
            ls_inv(5002, sub="3001", created=PAID_AT),
            ls_inv(5003, sub="3002", status="partial_refund", refunded_amount=400, created=PAID_AT),
        ],
        [
            ls_inv(5004, sub="3002", status="refunded", refunded_amount=1200, created=PAID_AT),
            ls_inv(5005, sub="3001", status="pending", created=PAID_AT),
            ls_inv(5006, sub="9999", created=PAID_AT),
        ],
    ]
    return alice, bob, invoices


def test_dry_run_lists_missing_and_writes_nothing(world):
    alice, bob, invoices = world
    before, plans = payments_snapshot(), plans_snapshot()
    code, out, err = run_backfill(Edge([[]], invoice_pages=invoices))
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == "billing_backfill_payments DRY-RUN ls_invoices=6 missing=3"
    day = PAID_AT.isoformat()
    assert lines[1:4] == [
        f"invoice=5002 date={day} amount=1200 USD status=paid sub=3001 user_pk={alice.pk}",
        f"invoice=5003 date={day} amount=1200 USD status=partial_refund refunded=400 sub=3002"
        f" user_pk={bob.pk}",
        f"invoice=5004 date={day} amount=1200 USD status=refunded refunded=1200 sub=3002 user_pk={bob.pk}",
    ]
    assert lines[4].startswith("dry-run: nothing written")
    assert err.splitlines() == [
        "WARNING skipped invoice=5006 sub=9999: no AccountPlan holds this subscription"
    ]
    assert (payments_snapshot(), plans_snapshot()) == (before, plans)
    assert "@" not in out + err and CUSTOMER_EMAIL not in out + err
    assert mail.outbox == []


def test_apply_creates_exactly_the_missing_rows_no_mail_plan_untouched_then_rerun_is_0(world):
    alice, bob, invoices = world
    plans = plans_snapshot()
    existing_before = BillingPayment.objects.filter(ls_invoice_id="5001").values().get()
    code, out, err = run_backfill(Edge([[]], invoice_pages=invoices), "--apply")
    assert code == 0
    assert out.splitlines()[0] == "billing_backfill_payments APPLY ls_invoices=6 missing=3"
    assert out.splitlines()[-1] == "created=3"
    assert sorted(BillingPayment.objects.values_list("ls_invoice_id", flat=True)) == [
        "5001",
        "5002",
        "5003",
        "5004",
    ]
    rows = {p.ls_invoice_id: p for p in BillingPayment.objects.all()}
    assert (rows["5002"].user_id, rows["5003"].user_id, rows["5004"].user_id) == (alice.pk, bob.pk, bob.pk)
    assert [
        (r.amount_cents, r.currency, r.refunded_amount_cents, r.refunded)
        for r in (rows["5002"], rows["5003"], rows["5004"])
    ] == [(1200, "USD", 0, False), (1200, "USD", 400, False), (1200, "USD", 1200, True)]
    assert rows["5002"].paid_at == PAID_AT and rows["5002"].refunded_at is None
    assert rows["5003"].refunded_at is not None and rows["5002"].ls_subscription_id == "3001"
    assert rows["5002"].card_last_four == "4242" and rows["5002"].reason == "renewal"
    assert all(rows[i].receipt_sent_at is None for i in ("5002", "5003", "5004"))
    assert (
        BillingPayment.objects.filter(ls_invoice_id="5001").values().get() == existing_before
    )  # not updated
    assert mail.outbox == []  # no receipt, no alert, no mail at all
    assert plans_snapshot() == plans
    assert "WARNING skipped invoice=5006" in err

    snapshot = payments_snapshot()
    code, out, _ = run_backfill(Edge([[]], invoice_pages=invoices), "--apply")
    assert code == 0
    assert out.splitlines()[0] == "billing_backfill_payments APPLY ls_invoices=6 missing=0"
    assert out.splitlines()[-1] == "created=0"
    assert payments_snapshot() == snapshot and mail.outbox == []


def test_apply_with_expect_count_refuses_on_drift_and_writes_nothing(world):
    _alice, _bob, invoices = world
    before = payments_snapshot()
    code, out, err = run_backfill(Edge([[]], invoice_pages=invoices), "--apply", "--expect-count", "2")
    assert code == 2
    assert err.splitlines()[-1] == (
        "billing_backfill_payments ERROR: --expect-count 2 but 3 rows are missing — nothing written"
    )
    assert payments_snapshot() == before
    code, out, _ = run_backfill(Edge([[]], invoice_pages=invoices), "--apply", "--expect-count", "3")
    assert (code, out.splitlines()[-1]) == (0, "created=3")


def test_unknown_subscription_is_skipped_with_a_warning_and_never_created():
    code, out, err = run_backfill(
        Edge([[]], invoice_pages=[[ls_inv(5101, sub="8888", created=PAID_AT)]]), "--apply"
    )
    assert code == 0
    assert err.splitlines() == [
        "WARNING skipped invoice=5101 sub=8888: no AccountPlan holds this subscription"
    ]
    assert out.splitlines()[-1] == "created=0"
    assert not BillingPayment.objects.exists()


def test_malformed_money_invoice_is_skipped_with_a_warning():
    local("bf_mal", "3001")
    bad = ls_inv(5201, created=PAID_AT)
    bad["attributes"]["total"] = None
    pages = [[bad]]
    code, out, err = run_backfill(Edge([[]], invoice_pages=pages), "--apply")
    assert code == 0
    assert err.splitlines() == [
        "WARNING skipped invoice=5201 sub=3001: malformed total/refunded_amount/created_at"
    ]
    assert not BillingPayment.objects.exists()


@pytest.mark.parametrize("args", [(), ("--apply",)])
def test_partial_listing_or_ls_down_is_exit_2_nothing_written(args):
    local("bf_down", "3001")
    pages = [[ls_inv(5301, created=PAID_AT)], [], [ls_inv(5303, created=PAID_AT)]]
    code, out, err = run_backfill(InvoiceEdge(pages), *args)
    assert (code, out) == (2, "")
    assert err.splitlines() == [
        "billing_backfill_payments ERROR: empty page 2 before lastPage 3 — nothing written"
    ]
    assert not BillingPayment.objects.exists()


@pytest.mark.parametrize("override", [dict(ENABLE_BILLING=False), dict(LS_API_KEY="")])
def test_billing_off_is_exit_2_without_any_call(override):
    edge = Edge([[]])
    code, out, err = run_backfill(edge, "--apply", **override)
    assert (code, out, edge.requests) == (2, "", [])
    assert err.splitlines() == [
        "billing_backfill_payments ERROR: ENABLE_BILLING off or LS_API_KEY not set — nothing read"
    ]


def test_row_created_by_a_webhook_meanwhile_is_never_overwritten():
    # race: the plan says "missing", a webhook inserts the row before create -> skipped, untouched.
    alice = local("bf_race", "3001")
    cands = backfill.plan({"5401": ls_inv(5401, created=PAID_AT)["attributes"]}, lambda _l: None)
    assert [c.invoice_id for c in cands] == ["5401"]
    payment(5401, alice, amount=777)
    assert backfill.create_missing(cands) == 0
    assert BillingPayment.objects.get(ls_invoice_id="5401").amount_cents == 777
    # IntegrityError path (row appears between the exists() check and the insert)
    BillingPayment.objects.all().delete()
    # The webhook's row is committed by ANOTHER connection, so after our savepoint
    # rolls back the duplicate is still visible: simulate that visibility.
    with (
        patch.object(BillingPayment.objects, "create", side_effect=IntegrityError("duplicate key")),
        patch.object(backfill, "_already_recorded", return_value=True),
    ):
        assert backfill.create_missing(cands) == 0
    assert backfill.create_missing(cands) == 1
    assert BillingPayment.objects.filter(ls_invoice_id="5401").count() == 1
    assert backfill.create_missing(cands) == 0


def test_integrity_error_that_is_not_the_duplicate_race_is_raised():
    "Public API."
    alice = local("bf_integrity", "3001")
    cands = backfill.plan({"5501": ls_inv(5501, created=PAID_AT)["attributes"]}, lambda _l: None)
    assert [c.invoice_id for c in cands] == ["5501"]
    assert alice is not None
    with patch.object(BillingPayment.objects, "create", side_effect=IntegrityError("fk")):
        with pytest.raises(IntegrityError):
            backfill.create_missing(cands)
    assert not BillingPayment.objects.filter(ls_invoice_id="5501").exists()
