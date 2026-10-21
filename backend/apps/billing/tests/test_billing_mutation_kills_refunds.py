"Public API."

import logging
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.billing.models import BillingPayment
from apps.billing.tests.mutation_kill_helpers import (
    STILL_PAID,
    T_REFUND,
    _messages,
    _payment,
    _refund_plan,
    _refund_spies,
    _unmatched,
    _user,
)

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("stored", "stored_fully", "payload_cents", "payload_fully", "expect_cents", "expect_fully", "changed"),
    [
        # partial, nothing stored yet
        (0, False, 500, False, 500, False, True),
        # refunded:true with amount 0 -> the stored invoice total (full)
        (0, False, 0, True, 1200, True, True),
        # refunded:true WITH an explicit (partial-looking) amount: the amount
        # is kept, the full flag comes from the payload (LS semantics)
        (0, False, 700, True, 700, True, True),
        # cumulative 1200 of 1200 without the flag: fully refunded at ==
        (500, False, 1200, False, 1200, True, True),
        # an older, lower cumulative amount: max(), unchanged, no save
        (900, False, 500, False, 900, False, False),
        # amount 0 and not fully: nothing to do
        (0, False, 0, False, 0, False, False),
        # already fully refunded stays fully refunded on a lower re-send
        (1200, True, 500, False, 1200, True, False),
    ],
)
def test_apply_refund_to_row_cumulative_rules(
    stored, stored_fully, payload_cents, payload_fully, expect_cents, expect_fully, changed
):
    # kills x__apply_refund_to_row__mutmut_1/2/3 (refunded:true + 0 -> total),
    # _11/_12 (full flag sources), _13 (>= at exactly the total) and the
    # rest of the max()/changed logic
    from apps.billing import views as bviews

    p = _payment(
        _user(f"ar_{stored}_{payload_cents}_{payload_fully}_{stored_fully}"),
        invoice=f"inv_ar_{stored}_{payload_cents}_{payload_fully}_{stored_fully}",
        refunded_amount_cents=stored,
        refunded=stored_fully,
    )
    assert bviews._apply_refund_to_row(p, payload_cents, payload_fully, T_REFUND) is changed
    p.refresh_from_db()
    assert (p.refunded_amount_cents, p.refunded) == (expect_cents, expect_fully)
    assert (p.refunded_at == T_REFUND) is changed


def test_apply_refund_to_row_missing_time_falls_back_to_now():
    from apps.billing import views as bviews

    p = _payment(_user("ar_now"), invoice="inv_ar_now")
    before = timezone.now()
    assert bviews._apply_refund_to_row(p, 300, False, None) is True
    p.refresh_from_db()
    assert p.refunded_at >= before


@pytest.mark.parametrize(
    ("attrs", "expected"),
    [
        ({"refunded_at": "2026-10-09T10:00:00Z", "updated_at": "2026-10-01T00:00:00Z"}, T_REFUND),
        ({"updated_at": "2026-10-09T10:00:00Z"}, T_REFUND),
        ({"refunded_at": None, "updated_at": "2026-10-09T10:00:00Z"}, T_REFUND),
    ],
)
def test_refund_time_prefers_refunded_at_then_updated_at(attrs, expected):
    # kills x__refund_time__mutmut_2/4/5/6/8
    from apps.billing import views as bviews

    assert bviews._refund_time(attrs) == expected


@pytest.mark.parametrize("attrs", [{}, {"refunded_at": "2026-13-45T10:00:00Z"}, {"updated_at": "garbage"}])
def test_refund_time_invalid_or_missing_is_now(attrs):
    # kills x__refund_time__mutmut_7 (`parsed and now()` -> None for a bad value)
    from apps.billing import views as bviews

    before = timezone.now()
    got = bviews._refund_time(attrs)
    assert got is not None and before <= got <= timezone.now()


def test_record_refund_order_refunded_exact_alert_non_usd():
    # kills x__record_refund__mutmut_3-9 (currency), _38-46 (labels),
    # _90/_91, _100-112, _133-144 (order footer)
    from apps.billing import views as bviews

    u, plan = _refund_plan("rr_order")
    qr, qp = _refund_spies()
    with qr as refund_alert, qp as problem_alert:
        bviews._record_refund(
            u.pk,
            plan,
            "order_refunded",
            {"id": "ord_1"},
            {"currency": "EUR", "total": 5000, "refunded_amount": 2000},
            webhook_id="wh_o",
        )
    problem_alert.assert_not_called()
    refund_alert.assert_called_once_with(
        title="Refund on LS order ord_1: 20.00 EUR of 50.00 EUR — no matching payment row",
        details=[
            ("User pk", u.pk),
            ("LS webhook id", "wh_o"),
            ("LS order id", "ord_1"),
            ("Current LS subscription id (ours)", "sub_9001"),
            ("Refunded (cumulative)", "20.00 EUR"),
            ("Order total", "50.00 EUR"),
            ("Our plan", STILL_PAID),
            ("LS order", "https://app.lemonsqueezy.com/orders/ord_1"),
            ("Current LS subscription (ours)", "https://app.lemonsqueezy.com/subscriptions/sub_9001"),
        ],
        footer=(
            "",
            "No BillingPayment row matched this order (we record subscription INVOICES,",
            "not orders), so Recent payments is NOT updated by this event. If this was a",
            "subscription payment, a separate subscription_payment_refunded alert follows",
            "and records it; otherwise check the order in Lemon Squeezy.",
        )
        + bviews._REFUND_FOOTER,
        event_name="order_refunded",
        user_pk=u.pk,
    )


def test_record_refund_order_refunded_unknown_amounts_and_ids():
    # kills x__record_refund__mutmut_26/_69/_75/_76/_79/_85/_86/_98/_120/_131
    from apps.billing import views as bviews

    u, plan = _refund_plan("rr_order_unknown", sub_id=None)
    qr, qp = _refund_spies()
    with qr as refund_alert, qp:
        bviews._record_refund(u.pk, plan, "order_refunded", {}, {})
    kw = refund_alert.call_args.kwargs
    assert kw["title"] == "Refund on LS order ?: unknown of unknown — no matching payment row"
    d = dict(kw["details"])
    assert (
        d["LS order id"],
        d["Refunded (cumulative)"],
        d["Order total"],
        d["LS order"],
        d["Current LS subscription (ours)"],
    ) == ("", "unknown", "unknown", "-", "-")


def test_record_refund_missing_amount_exact_problem_alert(caplog):
    # kills x__record_refund__mutmut_27-33/_51-63 (invoice labels, subscription
    # id from the payload), _157-173 (footer) + the malformed log line
    from apps.billing import views as bviews

    u, plan = _refund_plan("rr_noamt")
    qr, qp = _refund_spies()
    with qr as refund_alert, qp as problem_alert, caplog.at_level(logging.ERROR, logger="apps.billing.views"):
        bviews._record_refund(
            u.pk,
            plan,
            "subscription_payment_refunded",
            {"id": "inv_b"},
            {"subscription_id": 777, "total": 1200},
            webhook_id="wh_b",
        )
    refund_alert.assert_not_called()
    problem_alert.assert_called_once_with(
        "refund amount/invoice missing",
        "subscription_payment_refunded",
        details=[
            ("User pk", u.pk),
            ("LS webhook id", "wh_b"),
            ("LS invoice id", "inv_b"),
            ("LS subscription id", "777"),
        ],
        footer=(
            "",
            "A refund webhook arrived but could not be recorded (missing invoice id or",
            "refunded_amount). The plan was NOT changed. Check the invoice in Lemon Squeezy.",
        )
        + bviews._REFUND_FOOTER,
    )
    assert _messages(caplog, "apps.billing.views") == [
        f"billing_refund_malformed user_pk={u.pk} invoice=inv_b"
    ]


def test_record_refund_total_missing_exact_problem_alert_falls_back_to_our_subscription(caplog):
    # kills _29/_33/_63 (no payload subscription id -> ours), _190-214, log line
    from apps.billing import views as bviews

    u, plan = _refund_plan("rr_nototal")
    qr, qp = _refund_spies()
    with qr, qp as problem_alert, caplog.at_level(logging.ERROR, logger="apps.billing.views"):
        bviews._record_refund(
            u.pk,
            plan,
            "subscription_payment_refunded",
            {"id": "inv_c"},
            {"refunded_amount": 500},
            webhook_id="wh_c",
        )
    problem_alert.assert_called_once_with(
        "refund invoice total missing",
        "subscription_payment_refunded",
        details=[
            ("User pk", u.pk),
            ("LS webhook id", "wh_c"),
            ("LS invoice id", "inv_c"),
            ("LS subscription id", "sub_9001"),
            ("Refunded (cumulative)", "$5.00"),
        ],
        footer=(
            "",
            "The refund could not be recorded (invoice total missing/malformed).",
            "The plan was NOT changed. Check the invoice in Lemon Squeezy.",
        )
        + bviews._REFUND_FOOTER,
    )
    assert _messages(caplog, "apps.billing.views") == [
        f"billing_refund_malformed_total user_pk={u.pk} invoice=inv_c"
    ]


def test_record_refund_db_failure_exact_problem_alert(caplog):
    # kills x__record_refund__mutmut_254-279 + the record_failed log line
    from django.db import DatabaseError

    from apps.billing import views as bviews

    u, plan = _refund_plan("rr_dbfail")
    qr, qp = _refund_spies()
    with (
        qr as refund_alert,
        qp as problem_alert,
        caplog.at_level(logging.ERROR, logger="apps.billing.views"),
        patch("apps.billing.views.BillingPayment.objects.get_or_create", side_effect=DatabaseError("x")),
    ):
        bviews._record_refund(
            u.pk,
            plan,
            "subscription_payment_refunded",
            {"id": "inv_d"},
            {"refunded_amount": 500, "total": 1200, "subscription_id": 9},
            webhook_id="wh_d",
        )
    refund_alert.assert_not_called()
    problem_alert.assert_called_once_with(
        "refund record failed",
        "subscription_payment_refunded",
        details=[
            ("User pk", u.pk),
            ("LS webhook id", "wh_d"),
            ("LS invoice id", "inv_d"),
            ("LS subscription id", "9"),
            ("Refunded (cumulative)", "$5.00"),
        ],
        footer=(
            "",
            "The refund webhook returned 200 but the refund was NOT recorded on the",
            "payment row. The plan was NOT changed. Check the invoice in Lemon Squeezy.",
        )
        + bviews._REFUND_FOOTER,
    )
    assert _messages(caplog, "apps.billing.views") == [
        f"billing_refund_record_failed user_pk={u.pk} invoice=inv_d"
    ]


def test_record_refund_other_accounts_invoice_exact_problem_alert(caplog):
    # kills x__record_refund__mutmut_282-311 (mismatch alert + log)
    from apps.billing import views as bviews

    u, plan = _refund_plan("rr_mine")
    other = _user("rr_other")
    row = _payment(other, invoice="inv_e")
    qr, qp = _refund_spies()
    with qr as refund_alert, qp as problem_alert, caplog.at_level(logging.ERROR, logger="apps.billing.views"):
        bviews._record_refund(
            u.pk,
            plan,
            "subscription_payment_refunded",
            {"id": "inv_e"},
            {"refunded_amount": 500, "total": 1200},
            webhook_id="wh_e",
        )
    refund_alert.assert_not_called()
    problem_alert.assert_called_once_with(
        "refund invoice belongs to another account",
        "subscription_payment_refunded",
        details=[
            ("User pk", u.pk),
            ("LS webhook id", "wh_e"),
            ("LS invoice id", "inv_e"),
            ("LS subscription id", "sub_9001"),
            ("Payment row user pk", other.pk),
        ],
        footer=(
            "",
            "The refund was NOT recorded and no plan was changed. Check the invoice",
            "and both accounts in Lemon Squeezy / admin.",
        )
        + bviews._REFUND_FOOTER,
    )
    row.refresh_from_db()
    assert (row.refunded_amount_cents, row.refunded) == (0, False)
    assert _messages(caplog, "apps.billing.views") == [
        f"billing_refund_user_mismatch invoice=inv_e row_user={other.pk} payload_user={u.pk}"
    ]


def test_record_refund_full_flag_and_payload_refund_time_are_stored():
    # kills x__record_refund__mutmut_215 (refunded_at from the payload
    # dropped -> now()), _217-220 (LS `refunded: true` ignored -> a full
    # refund with a partial-looking cumulative amount stays "partial"),
    # _240/_241 (arguments to _apply_refund_to_row)
    from apps.billing import views as bviews

    u, plan = _refund_plan("rr_full")
    row = _payment(u, invoice="inv_f")
    qr, qp = _refund_spies()
    with qr as refund_alert, qp:
        bviews._record_refund(
            u.pk,
            plan,
            "subscription_payment_refunded",
            {"id": "inv_f"},
            {"refunded_amount": 700, "refunded": True, "total": 1200, "refunded_at": "2026-10-09T10:00:00Z"},
            webhook_id="wh_f",
        )
    row.refresh_from_db()
    assert (row.refunded_amount_cents, row.refunded, row.refunded_at) == (700, True, T_REFUND)
    assert refund_alert.call_args.kwargs["title"] == "Full refund: $7.00 of $12.00 (invoice inv_f)"


def test_unmatched_order_refund_exact_alert():
    # kills x__record_unmatched_refund__mutmut_19-24, _102, _111-114
    from apps.billing import views as bviews

    call = _unmatched("order_refunded", {"id": "ord_9"}, {"refunded_amount": 100})
    assert call.args == ("missing user_pk", "order_refunded")
    assert call.kwargs == {
        "details": [("LS webhook id", "wh_u"), ("LS invoice/order id", "ord_9")],
        "footer": (
            "",
            bviews._UNMATCHED_REFUND_LINE,
            "The refund was NOT recorded (order refunds have no payment row).",
        ),
    }


def test_unmatched_refund_without_row_or_with_odd_shapes_never_raises():
    # kills _3/_9/_10/_13 (non-dict data/attributes, empty id) and _15
    for data, attrs in (([], []), ({"id": None}, {}), ({"id": "inv_none"}, {"refunded_amount": 100})):
        call = _unmatched("subscription_payment_refunded", data, attrs)
        expected_id = "inv_none" if data == {"id": "inv_none"} else ""
        assert call.kwargs["details"][-1] == ("LS invoice/order id", expected_id)
        assert call.kwargs["footer"][2] == "The refund was NOT recorded (no payment row for this invoice)."


def test_unmatched_refund_missing_amount_not_recorded():
    # kills _46-49 and _42 (a bool refunded_amount with refunded:true must
    # NOT be treated as "fully refunded")
    row = _payment(_user("um_bool"), invoice="inv_bool")
    for attrs in ({"refunded": False}, {"refunded": True, "refunded_amount": True}):
        call = _unmatched("subscription_payment_refunded", {"id": "inv_bool"}, attrs)
        assert call.kwargs["footer"][2] == "The refund was NOT recorded (refunded_amount missing/malformed)."
    row.refresh_from_db()
    assert (row.refunded_amount_cents, row.refunded) == (0, False)


def test_unmatched_refund_full_without_amount_recorded_on_row_then_unchanged_and_orphan():
    # kills _27/_30-_41/_43/_44 (refunded:true + no amount -> row total),
    # _56-64 (payload_fully), _81-85 ("already recorded, unchanged"),
    # _86/_89 (orphan row: "none (deleted)")
    u = _user("um_full")
    row = _payment(u, invoice="inv_um")
    call = _unmatched(
        "subscription_payment_refunded",
        {"id": "inv_um"},
        {"refunded": True, "refunded_at": "2026-10-09T10:00:00Z"},
    )
    assert call.kwargs["footer"][2] == (
        "Recorded on the existing payment row for invoice inv_um: Refunded $12.00 of $12.00; "
        f"row user pk {u.pk}."
    )
    row.refresh_from_db()
    assert (row.refunded_amount_cents, row.refunded, row.refunded_at) == (1200, True, T_REFUND)
    BillingPayment.objects.filter(pk=row.pk).update(user=None)
    call = _unmatched("subscription_payment_refunded", {"id": "inv_um"}, {"refunded": True})
    assert call.kwargs["footer"][2] == (
        "Recorded on the existing payment row for invoice inv_um: Refunded $12.00 of $12.00 "
        "(already recorded, unchanged); row user pk none (deleted)."
    )


def test_unmatched_refund_partial_amount_uses_payload_flag():
    # kills _56-64 from the other side: refunded false + partial amount
    row = _payment(_user("um_part"), invoice="inv_part2")
    call = _unmatched(
        "subscription_payment_refunded", {"id": "inv_part2"}, {"refunded_amount": 500, "total": 1200}
    )
    assert "Partially refunded $5.00 of $12.00;" in call.kwargs["footer"][2]
    row.refresh_from_db()
    assert (row.refunded_amount_cents, row.refunded) == (500, False)


def test_unmatched_refund_db_failure_status_line(caplog):
    # kills _97-100 + the unmatched_record_failed log line
    from django.db import DatabaseError

    _payment(_user("um_db"), invoice="inv_db")
    with (
        patch("apps.billing.views.BillingPayment.objects.select_for_update", side_effect=DatabaseError("x")),
        caplog.at_level(logging.ERROR, logger="apps.billing.views"),
    ):
        call = _unmatched("subscription_payment_refunded", {"id": "inv_db"}, {"refunded_amount": 500})
    assert call.kwargs["footer"][2] == "Recording on the payment row FAILED (DB error) — record it by hand."
    assert _messages(caplog, "apps.billing.views") == [
        "billing_refund_unmatched_record_failed invoice=inv_db"
    ]


def test_record_refund_success_alert_details_exact():
    # kills x__record_refund__mutmut_344/345 (subscription link source),
    # _349 (event name), _357-368 (labels/"Recorded before"), _374-384
    from apps.billing import views as bviews

    u, plan = _refund_plan("rr_details")
    _payment(u, invoice="inv_g", ls_subscription_id="ls_sub_row")
    qr, qp = _refund_spies()
    with qr as refund_alert, qp:
        bviews._record_refund(
            u.pk,
            plan,
            "subscription_payment_refunded",
            {"id": "inv_g"},
            {"refunded_amount": 500, "total": 1200, "subscription_id": 55},
            webhook_id="wh_g",
        )
    refund_alert.assert_called_once_with(
        title="Partial refund: $5.00 of $12.00 (invoice inv_g)",
        details=[
            ("User pk", u.pk),
            ("LS webhook id", "wh_g"),
            ("LS invoice id", "inv_g"),
            ("LS subscription id", "55"),
            ("Refund", "Partial refund — $5.00 of $12.00 refunded (cumulative)"),
            ("Recorded before", "yes (payment row existed)"),
            ("Our plan", STILL_PAID),
            ("LS subscription", "https://app.lemonsqueezy.com/subscriptions/ls_sub_row"),
        ],
        footer=bviews._REFUND_FOOTER,
        event_name="subscription_payment_refunded",
        user_pk=u.pk,
    )
    # row created from the refund payload (no payment yet): "no — ..." and the
    # link falls back to the payload subscription id, then ours
    qr, qp = _refund_spies()
    with qr as refund_alert, qp:
        bviews._record_refund(
            u.pk,
            plan,
            "subscription_payment_refunded",
            {"id": "inv_h"},
            {"refunded_amount": 1200, "total": 1200},
            webhook_id="wh_h",
        )
    d = dict(refund_alert.call_args.kwargs["details"])
    assert (
        d["Recorded before"] == "no — row created from the refund payload (refund arrived before the payment)"
    )
    assert d["LS subscription"] == "https://app.lemonsqueezy.com/subscriptions/sub_9001"
