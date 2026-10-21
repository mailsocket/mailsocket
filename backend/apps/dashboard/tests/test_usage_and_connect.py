"Public API."

import re

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan, MonthlyUsage
from apps.inboxes.models import Inbox

pytestmark = pytest.mark.django_db(transaction=True)


def make_user(name, plan=AccountPlan.Plan.PAID):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user, plan=plan)
    return user


@pytest.fixture
def tenants():
    alice = make_user("a4-alice")
    bob = make_user("a4-bob")
    alice_inbox = Inbox.objects.create(user=alice, routing_key="alice_a4_box", label="Alice")
    bob_inbox = Inbox.objects.create(user=bob, routing_key="bob_a4_box", label="Bob secret")
    return alice, bob, alice_inbox, bob_inbox


def _usage_url():
    return reverse("dashboard:usage")


def test_usage_requires_login():
    # Anonymous -> login redirect (302), never a 200.
    response = Client().get(_usage_url())
    assert response.status_code == 302
    assert response.url.startswith("/login/")


def test_usage_shows_owners_real_numbers():
    user = make_user("usage-owner")
    Inbox.objects.create(user=user, routing_key="usage_owner_box", label="Owner")
    period_start = timezone.now().replace(day=1).date()
    MonthlyUsage.objects.create(
        user=user,
        period_start=period_start,
        messages_received=42,
        api_requests=7,
        webhook_attempts=3,
    )

    client = Client()
    client.force_login(user)
    response = client.get(_usage_url())
    assert response.status_code == 200
    content = response.content.decode()

    assert "42" in content  # messages received
    assert "10,000" in content  # paid message limit (UI-LAYOUT: thousands-grouped)
    assert "7" in content  # api requests
    assert "3" in content  # webhook attempts
    assert "1" in content  # inbox count
    assert "10" in content  # paid inbox limit
    # CSP-safe: no inline script (every <script> tag is an external src), no |safe.
    assert content.count("<script") == content.count('<script defer src="')
    assert "|safe" not in content


def test_usage_never_leaks_second_user():
    alice = make_user("usage-alice")
    bob = make_user("usage-bob")
    period_start = timezone.now().replace(day=1).date()
    MonthlyUsage.objects.create(user=alice, period_start=period_start, messages_received=42)
    MonthlyUsage.objects.create(user=bob, period_start=period_start, messages_received=999)

    client = Client()
    client.force_login(alice)
    content = client.get(_usage_url()).content.decode()

    assert "42" in content
    assert "999" not in content


def test_usage_plan_limits_free_vs_paid():
    free_user = make_user("usage-free", plan=AccountPlan.Plan.FREE)
    paid_user = make_user("usage-paid", plan=AccountPlan.Plan.PAID)
    period_start = timezone.now().replace(day=1).date()
    MonthlyUsage.objects.create(user=free_user, period_start=period_start, messages_received=0)
    MonthlyUsage.objects.create(user=paid_user, period_start=period_start, messages_received=0)

    free_client = Client()
    free_client.force_login(free_user)
    free_content = free_client.get(_usage_url()).content.decode()
    assert "Free" in free_content
    assert " / 1,000" in free_content  

    paid_client = Client()
    paid_client.force_login(paid_user)
    paid_content = paid_client.get(_usage_url()).content.decode()
    # QA #7: the stored plan "paid" is shown by its customer-facing name.
    assert "<strong>Pro</strong>" in paid_content
    assert "<strong>Paid</strong>" not in paid_content
    assert " / 10,000" in paid_content  # paid monthly message limit (UI-LAYOUT: thousands-grouped)


def test_usage_warning_when_near_or_exceeded():
    user = make_user("usage-warn")
    period_start = timezone.now().replace(day=1).date()
    # 85% of the 10000 paid limit -> near-limit warning state (>= 80%).
    MonthlyUsage.objects.create(user=user, period_start=period_start, messages_received=8500)

    client = Client()
    client.force_login(user)
    content = client.get(_usage_url()).content.decode()
    assert "usage-bar-warn" in content

    # exceeded -> still warns, plus the soft-limit copy.
    MonthlyUsage.objects.filter(user=user).update(messages_received=12000)
    content = client.get(_usage_url()).content.decode()
    assert "usage-bar-warn" in content
    assert "Soft limit exceeded" in content


def test_connect_snippet_shows_owners_inbox_only(tenants):
    alice, _, alice_inbox, bob_inbox = tenants
    client = Client()
    client.force_login(alice)

    response = client.get(reverse("dashboard:inboxes"))
    assert response.status_code == 200
    content = response.content.decode()

    
    assert alice_inbox.address in content
    assert f"/api/v1/inboxes/{alice_inbox.public_id}/messages/wait" in content

    # Foreign inbox id / address is never rendered.
    assert bob_inbox.public_id not in content
    assert bob_inbox.address not in content

    # Placeholder key only — no real-key secret appears in the snippet; the
    
    assert "$MAILSOCKET_API_KEY" in content
    assert 'os.environ["MAILSOCKET_API_KEY"]' in content
    assert "ms_live_\u2026" not in content
    assert re.search(r"ms_live_[A-Za-z0-9_-]{8,}", content) is None

    # CSP-safe: no inline script (every <script> tag is an external src), no |safe.
    assert content.count("<script") == content.count('<script defer src="')
    assert "|safe" not in content


def test_connect_snippet_has_api_key_pointer(tenants):
    alice, _, _, _ = tenants
    client = Client()
    client.force_login(alice)
    content = client.get(reverse("dashboard:inboxes")).content.decode()
    assert reverse("dashboard:api-keys") in content
    assert "Create one" in content
