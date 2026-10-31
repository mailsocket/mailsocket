"""Paused sales hide purchase paths without hiding existing subscriptions."""

import re

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan, TeamMembership
from apps.billing.models import BillingPayment
from apps.dashboard.views import _pricing_context


def make_account(name, plan):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="test-password"
    )
    Account.objects.create(user=user, email_normalized=user.email, email_verified_at=timezone.now())
    AccountPlan.objects.create(user=user, plan=plan)
    return user


def assert_no_purchase_path(body):
    for price in ("$12", "$49", "$149"):
        # Competitor prices (e.g. MailSlurp $129.99) are not our offer.
        assert not re.search(re.escape(price) + r"(?![\d.])", body)
    assert "billing/checkout" not in body
    assert "billing/change" not in body
    assert "Upgrade to" not in body
    assert 'href="/app/billing/"' not in body


@pytest.mark.parametrize(
    "name",
    [
        "docs",
        "api-reference",
        "faq",
        "changelog",
        "security",
        "versioning",
        "compare-mailpit",
        "compare-mailosaur",
        "compare-mailtrap",
        "compare-mailslurp",
    ],
)
def test_paused_public_pages_do_not_sell_paid_plans(settings, name):
    settings.PAID_PLANS_ON_SALE = False
    response = Client().get(reverse(f"dashboard:{name}"))
    assert response.status_code == 200
    assert_no_purchase_path(response.content.decode())


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["usage", "domains", "team", "webhooks"])
def test_paused_free_app_pages_do_not_upsell(settings, name):
    settings.PAID_PLANS_ON_SALE = False
    settings.ENABLE_TEAMS = True
    settings.ENABLE_BYOD = True
    user = make_account(f"paused-{name}", AccountPlan.Plan.FREE)
    client = Client()
    client.force_login(user)
    response = client.get(reverse(f"dashboard:{name}"))
    assert response.status_code == 200
    assert_no_purchase_path(response.content.decode())


@pytest.mark.django_db
@pytest.mark.parametrize("plan_key", [AccountPlan.Plan.SCALE, AccountPlan.Plan.BUSINESS])
def test_paused_paid_billing_keeps_subscription_not_other_plan_offers(settings, plan_key):
    settings.PAID_PLANS_ON_SALE = False
    settings.ENABLE_BILLING = True
    user = make_account(f"paused-owner-{plan_key}", plan_key)
    account_plan = AccountPlan.objects.get(user=user)
    account_plan.stripe_subscription_id = f"sub-{plan_key}"
    account_plan.save(update_fields=["stripe_subscription_id"])
    BillingPayment.objects.create(
        user=user,
        ls_invoice_id=f"inv-{plan_key}",
        amount_cents=500,
        paid_at=timezone.now(),
    )
    client = Client()
    client.force_login(user)
    response = client.get(reverse("dashboard:billing-page"))
    assert response.status_code == 200
    body = response.content.decode()
    tier = next(t for t in _pricing_context()["tiers"] if t["key"] == plan_key)
    assert "Current plan" in body
    assert tier["name"] in body
    assert f'class="plan-price">· {tier["price"]}' in body
    assert "Open billing portal" in body
    assert "Recent payments" in body
    assert "Upgrade your plan" not in body
    assert "Upgrade to" not in body
    assert "billing/checkout" not in body
    assert "billing/change" not in body
    for other in (t for t in _pricing_context()["tiers"] if t["key"] != plan_key and t["key"] != "free"):
        assert f"<strong>{other['price']}</strong>" not in body


@pytest.mark.django_db
def test_paused_paid_team_member_billing_stays_read_only(settings):
    settings.PAID_PLANS_ON_SALE = False
    settings.ENABLE_BILLING = True
    settings.ENABLE_TEAMS = True
    owner = make_account("paused-team-owner", AccountPlan.Plan.SCALE)
    member = make_account("paused-team-member", AccountPlan.Plan.FREE)
    TeamMembership.objects.create(account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER)
    client = Client()
    client.force_login(member)
    response = client.get(reverse("dashboard:billing-page"))
    assert response.status_code == 200
    body = response.content.decode()
    assert "Current plan" in body
    assert "Scale" in body
    assert "Only the account owner can change the plan or manage billing." in body
    assert "Open billing portal" not in body
    assert "Upgrade your plan" not in body
    assert "Upgrade to" not in body
    assert "billing/checkout" not in body
    assert "billing/change" not in body
