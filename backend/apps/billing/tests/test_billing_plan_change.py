"Public API."
import json
import urllib.error
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan, TeamMembership
from apps.billing.views import _RANK

pytestmark = pytest.mark.django_db(transaction=True)

LS_SECRET = "test-ls-webhook-secret"


def make_user(name, *, plan=AccountPlan.Plan.PAID, status=AccountPlan.Status.ACTIVE,
              sub_id="sub_1", price_id="2176466", email=None):
    user = get_user_model().objects.create_user(
        username=name, email=email or f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=email or f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.filter(user=user).delete()
    AccountPlan.objects.create(
        user=user, plan=plan, status=status,
        stripe_subscription_id=sub_id, stripe_price_id=price_id,
        stripe_customer_id="cust_1" if sub_id else "",
    )
    return user


def login(user):
    client = Client()
    client.force_login(user)
    return client


def _plan_snapshot(user):
    plan = AccountPlan.objects.get(user=user)
    return (
        plan.plan,
        plan.status,
        plan.stripe_customer_id,
        plan.stripe_subscription_id,
        plan.stripe_price_id,
        plan.current_period_start,
        plan.current_period_end,
        plan.billing_synced_at,
    )


def http_error(code):
    return urllib.error.HTTPError("https://api.lemonsqueezy.com/v1/x", code, "err", {}, None)


def preflight_ok(variant_id="2176466", cancelled=False, status="active"):
    return {"data": {"attributes": {"variant_id": variant_id, "cancelled": cancelled, "status": status}}}


def confirm_post(client, user, plan_key, *, follow=False, from_variant=None):
    """billing-confirm: the ONLY PATCH path is the confirm POST carrying a
    fresh signed token for (user, live current variant, target variant).
    Mints the token the confirm page would have rendered."""
    from apps.billing.views import _make_token, _plan_key_to_variant

    if from_variant is None:
        from_variant = AccountPlan.objects.get(user=user).stripe_price_id
    token = _make_token(user.pk, from_variant, _plan_key_to_variant()[plan_key], 0)
    return client.post(
        reverse("dashboard:billing-change"), {"plan": plan_key, "token": token}, follow=follow
    )


CHECKOUT_URL = reverse.__module__  # placeholder unused, keep import tidy


# --- 1: existing-sub upgrade -> PATCH, no checkout, 303, AccountPlan unchanged


@override_settings(ENABLE_BILLING=True)
def test_1_existing_sub_active_upgrade_patches_no_checkout_303_plan_unchanged():
    user = make_user("pc1", plan=AccountPlan.Plan.PAID, sub_id="sub_pc1", price_id="2176466")
    client = login(user)
    before = _plan_snapshot(user)

    with patch("apps.billing.views._ls_get", return_value=preflight_ok(variant_id="2176466")) as mock_get, \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = confirm_post(client, user, "scale")

    assert resp.status_code == 303
    assert resp.url == reverse("dashboard:billing-page")
    mock_post.assert_not_called()
    mock_patch.assert_called_once()
    call_path, call_body = mock_patch.call_args[0]
    assert call_path == "/subscriptions/sub_pc1"
    attrs = call_body["data"]["attributes"]
    assert attrs["variant_id"] == 2176478
    assert attrs["invoice_immediately"] is True
    assert "disable_prorations" not in attrs
    assert call_body["data"]["id"] == "sub_pc1"
    mock_get.assert_called_once_with("/subscriptions/sub_pc1")
    assert _plan_snapshot(user) == before


# --- 2: no-sub -> checkout regression pin


@override_settings(ENABLE_BILLING=True)
def test_2_no_sub_checkout_regression_pin():
    user = make_user("pc2", plan=AccountPlan.Plan.FREE, sub_id=None, price_id="")
    client = login(user)
    captured = {}

    def fake_post(path, body, **kwargs):
        captured["path"] = path
        captured["body"] = body
        return {"data": {"attributes": {"url": "https://checkout.example/pc2"}}}

    with patch("apps.billing.views._ls_post", side_effect=fake_post) as mock_post, \
            patch("apps.billing.views._ls_patch") as mock_patch:
        resp = client.post(reverse("dashboard:billing-checkout"), {"plan": "paid"})

    assert resp.status_code == 303
    assert resp.url == "https://checkout.example/pc2"
    mock_patch.assert_not_called()
    mock_post.assert_called_once()
    assert captured["path"] == "/checkouts"
    custom = captured["body"]["data"]["attributes"]["checkout_data"]["custom"]
    assert custom["user_pk"] == str(user.pk)


# --- 3: expired (id cleared) -> checkout, not PATCH


@override_settings(ENABLE_BILLING=True)
def test_3_expired_account_id_cleared_checkout_not_patch():
    user = make_user("pc3", plan=AccountPlan.Plan.FREE, sub_id=None, price_id=None)
    client = login(user)

    with patch("apps.billing.views._ls_post",
               return_value={"data": {"attributes": {"url": "https://checkout.example/pc3"}}}) as mock_post, \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_get") as mock_get:
        resp = client.post(reverse("dashboard:billing-checkout"), {"plan": "paid"})

    assert resp.status_code == 303
    mock_post.assert_called_once()
    mock_patch.assert_not_called()
    mock_get.assert_not_called()





@override_settings(ENABLE_BILLING=True)
def test_4_demo_agent_and_nonowner_member_403_no_ls_calls():
    from apps.accounts.services import AGENT_ACCOUNT_DOMAIN

    agent_user = make_user("pc4agent", email=f"agent@{AGENT_ACCOUNT_DOMAIN}", sub_id="sub_agent")
    client = login(agent_user)
    with patch("apps.billing.views._ls_get") as mock_get, \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = client.post(reverse("dashboard:billing-checkout"), {"plan": "scale"})
    assert resp.status_code == 403
    mock_get.assert_not_called()
    mock_patch.assert_not_called()
    mock_post.assert_not_called()

    owner = make_user("pc4owner", plan=AccountPlan.Plan.SCALE, sub_id="sub_owner4")
    member = make_user("pc4member", plan=AccountPlan.Plan.FREE, sub_id=None, price_id="")
    with override_settings(ENABLE_TEAMS=True):
        TeamMembership.objects.create(
            account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER
        )
        member_client = login(member)
        with patch("apps.billing.views._ls_get") as mock_get2, \
                patch("apps.billing.views._ls_patch") as mock_patch2, \
                patch("apps.billing.views._ls_post") as mock_post2:
            resp2 = member_client.post(reverse("dashboard:billing-checkout"), {"plan": "scale"})
        assert resp2.status_code == 403
        mock_get2.assert_not_called()
        mock_patch2.assert_not_called()
        mock_post2.assert_not_called()


# --- 5: past_due -> 409 no LS calls (reachable via HTTP: dashboard_required
# admits PAST_DUE); suspended -> 409 no LS calls, tested at the _change_plan
# unit level via RequestFactory since dashboard_required (views.py:684-687)
# only admits ACTIVE/PAST_DUE tenants and a SUSPENDED account can never reach
# checkout_create through the real HTTP path at all — the defensive
# status != ACTIVE check inside _change_plan still must refuse it directly.


@override_settings(ENABLE_BILLING=True)
def test_5_past_due_409_no_ls_calls_http():
    user_pd = make_user("pc5pd", plan=AccountPlan.Plan.PAID, status=AccountPlan.Status.PAST_DUE, sub_id="sub_pd")
    client_pd = login(user_pd)
    before = _plan_snapshot(user_pd)
    with patch("apps.billing.views._ls_get") as mock_get, \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = client_pd.post(reverse("dashboard:billing-checkout"), {"plan": "scale"})
    assert resp.status_code == 409
    mock_get.assert_not_called()
    mock_patch.assert_not_called()
    mock_post.assert_not_called()
    assert _plan_snapshot(user_pd) == before


@override_settings(ENABLE_BILLING=True)
def test_5b_suspended_409_no_ls_calls_change_plan_unit():
    from django.contrib.auth.middleware import AuthenticationMiddleware
    from django.contrib.sessions.middleware import SessionMiddleware
    from django.test import RequestFactory

    from apps.billing.views import _change_plan

    user_sus = make_user("pc5sus", plan=AccountPlan.Plan.PAID, status=AccountPlan.Status.SUSPENDED, sub_id="sub_sus")
    plan = AccountPlan.objects.get(user=user_sus)
    before = _plan_snapshot(user_sus)
    rf = RequestFactory()
    request = rf.post(reverse("dashboard:billing-checkout"), {"plan": "scale"})
    SessionMiddleware(lambda r: None).process_request(request)
    AuthenticationMiddleware(lambda r: None).process_request(request)
    request.user = user_sus

    with patch("apps.billing.views._ls_get") as mock_get, \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = _change_plan(request, plan, "scale", "2176478")
    assert resp.status_code == 409
    mock_get.assert_not_called()
    mock_patch.assert_not_called()
    mock_post.assert_not_called()
    assert _plan_snapshot(user_sus) == before


# --- 6: pre-flight 404 -> 409 support flash, no PATCH, no checkout, unchanged


@override_settings(ENABLE_BILLING=True)
def test_6_preflight_404_409_no_patch_no_checkout_unchanged():
    user = make_user("pc6", plan=AccountPlan.Plan.PAID, sub_id="sub_pc6")
    client = login(user)
    before = _plan_snapshot(user)

    with patch("apps.billing.views._ls_get", side_effect=http_error(404)) as mock_get, \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = confirm_post(client, user, "scale")

    assert resp.status_code == 409
    mock_get.assert_called_once()
    mock_patch.assert_not_called()
    mock_post.assert_not_called()
    assert _plan_snapshot(user) == before


# --- 7: pre-flight cancelled -> 409 resume, no PATCH


@override_settings(ENABLE_BILLING=True)
def test_7a_preflight_cancelled_true_status_active_409_resume_no_patch():
    user = make_user("pc7a", plan=AccountPlan.Plan.PAID, sub_id="sub_pc7a")
    client = login(user)
    before = _plan_snapshot(user)

    with patch("apps.billing.views._ls_get",
               return_value=preflight_ok(variant_id="2176466", cancelled=True, status="active")), \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = confirm_post(client, user, "scale")

    assert resp.status_code == 409
    mock_patch.assert_not_called()
    mock_post.assert_not_called()
    assert _plan_snapshot(user) == before


@override_settings(ENABLE_BILLING=True)
def test_7b_preflight_status_cancelled_cancelled_false_409_resume_no_patch():
    user = make_user("pc7b", plan=AccountPlan.Plan.PAID, sub_id="sub_pc7b")
    client = login(user)
    before = _plan_snapshot(user)

    with patch("apps.billing.views._ls_get",
               return_value=preflight_ok(variant_id="2176466", cancelled=False, status="cancelled")), \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = confirm_post(client, user, "scale")

    assert resp.status_code == 409
    mock_patch.assert_not_called()
    mock_post.assert_not_called()
    assert _plan_snapshot(user) == before


# --- 8: pre-flight variant == target -> already on this plan, no PATCH


@override_settings(ENABLE_BILLING=True)
def test_8_preflight_variant_equals_target_already_on_plan_no_patch():
    user = make_user("pc8", plan=AccountPlan.Plan.SCALE, sub_id="sub_pc8", price_id="2176478")
    client = login(user)
    before = _plan_snapshot(user)

    with patch("apps.billing.views._ls_get", return_value=preflight_ok(variant_id="2176478")), \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = confirm_post(client, user, "scale")

    assert resp.status_code == 302
    assert resp.url == reverse("dashboard:billing-page")
    mock_patch.assert_not_called()
    mock_post.assert_not_called()
    assert _plan_snapshot(user) == before


# --- 9: double-submit (billing-confirm): the second confirm POST is refused by
# the pending marker — exactly ONE PATCH, handler wrote nothing to AccountPlan.


@override_settings(ENABLE_BILLING=True)
def test_9_double_submit_second_confirm_refused_one_patch_wrote_nothing():
    # BL-02: billing-lifecycle scenario (skill billing-lifecycle)
    user = make_user("pc9", plan=AccountPlan.Plan.PAID, sub_id="sub_pc9")
    client = login(user)
    before = _plan_snapshot(user)

    with patch("apps.billing.views._ls_get", return_value=preflight_ok(variant_id="2176466")), \
            patch("apps.billing.views._ls_patch") as mock_patch:
        resp1 = confirm_post(client, user, "scale")
        resp2 = confirm_post(client, user, "scale")

    assert resp1.status_code == 303
    assert resp2.status_code == 303
    assert resp2.url == reverse("dashboard:billing-page")
    assert mock_patch.call_count == 1
    path, body = mock_patch.call_args[0]
    assert path == "/subscriptions/sub_pc9"
    assert body["data"]["attributes"]["variant_id"] == 2176478
    assert _plan_snapshot(user) == before


# --- 10: downgrade key posted direct -> disable_prorations, no invoice_immediately, next-renewal flash


@override_settings(ENABLE_BILLING=True)
def test_10_downgrade_key_direct_disable_prorations_next_renewal_flash():
    user = make_user("pc10", plan=AccountPlan.Plan.SCALE, sub_id="sub_pc10", price_id="2176478")
    client = login(user)
    assert _RANK["paid"] < _RANK["scale"]
    before = _plan_snapshot(user)

    with patch("apps.billing.views._ls_get", return_value=preflight_ok(variant_id="2176478")), \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = confirm_post(client, user, "paid", follow=True)

    assert resp.redirect_chain[0][1] == 303
    mock_patch.assert_called_once_with(
        "/subscriptions/sub_pc10",
        {
            "data": {
                "type": "subscriptions",
                "id": "sub_pc10",
                "attributes": {"variant_id": 2176466, "disable_prorations": True},
            },
        },
    )
    mock_post.assert_not_called()
    _, body = mock_patch.call_args[0]
    attrs = body["data"]["attributes"]
    assert attrs["variant_id"] == 2176466
    assert attrs["disable_prorations"] is True
    assert "invoice_immediately" not in attrs
    messages = list(resp.context["messages"]) if hasattr(resp, "context") and resp.context else []
    assert any("next renewal" in str(m) for m in messages)
    assert _plan_snapshot(user) == before


# --- 11: PATCH 402 -> 402 flash; PATCH 422 -> 502; timeout -> 502; AccountPlan unchanged in all


@override_settings(ENABLE_BILLING=True)
def test_11_patch_402_422_timeout_mapped_plan_unchanged():
    import urllib.error as ue

    user_402 = make_user("pc11a", plan=AccountPlan.Plan.PAID, sub_id="sub_11a")
    client_402 = login(user_402)
    before_402 = _plan_snapshot(user_402)
    with patch("apps.billing.views._ls_get", return_value=preflight_ok(variant_id="2176466")), \
            patch("apps.billing.views._ls_patch", side_effect=http_error(402)) as mock_patch_402, \
            patch("apps.billing.views._ls_post") as mock_post_402:
        resp = confirm_post(client_402, user_402, "scale")
    assert resp.status_code == 402
    assert mock_patch_402.call_count == 1
    body_402 = mock_patch_402.call_args[0][1]
    assert body_402["data"]["id"] == "sub_11a"
    assert body_402["data"]["attributes"]["variant_id"] == 2176478
    mock_post_402.assert_not_called()
    assert _plan_snapshot(user_402) == before_402

    user_422 = make_user("pc11b", plan=AccountPlan.Plan.PAID, sub_id="sub_11b")
    client_422 = login(user_422)
    before_422 = _plan_snapshot(user_422)
    with patch("apps.billing.views._ls_get", return_value=preflight_ok(variant_id="2176466")), \
            patch("apps.billing.views._ls_patch", side_effect=http_error(422)) as mock_patch_422, \
            patch("apps.billing.views._ls_post") as mock_post_422:
        resp2 = confirm_post(client_422, user_422, "scale")
    assert resp2.status_code == 502
    assert mock_patch_422.call_count == 1
    body_422 = mock_patch_422.call_args[0][1]
    assert body_422["data"]["id"] == "sub_11b"
    assert body_422["data"]["attributes"]["variant_id"] == 2176478
    mock_post_422.assert_not_called()
    assert _plan_snapshot(user_422) == before_422

    user_to = make_user("pc11c", plan=AccountPlan.Plan.PAID, sub_id="sub_11c")
    client_to = login(user_to)
    before_to = _plan_snapshot(user_to)
    with patch("apps.billing.views._ls_get", return_value=preflight_ok(variant_id="2176466")), \
            patch("apps.billing.views._ls_patch", side_effect=ue.URLError("timeout")) as mock_patch_to, \
            patch("apps.billing.views._ls_post") as mock_post_to:
        resp3 = confirm_post(client_to, user_to, "scale")
    assert resp3.status_code == 502
    assert mock_patch_to.call_count == 1
    body_to = mock_patch_to.call_args[0][1]
    assert body_to["data"]["id"] == "sub_11c"
    assert body_to["data"]["attributes"]["variant_id"] == 2176478
    mock_post_to.assert_not_called()
    assert _plan_snapshot(user_to) == before_to


# --- 11b: PATCH 404 -> 409 support flash, PATCH attempted correct sub id, no checkout fallback


@override_settings(ENABLE_BILLING=True)
def test_11b_patch_404_409_patch_attempted_no_checkout_fallback_unchanged():
    user = make_user("pc11d", plan=AccountPlan.Plan.PAID, sub_id="sub_11d")
    client = login(user)
    before = _plan_snapshot(user)

    with patch("apps.billing.views._ls_get", return_value=preflight_ok(variant_id="2176466")), \
            patch("apps.billing.views._ls_patch", side_effect=http_error(404)) as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = confirm_post(client, user, "scale")

    assert resp.status_code == 409
    assert mock_patch.call_count == 1
    body = mock_patch.call_args[0][1]
    assert body["data"]["id"] == "sub_11d"
    assert body["data"]["attributes"]["variant_id"] == 2176478
    mock_post.assert_not_called()
    assert _plan_snapshot(user) == before


# --- 11c: pre-flight non-404 HTTPError (500) -> 502, no PATCH, no checkout, unchanged


@override_settings(ENABLE_BILLING=True)
def test_11c_preflight_http_error_500_502_no_patch_no_checkout_unchanged():
    # BL-23: billing-lifecycle scenario (skill billing-lifecycle)
    user = make_user("pc11e", plan=AccountPlan.Plan.PAID, sub_id="sub_11e")
    client = login(user)
    before = _plan_snapshot(user)

    with patch("apps.billing.views._ls_get", side_effect=http_error(500)) as mock_get, \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = confirm_post(client, user, "scale")

    assert resp.status_code == 502
    mock_get.assert_called_once_with("/subscriptions/sub_11e")
    mock_patch.assert_not_called()
    mock_post.assert_not_called()
    assert _plan_snapshot(user) == before


# --- 11d: pre-flight URLError/timeout -> 502, no PATCH, no checkout, unchanged


@override_settings(ENABLE_BILLING=True)
def test_11d_preflight_urlerror_timeout_502_no_patch_no_checkout_unchanged():
    # BL-23: billing-lifecycle scenario (skill billing-lifecycle)
    import urllib.error as ue

    user = make_user("pc11f", plan=AccountPlan.Plan.PAID, sub_id="sub_11f")
    client = login(user)
    before = _plan_snapshot(user)

    with patch("apps.billing.views._ls_get", side_effect=ue.URLError("timeout")) as mock_get, \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = confirm_post(client, user, "scale")

    assert resp.status_code == 502
    mock_get.assert_called_once_with("/subscriptions/sub_11f")
    mock_patch.assert_not_called()
    mock_post.assert_not_called()
    assert _plan_snapshot(user) == before

    user_to = make_user("pc11i", plan=AccountPlan.Plan.PAID, sub_id="sub_11i")
    client_to = login(user_to)
    before_to = _plan_snapshot(user_to)

    with patch("apps.billing.views._ls_get", side_effect=TimeoutError("timed out")) as mock_get_to, \
            patch("apps.billing.views._ls_patch") as mock_patch_to, \
            patch("apps.billing.views._ls_post") as mock_post_to:
        resp_to = confirm_post(client_to, user_to, "scale")

    assert resp_to.status_code == 502
    mock_get_to.assert_called_once_with("/subscriptions/sub_11i")
    mock_patch_to.assert_not_called()
    mock_post_to.assert_not_called()
    assert _plan_snapshot(user_to) == before_to


# --- 11e: malformed pre-flight body (missing data/attributes or non-dict) -> 502 fail-closed
# (Part A regression pin: this test MUST fail with 500 before the views.py fix, pass after)


@override_settings(ENABLE_BILLING=True)
def test_11e_preflight_malformed_body_502_fail_closed_no_patch_no_checkout_unchanged():
    user_empty = make_user("pc11g", plan=AccountPlan.Plan.PAID, sub_id="sub_11g")
    client_empty = login(user_empty)
    before_empty = _plan_snapshot(user_empty)

    with patch("apps.billing.views._ls_get", return_value={}) as mock_get, \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = confirm_post(client_empty, user_empty, "scale")

    assert resp.status_code == 502
    mock_get.assert_called_once_with("/subscriptions/sub_11g")
    mock_patch.assert_not_called()
    mock_post.assert_not_called()
    assert _plan_snapshot(user_empty) == before_empty

    user_nondict = make_user("pc11h", plan=AccountPlan.Plan.PAID, sub_id="sub_11h")
    client_nondict = login(user_nondict)
    before_nondict = _plan_snapshot(user_nondict)

    with patch("apps.billing.views._ls_get", return_value={"data": "x"}) as mock_get2, \
            patch("apps.billing.views._ls_patch") as mock_patch2, \
            patch("apps.billing.views._ls_post") as mock_post2:
        resp2 = confirm_post(client_nondict, user_nondict, "scale")

    assert resp2.status_code == 502
    mock_get2.assert_called_once_with("/subscriptions/sub_11h")
    mock_patch2.assert_not_called()
    mock_post2.assert_not_called()
    assert _plan_snapshot(user_nondict) == before_nondict

    user_none_attrs = make_user("pc11j", plan=AccountPlan.Plan.PAID, sub_id="sub_11j")
    client_none_attrs = login(user_none_attrs)
    before_none_attrs = _plan_snapshot(user_none_attrs)

    with patch("apps.billing.views._ls_get", return_value={"data": {"attributes": None}}) as mock_get3, \
            patch("apps.billing.views._ls_patch") as mock_patch3, \
            patch("apps.billing.views._ls_post") as mock_post3:
        resp3 = confirm_post(client_none_attrs, user_none_attrs, "scale")

    assert resp3.status_code == 502
    mock_get3.assert_called_once_with("/subscriptions/sub_11j")
    mock_patch3.assert_not_called()
    mock_post3.assert_not_called()
    assert _plan_snapshot(user_none_attrs) == before_none_attrs

    user_list_attrs = make_user("pc11k", plan=AccountPlan.Plan.PAID, sub_id="sub_11k")
    client_list_attrs = login(user_list_attrs)
    before_list_attrs = _plan_snapshot(user_list_attrs)

    with patch("apps.billing.views._ls_get", return_value={"data": {"attributes": []}}) as mock_get4, \
            patch("apps.billing.views._ls_patch") as mock_patch4, \
            patch("apps.billing.views._ls_post") as mock_post4:
        resp4 = confirm_post(client_list_attrs, user_list_attrs, "scale")

    assert resp4.status_code == 502
    mock_get4.assert_called_once_with("/subscriptions/sub_11k")
    mock_patch4.assert_not_called()
    mock_post4.assert_not_called()
    assert _plan_snapshot(user_list_attrs) == before_list_attrs


# --- 11f: pre-flight body missing variant_id (dict, otherwise well-formed) -> 502 fail-closed
# (Part A regression pin: this test MUST fail before the views.py variant_id guard, pass after)


@override_settings(ENABLE_BILLING=True)
def test_11f_preflight_missing_variant_id_502_fail_closed_no_patch_no_checkout_unchanged():
    user = make_user("pc11f", plan=AccountPlan.Plan.PAID, sub_id="sub_11f")
    client = login(user)
    before = _plan_snapshot(user)

    with patch("apps.billing.views._ls_get",
               return_value={"data": {"attributes": {"status": "active", "cancelled": False}}}) as mock_get, \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = confirm_post(client, user, "scale")

    assert resp.status_code == 502
    mock_get.assert_called_once_with("/subscriptions/sub_11f")
    mock_patch.assert_not_called()
    mock_post.assert_not_called()
    assert _plan_snapshot(user) == before


# --- 12: unknown plan key -> 400 (regression); flag off -> 404 (regression)


@override_settings(ENABLE_BILLING=True)
def test_12a_unknown_plan_key_400():
    user = make_user("pc12a", plan=AccountPlan.Plan.PAID, sub_id="sub_12a")
    client = login(user)
    with patch("apps.billing.views._ls_get") as mock_get, \
            patch("apps.billing.views._ls_patch") as mock_patch:
        resp = client.post(reverse("dashboard:billing-checkout"), {"plan": "enterprise"})
    assert resp.status_code == 400
    mock_get.assert_not_called()
    mock_patch.assert_not_called()


@override_settings(ENABLE_BILLING=False)
def test_12b_flag_off_404():
    user = make_user("pc12b", plan=AccountPlan.Plan.PAID, sub_id="sub_12b")
    client = login(user)
    with patch("apps.billing.views._ls_get") as mock_get, \
            patch("apps.billing.views._ls_patch") as mock_patch:
        resp = client.post(reverse("dashboard:billing-checkout"), {"plan": "scale"})
    assert resp.status_code == 404
    mock_get.assert_not_called()
    mock_patch.assert_not_called()
    # audit 2026-10-11 sweep: plan row untouched
    plan = AccountPlan.objects.get(user=user)
    assert (plan.plan, plan.stripe_subscription_id) == (AccountPlan.Plan.PAID, "sub_12b")


# --- 13: webhook subscription_updated must include a VARIANT-CHANGE case (not just status)


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=LS_SECRET)
def test_13_webhook_subscription_updated_variant_change_paid_to_scale():
    import hashlib
    import hmac as hmac_mod

    user = make_user("pc13", plan=AccountPlan.Plan.PAID, sub_id="sub_pc13", price_id="2176466")
    now = timezone.now()
    payload = {
        "meta": {
            "event_name": "subscription_updated",
            "webhook_id": "wh_pc13",
            "custom_data": {"user_pk": str(user.pk)},
        },
        "data": {
            "id": "sub_pc13",
            "attributes": {
                "status": "active",
                "variant_id": "2176478",
                "customer_id": "cust_1",
                "updated_at": now.isoformat(),
                "renews_at": (now + __import__("datetime").timedelta(days=30)).isoformat(),
            },
        },
    }
    body = json.dumps(payload).encode("utf-8")
    sig = hmac_mod.new(LS_SECRET.encode(), body, hashlib.sha256).hexdigest()
    client = Client()
    resp = client.post(
        "/billing/webhook/ls/", data=body, content_type="application/json", HTTP_X_SIGNATURE=sig,
    )
    assert resp.status_code == 200
    plan = AccountPlan.objects.get(user=user)
    assert plan.plan == AccountPlan.Plan.SCALE
    assert plan.stripe_price_id == "2176478"
