"Public API."

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan
from apps.inboxes.models import Inbox
from apps.webhooks.models import Webhook

pytestmark = pytest.mark.django_db

CSS = Path(settings.BASE_DIR) / "apps/dashboard/static/dashboard/app.css"


def make_user(name, plan=AccountPlan.Plan.FREE):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user, plan=plan)
    return user


def _client(user):
    client = Client()
    client.force_login(user)
    return client


def _media_block(css, query):
    """Concatenated bodies of every ``@media(<query>){...}`` block."""
    out = []
    for match in re.finditer(r"@media\s*\(" + re.escape(query) + r"\)\s*\{", css):
        depth, i = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        out.append(css[match.end():i - 1])
    return "\n".join(out)





@override_settings(ENABLE_BILLING=True, ENABLE_BYOD=True)
def test_in_app_upgrade_links_go_to_billing():
    user = make_user("up-free")
    client = _client(user)
    billing = reverse("dashboard:billing-page")
    usage = client.get(reverse("dashboard:usage")).content.decode()
    assert f'<a href="{billing}">See plans</a>' in usage
    domains = client.get(reverse("dashboard:domains")).content.decode()
    assert f'<a href="{billing}">Upgrade to Scale or Business</a>' in domains
    webhooks = client.get(reverse("dashboard:webhooks")).content.decode()
    assert f'href="{billing}">Upgrade</a>' in webhooks
    for page in (usage, domains, webhooks):
        main = page.split('<main', 1)[1]
        assert reverse("dashboard:pricing") not in main


@override_settings(ENABLE_BILLING=True, ENABLE_TEAMS=True)
def test_team_upgrade_link_goes_to_billing():
    page = _client(make_user("up-team")).get(reverse("dashboard:team")).content.decode()
    assert f'href="{reverse("dashboard:billing-page")}">Upgrade</a>' in page


@override_settings(ENABLE_BILLING=False)
def test_upgrade_links_fall_back_to_pricing_when_billing_is_off():
    page = _client(make_user("up-off")).get(reverse("dashboard:usage")).content.decode()
    assert f'<a href="{reverse("dashboard:pricing")}">See plans</a>' in page


@override_settings(ENABLE_BILLING=True)
def test_pricing_ctas_same_for_signed_in_and_anonymous():
    """: the signed-in "Upgrade" branch is gone (dead on the apex, and
    it mislabelled tiers at/below the current plan). Signed-in visitors get
    the same CTAs; register?next=/app/billing/ redirects a signed-in user
    to billing via register_view."""
    user = make_user("pricing-authed")
    authed = _client(user).get(reverse("dashboard:pricing")).content.decode()
    anon = Client().get(reverse("dashboard:pricing")).content.decode()
    for key in ("paid", "scale", "business"):
        pattern = rf'<th[^>]*data-tier="{key}"[^>]*>(.*?)</th>'
        head = re.search(pattern, authed, re.S).group(1)
        assert ">Upgrade</a>" not in head, key
        assert head == re.search(pattern, anon, re.S).group(1), key
    scale = re.search(r'<th[^>]*data-tier="scale"[^>]*>(.*?)</th>', anon, re.S).group(1)
    assert f'href="{reverse("accounts:register")}?next=/app/billing/"' in scale
    # The fallback lands a signed-in user on billing.
    response = _client(user).get(reverse("accounts:register") + "?next=/app/billing/")
    assert response.status_code == 302 and response.url == "/app/billing/"





@override_settings(ENABLE_TEAMS=True)
def test_team_form_disabled_with_one_upgrade_card_on_plan_without_seats():
    page = _client(make_user("team-free")).get(reverse("dashboard:team")).content.decode()
    business = AccountPlan.DISPLAY_NAMES["business"]
    assert f"Team seats come with {business}." in page
    assert page.count('id="team-upsell"') == 1
    form = re.search(r'<form class="inline-form create-form".*?</form>', page, re.S).group(0)
    assert '<fieldset class="bare-fieldset" disabled>' in form
    # The duplicate "No seats available" flash is gone for this case.
    assert "No seats available on your current plan" not in page
    assert 'class="card flash"' not in page


@override_settings(ENABLE_TEAMS=True)
def test_team_form_enabled_on_business():
    page = _client(make_user("team-biz", AccountPlan.Plan.BUSINESS)).get(
        reverse("dashboard:team")
    ).content.decode()
    assert "team-upsell" not in page
    assert '<fieldset class="bare-fieldset">' in page


@override_settings(ENABLE_TEAMS=True)
def test_team_invite_server_check_unchanged_on_free():
    client = _client(make_user("team-post"))
    response = client.post(
        reverse("dashboard:team-invite-create"), {"email": "mate@example.com"}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 403
    assert "Your plan does not have an available team seat." in response.content.decode()


def test_webhooks_free_plan_neutral_card_and_disabled_create():
    user = make_user("wh-free")
    Inbox.objects.create(user=user, routing_key="wh_free_box", label="Free box")
    page = _client(user).get(reverse("dashboard:webhooks")).content.decode()
    paid = AccountPlan.DISPLAY_NAMES["paid"]
    assert f"Webhooks are on {paid} and above." in page
    assert page.count('id="webhooks-upsell"') == 1
    # Neutral card, not the flash/alert style.
    upsell = re.search(r'<section class="card upsell" id="webhooks-upsell">', page)
    assert upsell
    assert "Your current plan cannot create or enable webhooks" not in page
    form = re.search(r'<form class="webhook-form".*?</form>', page, re.S).group(0)
    assert '<fieldset class="bare-fieldset" disabled>' in form
    assert ">Create webhook</button>" in form


def test_webhooks_existing_webhook_on_free_stays_editable_for_disable():
    user = make_user("wh-free-existing")
    inbox = Inbox.objects.create(user=user, routing_key="wh_free_existing", label="Old")
    Webhook(inbox=inbox, url="https://receiver.example/hook", is_enabled=True).save(_url_validated=True)
    page = _client(user).get(reverse("dashboard:webhooks")).content.decode()
    form = re.search(r'<form class="webhook-form".*?</form>', page, re.S).group(0)
    assert "disabled>" not in form  # the Save form still lets them disable it


def test_webhooks_paid_plan_has_no_upsell_and_enabled_form():
    user = make_user("wh-paid", AccountPlan.Plan.PAID)
    Inbox.objects.create(user=user, routing_key="wh_paid_box", label="Paid box")
    page = _client(user).get(reverse("dashboard:webhooks")).content.decode()
    assert "webhooks-upsell" not in page
    assert '<fieldset class="bare-fieldset">' in page


def test_webhooks_free_post_single_error_and_server_check_unchanged():
    user = make_user("wh-post")
    inbox = Inbox.objects.create(user=user, routing_key="wh_post_box", label="Post")
    response = _client(user).post(
        reverse("dashboard:webhook-save", args=[inbox.public_id]),
        {"url": "https://receiver.example/hook", "is_enabled": "on"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 422
    body = response.content.decode()
    assert body.count("Your plan cannot configure or enable webhooks.") == 1
    assert "Your current plan cannot create or enable webhooks" not in body
    assert not Webhook.objects.filter(inbox=inbox).exists()


def test_upsell_plan_names_follow_settings(settings):
    from apps.dashboard.views import lowest_plan_name_with

    assert lowest_plan_name_with(lambda limits: limits.get("webhooks")) == AccountPlan.DISPLAY_NAMES["paid"]
    settings.BILLING_PLAN_LIMITS = {
        **settings.BILLING_PLAN_LIMITS,
        "paid": {**settings.BILLING_PLAN_LIMITS["paid"], "webhooks": False},
    }
    assert lowest_plan_name_with(lambda limits: limits.get("webhooks")) == AccountPlan.DISPLAY_NAMES["scale"]





def test_mobile_nav_wraps_all_items_without_scrollbar():
    css = CSS.read_text()
    phone = _media_block(css, "max-width:640px")
    assert re.search(r"\.sidebar nav\{[^}]*flex-wrap:wrap", phone)
    assert re.search(r"\.sidebar nav\{[^}]*overflow-x:visible", phone)
    assert re.search(r"\.sidebar nav\{[^}]*mask-image:none", phone)
    tablet = _media_block(css, "max-width:800px")
    assert ".sidebar nav::-webkit-scrollbar{display:none}" in tablet
    assert re.search(r"\.sidebar nav\{scrollbar-width:none\}", tablet)


def test_dashboard_pages_have_no_inline_style_or_script():
    user = make_user("csp-check", AccountPlan.Plan.PAID)
    Inbox.objects.create(user=user, routing_key="csp_box", label="CSP")
    client = _client(user)
    for name in ("dashboard:overview", "dashboard:inboxes", "dashboard:webhooks", "dashboard:usage"):
        page = client.get(reverse(name)).content.decode()
        assert "style=" not in page and "<style" not in page, name
        assert page.count("<script") == page.count('<script defer src="'), name





def test_pricing_mobile_sticky_label_column_and_swipe_hint():
    page = Client().get(reverse("dashboard:pricing")).content.decode()
    assert '<p class="pricing-swipe-hint" aria-hidden="true">Swipe sideways to compare all 5 plans →</p>' in page
    css = CSS.read_text()
    phone = _media_block(css, "max-width:640px")
    assert ".pricing-swipe-hint{display:block" in phone
    assert re.search(r"\.pricing-compare-table th:first-child\{position:sticky;left:0", phone)
    assert ".pricing-swipe-hint{display:none}" in css  # hidden on desktop
    assert "style=" not in page
