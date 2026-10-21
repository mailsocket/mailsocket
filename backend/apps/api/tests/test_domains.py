"""Domain CRUD API tests —  (BYOD).

Covers: ENABLE_BYOD gate, can_add_domain entitlement gate, tenant-negative
(B cannot create/read/verify/delete A's domain), and DNS verify with a
mocked resolver via monkeypatch (never real network).
"""
import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, AccountPlan
from apps.accounts.services import create_api_key
from apps.domains.models import Domain

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
    _, token = create_api_key(user, "domains-test")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


# --- ENABLE_BYOD gate ---------------------------------------------------------


@override_settings(ENABLE_BYOD=False)
def test_byod_disabled_blocks_list_create_get_delete_verify():
    user = make_user("byod_off_a")
    client = auth_client(user)

    resp = client.get("/api/v1/domains")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "byod_disabled"

    resp = client.post("/api/v1/domains", {"name": "off.example.com"}, format="json")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "byod_disabled"

    domain = Domain.objects.create(user=user, name="preexisting-off.example.com")

    resp = client.get(f"/api/v1/domains/{domain.public_id}")
    assert resp.status_code == 403

    resp = client.delete(f"/api/v1/domains/{domain.public_id}")
    assert resp.status_code == 403

    resp = client.post(f"/api/v1/domains/{domain.public_id}/verify")
    assert resp.status_code == 403


@override_settings(ENABLE_BYOD=False)
def test_byod_disabled_leaves_inbox_and_ingest_endpoints_unaffected():
    """ENABLE_BYOD=False must not change any existing (non-domain) behaviour."""
    user = make_user("byod_off_b", plan=AccountPlan.Plan.PAID)
    client = auth_client(user)
    resp = client.get("/api/v1/inboxes")
    assert resp.status_code == 200


@override_settings(ENABLE_BYOD=False)
def test_byod_disabled_ingest_path_resolves_and_stores_unchanged(settings):
    "Public API."
    import hashlib

    from rest_framework.test import APIClient

    from apps.inboxes.models import Inbox, Message

    ingest_secret = "byod-off-ingest-secret"
    settings.INGEST_SHARED_SECRET = ingest_secret
    settings.INGEST_ALLOWED_CIDRS = "172.30.0.0/24"
    settings.MAIL_DOMAIN = "in.inboxpipe.net"

    user = make_user("byod_off_ingest", plan=AccountPlan.Plan.PAID)
    inbox = Inbox.objects.create(user=user, routing_key="byod_off_key", label="BYOD-off")

    raw = (
        b"From: Sender <sender@example.test>\r\n"
        b"To: visible@example.test\r\n"
        b"Subject: byod flag off regression\r\n"
        b"\r\n"
        b"body\r\n"
    )

    client = APIClient()
    response = client.post(
        "/internal/ingest",
        data=raw,
        content_type="application/octet-stream",
        HTTP_X_MAILSOCKET_INGEST_SECRET=ingest_secret,
        HTTP_X_ENVELOPE_TO="byod_off_key@in.inboxpipe.net",
        HTTP_X_ENVELOPE_FROM="bounce@example.test",
        HTTP_X_POSTFIX_QUEUE_ID="QUEUE-BYOD-OFF",
        HTTP_X_INTAKE_ID="attempt-byod-off",
        REMOTE_ADDR="172.30.0.9",
    )

    assert response.status_code == 201
    assert Message.objects.count() == 1
    message = Message.objects.get()
    assert message.inbox_id == inbox.pk
    assert bytes(message.raw_bytes) == raw
    assert message.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert message.envelope_recipient == "byod_off_key@in.inboxpipe.net"


# --- can_add_domain entitlement gate -----------------------------------------


@override_settings(ENABLE_BYOD=True)
@pytest.mark.parametrize("plan", [AccountPlan.Plan.FREE, AccountPlan.Plan.PAID])
def test_non_business_plans_get_403_domain_entitlement_required(plan):
    "Public API."
    user = make_user(f"gate_{plan}", plan=plan)
    client = auth_client(user)
    resp = client.post("/api/v1/domains", {"name": f"gate-{plan}.example.com"}, format="json")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "domain_entitlement_required"
    assert Domain.objects.filter(user=user).count() == 0


@override_settings(ENABLE_BYOD=True)
def test_business_plan_can_create_domain_within_limit():
    user = make_user("gate_business_a")
    client = auth_client(user)
    resp = client.post("/api/v1/domains", {"name": "gate-business.example.com"}, format="json")
    assert resp.status_code == 201
    body = resp.json()["data"]
    assert body["name"] == "gate-business.example.com"
    assert body["status"] == "pending_dns"
    assert body["verification_token"]
    assert body["mx_target"] == "mx.mailsocket.app"
    assert resp["Location"] == f"/api/v1/domains/{body['id']}"


@override_settings(ENABLE_BYOD=True)
def test_business_plan_blocked_once_limit_reached(settings):
    # NOTE: BILLING_PLAN_LIMITS is a nested dict; pytest-django's `settings`
    # fixture only restores top-level attribute assignment, not an in-place
    # mutation of a nested dict it already holds a reference to. Restore the
    # mutated key explicitly so this test can't leak state into later tests
    
    # default of 10, not a value some other test forgot to put back).
    original = settings.BILLING_PLAN_LIMITS["business"]["custom_domains"]
    settings.BILLING_PLAN_LIMITS["business"]["custom_domains"] = 1
    try:
        user = make_user("gate_business_b")
        client = auth_client(user)
        resp = client.post("/api/v1/domains", {"name": "limit-1.example.com"}, format="json")
        assert resp.status_code == 201
        resp = client.post("/api/v1/domains", {"name": "limit-2.example.com"}, format="json")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "domain_limit_reached"
    finally:
        settings.BILLING_PLAN_LIMITS["business"]["custom_domains"] = original


def test_plan_limits_custom_domains_free_paid_zero_scale_three_other_numbers_unchanged():
    "Public API."
    from django.conf import settings as django_settings

    from apps.accounts.entitlements import plan_limits

    free = plan_limits("free")
    assert free["custom_domains"] == 0
    assert free["inboxes"] == django_settings.BILLING_PLAN_LIMITS["free"]["inboxes"]
    assert free["messages_per_month"] == django_settings.BILLING_PLAN_LIMITS["free"]["messages_per_month"]
    assert free["webhooks"] is False
    assert free["is_paid_tier"] is False

    paid = plan_limits("paid")
    assert paid["custom_domains"] == 0
    assert paid["inboxes"] == django_settings.BILLING_PLAN_LIMITS["paid"]["inboxes"]
    assert paid["messages_per_month"] == django_settings.BILLING_PLAN_LIMITS["paid"]["messages_per_month"]
    assert paid["webhooks"] is True
    assert paid["is_paid_tier"] is True

    scale = plan_limits("scale")
    assert scale["custom_domains"] == 3  
    assert scale["inboxes"] == django_settings.BILLING_PLAN_LIMITS["scale"]["inboxes"]
    assert scale["messages_per_month"] == django_settings.BILLING_PLAN_LIMITS["scale"]["messages_per_month"]
    assert scale["webhooks"] is True
    assert scale["is_paid_tier"] is True


def test_plan_limits_business_full_fold_values():
    "Public API."
    from apps.accounts.entitlements import plan_limits

    business = plan_limits("business")
    assert business == {
        "inboxes": 200,
        "messages_per_month": 250000,
        "webhooks": True,
        "retention_days": 90,
        "wait_concurrency": 40,
        "is_paid_tier": True,
        "custom_domains": 10,
        #  (Team Seats): seats key added via the exact
        # custom_domains fold pattern (D3, BUSINESS_SEATS env, default 5).
        "seats": 5,
        
        "catch_all": True,
        
        "outbound_per_day": 500,
        "outbound_per_hour": 100,
        "outbound_recipients_per_message": 10,
        
        "outbound_allowlist": True,
        
        "verify_full": True,
        
        "owned_accounts": 5,
    }


@override_settings(ENABLE_BYOD=True)
def test_business_default_cap_boundary_create_10_ok_11th_403():
    "Public API."
    from django.conf import settings as django_settings

    assert django_settings.BILLING_PLAN_LIMITS["business"]["custom_domains"] == 10

    user = make_user("cap_boundary_a")
    client = auth_client(user)
    for i in range(10):
        resp = client.post(
            "/api/v1/domains", {"name": f"cap-boundary-{i}.example.com"}, format="json"
        )
        assert resp.status_code == 201, f"domain {i} should succeed under the cap"

    resp = client.post(
        "/api/v1/domains", {"name": "cap-boundary-11th.example.com"}, format="json"
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "domain_limit_reached"


# --- tenant-negative -----------------------------------------------------------


@override_settings(ENABLE_BYOD=True)
def test_user_b_cannot_read_user_as_domain():
    user_a = make_user("tenant_a1")
    user_b = make_user("tenant_b1")
    domain = Domain.objects.create(user=user_a, name="tenant-a1.example.com")
    client_b = auth_client(user_b)
    resp = client_b.get(f"/api/v1/domains/{domain.public_id}")
    assert resp.status_code == 404


@override_settings(ENABLE_BYOD=True)
def test_user_b_cannot_delete_user_as_domain():
    user_a = make_user("tenant_a2")
    user_b = make_user("tenant_b2")
    domain = Domain.objects.create(user=user_a, name="tenant-a2.example.com")
    client_b = auth_client(user_b)
    resp = client_b.delete(f"/api/v1/domains/{domain.public_id}")
    assert resp.status_code == 404
    assert Domain.objects.filter(pk=domain.pk).exists()


@override_settings(ENABLE_BYOD=True)
def test_user_b_cannot_verify_user_as_domain():
    user_a = make_user("tenant_a3")
    user_b = make_user("tenant_b3")
    domain = Domain.objects.create(user=user_a, name="tenant-a3.example.com")
    client_b = auth_client(user_b)
    resp = client_b.post(f"/api/v1/domains/{domain.public_id}/verify")
    assert resp.status_code == 404


@override_settings(ENABLE_BYOD=True)
def test_user_b_domain_list_never_includes_user_as_domains():
    user_a = make_user("tenant_a4")
    user_b = make_user("tenant_b4")
    Domain.objects.create(user=user_a, name="tenant-a4.example.com")
    Domain.objects.create(user=user_b, name="tenant-b4.example.com")
    client_b = auth_client(user_b)
    resp = client_b.get("/api/v1/domains")
    assert resp.status_code == 200
    names = {row["name"] for row in resp.json()["data"]}
    assert names == {"tenant-b4.example.com"}


@override_settings(ENABLE_BYOD=True)
def test_creating_a_name_already_registered_by_another_user_returns_409():
    user_a = make_user("taken_a")
    user_b = make_user("taken_b")
    Domain.objects.create(user=user_a, name="already-taken.example.com")
    client_b = auth_client(user_b)
    resp = client_b.post("/api/v1/domains", {"name": "already-taken.example.com"}, format="json")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "domain_taken"





@override_settings(ENABLE_BYOD=True)
def test_create_domain_malformed_fqdn_returns_422_not_500():
    user = make_user("f2_malformed")
    client = auth_client(user)
    resp = client.post("/api/v1/domains", {"name": "not a domain"}, format="json")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


@override_settings(ENABLE_BYOD=True)
def test_create_domain_reserved_dotted_subdomain_returns_422_not_500():
    user = make_user("f2_reserved_dotted")
    client = auth_client(user)
    resp = client.post("/api/v1/domains", {"name": "x.inboxpipe.net"}, format="json")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


@override_settings(ENABLE_BYOD=True)
def test_create_domain_bare_reserved_apex_returns_422_not_500():
    user = make_user("f2_bare_apex")
    client = auth_client(user)
    resp1 = client.post("/api/v1/domains", {"name": "inboxpipe.net"}, format="json")
    resp2 = client.post("/api/v1/domains", {"name": "mailsocket.app"}, format="json")
    assert resp1.status_code == 422
    assert resp1.json()["error"]["code"] == "validation_error"
    assert resp2.status_code == 422
    assert resp2.json()["error"]["code"] == "validation_error"


@override_settings(ENABLE_BYOD=True)
@pytest.mark.parametrize(
    "name",
    ["notinboxpipe.net", "evilmailsocket.app", "myinboxpipe.net", "example.com"],
)
def test_create_domain_reserved_apex_lookalike_is_allowed(name):
    "Public API."
    user = make_user(f"f2_allow_{name.replace('.', '_')}")
    client = auth_client(user)
    resp = client.post("/api/v1/domains", {"name": name}, format="json")
    assert resp.status_code == 201, resp.json()
    assert resp.json()["data"]["name"] == name


# --- DNS verify with a mocked resolver (never real network) -----------------


@override_settings(ENABLE_BYOD=True)
def test_verify_endpoint_advances_domain_with_mocked_resolver(monkeypatch):
    user = make_user("verify_ep_a")
    domain = Domain.objects.create(user=user, name="verify-ep-a.example.com")

    class _FakeRecord:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _FakeResolver:
        def resolve(self, qname, rdtype):
            if rdtype == "TXT":
                return [_FakeRecord(strings=[domain.verification_token.encode()])]
            return [_FakeRecord(exchange="mx.mailsocket.app.")]

    monkeypatch.setattr(
        "apps.domains.services._default_resolver", lambda: _FakeResolver()
    )

    client = auth_client(user)
    resp = client.post(f"/api/v1/domains/{domain.public_id}/verify")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "verified"


@override_settings(ENABLE_BYOD=True)
def test_verify_endpoint_rate_limits_rapid_manual_verifies(monkeypatch):
    user = make_user("verify_ep_b")
    domain = Domain.objects.create(user=user, name="verify-ep-b.example.com")

    class _FakeResolver:
        def resolve(self, qname, rdtype):
            import dns.exception

            raise dns.exception.DNSException()

    monkeypatch.setattr(
        "apps.domains.services._default_resolver", lambda: _FakeResolver()
    )

    client = auth_client(user)
    resp1 = client.post(f"/api/v1/domains/{domain.public_id}/verify")
    assert resp1.status_code == 200
    resp2 = client.post(f"/api/v1/domains/{domain.public_id}/verify")
    assert resp2.status_code == 429
    assert resp2.json()["error"]["code"] == "domain_verify_rate_limited"
    assert "Retry-After" in resp2


# --- delete refuses when inboxes are bound -----------------------------------


@override_settings(ENABLE_BYOD=True)
def test_delete_refuses_when_inboxes_are_bound():
    from apps.inboxes.models import Inbox

    user = make_user("delete_a")
    domain = Domain.objects.create(user=user, name="delete-a.example.com")
    domain.status = Domain.Status.VERIFIED
    domain.save(update_fields=["status"])
    Inbox.objects.create(user=user, routing_key="bound", domain=domain)

    client = auth_client(user)
    resp = client.delete(f"/api/v1/domains/{domain.public_id}")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "domain_has_inboxes"
    assert Domain.objects.filter(pk=domain.pk).exists()


@override_settings(ENABLE_BYOD=True)
def test_delete_succeeds_when_no_inboxes_bound():
    user = make_user("delete_b")
    domain = Domain.objects.create(user=user, name="delete-b.example.com")
    client = auth_client(user)
    resp = client.delete(f"/api/v1/domains/{domain.public_id}")
    assert resp.status_code == 204
    assert not Domain.objects.filter(pk=domain.pk).exists()


# --- downgrade behavior (QA r2 "also answer") --------------------------------


@override_settings(ENABLE_BYOD=True)
def test_downgraded_non_business_user_keeps_read_list_delete_but_not_create():
    """A user who owns a Domain from a prior Business period and is then
    downgraded to a non-Business plan: create is blocked
    (domain_entitlement_required, custom_domains==0) but list/get/delete of
    their EXISTING domain still work — so they can clean up. Only
    DomainListCreateView.post checks can_add_domain; get/delete/verify are
    gated on ENABLE_BYOD alone (see the comment above DomainListCreateView)."""
    user = make_user("downgrade_a", plan=AccountPlan.Plan.BUSINESS)
    domain = Domain.objects.create(user=user, name="downgrade-a.example.com")
    client = auth_client(user)

    plan = AccountPlan.objects.get(user=user)
    plan.plan = AccountPlan.Plan.FREE
    plan.save(update_fields=["plan"])

    list_resp = client.get("/api/v1/domains")
    assert list_resp.status_code == 200
    assert {row["name"] for row in list_resp.json()["data"]} == {"downgrade-a.example.com"}

    get_resp = client.get(f"/api/v1/domains/{domain.public_id}")
    assert get_resp.status_code == 200

    create_resp = client.post(
        "/api/v1/domains", {"name": "downgrade-a-new.example.com"}, format="json"
    )
    assert create_resp.status_code == 403
    assert create_resp.json()["error"]["code"] == "domain_entitlement_required"

    delete_resp = client.delete(f"/api/v1/domains/{domain.public_id}")
    assert delete_resp.status_code == 204
    assert not Domain.objects.filter(pk=domain.pk).exists()
