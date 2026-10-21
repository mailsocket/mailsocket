"Public API."
import json
import time
import urllib.error
from datetime import datetime, timedelta
from datetime import timezone as dt_tz
from unittest.mock import patch

import pytest
from django.core import mail
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AccountPlan, TeamMembership
from apps.billing import views as bviews
from apps.billing.models import BillingEvent, BillingPayment, PlanChangeRequest
from apps.billing.tests.test_billing_ls import SECRET, event_body, post_event
from apps.billing.tests.test_billing_plan_change import _plan_snapshot, http_error, login, make_user

pytestmark = pytest.mark.django_db(transaction=True)
PAID, SCALE, BUSINESS = "2176466", "2176478", "2187025"
PRICES = {PAID: 1200, SCALE: 4900, BUSINESS: 14900}

# The real incident (): Pro -> Business clicked 2026-10-09
# 01:58:20 UTC, LS renews_at 2026-11-07 18:12 UTC, LS charged $131.15.
INCIDENT_NOW = datetime(2026, 10, 9, 1, 58, 20, tzinfo=dt_tz.utc)
INCIDENT_RENEWS = "2026-11-07T18:12:00.000000Z"

CHANGE_URL = reverse("dashboard:billing-change")
BILLING_URL = reverse("dashboard:billing-page")


def sub_attrs(variant=PAID, *, renews_at=INCIDENT_RENEWS, created_at="2026-06-07T18:12:00.000000Z",
              status="active", cancelled=False, brand="visa", last_four="6770"):
    return {
        "variant_id": int(variant), "status": status, "cancelled": cancelled,
        "card_brand": brand, "card_last_four": last_four,
        "renews_at": renews_at, "created_at": created_at,
    }


def fake_ls_get(sub=None, *, fail_variants=False, sub_exc=None, variants=None):
    """Route-aware _ls_get mock: /subscriptions/{id} and /variants/{id}.
    ``variants`` overrides attributes per variant id (e.g. a yearly one)."""
    sub = sub if sub is not None else sub_attrs()
    calls = []

    def _get(path, **kwargs):
        calls.append(path)
        if path.startswith("/subscriptions/"):
            if sub_exc is not None:
                raise sub_exc
            return {"data": {"attributes": dict(sub)}}
        if path.startswith("/variants/"):
            if fail_variants:
                raise urllib.error.URLError("down")
            vid = path.rsplit("/", 1)[1]
            attrs = {"price": PRICES[vid], "is_subscription": True, "interval": "month", "interval_count": 1}
            attrs.update((variants or {}).get(vid, {}))
            return {"data": {"attributes": attrs}}
        raise AssertionError(f"unexpected LS GET {path}")

    _get.calls = calls
    return _get


def pro_user(name, sub_id="sub_2589120", **kw):
    return make_user(name, plan=AccountPlan.Plan.PAID, sub_id=sub_id, price_id=PAID, **kw)


def token_for(user, target, *, from_variant=PAID, estimate=13115):
    return bviews._make_token(user.pk, from_variant, target, estimate)


def at_incident_time():
    return patch("apps.billing.views.timezone.now", return_value=INCIDENT_NOW)


# --- the estimate --------------------------------------------------------------


def test_estimate_real_incident_numbers_131_15():
    renews = datetime(2026, 11, 7, 18, 12, tzinfo=dt_tz.utc)
    cents = bviews.prorated_estimate_cents(1200, 14900, renews, "month", 1, INCIDENT_NOW)
    assert cents == 13115
    start = bviews.billing_period_start(renews, "month", 1)
    assert start == datetime(2026, 10, 7, 18, 12, tzinfo=dt_tz.utc)
    assert (renews - start).total_seconds() == 2_678_400


def test_estimate_ignores_subscription_age_old_sub_still_true_proration():
    """A sub born 4 months ago: created_at-based math would say $26.57; the
    period start must come from renews_at − 1 interval → still $131.15."""
    renews = datetime(2026, 11, 7, 18, 12, tzinfo=dt_tz.utc)
    created = datetime(2026, 6, 7, 18, 12, tzinfo=dt_tz.utc)
    wrong = int(13700 * (renews - INCIDENT_NOW).total_seconds() / (renews - created).total_seconds() + 0.5)
    assert wrong == 2657  # the trap the  flagged
    assert bviews.prorated_estimate_cents(1200, 14900, renews, "month", 1, INCIDENT_NOW) == 13115


def test_period_start_month_end_clamp_and_year():
    renews = datetime(2026, 3, 31, 12, 0, tzinfo=dt_tz.utc)
    assert bviews.billing_period_start(renews, "month", 1) == datetime(2026, 2, 28, 12, 0, tzinfo=dt_tz.utc)
    assert bviews.billing_period_start(renews, "year", 1) == datetime(2025, 3, 31, 12, 0, tzinfo=dt_tz.utc)


@override_settings(ENABLE_BILLING=True)
def test_confirm_page_old_subscription_shows_131_15_not_26_57():
    user = pro_user("bc_old")
    plan = AccountPlan.objects.get(user=user)
    # Stored period start mirrors the defective webhook created_at source.
    plan.current_period_start = datetime(2026, 6, 7, 18, 12, tzinfo=dt_tz.utc)
    plan.save(update_fields=["current_period_start"])
    with at_incident_time(), patch("apps.billing.views._ls_get", side_effect=fake_ls_get()), \
            patch("apps.billing.views._ls_patch") as mock_patch:
        resp = login(user).get(CHANGE_URL, {"plan": "business"})
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "You will be charged about $131.15 now" in html
    assert "$26.57" not in html
    mock_patch.assert_not_called()


# --- 1. first click never charges ---------------------------------------------


@pytest.mark.parametrize("plan_key", ["scale", "business", "paid"])
@override_settings(ENABLE_BILLING=True)
def test_first_click_existing_sub_redirects_to_confirm_never_patches(plan_key):  # BL-01
    user = make_user(f"bc_first_{plan_key}", plan=AccountPlan.Plan.SCALE, sub_id=f"sub_f_{plan_key}",
                     price_id=SCALE)
    before = _plan_snapshot(user)
    with patch("apps.billing.views._ls_get") as mock_get, \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = login(user).post(reverse("dashboard:billing-checkout"), {"plan": plan_key})
    assert resp.status_code == 303
    assert resp.url == f"{CHANGE_URL}?plan={plan_key}"
    mock_patch.assert_not_called()
    mock_post.assert_not_called()
    mock_get.assert_not_called()
    assert _plan_snapshot(user) == before
    assert not PlanChangeRequest.objects.filter(user=user).exists()


@override_settings(ENABLE_BILLING=True)
def test_confirm_get_never_patches_and_shows_full_upgrade_quote():  # BL-01 BL-03
    user = pro_user("bc_page")
    get = fake_ls_get()
    with at_incident_time(), patch("apps.billing.views._ls_get", side_effect=get), \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = login(user).get(CHANGE_URL, {"plan": "business"})
    assert resp.status_code == 200
    mock_patch.assert_not_called()
    mock_post.assert_not_called()
    assert "/subscriptions/sub_2589120" in get.calls
    html = resp.content.decode()
    for text in (
        "Upgrade to Business",
        "Pro · $12/month",
        "Business · $149/month",
        "Visa ••6770",
        "November 7, 2026 at 18:12 UTC",
        "You will be charged about $131.15 now",
        "(the difference for the rest of the current period, until November 7, 2026 at 18:12 UTC), "
        "then $149/month from November 7, 2026.",
        "Confirm and pay about $131.15",
        ">Cancel</a>",
        'name="token"',
        "data-disable-on-submit",
    ):
        assert text in html, text
    assert f'href="{BILLING_URL}">Cancel' in html.replace('class="button secondary" ', "")


@override_settings(ENABLE_BILLING=True)
def test_confirm_page_downgrade_says_plan_changes_now_no_charge():  # BL-04
    "Public API."
    user = make_user("bc_down", plan=AccountPlan.Plan.BUSINESS, sub_id="sub_down", price_id=BUSINESS)
    with at_incident_time(), patch("apps.billing.views._ls_get", side_effect=fake_ls_get(sub_attrs(BUSINESS))), \
            patch("apps.billing.views._ls_patch") as mock_patch:
        resp = login(user).get(CHANGE_URL, {"plan": "paid"})
    html = resp.content.decode()
    assert resp.status_code == 200
    assert ("Your plan changes to Pro <strong>now</strong> — Business limits end immediately. "
            "No charge now and nothing is refunded for the rest of this period. "
            "From November 7, 2026 you pay $12/month.") in html
    assert ">Change to Pro now</button>" in html
    assert "Confirm change to Pro" not in html
    assert "will change" not in html and "the new price applies from" not in html
    assert "You will be charged" not in html
    assert "Confirm and pay" not in html
    mock_patch.assert_not_called()


@pytest.mark.parametrize("failure", ["sub_urlerror", "sub_500", "variants_down", "no_renews_at", "malformed"])
@override_settings(ENABLE_BILLING=True)
def test_ls_unreachable_confirm_page_shows_error_and_no_confirm_button(failure):
    user = pro_user(f"bc_down_{failure}"[:30], sub_id=f"sub_u_{failure}")
    if failure == "sub_urlerror":
        get = fake_ls_get(sub_exc=urllib.error.URLError("timeout"))
    elif failure == "sub_500":
        get = fake_ls_get(sub_exc=http_error(500))
    elif failure == "variants_down":
        get = fake_ls_get(fail_variants=True)
    elif failure == "no_renews_at":
        get = fake_ls_get(sub_attrs(renews_at=None))
    else:
        def get(path, **kw):
            return {"data": "x"}
    with patch("apps.billing.views._ls_get", side_effect=get), \
            patch("apps.billing.views._ls_patch") as mock_patch:
        resp = login(user).get(CHANGE_URL, {"plan": "business"})
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "load your current subscription details" in html
    assert "Nothing has been changed or charged" in html
    assert 'name="token"' not in html
    assert "Confirm and pay" not in html
    assert f'action="{CHANGE_URL}"' not in html
    mock_patch.assert_not_called()


@override_settings(ENABLE_BILLING=True)
def test_confirm_page_already_on_plan_and_cancelled():
    user = make_user("bc_same", plan=AccountPlan.Plan.BUSINESS, sub_id="sub_same", price_id=BUSINESS)
    with patch("apps.billing.views._ls_get", side_effect=fake_ls_get(sub_attrs(BUSINESS))):
        resp = login(user).get(CHANGE_URL, {"plan": "business"})
    assert resp.status_code == 303 and resp.url == BILLING_URL

    user2 = pro_user("bc_canc", sub_id="sub_canc")
    with patch("apps.billing.views._ls_get", side_effect=fake_ls_get(sub_attrs(cancelled=True))):
        resp2 = login(user2).get(CHANGE_URL, {"plan": "business"})
    assert resp2.status_code == 409
    assert "set to cancel at period end" in resp2.content.decode()


# --- guards on the confirm page / POST ----------------------------------------


@override_settings(ENABLE_BILLING=True)
def test_confirm_guards_unknown_plan_past_due_manual_business_no_sub():
    user = pro_user("bc_g1", sub_id="sub_g1")
    with patch("apps.billing.views._ls_get") as mock_get, patch("apps.billing.views._ls_patch") as mock_patch:
        assert login(user).get(CHANGE_URL, {"plan": "enterprise"}).status_code == 400
        assert login(user).post(CHANGE_URL, {"plan": "free", "token": "x"}).status_code == 400

        pd = make_user("bc_g2", plan=AccountPlan.Plan.PAID, status=AccountPlan.Status.PAST_DUE, sub_id="sub_g2")
        assert login(pd).get(CHANGE_URL, {"plan": "scale"}).status_code == 409
        assert login(pd).post(CHANGE_URL, {"plan": "scale", "token": token_for(pd, SCALE)}).status_code == 409

        manual = make_user("bc_g3", plan=AccountPlan.Plan.BUSINESS, sub_id=None, price_id="")
        resp = login(manual).get(CHANGE_URL, {"plan": "scale"})
        assert resp.status_code == 409
        assert "managed by our team" in resp.content.decode()

        free = make_user("bc_g4", plan=AccountPlan.Plan.FREE, sub_id=None, price_id="")
        resp = login(free).get(CHANGE_URL, {"plan": "paid"})
        assert resp.status_code == 303 and resp.url == BILLING_URL
    mock_get.assert_not_called()
    mock_patch.assert_not_called()


@override_settings(ENABLE_BILLING=True, ENABLE_TEAMS=True)
def test_confirm_non_owner_member_and_agent_403_no_ls():
    from apps.accounts.services import AGENT_ACCOUNT_DOMAIN

    owner = pro_user("bc_owner", sub_id="sub_owner")
    member = make_user("bc_member", plan=AccountPlan.Plan.FREE, sub_id=None, price_id="")
    TeamMembership.objects.create(account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER)
    agent = pro_user("bc_agent", sub_id="sub_agent", email=f"bcagent@{AGENT_ACCOUNT_DOMAIN}")
    with patch("apps.billing.views._ls_get") as mock_get, patch("apps.billing.views._ls_patch") as mock_patch:
        r1 = login(member).get(CHANGE_URL, {"plan": "business"})
        r2 = login(member).post(CHANGE_URL, {"plan": "business", "token": token_for(owner, BUSINESS)})
        r3 = login(agent).get(CHANGE_URL, {"plan": "business"})
    assert r1.status_code == 403 and b"Only the account owner can manage billing." in r1.content
    assert r2.status_code == 403
    assert r3.status_code == 403
    mock_get.assert_not_called()
    mock_patch.assert_not_called()


@override_settings(ENABLE_BILLING=False)
def test_confirm_flag_off_404():
    user = pro_user("bc_flag", sub_id="sub_flag")
    assert login(user).get(CHANGE_URL, {"plan": "business"}).status_code == 404


# --- 2. token ------------------------------------------------------------------


@override_settings(ENABLE_BILLING=True)
def test_confirm_post_with_page_token_patches_once_and_flashes_amount():  # BL-03
    user = pro_user("bc_ok")
    client = login(user)
    before = _plan_snapshot(user)
    get = fake_ls_get()
    with at_incident_time(), patch("apps.billing.views._ls_get", side_effect=get):
        page = client.get(CHANGE_URL, {"plan": "business"})
    token = page.context["token"]
    with patch("apps.billing.views._ls_get", side_effect=get), \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = client.post(CHANGE_URL, {"plan": "business", "token": token}, follow=True)
    assert resp.redirect_chain[0] == (BILLING_URL, 303)
    mock_post.assert_not_called()
    mock_patch.assert_called_once_with(
        "/subscriptions/sub_2589120",
        {"data": {"type": "subscriptions", "id": "sub_2589120",
                  "attributes": {"variant_id": int(BUSINESS), "invoice_immediately": True}}},
    )
    html = resp.content.decode()
    assert ("Upgrade to Business confirmed. Your card ••6770 is being charged about $131.15; "
            "the receipt will be emailed to you.") in html
    assert _plan_snapshot(user) == before  # webhook stays the sole mutator
    req = PlanChangeRequest.objects.get(user=user)
    assert (req.from_plan, req.to_plan, req.estimate_cents, req.status) == ("paid", "business", 13115, "submitted")


@pytest.mark.parametrize("token", [None, "", "garbage", "a" * 2000])
@override_settings(ENABLE_BILLING=True)
def test_confirm_post_without_valid_token_never_patches(token):  # BL-01
    user = pro_user(f"bc_nt_{len(token or '')}", sub_id=f"sub_nt_{len(token or '')}")
    data = {"plan": "business"}
    if token is not None:
        data["token"] = token
    with patch("apps.billing.views._ls_get", side_effect=fake_ls_get()) as mock_get, \
            patch("apps.billing.views._ls_patch") as mock_patch:
        resp = login(user).post(CHANGE_URL, data)
    assert resp.status_code == 303
    assert resp.url == f"{CHANGE_URL}?plan=business"
    mock_patch.assert_not_called()
    mock_get.assert_not_called()  # rejected before any LS call


@override_settings(ENABLE_BILLING=True)
def test_old_direct_checkout_post_with_existing_sub_never_patches_even_with_token():
    user = pro_user("bc_old_post", sub_id="sub_old_post")
    with patch("apps.billing.views._ls_get", side_effect=fake_ls_get()), \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = login(user).post(reverse("dashboard:billing-checkout"),
                                {"plan": "business", "token": token_for(user, BUSINESS)})
    assert resp.status_code == 303 and resp.url.startswith(CHANGE_URL)
    mock_patch.assert_not_called()
    mock_post.assert_not_called()


@override_settings(ENABLE_BILLING=True)
def test_token_of_another_user_rejected():
    victim = pro_user("bc_victim", sub_id="sub_victim")
    attacker = pro_user("bc_attacker", sub_id="sub_attacker")
    with patch("apps.billing.views._ls_get", side_effect=fake_ls_get()), \
            patch("apps.billing.views._ls_patch") as mock_patch:
        resp = login(victim).post(CHANGE_URL, {"plan": "business", "token": token_for(attacker, BUSINESS)})
    assert resp.status_code == 303 and resp.url == f"{CHANGE_URL}?plan=business"
    mock_patch.assert_not_called()


@override_settings(ENABLE_BILLING=True)
def test_token_for_another_target_plan_rejected():
    user = pro_user("bc_otherplan", sub_id="sub_otherplan")
    scale_token = token_for(user, SCALE)
    with patch("apps.billing.views._ls_get", side_effect=fake_ls_get()), \
            patch("apps.billing.views._ls_patch") as mock_patch:
        resp = login(user).post(CHANGE_URL, {"plan": "business", "token": scale_token})
    assert resp.status_code == 303 and resp.url == f"{CHANGE_URL}?plan=business"
    mock_patch.assert_not_called()


@override_settings(ENABLE_BILLING=True)
def test_expired_token_rejected_fresh_accepted():
    user = pro_user("bc_exp", sub_id="sub_exp")
    token = token_for(user, BUSINESS)
    later = time.time() + bviews.PLAN_CHANGE_TOKEN_MAX_AGE + 5
    with patch("django.core.signing.time.time", return_value=later), \
            patch("apps.billing.views._ls_get", side_effect=fake_ls_get()), \
            patch("apps.billing.views._ls_patch") as mock_patch:
        resp = login(user).post(CHANGE_URL, {"plan": "business", "token": token}, follow=True)
    assert resp.redirect_chain[0] == (f"{CHANGE_URL}?plan=business", 303)
    assert "expired or doesn&#x27;t match" in resp.content.decode()
    mock_patch.assert_not_called()

    with patch("apps.billing.views._ls_get", side_effect=fake_ls_get()), \
            patch("apps.billing.views._ls_patch") as mock_patch2:
        login(user).post(CHANGE_URL, {"plan": "business", "token": token})
    mock_patch2.assert_called_once()


@override_settings(ENABLE_BILLING=True)
def test_tampered_or_wrong_salt_token_rejected():
    from django.core import signing

    user = pro_user("bc_tamper", sub_id="sub_tamper")
    good = token_for(user, BUSINESS)
    tampered = good[:-2] + ("AA" if not good.endswith("AA") else "BB")
    wrong_salt = signing.dumps({"u": user.pk, "f": PAID, "t": BUSINESS, "e": 1}, salt="other")
    with patch("apps.billing.views._ls_get", side_effect=fake_ls_get()), \
            patch("apps.billing.views._ls_patch") as mock_patch:
        for tok in (tampered, wrong_salt):
            resp = login(user).post(CHANGE_URL, {"plan": "business", "token": tok})
            assert resp.status_code == 303 and resp.url == f"{CHANGE_URL}?plan=business"
    mock_patch.assert_not_called()


@override_settings(ENABLE_BILLING=True)
def test_subscription_changed_since_page_load_rejected():
    """Token minted while on Pro; meanwhile the sub became Scale (portal,
    other tab) → re-confirm with the new numbers, no PATCH."""
    user = pro_user("bc_moved", sub_id="sub_moved")
    token = token_for(user, BUSINESS, from_variant=PAID)
    with patch("apps.billing.views._ls_get", side_effect=fake_ls_get(sub_attrs(SCALE))), \
            patch("apps.billing.views._ls_patch") as mock_patch:
        resp = login(user).post(CHANGE_URL, {"plan": "business", "token": token}, follow=True)
    assert resp.redirect_chain[0] == (f"{CHANGE_URL}?plan=business", 303)
    mock_patch.assert_not_called()


@override_settings(ENABLE_BILLING=True)
def test_replay_after_marker_expired_live_preflight_says_already_on_plan_no_patch():
    """: the token outlives the pending marker. A replay after the
    marker expired, once LS already has the target variant, must be stopped
    by the LIVE same-variant preflight ("already on this plan")."""
    user = pro_user("bc_replay", sub_id="sub_replay")
    client = login(user)
    token = token_for(user, BUSINESS)
    with patch("apps.billing.views._ls_get", side_effect=fake_ls_get()), \
            patch("apps.billing.views._ls_patch") as mock_patch:
        client.post(CHANGE_URL, {"plan": "business", "token": token})
    assert mock_patch.call_count == 1
    # Marker expires; webhook not yet applied locally (DB still says Pro).
    PlanChangeRequest.objects.filter(user=user).update(
        created_at=timezone.now() - timedelta(seconds=bviews.PLAN_CHANGE_PENDING_SECONDS + 1)
    )
    with patch("apps.billing.views._ls_get", side_effect=fake_ls_get(sub_attrs(BUSINESS))), \
            patch("apps.billing.views._ls_patch") as mock_patch2:
        resp = client.post(CHANGE_URL, {"plan": "business", "token": token}, follow=True)
    mock_patch2.assert_not_called()
    assert resp.redirect_chain[0] == (BILLING_URL, 302)
    assert "You&#x27;re already on this plan." in resp.content.decode()


# --- pending marker ------------------------------------------------------------


@override_settings(ENABLE_BILLING=True)
def test_pending_marker_hides_buttons_blocks_second_confirm_until_webhook():  # BL-02
    user = pro_user("bc_pending", sub_id="sub_pending")
    client = login(user)
    with patch("apps.billing.views._ls_get", side_effect=fake_ls_get()), \
            patch("apps.billing.views._ls_patch") as mock_patch:
        client.post(CHANGE_URL, {"plan": "business", "token": token_for(user, BUSINESS)})
        # A fresh, valid token for a DIFFERENT target: still refused.
        second = client.post(CHANGE_URL, {"plan": "scale", "token": token_for(user, SCALE)}, follow=True)
    assert mock_patch.call_count == 1
    assert "already in progress" in second.content.decode()

    page = client.get(BILLING_URL).content.decode()
    assert "Plan change in progress — refresh in a minute" in page
    assert "Upgrade to Scale" not in page and "Upgrade to Business" not in page
    with patch("apps.billing.views._ls_get") as mock_get:
        confirm_page = client.get(CHANGE_URL, {"plan": "scale"}).content.decode()
    mock_get.assert_not_called()
    assert "Plan change in progress" in confirm_page and 'name="token"' not in confirm_page

    # Webhook lands → plan is Business → marker no longer pending.
    AccountPlan.objects.filter(user=user).update(plan=AccountPlan.Plan.BUSINESS, stripe_price_id=BUSINESS)
    page2 = client.get(BILLING_URL).content.decode()
    assert "Plan change in progress" not in page2


@override_settings(ENABLE_BILLING=True)
def test_pending_marker_expires_buttons_return():
    user = make_user("bc_pexp", plan=AccountPlan.Plan.PAID, sub_id="sub_pexp", price_id=PAID)
    PlanChangeRequest.objects.create(
        user=user, ls_subscription_id="sub_pexp", from_plan="paid", to_plan="business",
        from_variant=PAID, to_variant=BUSINESS, status=PlanChangeRequest.Status.SUBMITTED,
        created_at=timezone.now() - timedelta(seconds=bviews.PLAN_CHANGE_PENDING_SECONDS + 5),
    )
    page = login(user).get(BILLING_URL).content.decode()
    assert "Plan change in progress" not in page
    assert "Upgrade to Business" in page


@override_settings(ENABLE_BILLING=True)
def test_patch_timeout_keeps_marker_unknown_and_blocks_retry():
    user = pro_user("bc_timeout", sub_id="sub_timeout")
    client = login(user)
    with patch("apps.billing.views._ls_get", side_effect=fake_ls_get()), \
            patch("apps.billing.views._ls_patch", side_effect=TimeoutError("t")) as mock_patch:
        r1 = client.post(CHANGE_URL, {"plan": "business", "token": token_for(user, BUSINESS)})
        r2 = client.post(CHANGE_URL, {"plan": "business", "token": token_for(user, BUSINESS)})
    assert r1.status_code == 502
    assert "Recent payments" in r1.content.decode()
    assert r2.status_code == 303
    assert mock_patch.call_count == 1
    assert PlanChangeRequest.objects.get(user=user).status == "unknown"


@override_settings(ENABLE_BILLING=True)
def test_patch_402_marks_failed_retry_allowed():
    user = pro_user("bc_402", sub_id="sub_402")
    client = login(user)
    with patch("apps.billing.views._ls_get", side_effect=fake_ls_get()), \
            patch("apps.billing.views._ls_patch", side_effect=http_error(402)):
        assert client.post(CHANGE_URL, {"plan": "business", "token": token_for(user, BUSINESS)}).status_code == 402
    assert PlanChangeRequest.objects.get(user=user).status == "failed"
    assert "Upgrade to Business" in client.get(BILLING_URL).content.decode()


# --- downgrade POST ------------------------------------------------------------


@override_settings(ENABLE_BILLING=True)
def test_downgrade_confirm_post_no_charge_flash_and_notice_mail():
    user = make_user("bc_dpost", plan=AccountPlan.Plan.BUSINESS, sub_id="sub_dpost", price_id=BUSINESS,
                     email="owner@example.test")
    mail.outbox.clear()
    with patch("apps.billing.views._ls_get", side_effect=fake_ls_get(sub_attrs(BUSINESS))), \
            patch("apps.billing.views._ls_patch") as mock_patch:
        resp = login(user).post(CHANGE_URL, {"plan": "paid", "token": token_for(user, PAID, from_variant=BUSINESS)},
                                follow=True)
    attrs = mock_patch.call_args[0][1]["data"]["attributes"]
    assert attrs == {"variant_id": int(PAID), "disable_prorations": True}
    assert ("Your plan changed to Pro now — Business limits end immediately. "
            "No charge now; your new price applies from November 7, 2026.") in resp.content.decode()
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    assert msg.to == ["owner@example.test"]
    assert msg.subject == "mailsocket: your plan changed to Pro"
    assert "Your mailsocket plan changed to Pro now. Business limits ended immediately." in msg.body
    assert "No charge now, and nothing is refunded for the rest of this period." in msg.body
    assert "Your new Pro price applies from November 7, 2026." in msg.body
    assert "will change" not in msg.body


# --- billing page buttons ------------------------------------------------------


@override_settings(ENABLE_BILLING=True)
def test_billing_page_subscriber_upgrade_buttons_are_get_to_confirm_page():
    user = pro_user("bc_btn", sub_id="sub_btn")
    html = login(user).get(BILLING_URL).content.decode()
    assert f'<form method="get" action="{CHANGE_URL}">' in html
    assert reverse("dashboard:billing-checkout") not in html
    assert "Upgrade to Business" in html


# --- 4. checkout return path ---------------------------------------------------


@override_settings(ENABLE_BILLING=True, DASHBOARD_HOST="dash.mailsocket.app")
def test_new_checkout_body_has_redirect_url():
    user = make_user("bc_new", plan=AccountPlan.Plan.FREE, sub_id=None, price_id="")
    captured = {}

    def fake_post(path, body, **kw):
        captured["body"] = body
        return {"data": {"attributes": {"url": "https://checkout.example/x"}}}

    with patch("apps.billing.views._ls_post", side_effect=fake_post), \
            patch("apps.billing.views._ls_patch") as mock_patch:
        resp = login(user).post(reverse("dashboard:billing-checkout"), {"plan": "paid"})
    assert resp.status_code == 303
    attrs = captured["body"]["data"]["attributes"]
    assert attrs["product_options"]["redirect_url"] == \
        "https://dash.mailsocket.app/app/billing/?checkout=success"
    assert "checkout_options" not in attrs
    assert attrs["checkout_data"]["custom"] == bviews.checkout_custom(user.pk)  # user_pk + user_sig
    mock_patch.assert_not_called()


@override_settings(ENABLE_BILLING=True)
def test_checkout_success_banner_text_only():
    free = make_user("bc_ban1", plan=AccountPlan.Plan.FREE, sub_id=None, price_id="")
    client = login(free)
    html = client.get(BILLING_URL, {"checkout": "success"}).content.decode()
    assert "Payment received — your plan updates within a minute" in html
    assert AccountPlan.objects.get(user=free).plan == AccountPlan.Plan.FREE  # param changes nothing
    assert "Payment received" not in client.get(BILLING_URL).content.decode()

    pro = pro_user("bc_ban2", sub_id="sub_ban2")
    html2 = login(pro).get(BILLING_URL, {"checkout": "success"}).content.decode()
    assert "Payment received — you&#x27;re on Pro." in html2


# --- 2/3. payments + receipt ---------------------------------------------------


def payment_event(user, *, invoice_id="8675633", webhook_id="wh_pay", reason="updated", total=13115,
                  formatted="$131.15", sub_id=2589120):
    return event_body(
        "subscription_payment_success", user_pk=user.pk, sub_id=invoice_id, webhook_id=webhook_id,
        updated_at=timezone.now().isoformat(),
        extra_attrs={
            "subscription_id": sub_id, "billing_reason": reason, "total": total, "total_usd": total,
            "total_formatted": formatted, "currency": "USD", "card_brand": "visa", "card_last_four": "6770",
            "created_at": "2026-10-09T01:58:21.000000Z", "status": "paid",
            "urls": {"invoice_url": f"https://app.lemonsqueezy.com/my-orders/x/subscription-invoice/{invoice_id}?sig=s"},
        },
    )


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET, OPS_ALERT_EMAIL="")
def test_payment_success_records_payment_receipt_once_per_invoice():  # BL-03
    user = pro_user("bc_pay", sub_id="sub_pay", email="owner44@example.test")
    # The subscription_updated webhook already moved the plan to the
    # confirmed target (the receipt label matches on to_variant).
    AccountPlan.objects.filter(user=user).update(plan=AccountPlan.Plan.BUSINESS, stripe_price_id=BUSINESS)
    PlanChangeRequest.objects.create(
        user=user, ls_subscription_id="sub_pay", from_plan="paid", to_plan="business",
        from_variant=PAID, to_variant=BUSINESS, estimate_cents=13115,
        status=PlanChangeRequest.Status.SUBMITTED,
    )
    client = Client()
    mail.outbox.clear()
    assert post_event(client, payment_event(user)).status_code == 200
    # Same delivery again (same webhook id) and a re-sent event for the same
    # invoice under a NEW webhook id: still exactly one row + one receipt.
    assert post_event(client, payment_event(user)).json() == {"duplicate": True}
    assert post_event(client, payment_event(user, webhook_id="wh_pay_again")).status_code == 200

    assert BillingPayment.objects.filter(ls_invoice_id="8675633").count() == 1
    p = BillingPayment.objects.get(ls_invoice_id="8675633")
    assert (p.user_id, p.amount_cents, p.currency, p.reason, p.card_last_four) == \
        (user.pk, 13115, "USD", "updated", "6770")
    assert p.receipt_sent_at is not None

    receipts = [m for m in mail.outbox if "receipt" in m.subject]
    assert len(receipts) == 1
    msg = receipts[0]
    assert msg.to == ["owner44@example.test"]
    assert msg.subject == "mailsocket receipt — $131.15 (Pro → Business)"
    for text in ("Plan: Pro → Business", "Amount: $131.15", "Date: 2026-10-09 01:58 UTC",
                 "Reason: Plan change", "Visa ••6770", "subscription-invoice/8675633",
                 "This is your mailsocket receipt (Lemon Squeezy also emails one — you were charged once)",
                 "Questions or a mistake? Reply or email sales@mailsocket.app — 14-day money-back"):
        assert text in msg.body, text

    # Recent payments on the billing page.
    html = login(user).get(BILLING_URL).content.decode()
    assert "Recent payments" in html
    assert "2026-10-09" in html and "$131.15" in html and "Plan change" in html
    assert "subscription-invoice/8675633" in html


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET, OPS_ALERT_EMAIL="")
def test_receipt_smtp_failure_never_breaks_webhook_and_is_not_resent():
    user = pro_user("bc_smtp", sub_id="sub_smtp")
    with patch("apps.billing.receipts.send_mail", side_effect=OSError("smtp down")) as mock_send:
        resp = post_event(Client(), payment_event(user, invoice_id="inv_smtp", webhook_id="wh_smtp",
                                                  reason="renewal", total=1200, formatted="$12.00"))
    assert resp.status_code == 200
    assert mock_send.call_count == 1
    assert BillingPayment.objects.get(ls_invoice_id="inv_smtp").reason == "renewal"


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET, OPS_ALERT_EMAIL="")
def test_recent_payments_only_for_owner_and_unsafe_invoice_url_dropped():
    user = pro_user("bc_rp", sub_id="sub_rp")
    body = json.loads(payment_event(user, invoice_id="inv_js", webhook_id="wh_js"))
    body["data"]["attributes"]["urls"]["invoice_url"] = "javascript:alert(1)"
    post_event(Client(), json.dumps(body).encode())
    assert BillingPayment.objects.get(ls_invoice_id="inv_js").invoice_url == ""
    assert "javascript:" not in login(user).get(BILLING_URL).content.decode()


# --- 5. BillingEvent.user ------------------------------------------------------


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_billing_event_user_set_when_resolved_null_when_not():
    user = pro_user("bc_evu", sub_id="sub_evu")
    client = Client()
    post_event(client, event_body("subscription_updated", user_pk=user.pk, sub_id="sub_evu",
                                  webhook_id="wh_evu1", updated_at=timezone.now().isoformat()))
    assert BillingEvent.objects.get(event_key="wh_evu1").user_id == user.pk

    post_event(client, event_body("subscription_updated", custom_data={}, webhook_id="wh_evu2"))
    post_event(client, event_body("subscription_updated", user_pk=999999, webhook_id="wh_evu3"))
    assert BillingEvent.objects.get(event_key="wh_evu2").user_id is None
    assert BillingEvent.objects.get(event_key="wh_evu3").user_id is None

    # Duplicate delivery: still one row, user unchanged (exactly-once kept).
    assert post_event(client, event_body("subscription_updated", user_pk=user.pk, sub_id="sub_evu",
                                         webhook_id="wh_evu1")).json() == {"duplicate": True}
    assert BillingEvent.objects.filter(event_key="wh_evu1").count() == 1


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_billing_event_user_rolled_back_with_the_event_on_crash():
    user = pro_user("bc_evcrash", sub_id="sub_evcrash")

    def boom(event_name, payload):
        raise RuntimeError("crash")

    with patch("apps.billing.views._apply_ls_event", side_effect=boom), pytest.raises(RuntimeError):
        post_event(Client(), event_body("subscription_updated", user_pk=user.pk, webhook_id="wh_crash"))
    assert not BillingEvent.objects.filter(event_key="wh_crash").exists()





@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET, OPS_ALERT_EMAIL="ops@example.test")
def test_owner_alert_includes_amount_email_and_telegram_no_customer_email():
    from apps.ops.testing import fake_telegram

    user = pro_user("bc_alert", sub_id="sub_alert", email="cust@example.test")
    # Like prod: a synced subscription (the payment event's data.id is the
    # INVOICE id, admitted by the existing "newer than last sync" rule).
    AccountPlan.objects.filter(user=user).update(billing_synced_at=timezone.now() - timedelta(days=1))
    mail.outbox.clear()
    with fake_telegram() as tg:
        assert post_event(Client(), payment_event(user, invoice_id="inv_alert", webhook_id="wh_alert")).status_code == 200
    alerts = [m for m in mail.outbox if m.to == ["ops@example.test"]]
    assert len(alerts) == 1
    assert "Amount: $131.15" in alerts[0].body
    assert "$131.15" in alerts[0].subject
    assert "cust@example.test" in alerts[0].body  # email keeps the account
    assert len(tg.texts) == 1
    assert "Amount: $131.15" in tg.texts[0]
    assert "cust@" not in tg.texts[0] and "@" not in tg.texts[0]
    receipts = [m for m in mail.outbox if m.to == ["cust@example.test"]]
    assert len(receipts) == 1


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET, OPS_ALERT_EMAIL="")
def test_payment_record_db_error_does_not_lose_webhook_or_plan_status():
    user = make_user("bc_dberr", plan=AccountPlan.Plan.PAID, status=AccountPlan.Status.PAST_DUE,
                     sub_id="sub_dberr", price_id=PAID)
    AccountPlan.objects.filter(user=user).update(billing_synced_at=timezone.now() - timedelta(days=1))
    from django.db import IntegrityError

    with patch("apps.billing.views.BillingPayment.objects.get_or_create", side_effect=IntegrityError("x")):
        resp = post_event(Client(), payment_event(user, invoice_id="inv_dberr", webhook_id="wh_dberr"))
    assert resp.status_code == 200
    assert AccountPlan.objects.get(user=user).status == AccountPlan.Status.ACTIVE
    assert BillingEvent.objects.get(event_key="wh_dberr").user_id == user.pk
    assert not BillingPayment.objects.filter(ls_invoice_id="inv_dberr").exists()






@override_settings(ENABLE_BILLING=True)
def test_locked_recheck_refuses_second_confirm_when_fast_path_missed_it():
    """Deterministic race: worker B passed the fast-path pending check
    BEFORE worker A committed its PlanChangeRequest (simulated by making the
    fast-path call return None once). Only the re-check under
    select_for_update (views.py, step 6) can stop B → exactly 1 PATCH."""
    user = pro_user("bc_race1", sub_id="sub_race1")
    client = login(user)
    with patch("apps.billing.views._ls_get", side_effect=fake_ls_get()), \
            patch("apps.billing.views._ls_patch") as mock_patch:
        # Worker A: full confirm, commits the marker, PATCHes once.
        client.post(CHANGE_URL, {"plan": "business", "token": token_for(user, BUSINESS)})
        assert mock_patch.call_count == 1

        real = bviews.pending_plan_change
        calls = []

        def fast_path_misses_once(u, plan=None):
            calls.append(plan)
            if len(calls) == 1:
                return None  # B's fast-path ran before A's row existed
            return real(u, plan)

        with patch("apps.billing.views.pending_plan_change", side_effect=fast_path_misses_once):
            resp = client.post(CHANGE_URL, {"plan": "business", "token": token_for(user, BUSINESS)})
    assert len(calls) == 2  # fast path + the locked re-check
    assert mock_patch.call_count == 1
    assert resp.status_code == 303 and resp.url == BILLING_URL
    assert "already in progress" in client.get(BILLING_URL).content.decode()
    assert PlanChangeRequest.objects.filter(user=user).count() == 1


@override_settings(ENABLE_BILLING=True)
def test_two_concurrent_confirms_threads_exactly_one_patch():  # BL-02
    """Two real workers (threads, separate DB connections) both pass the
    fast-path check, meet at a barrier inside the live preflight, then race
    for the AccountPlan row lock. The loser must see the winner's committed
    marker under the lock → exactly 1 _ls_patch."""
    import threading

    from django.db import connection

    user = pro_user("bc_race2", sub_id="sub_race2")
    barrier = threading.Barrier(2, timeout=20)
    base_get = fake_ls_get()

    def get_with_barrier(path, **kw):
        result = base_get(path, **kw)
        if path.startswith("/subscriptions/"):
            barrier.wait()  # both workers are past the fast-path check here
        return result

    results, errors = [], []

    def worker():
        try:
            c = login(user)
            r = c.post(CHANGE_URL, {"plan": "business", "token": token_for(user, BUSINESS)})
            results.append((r.status_code, r.get("Location")))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            connection.close()

    with patch("apps.billing.views._ls_get", side_effect=get_with_barrier), \
            patch("apps.billing.views._ls_patch") as mock_patch:
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    assert not errors, errors
    assert len(results) == 2
    assert mock_patch.call_count == 1
    assert PlanChangeRequest.objects.filter(user=user).count() == 1
    assert sorted(r[0] for r in results) == [303, 303]
    assert all(r[1] == BILLING_URL for r in results)


# --- r2 item 3: interval mismatch → needs our team, no confirm button ----------


@pytest.mark.parametrize("override", [
    {"interval": "year", "interval_count": 1},
    {"interval": "month", "interval_count": 3},
])
@override_settings(ENABLE_BILLING=True)
def test_interval_mismatch_refused_no_confirm_button(override):
    user = pro_user(f"bc_iv_{override['interval']}{override['interval_count']}",
                    sub_id=f"sub_iv_{override['interval']}{override['interval_count']}")
    get = fake_ls_get(variants={BUSINESS: override})
    with at_incident_time(), patch("apps.billing.views._ls_get", side_effect=get), \
            patch("apps.billing.views._ls_patch") as mock_patch:
        resp = login(user).get(CHANGE_URL, {"plan": "business"})
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "This change needs our team — contact sales@mailsocket.app" in html
    assert 'name="token"' not in html
    assert f'action="{CHANGE_URL}"' not in html
    assert "Confirm and pay" not in html and "You will be charged" not in html
    assert "/year" not in html
    mock_patch.assert_not_called()


def test_build_quote_raises_on_interval_mismatch_and_free_variant():
    user = pro_user("bc_iv_unit", sub_id="sub_iv_unit")
    plan = AccountPlan.objects.get(user=user)
    with at_incident_time(), patch("apps.billing.views._ls_get",
                                   side_effect=fake_ls_get(variants={BUSINESS: {"interval": "year"}})):
        with pytest.raises(bviews.QuoteError) as exc:
            bviews._build_quote(plan, "business", BUSINESS)
    assert exc.value.user_message == bviews.NEEDS_TEAM_MESSAGE
    # Same interval → still quotes normally.
    with at_incident_time(), patch("apps.billing.views._ls_get", side_effect=fake_ls_get()):
        outcome, quote = bviews._build_quote(plan, "business", BUSINESS)
    assert outcome == "ok" and quote["estimate_cents"] == 13115


# --- r2 item 4: on_trial and free <-> paid are refused --------------------------


@override_settings(ENABLE_BILLING=True)
def test_on_trial_refused_page_points_to_portal_and_post_never_patches():
    user = pro_user("bc_trial", sub_id="sub_trial")
    get = fake_ls_get(sub_attrs(status="on_trial"))
    with at_incident_time(), patch("apps.billing.views._ls_get", side_effect=get), \
            patch("apps.billing.views._ls_patch") as mock_patch:
        client = login(user)
        page = client.get(CHANGE_URL, {"plan": "business"})
        # Even a valid token (e.g. minted before the trial was visible).
        post = client.post(CHANGE_URL, {"plan": "business", "token": token_for(user, BUSINESS)})
    html = page.content.decode()
    assert page.status_code == 200
    assert "free trial" in html and "billing portal" in html and "sales@mailsocket.app" in html
    assert "Open billing portal" in html
    assert 'name="token"' not in html and "Confirm and pay" not in html
    assert f'action="{CHANGE_URL}"' not in html
    assert post.status_code == 409
    assert "free trial" in post.content.decode()
    mock_patch.assert_not_called()
    assert not PlanChangeRequest.objects.filter(user=user).exists()


@pytest.mark.parametrize("free_side", ["current", "target"])
@override_settings(ENABLE_BILLING=True)
def test_free_variant_on_either_side_refused(free_side):
    user = pro_user(f"bc_free_{free_side}", sub_id=f"sub_free_{free_side}")
    vid = PAID if free_side == "current" else BUSINESS
    get = fake_ls_get(variants={vid: {"price": 0}})
    with at_incident_time(), patch("apps.billing.views._ls_get", side_effect=get), \
            patch("apps.billing.views._ls_patch") as mock_patch:
        resp = login(user).get(CHANGE_URL, {"plan": "business"})
    html = resp.content.decode()
    assert "This change needs our team — contact sales@mailsocket.app" in html
    assert 'name="token"' not in html
    mock_patch.assert_not_called()


# --- r2 item 5: receipt label matches the confirmed target variant --------------


def _receipt_subject(user, invoice_id):
    receipts = [m for m in mail.outbox if m.subject.startswith("mailsocket receipt") and invoice_id in m.body]
    assert len(receipts) == 1, [m.subject for m in mail.outbox]
    return receipts[0].subject


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET, OPS_ALERT_EMAIL="")
def test_receipt_label_only_from_request_matching_current_variant_and_window():
    window = bviews.PLAN_CHANGE_PENDING_SECONDS * 12

    def scenario(name, *, to_variant, age_seconds, current_variant=BUSINESS):
        user = pro_user(f"bc_lbl_{name}", sub_id=f"sub_lbl_{name}")
        AccountPlan.objects.filter(user=user).update(
            plan=bviews._variant_map().get(current_variant, AccountPlan.Plan.PAID), stripe_price_id=current_variant
        )
        req = PlanChangeRequest.objects.create(
            user=user, ls_subscription_id=f"sub_lbl_{name}", from_plan="paid",
            to_plan=bviews._variant_map()[to_variant], from_variant=PAID, to_variant=to_variant,
            status=PlanChangeRequest.Status.SUBMITTED,
        )
        PlanChangeRequest.objects.filter(pk=req.pk).update(
            created_at=timezone.now() - timedelta(seconds=age_seconds)
        )
        mail.outbox.clear()
        inv = f"inv_lbl_{name}"
        assert post_event(Client(), payment_event(user, invoice_id=inv, webhook_id=f"wh_lbl_{name}")).status_code == 200
        return _receipt_subject(user, inv)

    # Matches the current variant, recent → "Pro → Business".
    assert scenario("match", to_variant=BUSINESS, age_seconds=60) == "mailsocket receipt — $131.15 (Pro → Business)"
    # Request targeted Scale but the subscription is on Business (e.g. a
    # portal change) → generic label, never the unrelated request.
    assert scenario("other", to_variant=SCALE, age_seconds=60) == "mailsocket receipt — $131.15 (Plan change)"
    # Matching variant but older than PLAN_CHANGE_PENDING_SECONDS×12 → generic.
    assert scenario("old", to_variant=BUSINESS, age_seconds=window + 60) == \
        "mailsocket receipt — $131.15 (Plan change)"
    # Webhook for the plan not synced yet (AccountPlan still on Pro) → generic.
    assert scenario("unsynced", to_variant=BUSINESS, age_seconds=60, current_variant=PAID) == \
        "mailsocket receipt — $131.15 (Plan change)"


# --- r2 item 6: lost payment record → ops problem alert (ids only) --------------


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET, OPS_ALERT_EMAIL="ops@example.test")
def test_payment_record_failure_queues_problem_alert_ids_only():
    from django.db import IntegrityError

    from apps.ops.testing import fake_telegram

    user = pro_user("bc_lost", sub_id="sub_lost", email="lost-cust@example.test")
    AccountPlan.objects.filter(user=user).update(billing_synced_at=timezone.now() - timedelta(days=1))
    mail.outbox.clear()
    with fake_telegram() as tg, \
            patch("apps.billing.views.BillingPayment.objects.get_or_create", side_effect=IntegrityError("x")):
        resp = post_event(Client(), payment_event(user, invoice_id="inv_lost", webhook_id="wh_lost"))
    assert resp.status_code == 200
    assert not BillingPayment.objects.filter(ls_invoice_id="inv_lost").exists()
    problems = [m for m in mail.outbox if "payment record failed" in m.subject]
    assert len(problems) == 1, [m.subject for m in mail.outbox]
    msg = problems[0]
    assert msg.to == ["ops@example.test"]
    assert msg.subject == "[mailsocket] Billing webhook problem: payment record failed"
    for text in (f"User pk: {user.pk}", "LS webhook id: wh_lost", "LS invoice id: inv_lost",
                 "LS subscription id: 2589120", "Event: subscription_payment_success",
                 "payment", "NOT recorded"):
        assert text in msg.body, text
    assert "lost-cust@" not in msg.body and "6770" not in msg.body and "$131.15" not in msg.body
    assert "NO plan change" not in msg.body  # the plan status WAS applied
    tg_problem = [t for t in tg.texts if "payment record failed" in t]
    assert len(tg_problem) == 1 and "@" not in tg_problem[0]
    # No customer receipt (no row), the plan status still applied.
    assert not [m for m in mail.outbox if m.to == ["lost-cust@example.test"]]
    assert AccountPlan.objects.get(user=user).status == AccountPlan.Status.ACTIVE




@pytest.mark.parametrize("status", ["past_due", "paused", "unpaid"])
@override_settings(ENABLE_BILLING=True)
def test_confirm_post_refuses_live_status_not_active_no_patch(status):
    user = pro_user(f"bc_st_{status}", sub_id=f"sub_st_{status}")
    get = fake_ls_get(sub_attrs(status=status))
    with at_incident_time(), patch("apps.billing.views._ls_get", side_effect=get), \
            patch("apps.billing.views._ls_patch") as mock_patch:
        client = login(user)
        post = client.post(CHANGE_URL, {"plan": "business", "token": token_for(user, BUSINESS)})
    assert post.status_code == 409
    assert "resolve your payment status" in post.content.decode()
    mock_patch.assert_not_called()
    assert not PlanChangeRequest.objects.filter(user=user).exists()
