"Public API."
import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient as RestAPIClient

from apps.accounts.models import Account, AccountPlan
from apps.accounts.services import create_api_key
from apps.domains.models import Domain
from apps.inboxes.models import Inbox

pytestmark = pytest.mark.django_db(transaction=True)


def make_user(name, *, plan=AccountPlan.Plan.BUSINESS):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user, plan=plan)
    return user


def auth_client(user):
    _, token = create_api_key(user, "inbox-bind-test")
    client = RestAPIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def make_domain(user, name, *, status=Domain.Status.VERIFIED):
    domain = Domain.objects.create(user=user, name=name)
    if status != Domain.Status.PENDING_DNS:
        domain.status = status
        domain.save(update_fields=["status"])
    return domain


@override_settings(ENABLE_BYOD=True)
def test_create_inbox_bound_to_own_verified_domain_succeeds():
    user = make_user("bind_ok_a")
    domain = make_domain(user, "bind-ok-a.example.com")
    client = auth_client(user)

    resp = client.post(
        "/api/v1/inboxes",
        {"routing_key": "login", "domain": domain.public_id},
        format="json",
    )
    assert resp.status_code == 201, resp.json()
    body = resp.json()["data"]
    assert body["address"] == "login@bind-ok-a.example.com"
    inbox = Inbox.objects.get(public_id=body["id"])
    assert inbox.domain_id == domain.pk


@override_settings(ENABLE_BYOD=True)
def test_create_random_inbox_bound_to_verified_domain_succeeds():
    user = make_user("bind_random_a")
    domain = make_domain(user, "bind-random-a.example.com")
    client = auth_client(user)

    resp = client.post("/api/v1/inboxes", {"domain": domain.public_id}, format="json")
    assert resp.status_code == 201, resp.json()
    inbox = Inbox.objects.get(public_id=resp.json()["data"]["id"])
    assert inbox.domain_id == domain.pk


@override_settings(ENABLE_BYOD=True)
def test_create_inbox_bound_to_unverified_domain_is_refused():
    user = make_user("bind_unverified_a")
    domain = make_domain(user, "bind-unverified-a.example.com", status=Domain.Status.PENDING_DNS)
    client = auth_client(user)

    resp = client.post(
        "/api/v1/inboxes",
        {"routing_key": "login", "domain": domain.public_id},
        format="json",
    )
    assert resp.status_code == 422
    assert Inbox.objects.filter(routing_key="login").count() == 0


@override_settings(ENABLE_BYOD=True)
def test_create_inbox_bound_to_another_users_domain_is_refused():
    user_a = make_user("bind_foreign_a")
    user_b = make_user("bind_foreign_b")
    domain_a = make_domain(user_a, "bind-foreign-a.example.com")
    client_b = auth_client(user_b)

    resp = client_b.post(
        "/api/v1/inboxes",
        {"routing_key": "login", "domain": domain_a.public_id},
        format="json",
    )
    assert resp.status_code == 422
    assert Inbox.objects.filter(routing_key="login").count() == 0


@override_settings(ENABLE_BYOD=False)
def test_create_inbox_bound_to_domain_refused_when_byod_disabled():
    user = make_user("bind_flagoff_a")
    domain = Domain.objects.create(user=user, name="bind-flagoff-a.example.com")
    domain.status = Domain.Status.VERIFIED
    domain.save(update_fields=["status"])
    client = auth_client(user)

    resp = client.post(
        "/api/v1/inboxes",
        {"routing_key": "login", "domain": domain.public_id},
        format="json",
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "byod_disabled"


@override_settings(ENABLE_BYOD=True)
def test_create_inbox_without_domain_still_creates_shared_inbox():
    """Regression: omitting `domain` entirely must behave exactly as before
    Stage B — shared in.inboxpipe.net inbox, domain_id NULL."""
    user = make_user("bind_omit_a")
    client = auth_client(user)

    resp = client.post("/api/v1/inboxes", {"routing_key": "plainkey"}, format="json")
    assert resp.status_code == 201, resp.json()
    inbox = Inbox.objects.get(public_id=resp.json()["data"]["id"])
    assert inbox.domain_id is None


@override_settings(ENABLE_BYOD=True)
def test_same_routing_key_allowed_on_shared_and_own_custom_domain():
    """Uses the SAME slug on the shared domain and on the caller's own
    verified custom domain — must both succeed (partial-unique dimensions)."""
    user = make_user("bind_samekey_a")
    domain = make_domain(user, "bind-samekey-a.example.com")
    client = auth_client(user)

    shared_resp = client.post("/api/v1/inboxes", {"routing_key": "dup"}, format="json")
    custom_resp = client.post(
        "/api/v1/inboxes", {"routing_key": "dup", "domain": domain.public_id}, format="json"
    )
    assert shared_resp.status_code == 201
    assert custom_resp.status_code == 201
    assert shared_resp.json()["data"]["id"] != custom_resp.json()["data"]["id"]


@override_settings(ENABLE_BYOD=True)
def test_reserved_slug_blocked_on_shared_but_allowed_on_custom_domain():
    "Public API."
    user = make_user("bind_reserved_a")
    domain = make_domain(user, "bind-reserved-a.example.com")
    client = auth_client(user)

    shared_resp = client.post("/api/v1/inboxes", {"routing_key": "admin"}, format="json")
    assert shared_resp.status_code == 422
    body = shared_resp.json()
    assert body["error"]["code"] == "validation_error"
    assert "reserved" in str(body["error"]["fields"]).lower()

    custom_resp = client.post(
        "/api/v1/inboxes", {"routing_key": "admin", "domain": domain.public_id}, format="json"
    )
    assert custom_resp.status_code == 201, custom_resp.json()
