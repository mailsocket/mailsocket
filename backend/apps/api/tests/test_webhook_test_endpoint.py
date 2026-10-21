"Public API."

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, AccountPlan
from apps.accounts.services import create_api_key
from apps.inboxes.models import Inbox
from apps.webhooks.models import WebhookDelivery
from apps.webhooks.security import SSRFValidationError
from apps.webhooks.services import create_webhook, derive_signing_secret, verify_signature
from apps.webhooks.transport import DeliveryResponse, DeliveryTransportError

pytestmark = pytest.mark.django_db(transaction=True)

PUBLIC = ["93.184.216.34"]


def public_resolver(_hostname):
    return PUBLIC


def make_user(name, *, plan=AccountPlan.Plan.PAID):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user, plan=plan)
    return user


def client_for(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


@pytest.fixture
def tenants():
    alice = make_user("wht_alice")
    bob = make_user("wht_bob")
    _, alice_token = create_api_key(alice, "Alice webhook-test CI")
    _, bob_token = create_api_key(bob, "Bob webhook-test CI")
    alice_inbox = Inbox.objects.create(user=alice, routing_key="wht_alice_box")
    bob_inbox = Inbox.objects.create(user=bob, routing_key="wht_bob_box")
    return {
        "alice": alice,
        "bob": bob,
        "alice_token": alice_token,
        "bob_token": bob_token,
        "alice_inbox": alice_inbox,
        "bob_inbox": bob_inbox,
    }


def _test_url(inbox):
    return f"/api/v1/inboxes/{inbox.public_id}/webhook/test"


def test_1_happy_path_returns_delivered_true_and_response_status(tenants, monkeypatch):
    inbox = tenants["alice_inbox"]
    webhook, _ = create_webhook(inbox, "https://receiver.example/hook", resolver=public_resolver)
    monkeypatch.setattr(
        "apps.api.views.post_json_pinned", lambda *a, **k: DeliveryResponse(200)
    )

    response = client_for(tenants["alice_token"]).post(_test_url(inbox))

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["delivered"] is True
    assert data["response_status"] == 200
    assert "event_id" in data and data["event_id"].startswith("evt_test_")


def test_2_signature_matches_the_real_delivery_signing_primitives(tenants, monkeypatch):
    inbox = tenants["alice_inbox"]
    webhook, _ = create_webhook(inbox, "https://receiver.example/hook", resolver=public_resolver)
    captured = {}

    def fake_transport(url, body, headers):
        captured["url"] = url
        captured["body"] = body
        captured["headers"] = headers
        return DeliveryResponse(200)

    monkeypatch.setattr("apps.api.views.post_json_pinned", fake_transport)

    response = client_for(tenants["alice_token"]).post(_test_url(inbox))

    assert response.status_code == 200
    header = captured["headers"]["X-Mailsocket-Signature"]
    assert verify_signature(derive_signing_secret(webhook), header, captured["body"]) is True
    assert captured["headers"]["X-Mailsocket-Event"] == "webhook.test"


def test_3_receiver_500_still_delivered_true_no_retry_row_created(tenants, monkeypatch):
    inbox = tenants["alice_inbox"]
    create_webhook(inbox, "https://receiver.example/hook", resolver=public_resolver)
    monkeypatch.setattr(
        "apps.api.views.post_json_pinned", lambda *a, **k: DeliveryResponse(500)
    )

    response = client_for(tenants["alice_token"]).post(_test_url(inbox))

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["delivered"] is True
    assert data["response_status"] == 500
    assert WebhookDelivery.objects.count() == 0


def test_4_ssrf_internal_ip_resolver_rejected_no_socket_opened(tenants, monkeypatch):
    inbox = tenants["alice_inbox"]
    create_webhook(inbox, "https://internal.example/hook", resolver=public_resolver)

    socket_calls = []

    def no_socket(*args, **kwargs):
        socket_calls.append(args)
        raise AssertionError("a real socket connection was attempted")

    # Re-resolve the SAME hostname to an internal address at send time
    # (simulating a resolver injection / DNS-rebind attempt) and prove
    # post_json_pinned rejects it before ever calling socket_factory.
    import apps.webhooks.transport as transport_module

    def rebinding_post_json_pinned(url, body, headers):
        return transport_module.post_json_pinned(
            url, body, headers, resolver=lambda _h: ["169.254.169.254"], socket_factory=no_socket
        )

    monkeypatch.setattr("apps.api.views.post_json_pinned", rebinding_post_json_pinned)

    response = client_for(tenants["alice_token"]).post(_test_url(inbox))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "webhook_ssrf_rejected"
    assert socket_calls == []
    assert WebhookDelivery.objects.count() == 0


def test_6_no_webhook_configured_returns_404_not_500(tenants):
    inbox = tenants["alice_inbox"]

    response = client_for(tenants["alice_token"]).post(_test_url(inbox))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "webhook_not_configured"


def test_7_free_plan_gets_403_entitlement_required_sends_nothing(tenants, monkeypatch):
    free_user = make_user("wht_free", plan=AccountPlan.Plan.FREE)
    _, free_token = create_api_key(free_user, "Free webhook-test CI")
    inbox = Inbox.objects.create(user=free_user, routing_key="wht_free_box")
    sent = []
    monkeypatch.setattr(
        "apps.api.views.post_json_pinned", lambda *a, **k: sent.append(1) or DeliveryResponse(200)
    )

    response = client_for(free_token).post(_test_url(inbox))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "webhook_entitlement_required"
    assert sent == []


def test_8_rate_limit_per_inbox_and_per_account(tenants, monkeypatch):
    inbox = tenants["alice_inbox"]
    create_webhook(inbox, "https://receiver.example/hook", resolver=public_resolver)
    monkeypatch.setattr(
        "apps.api.views.post_json_pinned", lambda *a, **k: DeliveryResponse(200)
    )
    client = client_for(tenants["alice_token"])

    # Drive the per-inbox bucket directly to its cap (10/hour) rather than
    # guessing which hashed RateLimitBucket row belongs to it.
    from apps.accounts.services import increment_rate_limit

    for _ in range(10):
        increment_rate_limit(
            "api:webhook-test:inbox:hour", inbox.public_id, limit=10, window_seconds=3600
        )

    response = client.post(_test_url(inbox))

    assert response.status_code == 429
    assert "Retry-After" in response


def test_8b_account_rate_limit_cannot_be_evaded_by_switching_inbox(tenants, monkeypatch):
    "Public API."
    from apps.accounts.services import increment_rate_limit

    alice = tenants["alice"]
    inbox_a = tenants["alice_inbox"]
    inbox_b = Inbox.objects.create(user=alice, routing_key="wht_alice_box_2")
    inbox_c = Inbox.objects.create(user=alice, routing_key="wht_alice_box_3")
    for inbox in (inbox_a, inbox_b, inbox_c):
        create_webhook(inbox, "https://receiver.example/hook", resolver=public_resolver)
    monkeypatch.setattr(
        "apps.api.views.post_json_pinned", lambda *a, **k: DeliveryResponse(200)
    )

    # Fill the ACCOUNT bucket (30/hour) directly — well under any single
    # inbox's 10/hour cap, so a per-inbox-only limiter would never trigger.
    for _ in range(30):
        increment_rate_limit(
            "api:webhook-test:account:hour", str(alice.pk), limit=30, window_seconds=3600
        )

    client = client_for(tenants["alice_token"])
    responses = [
        client.post(_test_url(inbox_a)),
        client.post(_test_url(inbox_b)),
        client.post(_test_url(inbox_c)),
    ]

    assert all(response.status_code == 429 for response in responses)
    for response in responses:
        assert "Retry-After" in response


def test_9_tenant_negative_cannot_test_foreign_inbox_webhook(tenants, monkeypatch):
    bob_inbox = tenants["bob_inbox"]
    create_webhook(bob_inbox, "https://bob-receiver.example/hook", resolver=public_resolver)
    sent = []
    monkeypatch.setattr(
        "apps.api.views.post_json_pinned", lambda *a, **k: sent.append(1) or DeliveryResponse(200)
    )

    response = client_for(tenants["alice_token"]).post(_test_url(bob_inbox))

    assert response.status_code == 404
    
    # returns (not_found), not webhook_not_configured — a foreign inbox must
    # 404 at the ownership check, before ever reaching the webhook lookup.
    assert response.json()["error"]["code"] == "not_found"
    assert sent == []


def test_10_transport_timeout_returns_502_not_500_no_retry_row(tenants, monkeypatch):
    inbox = tenants["alice_inbox"]
    create_webhook(inbox, "https://receiver.example/hook", resolver=public_resolver)

    def raise_timeout(*args, **kwargs):
        raise DeliveryTransportError("timeout")

    monkeypatch.setattr("apps.api.views.post_json_pinned", raise_timeout)

    response = client_for(tenants["alice_token"]).post(_test_url(inbox))

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "webhook_unreachable"
    assert WebhookDelivery.objects.count() == 0


def test_allowed_when_webhook_disabled(tenants, monkeypatch):
    """Decision Q3: test is allowed even when the webhook is disabled."""
    inbox = tenants["alice_inbox"]
    webhook, _ = create_webhook(inbox, "https://receiver.example/hook", enabled=False, resolver=public_resolver)
    assert webhook.is_enabled is False
    monkeypatch.setattr(
        "apps.api.views.post_json_pinned", lambda *a, **k: DeliveryResponse(200)
    )

    response = client_for(tenants["alice_token"]).post(_test_url(inbox))

    assert response.status_code == 200
    assert response.json()["data"]["delivered"] is True


def test_11_success_increments_webhook_attempts(tenants, monkeypatch):
    "Public API."
    from apps.accounts.entitlements import usage_summary

    inbox = tenants["alice_inbox"]
    alice = tenants["alice"]
    create_webhook(inbox, "https://receiver.example/hook", resolver=public_resolver)
    monkeypatch.setattr(
        "apps.api.views.post_json_pinned", lambda *a, **k: DeliveryResponse(200)
    )

    before = usage_summary(alice)["webhook_attempts"]
    response = client_for(tenants["alice_token"]).post(_test_url(inbox))
    after = usage_summary(alice)["webhook_attempts"]

    assert response.status_code == 200
    assert after == before + 1


def test_12_failed_transport_still_increments_webhook_attempts(tenants, monkeypatch):
    "Public API."
    from apps.accounts.entitlements import usage_summary

    inbox = tenants["alice_inbox"]
    alice = tenants["alice"]
    create_webhook(inbox, "https://receiver.example/hook", resolver=public_resolver)

    def raise_timeout(*args, **kwargs):
        raise DeliveryTransportError("timeout")

    monkeypatch.setattr("apps.api.views.post_json_pinned", raise_timeout)

    before = usage_summary(alice)["webhook_attempts"]
    response = client_for(tenants["alice_token"]).post(_test_url(inbox))
    after = usage_summary(alice)["webhook_attempts"]

    assert response.status_code == 502
    assert after == before + 1


def test_13_ssrf_rejected_still_increments_webhook_attempts(tenants, monkeypatch):
    "Public API."
    from apps.accounts.entitlements import usage_summary

    inbox = tenants["alice_inbox"]
    alice = tenants["alice"]
    create_webhook(inbox, "https://internal.example/hook", resolver=public_resolver)

    def raise_ssrf(*args, **kwargs):
        raise SSRFValidationError("blocked_ip")

    monkeypatch.setattr("apps.api.views.post_json_pinned", raise_ssrf)

    before = usage_summary(alice)["webhook_attempts"]
    response = client_for(tenants["alice_token"]).post(_test_url(inbox))
    after = usage_summary(alice)["webhook_attempts"]

    assert response.status_code == 400
    assert after == before + 1
