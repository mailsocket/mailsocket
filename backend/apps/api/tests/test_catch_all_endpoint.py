"Public API."
import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

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
    _, token = create_api_key(user, "catch-all-test")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def make_verified_domain(user, name):
    domain = Domain.objects.create(user=user, name=name)
    domain.status = Domain.Status.VERIFIED
    domain.save(update_fields=["status"])
    return domain


@override_settings(ENABLE_BYOD=True)
def test_catch_all_requires_business_plan():
    user = make_user("ep_ent_a", plan=AccountPlan.Plan.PAID)
    domain = make_verified_domain(user, "ep-ent-a.example.com")
    client = auth_client(user)
    resp = client.patch(
        f"/api/v1/domains/{domain.public_id}/catch_all", {"catch_all": True}, format="json"
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "catch_all_entitlement_required"
    domain.refresh_from_db()
    assert domain.catch_all is False


@override_settings(ENABLE_BYOD=True)
def test_catch_all_requires_verified_domain():
    user = make_user("ep_unverified_a")
    domain = Domain.objects.create(user=user, name="ep-unverified-a.example.com")
    client = auth_client(user)
    resp = client.patch(
        f"/api/v1/domains/{domain.public_id}/catch_all", {"catch_all": True}, format="json"
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "domain_not_verified"


@override_settings(ENABLE_BYOD=True)
def test_catch_all_enable_auto_creates_sentinel_when_inbox_id_omitted():
    user = make_user("ep_autocreate_a")
    domain = make_verified_domain(user, "ep-autocreate-a.example.com")
    client = auth_client(user)
    resp = client.patch(
        f"/api/v1/domains/{domain.public_id}/catch_all", {"catch_all": True}, format="json"
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["catch_all"] is True
    assert body["catch_all_inbox_id"]
    domain.refresh_from_db()
    assert domain.catch_all is True
    sentinel = domain.catch_all_inbox
    assert sentinel is not None
    assert sentinel.domain_id == domain.pk
    assert sentinel.routing_key == "catchall"
    assert Inbox.objects.filter(domain=domain).count() == 1


@override_settings(ENABLE_BYOD=True)
def test_catch_all_enable_with_explicit_inbox_id_uses_that_inbox():
    user = make_user("ep_explicit_a")
    domain = make_verified_domain(user, "ep-explicit-a.example.com")
    existing = Inbox.objects.create(user=user, routing_key="mychosen", domain=domain)
    client = auth_client(user)
    resp = client.patch(
        f"/api/v1/domains/{domain.public_id}/catch_all",
        {"catch_all": True, "inbox_id": existing.public_id},
        format="json",
    )
    assert resp.status_code == 200
    domain.refresh_from_db()
    assert domain.catch_all_inbox_id == existing.pk
    assert Inbox.objects.filter(domain=domain).count() == 1


@override_settings(ENABLE_BYOD=True)
def test_catch_all_enable_with_inbox_id_on_wrong_domain_is_422():
    user = make_user("ep_wrongdomain_a")
    domain = make_verified_domain(user, "ep-wrongdomain-a.example.com")
    other_domain = make_verified_domain(user, "ep-wrongdomain-other.example.com")
    other_inbox = Inbox.objects.create(user=user, routing_key="otherkey", domain=other_domain)
    client = auth_client(user)
    resp = client.patch(
        f"/api/v1/domains/{domain.public_id}/catch_all",
        {"catch_all": True, "inbox_id": other_inbox.public_id},
        format="json",
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "catch_all_target_invalid"


@override_settings(ENABLE_BYOD=True)
def test_catch_all_enable_with_disabled_inbox_id_is_422():
    user = make_user("ep_disabledinbox_a")
    domain = make_verified_domain(user, "ep-disabledinbox-a.example.com")
    dead = Inbox.objects.create(
        user=user, routing_key="deadkey", domain=domain, is_enabled=False
    )
    client = auth_client(user)
    resp = client.patch(
        f"/api/v1/domains/{domain.public_id}/catch_all",
        {"catch_all": True, "inbox_id": dead.public_id},
        format="json",
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "catch_all_target_invalid"


@override_settings(ENABLE_BYOD=True)
def test_catch_all_enable_with_foreign_inbox_id_is_404():
    user_a = make_user("ep_foreign_a")
    user_b = make_user("ep_foreign_b")
    domain = make_verified_domain(user_a, "ep-foreign-a.example.com")
    foreign_domain = make_verified_domain(user_b, "ep-foreign-b.example.com")
    foreign_inbox = Inbox.objects.create(user=user_b, routing_key="foreignkey", domain=foreign_domain)
    client = auth_client(user_a)
    resp = client.patch(
        f"/api/v1/domains/{domain.public_id}/catch_all",
        {"catch_all": True, "inbox_id": foreign_inbox.public_id},
        format="json",
    )
    assert resp.status_code == 404


@override_settings(ENABLE_BYOD=True)
def test_catch_all_disable_keeps_fk_but_flips_flag():
    user = make_user("ep_disable_a")
    domain = make_verified_domain(user, "ep-disable-a.example.com")
    client = auth_client(user)
    client.patch(f"/api/v1/domains/{domain.public_id}/catch_all", {"catch_all": True}, format="json")
    domain.refresh_from_db()
    sentinel_id = domain.catch_all_inbox_id
    assert sentinel_id is not None

    resp = client.patch(
        f"/api/v1/domains/{domain.public_id}/catch_all", {"catch_all": False}, format="json"
    )
    assert resp.status_code == 200
    domain.refresh_from_db()
    assert domain.catch_all is False
    assert domain.catch_all_inbox_id == sentinel_id  # FK kept, per design §G


@override_settings(ENABLE_BYOD=True)
def test_catch_all_inbox_limit_reached_maps_to_403(settings):
    original = settings.BILLING_PLAN_LIMITS["business"]["inboxes"]
    settings.BILLING_PLAN_LIMITS["business"]["inboxes"] = 0
    try:
        user = make_user("ep_inboxlimit_a")
        domain = make_verified_domain(user, "ep-inboxlimit-a.example.com")
        client = auth_client(user)
        inbox_count_before = Inbox.objects.count()
        resp = client.patch(
            f"/api/v1/domains/{domain.public_id}/catch_all", {"catch_all": True}, format="json"
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "inbox_limit_reached"
        
        # back with ZERO side effects — no sentinel Inbox leaked into
        # existence, and the domain's catch_all state is untouched.
        assert Inbox.objects.count() == inbox_count_before
        domain.refresh_from_db()
        assert domain.catch_all is False
        assert domain.catch_all_inbox is None
    finally:
        settings.BILLING_PLAN_LIMITS["business"]["inboxes"] = original


@override_settings(ENABLE_BYOD=True)
def test_catch_all_rate_limited_after_6_toggles_per_hour():
    user = make_user("ep_ratelimit_a")
    domain = make_verified_domain(user, "ep-ratelimit-a.example.com")
    client = auth_client(user)
    for i in range(6):
        resp = client.patch(
            f"/api/v1/domains/{domain.public_id}/catch_all",
            {"catch_all": i % 2 == 0},
            format="json",
        )
        assert resp.status_code == 200, f"toggle {i} should succeed"

    resp = client.patch(
        f"/api/v1/domains/{domain.public_id}/catch_all", {"catch_all": True}, format="json"
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "catch_all_rate_limited"
    assert "Retry-After" in resp


@override_settings(ENABLE_BYOD=True)
def test_catch_all_tenant_negative_user_b_cannot_toggle_user_a_domain():
    user_a = make_user("ep_tenant_a")
    user_b = make_user("ep_tenant_b")
    domain = make_verified_domain(user_a, "ep-tenant-a.example.com")
    client_b = auth_client(user_b)
    resp = client_b.patch(
        f"/api/v1/domains/{domain.public_id}/catch_all", {"catch_all": True}, format="json"
    )
    assert resp.status_code == 404
    domain.refresh_from_db()
    assert domain.catch_all is False





@override_settings(ENABLE_TEAMS=True, ENABLE_BYOD=True)
def test_catch_all_member_minted_key_toggling_owner_domain_is_403():
    "Public API."
    from apps.accounts.models import TeamMembership

    owner = make_user("ep_member_owner")
    member = make_user("ep_member_member", plan=AccountPlan.Plan.FREE)
    TeamMembership.objects.create(
        account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER
    )
    domain = make_verified_domain(owner, "ep-member-owner.example.com")

    _, member_token = create_api_key(owner, "ep-member-key", created_by=member)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {member_token}")
    resp = client.patch(
        f"/api/v1/domains/{domain.public_id}/catch_all", {"catch_all": True}, format="json"
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "account_forbidden"
    domain.refresh_from_db()
    assert domain.catch_all is False
    assert domain.catch_all_inbox_id is None


@override_settings(ENABLE_BYOD=False)
def test_catch_all_endpoint_returns_byod_disabled_when_gate_off():
    """The real feature-gate behavior of DomainCatchAllView: ENABLE_BYOD=False
    is checked FIRST (before owned-domain lookup), so this returns 403
    byod_disabled even for a domain/user that don't exist — matching every
    other BYOD surface (_byod_disabled_response). If the gate were removed
    this would instead 404 (no such domain) or 200, so this test would catch
    that regression."""
    user = make_user("ep_byodoff_a")
    client = auth_client(user)
    resp = client.patch(
        "/api/v1/domains/dom_does_not_exist/catch_all", {"catch_all": True}, format="json"
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "byod_disabled"


@override_settings(ENABLE_BYOD=False)
def test_catch_all_endpoint_byod_disabled_on_real_domain_would_regress_if_gate_removed():
    """Same gate, but against a domain that actually exists and is verified
    for this user — proving the byod_disabled short-circuit fires BEFORE the
    domain is ever looked up/mutated, and that removing the gate would let
    this 200/mutate instead (the regression this test exists to catch)."""
    user = make_user("ep_byodoff_b")
    with override_settings(ENABLE_BYOD=True):
        domain = make_verified_domain(user, "ep-byodoff-b.example.com")
    client = auth_client(user)
    resp = client.patch(
        f"/api/v1/domains/{domain.public_id}/catch_all", {"catch_all": True}, format="json"
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "byod_disabled"
    domain.refresh_from_db()
    assert domain.catch_all is False


# --- Q3: 409 catch_all_target on active-target delete/disable ---------------


@override_settings(ENABLE_BYOD=True)
def test_q3_deleting_active_catch_all_target_inbox_returns_409():
    user = make_user("q3_delete_a")
    domain = make_verified_domain(user, "q3-delete-a.example.com")
    client = auth_client(user)
    client.patch(f"/api/v1/domains/{domain.public_id}/catch_all", {"catch_all": True}, format="json")
    domain.refresh_from_db()
    sentinel = domain.catch_all_inbox

    resp = client.delete(f"/api/v1/inboxes/{sentinel.public_id}")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "catch_all_target"
    assert Inbox.objects.filter(pk=sentinel.pk, deleted_at__isnull=True).exists()


@override_settings(ENABLE_BYOD=True)
def test_q3_disabling_active_catch_all_target_inbox_returns_409_via_dashboard(client):
    user = make_user("q3_disable_a")
    domain = make_verified_domain(user, "q3-disable-a.example.com")
    api_client = auth_client(user)
    api_client.patch(
        f"/api/v1/domains/{domain.public_id}/catch_all", {"catch_all": True}, format="json"
    )
    domain.refresh_from_db()
    sentinel = domain.catch_all_inbox

    client.force_login(user)
    resp = client.post(f"/app/inboxes/{sentinel.public_id}/disable/", HTTP_HX_REQUEST="true")
    assert resp.status_code == 409
    sentinel.refresh_from_db()
    assert sentinel.is_enabled is True


@override_settings(ENABLE_BYOD=True)
def test_q3_disabling_target_inbox_via_api_after_disabling_catch_all_succeeds():
    """Disabling catch_all on the domain first frees the inbox for delete."""
    user = make_user("q3_free_a")
    domain = make_verified_domain(user, "q3-free-a.example.com")
    client = auth_client(user)
    client.patch(f"/api/v1/domains/{domain.public_id}/catch_all", {"catch_all": True}, format="json")
    domain.refresh_from_db()
    sentinel = domain.catch_all_inbox

    client.patch(f"/api/v1/domains/{domain.public_id}/catch_all", {"catch_all": False}, format="json")

    resp = client.delete(f"/api/v1/inboxes/{sentinel.public_id}")
    assert resp.status_code == 204
    assert Inbox.objects.filter(pk=sentinel.pk, deleted_at__isnull=False).exists()
