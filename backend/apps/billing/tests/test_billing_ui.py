"Public API."

import pytest
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan

pytestmark = pytest.mark.django_db(transaction=True)


def make_user(name, *, plan=AccountPlan.Plan.FREE, status=AccountPlan.Status.ACTIVE, email=None):
    user = get_user_model().objects.create_user(
        username=name, email=email or f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=email or f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.filter(user=user).delete()
    AccountPlan.objects.create(user=user, plan=plan, status=status)
    return user


def login(user):
    client = Client()
    client.force_login(user)
    return client


@override_settings(ENABLE_BILLING=True)
def test_ui_business_owner_billing_page_renders_200_not_500():
    "Public API."
    user = make_user("uibusiness1", plan=AccountPlan.Plan.BUSINESS, status=AccountPlan.Status.ACTIVE)
    client = login(user)
    resp = client.get(reverse("dashboard:billing-page"))
    assert resp.status_code == 200


@override_settings(ENABLE_BILLING=True)
def test_ui_free_user_shows_both_upgrades_and_no_manage_billing():
    user = make_user("uifree1", plan=AccountPlan.Plan.FREE)
    client = login(user)
    resp = client.get(reverse("dashboard:billing-page"))
    assert resp.status_code == 200
    assert b"Upgrade to Pro" in resp.content
    assert b"Upgrade to Scale" in resp.content
    assert b"Upgrade to Business" in resp.content
    assert b"14-day" in resp.content
    assert b"Manage billing" not in resp.content


@override_settings(ENABLE_BILLING=True)
def test_ui_paid_active_user_shows_scale_only_and_manage_billing():
    user = make_user("uipaid1", plan=AccountPlan.Plan.PAID, status=AccountPlan.Status.ACTIVE)
    AccountPlan.objects.filter(user=user).update(stripe_subscription_id="sub_uipaid1")
    client = login(user)
    resp = client.get(reverse("dashboard:billing-page"))
    assert resp.status_code == 200
    assert b"Upgrade to Scale" in resp.content
    assert b"Upgrade to Business" in resp.content
    assert b"Upgrade to Pro" not in resp.content
    assert b"Manage billing" in resp.content


@override_settings(ENABLE_BILLING=True)
def test_ui_scale_user_shows_business_upgrade_only():
    user = make_user("uiscale1", plan=AccountPlan.Plan.SCALE, status=AccountPlan.Status.ACTIVE)
    client = login(user)
    resp = client.get(reverse("dashboard:billing-page"))
    assert resp.status_code == 200
    assert b"Upgrade to Business" in resp.content
    assert b'name="plan" value="business"' in resp.content
    assert b"Upgrade to Pro" not in resp.content
    assert b"Upgrade to Scale" not in resp.content
    assert b"highest self-serve plan" not in resp.content


@override_settings(ENABLE_BILLING=True)
def test_ui_business_user_no_upgrade_shows_highest_plan_empty_state():
    user = make_user("uibiz2", plan=AccountPlan.Plan.BUSINESS, status=AccountPlan.Status.ACTIVE)
    client = login(user)
    resp = client.get(reverse("dashboard:billing-page"))
    assert resp.status_code == 200
    assert b"Upgrade to" not in resp.content
    assert b"highest self-serve plan" in resp.content


@override_settings(ENABLE_BILLING=True)
def test_ui_past_due_user_shows_warning_and_update_payment_no_upsell():
    user = make_user("uipastdue1", plan=AccountPlan.Plan.PAID, status=AccountPlan.Status.PAST_DUE)
    AccountPlan.objects.filter(user=user).update(stripe_subscription_id="sub_uipastdue1")
    client = login(user)
    resp = client.get(reverse("dashboard:billing-page"))
    assert resp.status_code == 200
    assert b"Past due" in resp.content
    assert b"Update payment method" in resp.content
    assert b"Upgrade to" not in resp.content


@override_settings(ENABLE_BILLING=True)
def test_ui_past_due_copy_matches_the_enforced_entitlements():
    "Public API."
    from apps.accounts import entitlements

    user = make_user("uipastdue2", plan=AccountPlan.Plan.PAID, status=AccountPlan.Status.PAST_DUE)
    AccountPlan.objects.filter(user=user).update(stripe_subscription_id="sub_uipastdue2")
    resp = login(user).get(reverse("dashboard:billing-page"))
    body = resp.content.decode()
    assert "Your plan stays active" not in body
    assert "existing inboxes keep receiving mail and your API keys keep working" in body
    assert "paid actions are paused" in body
    assert "t create new inboxes, webhooks or domains" in body  # "can't" may render as can&#x27;t
    # the copy is only honest while the enforcement really says so
    with transaction.atomic():
        assert entitlements.can_create_inbox(user) is False
        assert entitlements.can_enable_webhook(user) is False


@override_settings(ENABLE_BILLING=True)
def test_ui_current_plan_name_shows_pro_not_paid():
    user = make_user("uihonesty1", plan=AccountPlan.Plan.PAID, status=AccountPlan.Status.ACTIVE)
    client = login(user)
    resp = client.get(reverse("dashboard:billing-page"))
    assert resp.status_code == 200
    assert b"Pro" in resp.content
    assert b"Paid" not in resp.content


@override_settings(ENABLE_BILLING=False)
def test_ui_flag_off_still_404():
    user = make_user("uiflagoff1", plan=AccountPlan.Plan.PAID)
    client = login(user)
    resp = client.get(reverse("dashboard:billing-page"))
    assert resp.status_code == 404
