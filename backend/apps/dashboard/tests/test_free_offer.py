"""The paused public offer does not change stored plan entitlements."""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.checks import paid_plans_on_sale_errors
from apps.accounts.models import Account, AccountPlan


@pytest.mark.parametrize(
    "name",
    ["pricing", "root", "compare-agentmail", "llms-txt"],
)
def test_paused_public_offer_has_no_paid_prices(settings, name):
    settings.PAID_PLANS_ON_SALE = False
    response = Client().get(reverse(f"dashboard:{name}"))
    assert response.status_code == 200
    body = response.content.decode()
    for price in ("$12", "$49", "$149"):
        assert price not in body
    assert "billing/checkout" not in body
    assert "mailsocket.lemonsqueezy.com" not in body
    assert "Free" in body


@pytest.mark.parametrize("name", ["pricing", "root", "compare-agentmail", "llms-txt"])
def test_on_sale_restores_paid_public_copy(settings, name):
    settings.PAID_PLANS_ON_SALE = True
    body = Client().get(reverse(f"dashboard:{name}")).content.decode()
    assert "$12" in body


def test_paused_pricing_shows_real_free_limits(settings):
    settings.PAID_PLANS_ON_SALE = False
    body = Client().get(reverse("dashboard:pricing")).content.decode()
    assert "Paid plans are paused. Need higher limits? Email" in body
    assert "sales@mailsocket.app" in body
    assert 'data-tier="paid"' not in body
    assert 'data-tier="free"' in body


@pytest.mark.django_db
def test_free_billing_has_no_upgrade_checkout(settings):
    settings.PAID_PLANS_ON_SALE = False
    settings.ENABLE_BILLING = True
    user = get_user_model().objects.create_user(
        username="paused-billing", email="paused@example.test", password="test-password"
    )
    Account.objects.create(user=user, email_normalized=user.email, email_verified_at=timezone.now())
    AccountPlan.objects.create(user=user, plan=AccountPlan.Plan.FREE)
    client = Client()
    client.force_login(user)
    body = client.get(reverse("dashboard:billing-page")).content.decode()
    assert "Paid plans are paused" in body
    assert "billing/checkout" not in body
    assert "Upgrade to" not in body


def test_invalid_public_offer_switch_is_system_error(settings):
    settings.PAID_PLANS_ON_SALE = None
    settings.PAID_PLANS_ON_SALE_RAW = "yes"
    errors = paid_plans_on_sale_errors()
    assert [error.id for error in errors] == ["mailsocket.E009"]
    assert "yes" in errors[0].msg
