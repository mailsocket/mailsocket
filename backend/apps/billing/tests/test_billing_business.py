"Public API."
import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.entitlements import can_add_domain, can_invite_member, can_toggle_catch_all
from apps.accounts.models import Account, AccountPlan
from apps.billing.checkout_token import checkout_custom
from apps.billing.views import CHECKOUT_PLAN_KEYS, _plan_key_to_variant, _variant_map

pytestmark = pytest.mark.django_db(transaction=True)

SECRET = "test-ls-webhook-secret"
WEBHOOK_URL = "/billing/webhook/ls/"
BUSINESS_VARIANT = "2187025"
SCALE_VARIANT = "2176478"
PAID_VARIANT = "2176466"


def make_user(name, *, plan=AccountPlan.Plan.FREE, sub_id=None, price_id=None):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.filter(user=user).delete()
    AccountPlan.objects.create(
        user=user, plan=plan, status=AccountPlan.Status.ACTIVE,
        stripe_subscription_id=sub_id, stripe_price_id=price_id,
        stripe_customer_id="cust_b" if sub_id else None,
    )
    return user


def login(user):
    client = Client()
    client.force_login(user)
    return client


def post_event(event_name, *, user, variant_id=BUSINESS_VARIANT, sub_id="sub_biz",
               status="active", webhook_id, updated_at=None):
    now = updated_at or timezone.now()
    payload = {
        "meta": {"event_name": event_name, "webhook_id": webhook_id,
                 "custom_data": checkout_custom(user.pk)},
        "data": {"id": sub_id, "attributes": {
            "status": status,
            "variant_id": variant_id,
            "customer_id": "cust_b",
            "updated_at": now.isoformat(),
            "created_at": now.isoformat(),
            "renews_at": (now + timedelta(days=30)).isoformat(),
        }},
    }
    body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return Client().post(WEBHOOK_URL, data=body, content_type="application/json",
                         HTTP_X_SIGNATURE=sig)


def preflight(variant_id):
    return {"data": {"attributes": {"variant_id": variant_id, "cancelled": False, "status": "active"}}}


def business_entitlements(user):
    with transaction.atomic():
        return (
            can_add_domain(user),
            can_invite_member(user),
            can_toggle_catch_all(user),
        )


# --- config wiring --------------------------------------------------------------


def test_business_variant_setting_and_maps():
    from django.conf import settings

    assert settings.LS_VARIANT_BUSINESS == BUSINESS_VARIANT
    assert "business" in CHECKOUT_PLAN_KEYS
    assert _plan_key_to_variant()["business"] == BUSINESS_VARIANT
    assert _variant_map()[BUSINESS_VARIANT] == AccountPlan.Plan.BUSINESS
    # Existing tiers untouched.
    assert _variant_map()[PAID_VARIANT] == AccountPlan.Plan.PAID
    assert _variant_map()[SCALE_VARIANT] == AccountPlan.Plan.SCALE


# --- a) checkout posts variant 2187025 -----------------------------------------


@override_settings(ENABLE_BILLING=True)
def test_a_checkout_business_posts_business_variant():
    user = make_user("biz_a")
    client = login(user)
    captured = {}

    def fake_post(path, body, **kwargs):
        captured["path"] = path
        captured["body"] = body
        return {"data": {"attributes": {"url": "https://checkout.example/biz"}}}

    with patch("apps.billing.views._ls_post", side_effect=fake_post) as mock_post, \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_get") as mock_get:
        resp = client.post(reverse("dashboard:billing-checkout"), {"plan": "business"})

    assert resp.status_code == 303
    assert resp.url == "https://checkout.example/biz"
    mock_post.assert_called_once()
    mock_patch.assert_not_called()
    mock_get.assert_not_called()
    assert captured["path"] == "/checkouts"
    rel = captured["body"]["data"]["relationships"]
    assert rel["variant"]["data"] == {"type": "variants", "id": BUSINESS_VARIANT}
    assert captured["body"]["data"]["attributes"]["checkout_data"]["custom"] == checkout_custom(user.pk)
    # Checkout itself never mutates the plan (webhook is the sole mutator).
    assert AccountPlan.objects.get(user=user).plan == AccountPlan.Plan.FREE


# --- b) webhook created/updated -> business + entitlements ----------------------


@pytest.mark.parametrize("event_name", ["subscription_created", "subscription_updated"])
@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_b_webhook_business_variant_sets_business_and_entitlements(event_name):
    user = make_user(f"biz_b_{event_name}")
    assert business_entitlements(user) == (False, False, False)

    resp = post_event(event_name, user=user, webhook_id=f"wh_b_{event_name}")

    assert resp.status_code == 200
    plan = AccountPlan.objects.get(user=user)
    assert plan.plan == AccountPlan.Plan.BUSINESS
    assert plan.status == AccountPlan.Status.ACTIVE
    assert plan.stripe_price_id == BUSINESS_VARIANT
    assert plan.stripe_subscription_id == "sub_biz"
    assert business_entitlements(user) == (True, True, True)


# --- c) expired -> free, entitlements revoked; D13: no sweep --------------------


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_c_business_expired_back_to_free_entitlements_false_domains_retained():
    from apps.domains.models import Domain

    user = make_user("biz_c")
    t0 = timezone.now()
    assert post_event("subscription_created", user=user, webhook_id="wh_c1",
                      updated_at=t0).status_code == 200
    assert business_entitlements(user) == (True, True, True)
    # A custom domain created while entitled.
    domain = Domain.objects.create(user=user, name="biz-c.example.com")

    resp = post_event("subscription_expired", user=user, webhook_id="wh_c2",
                      status="expired", updated_at=t0 + timedelta(days=31))

    assert resp.status_code == 200
    plan = AccountPlan.objects.get(user=user)
    assert plan.plan == AccountPlan.Plan.FREE
    assert plan.status == AccountPlan.Status.ACTIVE
    assert plan.stripe_subscription_id is None
    assert plan.stripe_price_id is None
    assert business_entitlements(user) == (False, False, False)
    # D13 (accepted risk, no downgrade sweep): pre-existing domain is kept.
    assert Domain.objects.filter(pk=domain.pk).exists()


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_c2_business_cancelled_keeps_business_until_expired():
    """Same grace policy as Pro/Scale: cancel keeps the plan to period end."""
    user = make_user("biz_c2")
    t0 = timezone.now()
    post_event("subscription_created", user=user, webhook_id="wh_c2a", updated_at=t0)
    resp = post_event("subscription_cancelled", user=user, webhook_id="wh_c2b",
                      status="cancelled", updated_at=t0 + timedelta(minutes=1))
    assert resp.status_code == 200
    assert AccountPlan.objects.get(user=user).plan == AccountPlan.Plan.BUSINESS
    assert business_entitlements(user) == (True, True, True)


# --- d) plan changes via PATCH --------------------------------------------------


@override_settings(ENABLE_BILLING=True)
def test_d1_pro_to_business_patches_business_variant_upgrade():
    user = make_user("biz_d1", plan=AccountPlan.Plan.PAID, sub_id="sub_d1", price_id=PAID_VARIANT)
    client = login(user)

    with patch("apps.billing.views._ls_get", return_value=preflight(PAID_VARIANT)), \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = confirm_post(client, user, "business")

    assert resp.status_code == 303
    assert resp.url == reverse("dashboard:billing-page")
    mock_post.assert_not_called()
    mock_patch.assert_called_once_with(
        "/subscriptions/sub_d1",
        {"data": {"type": "subscriptions", "id": "sub_d1",
                  "attributes": {"variant_id": int(BUSINESS_VARIANT), "invoice_immediately": True}}},
    )
    assert AccountPlan.objects.get(user=user).plan == AccountPlan.Plan.PAID


@override_settings(ENABLE_BILLING=True)
def test_d2_business_to_scale_patches_scale_variant_downgrade():
    user = make_user("biz_d2", plan=AccountPlan.Plan.BUSINESS, sub_id="sub_d2",
                     price_id=BUSINESS_VARIANT)
    client = login(user)

    with patch("apps.billing.views._ls_get", return_value=preflight(BUSINESS_VARIANT)), \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = confirm_post(client, user, "scale")

    assert resp.status_code == 303
    mock_post.assert_not_called()
    mock_patch.assert_called_once_with(
        "/subscriptions/sub_d2",
        {"data": {"type": "subscriptions", "id": "sub_d2",
                  "attributes": {"variant_id": int(SCALE_VARIANT), "disable_prorations": True}}},
    )
    assert AccountPlan.objects.get(user=user).plan == AccountPlan.Plan.BUSINESS


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_d3_webhook_updated_business_to_scale_revokes_business_entitlements():
    # BL-04: billing-lifecycle scenario (skill billing-lifecycle)
    user = make_user("biz_d3", plan=AccountPlan.Plan.BUSINESS, sub_id="sub_biz",
                     price_id=BUSINESS_VARIANT)
    assert business_entitlements(user) == (True, True, True)
    resp = post_event("subscription_updated", user=user, variant_id=SCALE_VARIANT,
                      webhook_id="wh_d3")
    assert resp.status_code == 200
    plan = AccountPlan.objects.get(user=user)
    assert plan.plan == AccountPlan.Plan.SCALE
    assert plan.stripe_price_id == SCALE_VARIANT
    
    # the Business-only seats and catch-all are revoked; adding a domain is
    # still allowed (0 owned < Scale's cap).
    assert business_entitlements(user) == (True, False, False)


# --- e) unknown variant still ignored --------------------------------------------


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_e_unknown_variant_ignored_business_untouched():
    user = make_user("biz_e", plan=AccountPlan.Plan.BUSINESS, sub_id="sub_biz",
                     price_id=BUSINESS_VARIANT)
    resp = post_event("subscription_updated", user=user, variant_id="9999999", webhook_id="wh_e")
    assert resp.status_code == 200
    plan = AccountPlan.objects.get(user=user)
    assert plan.plan == AccountPlan.Plan.BUSINESS
    assert plan.stripe_price_id == BUSINESS_VARIANT

    free_user = make_user("biz_e_free")
    resp = post_event("subscription_created", user=free_user, variant_id="9999999",
                      sub_id="sub_e_free", webhook_id="wh_e2")
    assert resp.status_code == 200
    assert AccountPlan.objects.get(user=free_user).plan == AccountPlan.Plan.FREE


# --- /pricing/ + llms.txt copy (step 3) --------------------------------------------


def _tier_cells(content, key):
    import re

    return "\n".join(re.findall(rf'data-tier="{key}"[^>]*>(.*?)</t[hd]>', content, re.DOTALL))


@override_settings(ENABLE_BILLING=True)
def test_pricing_business_self_serve_when_billing_on():
    content = Client().get(reverse("dashboard:pricing")).content.decode()
    cells = _tier_cells(content, "business")
    assert "$149/mo" in cells
    # Trust pass A (#24): available tiers carry no badge, only the CTA.
    assert "Coming soon" not in cells and "Available now" not in cells
    # Same CTA as Scale: anon -> register, ?next= brings them to billing.
    assert f'href="{reverse("accounts:register")}?next={reverse("dashboard:billing-page")}"' in cells
    assert f'href="{reverse("accounts:register")}?next={reverse("dashboard:billing-page")}"' in _tier_cells(content, "scale")
    assert "Start free" in cells
    assert "mailto:" not in cells
    # Honest: only live capabilities, no outbound send / full SMTP verify promise.
    assert "Team seats, catch-all" in cells
    assert "10 domains" in cells  
    assert "(planned)" not in cells
    for banned in ("send", "Send", "SMTP", "outbound", "Outbound"):
        assert banned not in cells, banned
    ent = _tier_cells(content, "enterprise")
    assert "Custom" in ent and "Contact sales" in ent
    assert "mailto:sales@mailsocket.app" in ent
    assert "$399" not in content and "SSO" not in content
    assert "Pro, Scale and Business are available for purchase" in content
    assert "not on sale yet" not in content


@override_settings(ENABLE_BILLING=True)
def test_llms_txt_business_available_enterprise_contact_sales():
    body = Client().get(reverse("dashboard:llms-txt")).content.decode()
    assert "- Business ($149/mo, available now)" in body
    assert "- Enterprise (Custom, contact sales)" in body
    assert "coming soon" not in body.lower()
    assert "$399" not in body


@override_settings(ENABLE_BILLING=True)
def test_wait_caps_display_lists_business_enforced_cap():
    from django.conf import settings

    from apps.dashboard.views import _wait_caps_display

    cap = settings.BILLING_PLAN_LIMITS["business"]["wait_concurrency"]
    assert _wait_caps_display().endswith(f" / {cap} Business")


def test_usage_page_no_longer_says_team_plans_coming_soon():
    user = make_user("biz_usage", plan=AccountPlan.Plan.PAID)
    content = login(user).get(reverse("dashboard:usage")).content.decode()
    assert "Team plans are coming soon" not in content
    assert "Team seats are included in the Business plan" in content


def test_changelog_business_plan_now_available_entry_matches_settings():
    import re

    from django.conf import settings

    page = Client().get(reverse("dashboard:changelog")).content.decode()
    m = re.search(
        r'<time class="changelog-date" datetime="2026-09-30">[^<]*</time>\s*Business plan now available</h2>(.*?)</article>',
        page, re.S,
    )
    assert m, "missing 2026-09-30 'Business plan now available' entry"
    body = m.group(1)
    # Newest entry on the page.
    assert page.index("Business plan now available") < page.index("Remote MCP server")
    biz = settings.BILLING_PLAN_LIMITS["business"]
    assert "$149/mo" in body
    assert f'{biz["inboxes"]} inboxes' in body
    assert f'{biz["messages_per_month"]:,} messages a month' in body
    assert f'{settings.RETENTION_DAYS["business"]}-day retention' in body
    assert f'{biz["wait_concurrency"]} concurrent waits' in body
    assert "mailto:sales@mailsocket.app" in body
    # Honest: no outbound send / SMTP verify / SSO promise.
    for banned in ("send", "SMTP", "SSO", "$399"):
        assert banned not in body, banned





def _make_member_of(owner, name):
    from apps.accounts.models import TeamMembership

    member = make_user(name)
    TeamMembership.objects.create(
        account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER
    )
    return member


def _demo_user():
    from apps.accounts.services import DEMO_USER_EMAIL

    user = make_user("r2demo")
    user.email = DEMO_USER_EMAIL
    user.save(update_fields=["email"])
    return user


def confirm_post(client, user, plan_key):
    from apps.billing.tests.test_billing_plan_change import confirm_post as _cp

    return _cp(client, user, plan_key)


def _agent_user(name="r2agent"):
    from apps.accounts.services import AGENT_ACCOUNT_DOMAIN

    user = make_user(name)
    user.email = f"{name}@{AGENT_ACCOUNT_DOMAIN}"
    user.save(update_fields=["email"])
    return user


def _ls_mocks():
    return (
        patch("apps.billing.views._ls_get"),
        patch("apps.billing.views._ls_post"),
        patch("apps.billing.views._ls_patch"),
    )





@override_settings(ENABLE_BILLING=True, ENABLE_TEAMS=True)
def test_r2_1_member_portal_403_no_ls_call_and_no_portal_form():
    owner = make_user("r2_1_owner", plan=AccountPlan.Plan.BUSINESS, sub_id="sub_owner",
                      price_id=BUSINESS_VARIANT)
    member = _make_member_of(owner, "r2_1_member")
    client = login(member)

    g, p, pa = _ls_mocks()
    with g as mock_get, p as mock_post, pa as mock_patch:
        mock_get.return_value = {"data": {"attributes": {"urls": {
            "customer_portal": "https://portal.example/OWNER"}}}}
        resp = client.post(reverse("dashboard:billing-portal"))
    assert resp.status_code == 403
    assert b"Only the account owner can manage billing." in resp.content
    mock_get.assert_not_called()
    mock_post.assert_not_called()
    mock_patch.assert_not_called()

    page = client.get(reverse("dashboard:billing-page"))
    assert page.status_code == 200
    html = page.content.decode()
    assert reverse("dashboard:billing-portal") not in html
    assert "Open billing portal" not in html
    assert reverse("dashboard:billing-checkout") not in html
    # Read-only plan/status still visible.
    assert "Business" in html and "Active" in html

    
    owner_client = login(owner)
    assert reverse("dashboard:billing-portal") in owner_client.get(
        reverse("dashboard:billing-page")).content.decode()
    with patch("apps.billing.views._ls_get", return_value={"data": {"attributes": {"urls": {
            "customer_portal": "https://portal.example/OWNER"}}}}) as mock_get:
        resp = owner_client.post(reverse("dashboard:billing-portal"))
    assert resp.status_code == 303 and resp.url == "https://portal.example/OWNER"
    mock_get.assert_called_once_with("/subscriptions/sub_owner")


@pytest.mark.parametrize("status", [AccountPlan.Status.PAST_DUE, AccountPlan.Status.SUSPENDED])
@override_settings(ENABLE_BILLING=True, ENABLE_TEAMS=True)
def test_r2_1_member_billing_page_hides_past_due_suspended_portal_buttons(status):
    owner = make_user(f"r2_1s_owner_{status}", plan=AccountPlan.Plan.BUSINESS,
                      sub_id=f"sub_s_{status}", price_id=BUSINESS_VARIANT)
    member = _make_member_of(owner, f"r2_1s_member_{status}")
    owner_client = login(owner)
    member_client = login(member)
    
    # (dashboard_required admits PAST_DUE; SUSPENDED is rendered via the view
    # directly with a patched plan status below).
    AccountPlan.objects.filter(user=owner).update(status=status)
    if status == AccountPlan.Status.PAST_DUE:
        owner_html = owner_client.get(reverse("dashboard:billing-page")).content.decode()
        member_html = member_client.get(reverse("dashboard:billing-page")).content.decode()
    else:
        from django.template.loader import render_to_string
        from django.test import RequestFactory

        from apps.accounts.tenancy import require_owner  

        def render_for(user, is_owner):
            req = RequestFactory().get("/app/billing/")
            req.user = user
            return render_to_string("dashboard/billing.html", {
                "plan": AccountPlan.objects.get(user=owner), "has_subscription": True,
                "plan_status": status, "current_plan_name": "Business", "upgradeable": [],
                "is_owner": is_owner,
            }, request=req)
        owner_html = render_for(owner, True)
        member_html = render_for(member, False)
    portal_url = reverse("dashboard:billing-portal")
    assert portal_url in owner_html
    assert portal_url not in member_html
    assert ("Past due" if status == AccountPlan.Status.PAST_DUE else "Suspended") in member_html


@pytest.mark.parametrize("kind", ["demo", "agent"])
@override_settings(ENABLE_BILLING=True, ENABLE_TEAMS=True)
def test_r2_1_demo_agent_portal_403_no_ls_call(kind):
    user = _demo_user() if kind == "demo" else _agent_user()
    # Give them a sub id so the old code would have reached _ls_get.
    AccountPlan.objects.filter(user=user).update(stripe_subscription_id=f"sub_{kind}")
    client = login(user)
    g, p, pa = _ls_mocks()
    with g as mock_get, p as mock_post, pa as mock_patch:
        mock_get.return_value = {"data": {"attributes": {"urls": {
            "customer_portal": "https://portal.example/x"}}}}
        resp = client.post(reverse("dashboard:billing-portal"))
    assert resp.status_code == 403
    mock_get.assert_not_called()
    mock_post.assert_not_called()
    mock_patch.assert_not_called()
    html = client.get(reverse("dashboard:billing-page")).content.decode()
    assert reverse("dashboard:billing-portal") not in html


# R2 item 2 (sol medium): changelog Business numbers follow settings, not literals.


def test_r2_2_changelog_business_numbers_follow_settings_override():
    import copy
    import re

    from django.conf import settings

    limits = copy.deepcopy(settings.BILLING_PLAN_LIMITS)
    limits["business"].update(inboxes=321, messages_per_month=654321,
                              retention_days=45, wait_concurrency=7)
    retention = dict(settings.RETENTION_DAYS, business=45)
    with override_settings(BILLING_PLAN_LIMITS=limits, RETENTION_DAYS=retention):
        page = Client().get(reverse("dashboard:changelog")).content.decode()
    m = re.search(r"Business plan now available</h2>(.*?)</article>", page, re.S)
    assert m
    body = m.group(1)
    assert "321 inboxes" in body
    assert "654,321 messages a month" in body
    assert "45-day retention" in body
    assert "7 concurrent waits" in body
    # None of the old hard-coded defaults leak through.
    for stale in ("200 inboxes", "250,000", "90-day", "16 concurrent"):
        assert stale not in body, stale


# R2 item 3 (sol low): Business-specific checkout denial — member/demo/agent -> 403, no LS call.


@pytest.mark.parametrize("kind", ["member", "demo", "agent"])
@override_settings(ENABLE_BILLING=True, ENABLE_TEAMS=True)
def test_r2_3_business_checkout_denied_member_demo_agent_no_ls_call(kind):
    if kind == "member":
        owner = make_user("r2_3_owner", plan=AccountPlan.Plan.PAID, sub_id="sub_r2_3",
                          price_id=PAID_VARIANT)
        user = _make_member_of(owner, "r2_3_member")
        expected = b"Only the account owner can manage billing."
    else:
        user = _demo_user() if kind == "demo" else _agent_user("r2_3agent")
        expected = b"Billing is not available for this account."
    client = login(user)
    g, p, pa = _ls_mocks()
    with g as mock_get, p as mock_post, pa as mock_patch:
        mock_post.return_value = {"data": {"attributes": {"url": "https://checkout.example/x"}}}
        mock_get.return_value = preflight(PAID_VARIANT)
        resp = client.post(reverse("dashboard:billing-checkout"), {"plan": "business"})
    assert resp.status_code == 403
    assert expected in resp.content
    mock_get.assert_not_called()
    mock_post.assert_not_called()
    mock_patch.assert_not_called()
    if kind == "member":
        assert AccountPlan.objects.get(user=owner).plan == AccountPlan.Plan.PAID



# self-downgrade by opening a fresh LS checkout.


@pytest.mark.parametrize("plan_key", ["paid", "scale", "business"])
@override_settings(ENABLE_BILLING=True)
def test_r2_4_manual_business_without_subscription_checkout_refused(plan_key):
    user = make_user(f"r2_4_{plan_key}", plan=AccountPlan.Plan.BUSINESS)  # no sub id
    client = login(user)
    g, p, pa = _ls_mocks()
    with g as mock_get, p as mock_post, pa as mock_patch:
        mock_post.return_value = {"data": {"attributes": {"url": "https://checkout.example/x"}}}
        resp = client.post(reverse("dashboard:billing-checkout"), {"plan": plan_key})
    assert resp.status_code == 409
    assert "Your plan is managed by our team — contact sales@mailsocket.app" in resp.content.decode()
    mock_post.assert_not_called()
    mock_get.assert_not_called()
    mock_patch.assert_not_called()
    plan = AccountPlan.objects.get(user=user)
    assert plan.plan == AccountPlan.Plan.BUSINESS and plan.stripe_subscription_id is None


@override_settings(ENABLE_BILLING=True)
def test_r2_4_self_serve_business_with_subscription_still_changes_plan():
    """Guard is scoped to sub-less Business: an LS Business subscriber keeps the PATCH path."""
    user = make_user("r2_4_ls", plan=AccountPlan.Plan.BUSINESS, sub_id="sub_r2_4",
                     price_id=BUSINESS_VARIANT)
    with patch("apps.billing.views._ls_get", return_value=preflight(BUSINESS_VARIANT)), \
            patch("apps.billing.views._ls_patch") as mock_patch, \
            patch("apps.billing.views._ls_post") as mock_post:
        resp = confirm_post(login(user), user, "scale")
    assert resp.status_code == 303
    mock_patch.assert_called_once()
    mock_post.assert_not_called()





def test_r2_5_single_rank_constant():
    import inspect
    import re

    from apps.billing import views

    src = inspect.getsource(views)
    assert len(re.findall(r"^\s*_RANK\s*=", src, re.M)) == 1
    assert views._RANK == {"free": 0, "paid": 1, "scale": 2, "business": 3}
    # The single definition is module-level (column 0), not a function local.
    assert re.search(r"^_RANK = ", src, re.M)
