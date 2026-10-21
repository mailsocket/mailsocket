"""CP-1 plan change: survivors of change_plan_confirm, _common_change_guards
and _change_plan killed here. Exact refusal flashes, PlanChangeRequest marker
status per PATCH outcome, and upgrade/downgrade direction (a downgrade must
never be PATCHed with invoice_immediately).
"""

import logging
from datetime import datetime
from datetime import timezone as dt_tz
from unittest.mock import patch

import pytest
from django.test import override_settings

from apps.billing.tests.mutation_kill_helpers import (
    BILLING_URL,
    BUSINESS_V,
    CANCEL_TEXT,
    CHANGE_URL,
    IN_PROGRESS_TEXT,
    NOT_FOUND_TEXT,
    PAID_V,
    SCALE_V,
    STATUS_TEXT,
    _confirm,
    _confirm_get,
    _flash_html,
    _http_error,
    _live,
    _messages,
    _msgs,
    _pc_user,
    _raise,
    _Templates,
)

pytestmark = pytest.mark.django_db


@override_settings(ENABLE_BILLING=True)
@pytest.mark.parametrize(
    ("plan_key", "plan", "status", "sub_id", "code", "text"),
    [
        ("gold", "paid", "active", "sub_mk", 400, "Unknown plan."),
        (
            "scale",
            "business",
            "active",
            "",
            409,
            "Your plan is managed by our team — contact sales@mailsocket.app",
        ),
        ("scale", "paid", "past_due", "sub_mk", 409, STATUS_TEXT),
    ],
)
def test_common_change_guards_exact_flash(plan_key, plan, status, sub_id, code, text):
    # kills x__common_change_guards__mutmut_6/11/12/13/29/30/50 (flash text)
    # on both the confirm GET and POST; no LS call on any refusal
    from django.test import Client

    u = _pc_user(f"gd_{plan_key}_{status}_{bool(sub_id)}", plan=plan, status=status, sub_id=sub_id)
    client = Client()
    client.force_login(u)
    for send in (
        lambda: client.get(CHANGE_URL, {"plan": plan_key}),
        lambda: client.post(CHANGE_URL, {"plan": plan_key, "token": "x"}),
    ):
        with (
            patch("apps.billing.views._ls_get") as g,
            patch("apps.billing.views._ls_patch") as p,
            _Templates() as t,
        ):
            resp = send()
        assert (resp.status_code, resp.content) == (code, _flash_html(text))
        assert t.names == ["dashboard/partials/flash.html"]
        # rendered WITH the request (kills _common_change_guards__mutmut_5/23/42)
        assert resp.context["user"] == u
        g.assert_not_called()
        p.assert_not_called()


def test_change_plan_direct_call_refuses_inactive_plan_exact_flash(rf):
    # kills x__change_plan__mutmut_7/11/14-19: _change_plan's OWN status
    # guard (defence in depth; the view's guard runs first, so only a direct
    # call reaches it). Never an LS call.
    from apps.accounts.models import AccountPlan
    from apps.billing import views as bviews

    u = _pc_user("cp_direct", status="past_due")
    request = rf.post(CHANGE_URL)
    request.user = u
    request.session = {}
    plan = AccountPlan.objects.get(user=u)
    with (
        patch("apps.billing.views._ls_get") as g,
        patch("apps.billing.views._ls_patch") as p,
        _Templates() as t,
    ):
        resp = bviews._change_plan(request, plan, "business", BUSINESS_V, token="x")
    assert (resp.status_code, resp.content) == (409, _flash_html(STATUS_TEXT))
    assert t.names == ["dashboard/partials/flash.html"]
    g.assert_not_called()
    p.assert_not_called()


@override_settings(ENABLE_BILLING=True)
def test_change_plan_bad_token_message_and_log(caplog):
    # kills x__change_plan__mutmut_33-40 (log) and _47/_48 (message text)
    u = _pc_user("cp_tok")
    with caplog.at_level(logging.INFO, logger="apps.billing.views"):
        resp, g, p = _confirm(u, "business", token="garbage")
    assert (resp.status_code, resp.url) == (303, f"{CHANGE_URL}?plan=business")
    assert _msgs(resp) == [
        "This confirmation has expired or doesn't match — please review the details and confirm again."
    ]
    assert f"billing_change_plan_token_rejected user_pk={u.pk} plan=business" in _messages(
        caplog, "apps.billing.views"
    )
    g.assert_not_called()
    p.assert_not_called()


def _token(user, target, *, from_variant=PAID_V, estimate=13115):
    from apps.billing import views as bviews

    return bviews._make_token(user.pk, from_variant, bviews._plan_key_to_variant()[target], estimate)


@override_settings(ENABLE_BILLING=True)
def test_change_plan_pending_marker_refuses_before_any_ls_call():
    # kills x__change_plan__mutmut_50 (fast-path check for user None: the
    # locked re-check would still refuse, but only AFTER an LS round-trip)
    # and _59/_60 (message text)
    from apps.billing.models import PlanChangeRequest

    u = _pc_user("cp_pend")
    PlanChangeRequest.objects.create(
        user=u,
        ls_subscription_id="sub_mk",
        from_plan="paid",
        to_plan="business",
        from_variant=PAID_V,
        to_variant=BUSINESS_V,
    )
    resp, g, p = _confirm(u, "business")
    assert (resp.status_code, resp.url) == (303, BILLING_URL)
    assert _msgs(resp) == [IN_PROGRESS_TEXT]
    g.assert_not_called()
    p.assert_not_called()


@override_settings(ENABLE_BILLING=True)
def test_change_plan_second_confirm_for_the_same_change_is_refused_without_a_patch():
    # Behaviour, not call order: two confirms of the SAME pending change (the
    # second one submitted after the first committed its marker, e.g. a
    # double click / second tab with the same token). Only ONE LS PATCH and
    # ONE PlanChangeRequest marker; the second gets the exact in-progress
    # flash. A true two-transaction race is not reproducible inside a
    # pytest-django test transaction; this is the post-commit replay of it.
    # Kills x__change_plan__mutmut_275/276 (refusal text) together with the
    # fast-path pending tests above.
    from apps.billing.models import PlanChangeRequest

    u = _pc_user("cp_twice")
    token = _token(u, "business")
    patches = []
    first, _, _ = _confirm(u, "business", token=token, ls_patch=lambda *a, **k: patches.append(a))
    assert (first.status_code, first.url) == (303, BILLING_URL)
    second, g2, p2 = _confirm(u, "business", token=token, ls_patch=lambda *a, **k: patches.append(a))
    assert (second.status_code, second.url) == (303, BILLING_URL)
    assert _msgs(second) == [IN_PROGRESS_TEXT]
    p2.assert_not_called()
    assert len(patches) == 1
    assert list(PlanChangeRequest.objects.filter(user=u).values_list("to_plan", "status")) == [
        ("business", "submitted")
    ]


@override_settings(ENABLE_BILLING=True)
def test_change_plan_marker_committed_between_fast_path_and_lock_is_refused_without_a_patch():
    # The race window itself, with real DB state: another confirm commits its
    # marker AFTER this request passed the fast-path check but BEFORE it takes
    # the AccountPlan row lock (simulated by committing that marker from the
    # LS preflight GET, which runs exactly in that window). The locked
    # re-check must refuse: no PATCH, still exactly one marker (the other's).
    from apps.billing.models import PlanChangeRequest

    u = _pc_user("cp_race")
    live = _live()

    def preflight_then_other_tab_confirms(path, **kw):
        if not PlanChangeRequest.objects.filter(user=u).exists():
            PlanChangeRequest.objects.create(
                user=u,
                ls_subscription_id="sub_mk",
                from_plan="paid",
                to_plan="business",
                from_variant=PAID_V,
                to_variant=BUSINESS_V,
                status="submitted",
            )
        return live(path, **kw)

    resp, g, p = _confirm(u, "business", ls_get=preflight_then_other_tab_confirms)
    assert g.call_count == 1  # the fast path did NOT see a marker: we got to the preflight
    assert (resp.status_code, resp.url) == (303, BILLING_URL)
    assert _msgs(resp) == [IN_PROGRESS_TEXT]
    p.assert_not_called()
    assert PlanChangeRequest.objects.filter(user=u).count() == 1


@override_settings(ENABLE_BILLING=True)
@pytest.mark.parametrize(
    ("ls_get", "code", "text", "log"),
    [
        (_raise(_http_error(404)), 409, NOT_FOUND_TEXT, "billing_change_plan_preflight_404 user_pk={pk}"),
        (
            _raise(_http_error(500)),
            502,
            "Could not reach billing — please try again.",
            "billing_change_plan_preflight_failed user_pk={pk}",
        ),
        (
            _raise(TimeoutError("t")),
            502,
            "Could not reach billing — please try again.",
            "billing_change_plan_preflight_failed user_pk={pk}",
        ),
        (_live(cancelled=True), 409, CANCEL_TEXT, None),
        (_live(status="on_trial"), 409, None, None),
        (_live(status="past_due"), 409, STATUS_TEXT, None),
    ],
)
def test_change_plan_preflight_refusals_exact_flash_no_patch(caplog, ls_get, code, text, log):
    # kills x__change_plan__mutmut_67-70/72-86/89-92/94-103 (preflight 404/5xx/
    # timeout), _151-156 (cancelled), _164/_173 (trial), _183/_192/_195/_196
    # (live status): exact flash, real template, log line, NEVER a PATCH and
    # no PlanChangeRequest marker
    from apps.billing import views as bviews
    from apps.billing.models import PlanChangeRequest

    u = _pc_user(f"cp_pre_{code}_{bool(log)}_{text and text[:5]}")
    with caplog.at_level(logging.INFO, logger="apps.billing.views"), _Templates() as t:
        resp, g, p = _confirm(u, "business", ls_get=ls_get)
    assert (resp.status_code, resp.content) == (code, _flash_html(text or bviews.TRIAL_MESSAGE))
    assert t.names == ["dashboard/partials/flash.html"]
    if log:
        assert log.format(pk=u.pk) in _messages(caplog, "apps.billing.views")
    p.assert_not_called()
    assert not PlanChangeRequest.objects.filter(user=u).exists()


@override_settings(ENABLE_BILLING=True)
def test_change_plan_subscription_changed_since_page_message():
    # kills x__change_plan__mutmut_228/232-234
    u = _pc_user("cp_changed")
    resp, g, p = _confirm(u, "business", ls_get=_live(SCALE_V), from_variant=PAID_V)
    assert (resp.status_code, resp.url) == (303, f"{CHANGE_URL}?plan=business")
    assert _msgs(resp) == [
        "Your subscription changed since this page was opened — please review the new details and confirm "
        "again."
    ]
    p.assert_not_called()


@override_settings(ENABLE_BILLING=True)
def test_change_plan_already_on_target_message():
    # kills x__change_plan__mutmut_212 (message). _216/_218 (status=None /
    # dropped) are EQUIVALENT: HttpResponseRedirect defaults to 302.
    u = _pc_user("cp_already")
    resp, g, p = _confirm(u, "business", ls_get=_live(BUSINESS_V))
    assert (resp.status_code, resp.url) == (302, BILLING_URL)
    assert _msgs(resp) == ["You're already on this plan."]
    p.assert_not_called()


@override_settings(ENABLE_BILLING=True)
@pytest.mark.parametrize(
    ("exc", "code", "text", "status", "log"),
    [
        (_http_error(404), 409, NOT_FOUND_TEXT, "failed", "billing_change_plan_patch_404 user_pk={pk}"),
        (
            _http_error(402),
            402,
            "We couldn't charge your payment method for this change. Update it in the billing "
            "portal, then try again.",
            "failed",
            "billing_change_plan_patch_402 user_pk={pk}",
        ),
        (
            _http_error(400),
            502,
            "Could not change plan — please try again or contact support.",
            "failed",
            "billing_change_plan_patch_failed user_pk={pk}",
        ),
        (
            _http_error(499),
            502,
            "Could not change plan — please try again or contact support.",
            "failed",
            "billing_change_plan_patch_failed user_pk={pk}",
        ),
        (
            _http_error(500),
            502,
            "Could not change plan — please try again or contact support.",
            "unknown",
            "billing_change_plan_patch_failed user_pk={pk}",
        ),
        (
            TimeoutError("t"),
            502,
            "Could not reach billing — we can't tell yet whether the change went through. "
            "Check Recent payments on the billing page in a minute (we email a receipt if you "
            "were charged) before trying again.",
            "unknown",
            "billing_change_plan_patch_failed user_pk={pk}",
        ),
    ],
)
def test_change_plan_patch_failures_marker_status_flash_and_log(caplog, exc, code, text, status, log):
    # kills x__change_plan__mutmut_332-351 (404), _357-376 (402), _378-381
    # (4xx = failed / 5xx = unknown boundaries), _383 (_mark(None)), _385-404
    # (other HTTP errors), _408-432 (timeout: outcome unknown)
    from apps.billing.models import PlanChangeRequest

    u = _pc_user(f"cp_patch_{getattr(exc, 'code', 'to')}")
    with caplog.at_level(logging.INFO, logger="apps.billing.views"), _Templates() as t:
        resp, g, p = _confirm(u, "business", ls_patch=exc)
    assert (resp.status_code, resp.content) == (code, _flash_html(text))
    assert t.names == ["dashboard/partials/flash.html"]
    assert p.call_count == 1
    assert PlanChangeRequest.objects.get(user=u).status == status
    assert log.format(pk=u.pk) in _messages(caplog, "apps.billing.views")


@override_settings(ENABLE_BILLING=True)
def test_change_plan_marker_audit_fields_and_updated_at():
    # kills x__change_plan__mutmut_287/290/291/293/294 (audit fields; the
    # receipt label matches on to_variant) and _322 (_mark keeps updated_at).
    # Deterministic clock, no sleep: timezone.now returns T0 until the PATCH
    # has gone out, then T1. The marker's updated_at (auto_now) is T0 at
    # creation; _mark must stamp exactly T1. (created_at's default binds the
    # real timezone.now at import, so it is not pinned here.)
    from apps.billing.models import PlanChangeRequest

    t0 = datetime(2026, 10, 9, 1, 58, 20, tzinfo=dt_tz.utc)
    t1 = datetime(2026, 10, 9, 1, 58, 23, tzinfo=dt_tz.utc)
    clock = {"now": t0}

    def patched(*a, **k):
        clock["now"] = t1  # LS answered: every later timezone.now() is T1

    u = _pc_user("cp_audit", sub_id="sub_audit")
    token = _token(u, "business")
    seen = []

    def patched_and_record(*a, **k):
        seen.append(PlanChangeRequest.objects.get(user=u).updated_at)  # before _mark
        patched()

    # apps.billing.views.timezone is django.utils.timezone: this also drives
    # the auto_now stamp of the INSERT
    with patch("apps.billing.views.timezone.now", side_effect=lambda: clock["now"]):
        resp, g, p = _confirm(u, "business", token=token, ls_patch=patched_and_record)
    assert resp.status_code == 303
    r = PlanChangeRequest.objects.get(user=u)
    assert (
        r.ls_subscription_id,
        r.from_plan,
        r.to_plan,
        r.from_variant,
        r.to_variant,
        r.estimate_cents,
        r.status,
    ) == ("sub_audit", "paid", "business", PAID_V, BUSINESS_V, 13115, "submitted")
    assert (seen, r.updated_at) == ([t0], t1)


@override_settings(ENABLE_BILLING=True)
@pytest.mark.parametrize(
    ("stored_plan", "target", "downgrade"),
    [("business", "paid", True), ("paid", "paid", False), ("paid", "business", False)],
)
def test_change_plan_unknown_live_variant_uses_stored_plan_for_direction(stored_plan, target, downgrade):
    # kills x__change_plan__mutmut_236/237/239 (fallback to the stored plan
    # dropped: a DOWNGRADE from a legacy/unknown variant would be sent as an
    # upgrade with invoice_immediately = an immediate charge) and _247 (< vs <=)
    from apps.billing.models import PlanChangeRequest

    legacy = "999001"
    u = _pc_user(f"cp_legacy_{stored_plan}_{target}", plan=stored_plan, variant=legacy)
    resp, g, p = _confirm(u, target, ls_get=_live(legacy), from_variant=legacy, estimate=500)
    attrs = p.call_args.args[1]["data"]["attributes"]
    if downgrade:
        assert attrs == {"variant_id": int(PAID_V), "disable_prorations": True}
    else:
        assert attrs["invoice_immediately"] is True and "disable_prorations" not in attrs
    r = PlanChangeRequest.objects.get(user=u)
    assert (r.from_plan, r.estimate_cents) == (stored_plan, 0 if downgrade else 500)


@override_settings(ENABLE_BILLING=True)
def test_change_plan_downgrade_never_records_an_estimate_and_falls_back_to_next_renewal():
    # kills x__change_plan__mutmut_255/257 (downgrade estimate = token value
    # or 1 cent) and _463 ("your next renewal" when LS has no renews_at)
    from apps.billing.models import PlanChangeRequest

    u = _pc_user("cp_down", plan="business", variant=BUSINESS_V)
    resp, g, p = _confirm(
        u, "paid", ls_get=_live(BUSINESS_V, renews_at=None), from_variant=BUSINESS_V, estimate=999
    )
    assert PlanChangeRequest.objects.get(user=u).estimate_cents == 0
    assert _msgs(resp) == [
        "Your plan changed to Pro now — Business limits end immediately. "
        "No charge now; your new price applies from your next renewal."
    ]


@override_settings(ENABLE_BILLING=True)
def test_change_plan_upgrade_zero_estimate_and_no_card():
    # kills x__change_plan__mutmut_263 (`or 1`: a 0 estimate becomes 1 cent)
    # and _478 (no card on file -> "Your payment method", not "Your card ")
    from apps.billing.models import PlanChangeRequest

    u = _pc_user("cp_zero")
    resp, g, p = _confirm(u, "business", ls_get=_live(last_four=""), estimate=0)
    assert PlanChangeRequest.objects.get(user=u).estimate_cents == 0
    assert _msgs(resp) == [
        "Upgrade to Business confirmed. Your payment method is being charged about $0.00; "
        "the receipt will be emailed to you."
    ]


@override_settings(ENABLE_BILLING=True)
@pytest.mark.parametrize(
    ("brand", "payer"),
    [
        # an unknown multi-word LS brand: _card_label gives "Diners Club ••1234";
        # the flash keeps everything after the FIRST space (current behaviour,
        # pinned as-is; cosmetic, see the r2 report)
        ("diners club", "Your card Club ••1234"),
        ("Diners Club", "Your card Club ••1234"),
        ("visa", "Your card ••1234"),  # a known one-word brand
    ],
)
def test_change_plan_upgrade_flash_card_label(brand, payer):
    # kills x__change_plan__mutmut_483 (split(" ", 1) -> rsplit(" ", 1): an
    # unknown multi-word brand would print "Your card ••1234"). NOT equivalent
    
    u = _pc_user(f"cp_card_{brand.replace(' ', '_')}")
    resp, g, p = _confirm(u, "business", ls_get=_live(brand=brand, last_four="1234"), estimate=13115)
    assert (resp.status_code, resp.url) == (303, BILLING_URL)
    assert _msgs(resp) == [
        f"Upgrade to Business confirmed. {payer} is being charged about $131.15; "
        "the receipt will be emailed to you."
    ]


@override_settings(ENABLE_BILLING=True)
def test_confirm_view_method_not_allowed_and_head_allowed():
    # kills x_change_plan_confirm__mutmut_30/31 (HEAD refused) and _32-36
    # (405 without the right Allow header)
    u = _pc_user("cf_methods")
    resp, _, _ = _confirm_get(u, method="put")
    assert (resp.status_code, resp["Allow"]) == (405, "GET, POST")
    resp, _, _ = _confirm_get(u, method="head")
    assert resp.status_code == 200


@override_settings(ENABLE_BILLING=True)
def test_confirm_view_pending_page():
    # kills x_change_plan_confirm__mutmut_80/81/88
    from apps.billing.models import PlanChangeRequest

    u = _pc_user("cf_pending")
    PlanChangeRequest.objects.create(
        user=u,
        ls_subscription_id="sub_mk",
        from_plan="paid",
        to_plan="business",
        from_variant=PAID_V,
        to_variant=BUSINESS_V,
    )
    resp, g, names = _confirm_get(u, "scale")
    assert resp.status_code == 200 and names[0] == "dashboard/billing_change.html"
    assert resp.context["user"] == u  # rendered WITH the request (context processors ran)
    assert "Your change to Business is being processed" in resp.content.decode()
    g.assert_not_called()


@override_settings(ENABLE_BILLING=True)
def test_confirm_view_quote_error_generic_text_and_log(caplog):
    # kills x_change_plan_confirm__mutmut_57 (target_name), _97-104 (log),
    # _110-118 (generic text), _122/_129 (render)
    import urllib.error

    u = _pc_user("cf_qerr")
    with caplog.at_level(logging.WARNING, logger="apps.billing.views"):
        resp, g, names = _confirm_get(u, ls_get=_raise(urllib.error.URLError("down")))
    html = resp.content.decode()
    assert resp.status_code == 200 and names[0] == "dashboard/billing_change.html"
    assert resp.context["user"] == u
    assert "Change to Business" in html
    from django.utils.html import escape

    assert (
        escape(
            "We couldn't load your current subscription details from our billing provider, so we can't "
            "show what you'd be charged. Nothing has been changed or charged. Please try again in a minute."
        )
        in html
    )
    assert _messages(caplog, "apps.billing.views") == [
        f"billing_change_quote_failed user_pk={u.pk} plan=business reason=subscription unreachable"
    ]


@override_settings(ENABLE_BILLING=True)
def test_confirm_view_already_and_cancelled():
    # kills x_change_plan_confirm__mutmut_134-139 (already message) and
    # _143/_149/_150 (cancelled flash)
    u = _pc_user("cf_already")
    resp, _, _ = _confirm_get(u, ls_get=_live(BUSINESS_V))
    assert (resp.status_code, resp.url) == (303, BILLING_URL)
    assert _msgs(resp) == ["You're already on this plan."]
    resp, _, names = _confirm_get(u, ls_get=_live(cancelled=True))
    assert (resp.status_code, resp.content) == (409, _flash_html(CANCEL_TEXT))
    assert resp.context["user"] == u and names == ["dashboard/partials/flash.html"]


@override_settings(ENABLE_BILLING=True)
def test_confirm_view_ok_page_template():
    # kills x_change_plan_confirm__mutmut_178
    from apps.billing.tests.test_billing_confirm import fake_ls_get, sub_attrs

    u = _pc_user("cf_ok")
    with patch(
        "apps.billing.views.timezone.now", return_value=datetime(2026, 10, 9, 1, 58, tzinfo=dt_tz.utc)
    ):
        resp, _, names = _confirm_get(u, ls_get=fake_ls_get(sub_attrs()))
    assert resp.status_code == 200 and names[0] == "dashboard/billing_change.html"
    assert resp.context["token"]


@override_settings(ENABLE_BILLING=True)
def test_change_plan_live_status_cancelled_without_flag_refused():
    # kills x__change_plan__mutmut_136-141 (LS status "cancelled" with
    # cancelled=false must still refuse, never PATCH)
    u = _pc_user("cp_status_cancel")
    resp, g, p = _confirm(u, "business", ls_get=_live(status="cancelled", cancelled=False))
    assert (resp.status_code, resp.content) == (409, _flash_html(CANCEL_TEXT))
    p.assert_not_called()


@override_settings(ENABLE_BILLING=True)
def test_change_plan_direction_comes_from_the_live_variant_not_the_stale_stored_plan():
    # kills x__change_plan__mutmut_236 (live variant ignored -> stored plan
    # decides): LS already has Business (webhook not landed, we still store
    # Pro); Business -> Scale is a DOWNGRADE and must never be PATCHed with
    # invoice_immediately (= an immediate charge)
    from apps.billing.models import PlanChangeRequest

    u = _pc_user("cp_stale", plan="paid", variant=PAID_V)
    resp, g, p = _confirm(u, "scale", ls_get=_live(BUSINESS_V), from_variant=BUSINESS_V, estimate=999)
    assert p.call_args.args[1]["data"]["attributes"] == {
        "variant_id": int(SCALE_V),
        "disable_prorations": True,
    }
    r = PlanChangeRequest.objects.get(user=u)
    assert (r.from_plan, r.estimate_cents) == ("business", 0)
