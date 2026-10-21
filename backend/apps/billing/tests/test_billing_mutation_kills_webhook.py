"""CP-2 Lemon Squeezy webhook: ls_webhook size/signature boundaries and exact
JSON responses, _apply_ls_event ordering (stale-skip, subscription mismatch,
status-only transitions) and _strict_cents.
"""

import json
import logging
from datetime import datetime
from datetime import timezone as dt_tz
from unittest.mock import patch

import pytest
from django.db import transaction
from django.test import override_settings

from apps.billing.models import BillingPayment
from apps.billing.tests.mutation_kill_helpers import (
    PAID_AT,
    PAID_V,
    SCALE_V,
    STILL_PAID,
    T_REFUND,
    WEBHOOK,
    WH_SECRET,
    _apply,
    _apply_real,
    _invoice_payload,
    _ls_payload,
    _messages,
    _notify_texts,
    _pc_user,
    _plan_snapshot,
    _post_raw,
    _sig,
)

pytestmark = pytest.mark.django_db


# =============================================================================
# apps/billing/views.py — CP-2 webhook (_apply_ls_event, _strict_cents)
# =============================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, None), (False, None), (None, None), (-1, None), (0, 0), (1, 1), ("250", 250), ("x", None)],
)
def test_strict_cents(raw, expected):
    # BL-19: billing-lifecycle scenario (skill billing-lifecycle)
    # kills x__strict_cents__mutmut_1 (a bool total would become 1 cent) and
    # _6/_7/_8 (negative rejected, 0 kept)
    from apps.billing import views as bviews

    assert bviews._strict_cents(raw) == expected


@pytest.mark.parametrize(
    ("payload", "reason", "details", "log"),
    [
        (
            {"meta": {"webhook_id": "wh_m"}, "data": ["x"]},
            "malformed data",
            [
                ("LS webhook id", "wh_m"),
                ("LS subscription id", None),
                ("LS customer id", None),
                ("LS customer email", None),
            ],
            "billing_webhook_malformed_data event=subscription_updated",
        ),
        (
            {"meta": {"webhook_id": "wh_m"}, "data": {"id": "sub_x", "attributes": ["x"]}},
            "malformed attributes",
            [
                ("LS webhook id", "wh_m"),
                ("LS subscription id", "sub_x"),
                ("LS customer id", None),
                ("LS customer email", None),
            ],
            "billing_webhook_malformed_attributes event=subscription_updated",
        ),
        (
            {
                "meta": {"webhook_id": "wh_m"},
                "data": {"id": "sub_x", "attributes": {"customer_id": 5, "user_email": "c@x.test"}},
            },
            "missing user_pk",
            [
                ("LS webhook id", "wh_m"),
                ("LS subscription id", "sub_x"),
                ("LS customer id", 5),
                ("LS customer email", "c@x.test"),
            ],
            "billing_webhook_missing_user_pk event=subscription_updated",
        ),
    ],
)
def test_apply_ls_event_malformed_payload_exact_problem_alert_and_log(caplog, payload, reason, details, log):
    # kills x__apply_ls_event__mutmut_8-11/14/18/21 (malformed data),
    # _32-35/38/42/45/46 (malformed attributes), _61-64 (missing user_pk log)
    with caplog.at_level(logging.ERROR, logger="apps.billing.views"):
        result, qp, unmatched = _apply("subscription_updated", payload)
    assert result is None
    qp.assert_called_once_with(reason, "subscription_updated", details=details)
    unmatched.assert_not_called()
    assert _messages(caplog, "apps.billing.views") == [log]


def test_apply_ls_event_refund_without_user_goes_to_unmatched_with_event_name():
    # kills x__apply_ls_event__mutmut_68 (event name dropped). _69 (payload
    # None) is EQUIVALENT: _record_unmatched_refund never reads ``payload``.
    payload = {"meta": {"webhook_id": "wh_r"}, "data": {"id": "ord_1", "attributes": {"refunded_amount": 1}}}
    result, qp, unmatched = _apply("order_refunded", payload)
    assert result is None
    qp.assert_not_called()
    args, kwargs = unmatched.call_args
    assert args[:2] == ("missing user_pk", "order_refunded")
    assert args[3] == payload["data"] and args[4] == payload["data"]["attributes"]


def test_apply_ls_event_unresolvable_user_refund_and_plan_events(caplog):
    # kills x__apply_ls_event__mutmut_79/81-83 (missing user_pk refund),
    # _114/_125/_131-133 (no plan, refund) and _138/_149-151 (no plan, other)
    payload = {
        "meta": {"webhook_id": "wh_n"},
        "data": {"id": "inv_n", "attributes": {"customer_id": 4, "user_email": "e@x.test"}},
    }
    ids = [
        ("LS webhook id", "wh_n"),
        ("LS subscription id", "inv_n"),
        ("LS customer id", 4),
        ("LS customer email", "e@x.test"),
    ]
    result, qp, unmatched = _apply("subscription_payment_refunded", payload)
    assert result is None
    assert unmatched.call_args.args[:2] == ("missing user_pk", "subscription_payment_refunded")
    assert unmatched.call_args.kwargs == {"details": ids}

    gone = dict(payload, meta={"webhook_id": "wh_n", "custom_data": {"user_pk": "987654"}})
    result, qp, unmatched = _apply("subscription_payment_refunded", gone)
    assert result is None
    assert unmatched.call_args.args[:2] == ("no plan for user", "subscription_payment_refunded")
    assert unmatched.call_args.kwargs == {"details": [("User pk", 987654)] + ids}
    qp.assert_not_called()

    with caplog.at_level(logging.ERROR, logger="apps.billing.views"):
        result, qp, unmatched = _apply("subscription_updated", gone)
    assert result is None
    qp.assert_called_once_with(
        "no plan for user", "subscription_updated", details=[("User pk", 987654)] + ids
    )
    assert "billing_webhook_no_plan_for_user user_pk=987654 event=subscription_updated" in _messages(
        caplog, "apps.billing.views"
    )


def _synced_user(name, **kw):
    """A subscriber whose plan was last synced BEFORE the invoice payloads
    below (data.id of a payment/refund payload is the INVOICE id, so only a
    newer updated_at gets past the subscription-mismatch guard, as in prod)."""
    from apps.accounts.models import AccountPlan

    u = _pc_user(name, **kw)
    AccountPlan.objects.filter(user=u).update(billing_synced_at=datetime(2026, 10, 1, tzinfo=dt_tz.utc))
    return u


def test_apply_ls_event_payment_success_records_the_real_payment_row_receipt_and_alert():
    # Real _record_payment against the test DB (no mock of our own money
    # code; only the ops notifier edge is mocked, the receipt goes to the
    # locmem outbox). Kills x__apply_ls_event__mutmut_188-193 (payment not
    # recorded / wrong args) and _205/_206 (amount missing from the alert).
    from django.core import mail

    from apps.accounts.models import AccountPlan

    u = _synced_user("ae_pay")
    before = _plan_snapshot(u)
    payload = _invoice_payload(u, "inv_pay", webhook_id="wh_pay")
    result, notify = _apply_real("subscription_payment_success", payload)
    assert result == u.pk
    row = BillingPayment.objects.get(ls_invoice_id="inv_pay")
    assert (
        row.user_id,
        row.amount_cents,
        row.currency,
        row.amount_formatted,
        row.reason,
        row.ls_subscription_id,
        row.card_brand,
        row.card_last_four,
        row.paid_at,
        row.refunded,
        row.refunded_amount_cents,
        row.refunded_at,
    ) == (u.pk, 1200, "USD", "$12.00", "renewal", "sub_mk", "visa", "4242", PAID_AT, False, 0, None)
    # plan identity unchanged (payment_success only re-asserts ACTIVE)
    p = AccountPlan.objects.get(user=u)
    assert (p.plan, p.status, p.stripe_subscription_id, p.stripe_price_id) == before[:4]
    # one money alert with the amount, one customer receipt
    assert [(lvl, title) for lvl, title, _, _ in _notify_texts(notify)] == [
        ("money", "Billing event subscription_payment_success: Pro (paid) — $12.00")
    ]
    assert len(mail.outbox) == 1 and mail.outbox[0].subject == "mailsocket receipt — $12.00 (Pro renewal)"


def test_apply_ls_event_payment_record_failure_alert_carries_webhook_id_and_event():
    # A REAL DB failure inside _record_payment's savepoint (invoice id longer
    # than the 64-char column): the plan update still commits and the ops
    # problem alert names the webhook and event. Kills
    # x__apply_ls_event__mutmut_194/195 and _200-204 (event name / webhook id
    # not passed to _record_payment).
    from apps.accounts.models import AccountPlan

    u = _synced_user("ae_payfail", status="past_due")
    payload = _invoice_payload(u, "i" * 65, webhook_id="wh_payfail")
    result, notify = _apply_real("subscription_payment_success", payload)
    assert result == u.pk
    assert not BillingPayment.objects.filter(user=u).exists()
    assert AccountPlan.objects.get(user=u).status == "active"
    texts = _notify_texts(notify)
    problem = [t for t in texts if t[1] == "Billing webhook problem: payment record failed"]
    assert len(problem) == 1
    body = problem[0][3].splitlines()
    assert "Event: subscription_payment_success" in body
    assert "LS webhook id: wh_payfail" in body
    assert f"User pk: {u.pk}" in body
    # the plan event alert still goes out, without an amount
    assert ("money", "Billing event subscription_payment_success: Pro (paid)") in [t[:2] for t in texts]


def test_apply_ls_event_payment_without_invoice_id_records_nothing_and_alerts_without_amount():
    # Real path: a payment_success payload without data.id records no row
    # and sends no receipt; the plan alert carries no amount. (r1's mock-based
    # test of `amount is None` here was internal wiring: _178, amount "" vs
    # None, is EQUIVALENT at the notifier edge, `if amount:` treats both alike.)
    from django.core import mail

    u = _synced_user("ae_noinv", status="past_due")
    payload = _invoice_payload(u, "", webhook_id="wh_noinv")
    result, notify = _apply_real("subscription_payment_success", payload)
    assert result == u.pk
    assert not BillingPayment.objects.filter(user=u).exists()
    assert mail.outbox == []
    [(level, title, tg, email)] = _notify_texts(notify)
    assert (level, title) == ("money", "Billing event subscription_payment_success: Pro (paid)")
    assert not any(line.startswith("Amount:") for line in email.splitlines())


def test_apply_ls_event_refund_records_real_refund_fields_plan_unchanged_and_alerts():
    # Real _record_refund against the test DB: refund fields on the
    # BillingPayment row (cumulative max, partial then full), AccountPlan
    # untouched, one money alert per change carrying the webhook id. Kills
    # x__apply_ls_event__mutmut_158/168/174-177 (refund webhook id lost).
    u = _synced_user("ae_ref")
    _apply_real("subscription_payment_success", _invoice_payload(u, "inv_ref", webhook_id="wh_pay0"))
    before = _plan_snapshot(u)

    partial = _invoice_payload(
        u,
        "inv_ref",
        webhook_id="wh_ref",
        refunded_amount=500,
        refunded_at="2026-10-09T10:00:00.000000Z",
        updated_at="2026-10-09T10:00:00.000000Z",
    )
    result, notify = _apply_real("subscription_payment_refunded", partial)
    assert result == u.pk
    row = BillingPayment.objects.get(ls_invoice_id="inv_ref")
    assert (row.amount_cents, row.refunded_amount_cents, row.refunded, row.refunded_at) == (
        1200,
        500,
        False,
        T_REFUND,
    )
    assert _plan_snapshot(u) == before
    [(level, title, tg, email)] = _notify_texts(notify)
    assert (level, title) == ("money", "Partial refund: $5.00 of $12.00 (invoice inv_ref)")
    for line in (
        "Event: subscription_payment_refunded",
        f"User pk: {u.pk}",
        "LS webhook id: wh_ref",
        "LS invoice id: inv_ref",
        "Refund: Partial refund — $5.00 of $12.00 refunded (cumulative)",
        "Recorded before: yes (payment row existed)",
        f"Our plan: {STILL_PAID}",
    ):
        assert line in tg.splitlines() and line in email.splitlines(), line

    # full refund, delivered with a meta that has no webhook id
    full = _invoice_payload(u, "inv_ref", webhook_id=None, refunded=True, refunded_amount=1200)
    del full["meta"]["webhook_id"]
    _, notify = _apply_real("subscription_payment_refunded", full)
    row.refresh_from_db()
    assert (row.refunded_amount_cents, row.refunded) == (1200, True)
    assert _plan_snapshot(u) == before
    [(level, title, tg, _)] = _notify_texts(notify)
    assert (level, title) == ("money", "Full refund: $12.00 of $12.00 (invoice inv_ref)")
    assert "LS webhook id: -" in tg.splitlines()

    # the same cumulative amount again: nothing changes, no alert
    _, notify = _apply_real("subscription_payment_refunded", full)
    row.refresh_from_db()
    assert (row.refunded_amount_cents, row.refunded) == (1200, True)
    assert _notify_texts(notify) == []


def test_apply_ls_event_same_timestamp_is_not_stale():
    # kills x__apply_ls_event__mutmut_214 (`<=`: an event with the SAME
    # updated_at as the last sync would be skipped)
    from apps.accounts.models import AccountPlan

    u = _pc_user("ae_same", status="past_due")
    AccountPlan.objects.filter(user=u).update(billing_synced_at=T_REFUND)
    result, notify = _apply_real("subscription_updated", _ls_payload(u, updated_at="2026-10-09T10:00:00Z"))
    assert result == u.pk
    p = AccountPlan.objects.get(user=u)
    assert (p.status, p.billing_synced_at) == ("active", T_REFUND)
    assert _notify_texts(notify) == []  # subscription_updated is not an alert event


def test_apply_ls_event_subscription_mismatch_rules(caplog):
    # kills x__apply_ls_event__mutmut_229 (no payload id -> no mismatch),
    # _236/_237 ("newer" override), _239-241 (subscription_created always
    # applies) and the mismatch log line _243-248
    from apps.accounts.models import AccountPlan

    def fresh(name):
        u = _pc_user(name, sub_id=f"old_{name}", status="past_due")
        AccountPlan.objects.filter(user=u).update(billing_synced_at=T_REFUND)
        return u

    # older/same timestamp, other subscription: ignored + logged
    u = fresh("ae_mm_old")
    with caplog.at_level(logging.INFO, logger="apps.billing.views"):
        _apply_real(
            "subscription_updated", _ls_payload(u, sub_id="sub_new", updated_at="2026-10-09T10:00:00Z")
        )
    assert AccountPlan.objects.get(user=u).status == "past_due"
    assert (
        f"billing_webhook_subscription_mismatch user_pk={u.pk} event=subscription_updated "
        "stored=old_ae_mm_old payload=sub_new"
    ) in _messages(caplog, "apps.billing.views")
    # no updated_at at all: not "newer" -> ignored
    u = fresh("ae_mm_none")
    _apply_real("subscription_updated", _ls_payload(u, sub_id="sub_new2"))
    assert AccountPlan.objects.get(user=u).stripe_subscription_id == "old_ae_mm_none"
    # strictly newer: applied, subscription id moves
    u = fresh("ae_mm_newer")
    _apply_real("subscription_updated", _ls_payload(u, sub_id="sub_new3", updated_at="2026-10-09T10:00:01Z"))
    assert AccountPlan.objects.get(user=u).stripe_subscription_id == "sub_new3"
    # subscription_created always applies (re-subscribe)
    u = fresh("ae_mm_created")
    _apply_real("subscription_created", _ls_payload(u, sub_id="sub_new4", updated_at="2026-10-09T10:00:00Z"))
    assert AccountPlan.objects.get(user=u).stripe_subscription_id == "sub_new4"
    # payload without an id never counts as a mismatch
    u = fresh("ae_mm_noid")
    _apply_real("subscription_updated", _ls_payload(u, sub_id="", updated_at="2026-10-09T10:00:00Z"))
    p = AccountPlan.objects.get(user=u)
    assert (p.status, p.stripe_subscription_id) == ("active", "old_ae_mm_noid")


def test_apply_ls_event_on_trial_activates_the_mapped_plan():
    # kills x__apply_ls_event__mutmut_282/283 (on_trial no longer grants the plan)
    from apps.accounts.models import AccountPlan

    u = _pc_user("ae_trial", plan="free", status="active", sub_id=None)
    _apply_real(
        "subscription_created", _ls_payload(u, sub_id="sub_trial", status="on_trial", variant=SCALE_V)
    )
    p = AccountPlan.objects.get(user=u)
    assert (p.plan, p.status, p.stripe_subscription_id, p.stripe_price_id) == (
        "scale",
        "active",
        "sub_trial",
        SCALE_V,
    )


def test_apply_ls_event_unknown_variant_exact_problem_alert_and_log(caplog):
    # kills x__apply_ls_event__mutmut_273 ("" variant), _290-300 (log + alert
    # event), _314-316 (ids)
    from apps.accounts.models import AccountPlan
    from apps.billing import views as bviews

    u = _pc_user("ae_unknown", plan="free", sub_id=None)
    payload = _ls_payload(u, sub_id="sub_u", variant=PAID_V, webhook_id="wh_u")
    payload["data"]["attributes"]["variant_id"] = None
    payload["data"]["attributes"]["user_email"] = "u@x.test"
    with (
        transaction.atomic(),
        patch("apps.billing.views.queue_problem_alert") as qp,
        caplog.at_level(logging.ERROR, logger="apps.billing.views"),
    ):
        assert bviews._apply_ls_event("subscription_created", payload) == u.pk
    qp.assert_called_once_with(
        "unknown variant",
        "subscription_created",
        details=[
            ("User pk", u.pk),
            ("Variant id", ""),
            ("LS webhook id", "wh_u"),
            ("LS subscription id", "sub_u"),
            ("LS customer id", "cust_mk"),
            ("LS customer email", "u@x.test"),
        ],
    )
    assert _messages(caplog, "apps.billing.views") == [
        f"billing_webhook_unknown_variant user_pk={u.pk} event=subscription_created variant_id="
    ]
    assert AccountPlan.objects.get(user=u).plan == "free"


@override_settings(ENABLE_BILLING=False)
def test_ls_webhook_flag_off_404():
    # kills x_ls_webhook__mutmut_2-5 are EQUIVALENT (Http404 text is never
    # shown: DEBUG=False renders the generic 404 page). Pin the status only.
    assert _post_raw(b"{}").status_code == 404


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=WH_SECRET)
@pytest.mark.parametrize(
    ("body", "sig", "code", "payload"),
    [
        (b'{"meta": {"event_name": "x"}}', "bad", 401, {"detail": "Invalid signature."}),
        (b"not json", None, 400, {"detail": "Malformed JSON."}),
        (b'{"meta": {}}', None, 400, {"detail": "Malformed payload."}),
        (b'{"meta": {"event_name": "unhandled_x", "webhook_id": "wh_ok"}}', None, 200, {"ok": True}),
    ],
)
def test_ls_webhook_exact_json_responses(body, sig, code, payload):
    # kills x_ls_webhook__mutmut_99-103 (401 body), _111-115 (bad JSON),
    # _127-131 (bad payload), _161/_163 are EQUIVALENT (JsonResponse
    # defaults to 200) but the body is pinned
    resp = _post_raw(body, sig=sig)
    assert (resp.status_code, resp.json()) == (code, payload)


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=WH_SECRET)
def test_ls_webhook_duplicate_exact_json():
    body = b'{"meta": {"event_name": "unhandled_x", "webhook_id": "wh_dup_mk"}}'
    assert _post_raw(body).json() == {"ok": True}
    resp = _post_raw(body)
    assert (resp.status_code, resp.json()) == (200, {"duplicate": True})


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=WH_SECRET)
def test_ls_webhook_body_exactly_at_the_cap_is_accepted_one_over_is_400():
    # kills x_ls_webhook__mutmut_12 (Content-Length `>=`), _44 (body `>=`)
    # and _34-54 (exact 400 body)
    body = b'{"meta": {"event_name": "unhandled_x", "webhook_id": "wh_cap"}}'
    with override_settings(LS_WEBHOOK_MAX_BYTES=len(body)):
        assert _post_raw(body).json() == {"ok": True}
    with override_settings(LS_WEBHOOK_MAX_BYTES=len(body) - 1):
        resp = _post_raw(body)
    assert (resp.status_code, resp.json()) == (400, {"detail": "Payload too large."})


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=WH_SECRET, LS_WEBHOOK_MAX_BYTES=100)
def test_ls_webhook_content_length_checked_before_reading_the_body():
    # kills x_ls_webhook__mutmut_6-9/_17-21: a declared Content-Length over
    # the cap is refused WITHOUT reading the body (request.body never touched)
    from django.http import HttpRequest

    reads = []
    real_body = HttpRequest.body

    def spy(self):
        reads.append(1)
        return real_body.fget(self)

    with patch.object(HttpRequest, "body", property(spy)):
        resp = _post_raw(b"{}", CONTENT_LENGTH="500")
    assert (resp.status_code, resp.json()) == (400, {"detail": "Payload too large."})
    assert reads == []


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=WH_SECRET)
def test_ls_webhook_malformed_content_length_400():
    # kills x_ls_webhook__mutmut_23-32
    from django.test import RequestFactory

    from apps.billing import views as bviews

    request = RequestFactory().generic(
        "POST", WEBHOOK, b"{}", content_type="application/json", HTTP_X_SIGNATURE=_sig(b"{}")
    )
    request.META["CONTENT_LENGTH"] = "abc"
    resp = bviews.ls_webhook(request)
    assert (resp.status_code, json.loads(resp.content)) == (400, {"detail": "Malformed Content-Length."})


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=WH_SECRET)
def test_ls_webhook_missing_signature_header_401():
    # x_ls_webhook__mutmut_61/62 (header name case) and _63 (default "XXXX")
    # are EQUIVALENT: request.headers is case-insensitive and any default
    # fails the HMAC compare. Pinned: no header -> 401, lower-case header ok.
    from django.test import Client

    body = b'{"meta": {"event_name": "unhandled_x", "webhook_id": "wh_case"}}'
    resp = Client().generic("POST", WEBHOOK, body, content_type="application/json")
    assert (resp.status_code, resp.json()) == (401, {"detail": "Invalid signature."})
    resp = Client().generic(
        "POST", WEBHOOK, body, content_type="application/json", headers={"x-signature": _sig(body)}
    )
    assert resp.json() == {"ok": True}


# --- last batch: state transitions by STATUS alone, ids, refund alert details --


@pytest.mark.parametrize(
    ("status", "expected_status"),
    [("past_due", "past_due"), ("cancelled", "active"), ("paused", "past_due")],
)
def test_apply_ls_event_status_alone_drives_the_transition(status, expected_status):
    # kills x__apply_ls_event__mutmut_367/368 (past_due), _389/_390/_393-395
    # (cancelled -> grace, stays active, ends_at kept) and _409/_413-415
    # (paused) for a subscription_updated that only carries the new LS status
    from apps.accounts.models import AccountPlan

    u = _pc_user(f"ae_status_{status}", status="active" if status != "cancelled" else "past_due")
    payload = _ls_payload(u, status=status, extra={"ends_at": "2026-11-07T18:12:00Z"})
    result, notify = _apply_real("subscription_updated", payload)
    p = AccountPlan.objects.get(user=u)
    assert (result, p.plan, p.status) == (u.pk, "paid", expected_status)
    assert _notify_texts(notify) == []
    if status == "cancelled":
        assert p.current_period_end == datetime(2026, 11, 7, 18, 12, tzinfo=dt_tz.utc)


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            "subscription_cancelled",
            ("paid", "active", "sub_mk", datetime(2026, 11, 7, 18, 12, tzinfo=dt_tz.utc)),
        ),
        ("subscription_paused", ("paid", "past_due", "sub_mk", None)),
        ("subscription_expired", ("free", "active", None, None)),
    ],
)
def test_apply_ls_event_event_name_alone_drives_the_transition(event, expected):
    # The counterpart of the test above: the EVENT NAME decides when the
    # payload carries no status attribute. Kills x__apply_ls_event__mutmut_391/392
    # (subscription_cancelled not recognised -> unhandled, no grace end),
    # _411/_412 (subscription_paused not recognised) and _410 (`!=`: an
    # expired subscription would be paused instead of moved to FREE).
    from apps.accounts.models import AccountPlan

    u = _pc_user(f"ae_ev_{event}")
    AccountPlan.objects.filter(user=u).update(current_period_end=None)
    payload = _ls_payload(u, extra={"ends_at": "2026-11-07T18:12:00Z"})
    del payload["data"]["attributes"]["status"]
    result, notify = _apply_real(event, payload)
    p = AccountPlan.objects.get(user=u)
    assert result == u.pk
    assert (p.plan, p.status, p.stripe_subscription_id, p.current_period_end) == expected
    alerts = [title for _, title, _, _ in _notify_texts(notify)]
    if event == "subscription_paused":
        assert alerts == []  # not an alert event
    else:
        assert alerts == [f"Billing event {event}: {'Free (free)' if p.plan == 'free' else 'Pro (paid)'}"]


def test_apply_ls_event_missing_customer_id_keeps_the_stored_one():
    # kills x__apply_ls_event__mutmut_328 (stripe_customer_id overwritten
    # with a placeholder when LS omits customer_id)
    from apps.accounts.models import AccountPlan

    u = _pc_user("ae_cust")
    payload = _ls_payload(u, status="active", variant=SCALE_V)
    del payload["data"]["attributes"]["customer_id"]
    _apply_real("subscription_updated", payload)
    p = AccountPlan.objects.get(user=u)
    assert (p.plan, p.stripe_customer_id) == ("scale", "cust_mk")


@override_settings(
    ENABLE_BILLING=True,
    LS_WEBHOOK_SECRET=WH_SECRET,
    LS_WEBHOOK_MAX_BYTES=1000,
    DATA_UPLOAD_MAX_MEMORY_SIZE=10,
)
def test_ls_webhook_request_data_too_big_400():
    # kills x_ls_webhook__mutmut_34-43 (Django's RequestDataTooBig path)
    body = b'{"meta": {"event_name": "unhandled_x", "webhook_id": "wh_big"}}'
    resp = _post_raw(body)
    assert (resp.status_code, resp.json()) == (400, {"detail": "Payload too large."})


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=WH_SECRET, LS_WEBHOOK_MAX_BYTES=10)
def test_ls_webhook_body_over_cap_without_content_length_400():
    # kills x_ls_webhook__mutmut_45-54: a body over the cap with NO declared
    # Content-Length (chunked) is still refused before the signature check
    from django.test import RequestFactory

    from apps.billing import views as bviews

    body = b'{"meta": {"event_name": "x"}}'
    request = RequestFactory().generic(
        "POST", WEBHOOK, b"", content_type="application/json", HTTP_X_SIGNATURE=_sig(body)
    )
    request.META.pop("CONTENT_LENGTH", None)
    request._body = body
    with patch("apps.billing.views.hmac.compare_digest") as digest:
        resp = bviews.ls_webhook(request)
    assert (resp.status_code, json.loads(resp.content)) == (400, {"detail": "Payload too large."})
    digest.assert_not_called()
