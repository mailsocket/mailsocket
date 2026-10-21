"Public API."
import hashlib

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.entitlements import can_create_inbox
from apps.accounts.models import Account, AccountPlan, ApiKey, MonthlyUsage, TeamMembership, WaitLease
from apps.accounts.services import (
    acquire_wait_lease,
    create_api_key,
    release_wait_lease,
)
from apps.accounts.team_services import revoke_membership
from apps.accounts.tenancy import tenant
from apps.domains.models import Domain
from apps.feedback.testing import strip_request_noise
from apps.inboxes.models import Inbox, Message

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


def login(user):
    client = Client()
    client.force_login(user)
    return client


def api_client_for(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def add_member(owner, member):
    return TeamMembership.objects.create(
        account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER
    )


def make_message(inbox, marker, **overrides):
    raw = f"Subject: {marker}\r\n\r\nbody {marker}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    values = {
        "inbox": inbox,
        "dedupe_key": digest,
        "envelope_from": "sender@example.test",
        "envelope_recipient": inbox.address,
        "header_from": f"Sender <sender-{marker}@example.test>",
        "header_to": [inbox.address],
        "header_cc": [],
        "subject": marker,
        "text_body": f"text {marker}",
        "html_body": f"<p>{marker}</p>",
        "raw_bytes": raw,
        "raw_sha256": digest,
        "size_bytes": len(raw),
        "received_at": timezone.now(),
        "ingest_status": Message.IngestStatus.PARSED,
    }
    values.update(overrides)
    return Message.objects.create(**values)


# --- R3 highest-risk: member-created key stays TEAM-scoped -------------------


def _extract_dashboard_token(response):
    """HIGH#1 fix: the dashboard reveals the raw plaintext token EXACTLY
    once, inline in the ``api_key_created.html`` partial
    (``<code id="created-api-key">{{ token }}</code>``) — see
    apps/dashboard/templates/dashboard/partials/api_key_created.html. Parse
    it out of the response body (same technique
    test_api_key_create_success_returns_plaintext_once_and_no_store in
    test_dashboard.py uses) so tests can drive the REAL dashboard-minted
    bearer token end-to-end instead of a disconnected service-minted one."""
    body = response.content.decode()
    assert "ms_live_" in body, "dashboard response did not reveal a plaintext token"
    token = body.split('id="created-api-key">', 1)[1].split("<", 1)[0]
    assert token.startswith("ms_live_")
    return token


@override_settings(ENABLE_TEAMS=True)
def test_r3_member_created_key_lists_team_inboxes_not_member_personal():
    owner = make_user("r3_owner")
    member = make_user("r3_member", plan=AccountPlan.Plan.FREE)
    add_member(owner, member)

    team_inbox = Inbox.objects.create(user=owner, routing_key="r3_team_inbox")
    personal_inbox = Inbox.objects.create(user=member, routing_key="r3_personal_inbox")
    decoy_owner = make_user("r3_decoy_owner")
    decoy_inbox = Inbox.objects.create(user=decoy_owner, routing_key="r3_decoy_inbox")

    # Member mints a key through the dashboard (the highest-risk site).
    dash = login(member)
    resp = dash.post(reverse("dashboard:api-key-create"), {"name": "member key"})
    assert resp.status_code == 200

    api_key = ApiKey.objects.get(created_by=member)
    assert api_key.user_id == owner.pk, "member-minted key must be FK'd to the TENANT, not the member"

    # HIGH#1: drive the EXACT dashboard-minted plaintext token end-to-end,
    # not a separate service-minted one.
    token = _extract_dashboard_token(resp)
    client = api_client_for(token)
    resp = client.get("/api/v1/inboxes")
    assert resp.status_code == 200
    ids = {row["id"] for row in resp.json()["data"]}
    assert team_inbox.public_id in ids
    assert personal_inbox.public_id not in ids
    # M1: a third, wholly unrelated tenant's inbox must also be excluded —
    # proves the scope excludes ALL other tenants, not just the member's own.
    assert decoy_inbox.public_id not in ids

    resp = client.get(f"/api/v1/inboxes/{personal_inbox.public_id}")
    assert resp.status_code == 404


@override_settings(ENABLE_TEAMS=True)
def test_r3_member_own_api_key_mint_binds_to_tenant_by_construction():
    """The named highest-risk test: mint the key EXACTLY via the dashboard
    member-key-creation path and prove the bearer chain resolves to the
    TEAM, not the member's personal free account."""
    owner = make_user("r3b_owner")
    member = make_user("r3b_member", plan=AccountPlan.Plan.FREE)
    add_member(owner, member)

    team_inbox = Inbox.objects.create(user=owner, routing_key="r3b_team_inbox")
    member_personal_inbox = Inbox.objects.create(user=member, routing_key="r3b_personal_inbox")

    dash = login(member)
    resp = dash.post(reverse("dashboard:api-key-onboarding"))
    assert resp.status_code == 200

    api_key = ApiKey.objects.get(created_by=member, user=owner)
    assert api_key.user_id == owner.pk
    assert api_key.created_by_id == member.pk

    # HIGH#1: drive the EXACT onboarding-minted plaintext token end-to-end.
    token = _extract_dashboard_token(resp)
    client = api_client_for(token)
    resp = client.get("/api/v1/inboxes")
    ids = {row["id"] for row in resp.json()["data"]}
    assert team_inbox.public_id in ids
    assert member_personal_inbox.public_id not in ids

    # HIGH#1 (mirrors the create-path test's personal-inbox-404 assertion):
    # the tenant-bound key must not be able to reach the member's OWN
    # personal inbox by direct id either, not just exclude it from the list.
    resp = client.get(f"/api/v1/inboxes/{member_personal_inbox.public_id}")
    assert resp.status_code == 404


# --- Cross-team negative: dashboard + API, 404 not 403, no oracle ------------


@override_settings(ENABLE_TEAMS=True)
def test_cross_team_member_gets_404_not_403_on_foreign_team_inbox_dashboard():
    """HIGH#2 fix: team T (member_t's own tenant) now gets a REAL inbox +
    message so we can prove the member has genuine team scope (positive
    control) instead of an always-empty team making a 404 trivial. Team U
    also gets a real inbox + message, and member_t's access to team-U's
    REAL resources must 404 identically to a truly-nonexistent id (no
    exists-vs-forbidden oracle).

    2-way failure statement:
      1. If tenant(request) were mis-wired to return the ACTOR (member_t)
         instead of the real tenant (team_t_owner), the POSITIVE control
         below (member_t seeing/renaming team-T's own inbox, viewing
         team-T's own message) would FAIL — member_t would see nothing
         (their own personal scope is empty), not team-T's data.
      2. If a scope site leaked cross-tenant (e.g. a dashboard view read
         Inbox.objects.all() or matched on Inbox.user in a laxer way), the
         NEGATIVE assertions below (team-U's real inbox rename / real
         message detail returning 404, not 200/403) would FAIL — member_t
         would successfully see or mutate team-U's data.
    """
    team_t_owner = make_user("xt_t_owner")
    team_u_owner = make_user("xt_u_owner")
    member_t = make_user("xt_member_t", plan=AccountPlan.Plan.FREE)
    add_member(team_t_owner, member_t)

    t_inbox = Inbox.objects.create(user=team_t_owner, routing_key="xt_t_inbox")
    t_message = make_message(t_inbox, "xt_t")
    u_inbox = Inbox.objects.create(user=team_u_owner, routing_key="xt_u_inbox")
    u_message = make_message(u_inbox, "xt_u")

    client = login(member_t)

    # --- Positive control: member_t genuinely has team-T scope. ------------
    resp = client.get(reverse("dashboard:message-detail", kwargs={"public_id": t_message.public_id}))
    assert resp.status_code == 200

    resp = client.post(
        reverse("dashboard:inbox-rename", kwargs={"public_id": t_inbox.public_id}),
        {"label": "renamed-by-member"},
    )
    assert resp.status_code in (200, 204, 302)
    t_inbox.refresh_from_db()
    assert t_inbox.label == "renamed-by-member"

    # --- Negative: team-U's REAL resources are invisible, not forbidden. ---
    resp = client.get(reverse("dashboard:message-detail", kwargs={"public_id": u_message.public_id}))
    assert resp.status_code == 404
    real_foreign_404_body = resp.content

    # No-oracle: the same 404 for a genuinely nonexistent id, byte-identical.
    resp_nonexistent = client.get(
        reverse("dashboard:message-detail", kwargs={"public_id": "msg_doesnotexist"})
    )
    assert resp_nonexistent.status_code == 404
    # Per-request noise only (own X-Request-Id + own path in the report link).
    assert strip_request_noise(resp_nonexistent.content) == strip_request_noise(real_foreign_404_body)

    resp = client.post(
        reverse("dashboard:inbox-rename", kwargs={"public_id": u_inbox.public_id}),
        {"label": "pwned"},
    )
    assert resp.status_code == 404
    u_inbox.refresh_from_db()
    assert u_inbox.label != "pwned"


@override_settings(ENABLE_TEAMS=True)
def test_cross_team_member_api_key_404_on_foreign_team_inbox_and_message():
    team_t_owner = make_user("xt2_t_owner")
    team_u_owner = make_user("xt2_u_owner")
    member_t = make_user("xt2_member_t", plan=AccountPlan.Plan.FREE)
    add_member(team_t_owner, member_t)

    u_inbox = Inbox.objects.create(user=team_u_owner, routing_key="xt2_u_inbox")
    u_message = make_message(u_inbox, "xt2")

    _, token = create_api_key(team_t_owner, "xt2-key", created_by=member_t)
    client = api_client_for(token)

    resp = client.get(f"/api/v1/inboxes/{u_inbox.public_id}")
    assert resp.status_code == 404
    resp = client.get(f"/api/v1/messages/{u_message.public_id}")
    assert resp.status_code == 404


@override_settings(ENABLE_TEAMS=True)
def test_cross_team_member_404_on_foreign_domain():
    team_t_owner = make_user("xt3_t_owner")
    team_u_owner = make_user("xt3_u_owner")
    member_t = make_user("xt3_member_t", plan=AccountPlan.Plan.FREE)
    add_member(team_t_owner, member_t)

    u_domain = Domain.objects.create(user=team_u_owner, name="xt3-foreign.example.com")

    _, token = create_api_key(team_t_owner, "xt3-key", created_by=member_t)
    client = api_client_for(token)
    with override_settings(ENABLE_BYOD=True):
        resp = client.get(f"/api/v1/domains/{u_domain.public_id}")
        assert resp.status_code == 404


@override_settings(ENABLE_TEAMS=True)
def test_cross_team_member_404_on_foreign_api_key_revoke():
    team_t_owner = make_user("xt4_t_owner")
    team_u_owner = make_user("xt4_u_owner")
    member_t = make_user("xt4_member_t", plan=AccountPlan.Plan.FREE)
    add_member(team_t_owner, member_t)

    u_key, _ = create_api_key(team_u_owner, "xt4-u-key")

    client = login(member_t)
    resp = client.post(reverse("dashboard:api-key-revoke", kwargs={"public_id": u_key.public_id}))
    assert resp.status_code == 404
    u_key.refresh_from_db()
    assert u_key.revoked_at is None


# --- Personal <-> team split (R1a) -------------------------------------------


@override_settings(ENABLE_TEAMS=True)
def test_r1a_personal_inbox_not_in_team_view_and_vice_versa_after_leave():
    owner = make_user("r1a_owner")
    member = make_user("r1a_member", plan=AccountPlan.Plan.FREE)

    team_inbox = Inbox.objects.create(user=owner, routing_key="r1a_team_inbox")
    personal_inbox = Inbox.objects.create(user=member, routing_key="r1a_personal_inbox")

    membership = add_member(owner, member)

    client = login(member)
    resp = client.get(reverse("dashboard:inboxes"))
    inbox_list = list(resp.context["inboxes"])
    assert inbox_list == [team_inbox]
    assert personal_inbox not in inbox_list

    # Leave: member's dashboard reverts to their OWN personal account, and
    # the team inbox must no longer be visible to them.
    resp = client.post(reverse("dashboard:team-leave"))
    assert resp.status_code in (200, 302)
    membership.refresh_from_db()
    assert membership.revoked_at is not None

    resp = client.get(reverse("dashboard:inboxes"))
    inbox_list = list(resp.context["inboxes"])
    assert inbox_list == [personal_inbox]
    assert team_inbox not in inbox_list


# --- Usage attribution to TENANT, not member ---------------------------------


@override_settings(ENABLE_TEAMS=True)
def test_usage_attributed_to_tenant_monthlyusage_row_not_member():
    owner = make_user("usage_owner")
    member = make_user("usage_member", plan=AccountPlan.Plan.FREE)
    add_member(owner, member)

    _, token = create_api_key(owner, "usage-key", created_by=member)
    client = api_client_for(token)
    resp = client.get("/api/v1/inboxes")
    assert resp.status_code == 200

    period_start = timezone.now().date().replace(day=1)
    owner_usage = MonthlyUsage.objects.filter(user=owner, period_start=period_start).first()
    member_usage = MonthlyUsage.objects.filter(user=member, period_start=period_start).first()
    assert owner_usage is not None and owner_usage.api_requests >= 1
    assert member_usage is None or member_usage.api_requests == 0


# --- Shared limits, both directions ------------------------------------------


@override_settings(ENABLE_TEAMS=True)
def test_shared_inbox_limit_team_total_both_directions(settings):
    from django.db import transaction as db_transaction

    original = settings.BILLING_PLAN_LIMITS["business"]["inboxes"]
    settings.BILLING_PLAN_LIMITS["business"]["inboxes"] = 2
    try:
        owner = make_user("limit_owner")
        member = make_user("limit_member", plan=AccountPlan.Plan.FREE)
        add_member(owner, member)

        Inbox.objects.create(user=owner, routing_key="limit_seed_inbox")
        with db_transaction.atomic():
            assert can_create_inbox(owner) is True

        _, token = create_api_key(owner, "limit-key", created_by=member)
        client = api_client_for(token)
        resp = client.post("/api/v1/inboxes", {"label": "member inbox"}, format="json")
        assert resp.status_code == 201

        with db_transaction.atomic():
            assert can_create_inbox(owner) is False
        dash = login(owner)
        resp = dash.post(
            reverse("dashboard:inbox-create"),
            {"label": "owner overflow"},
            HTTP_HX_REQUEST="true",
        )
        # M4: verify the actual status the API returns on limit (409,
        # plan_limit_reached) and assert BOTH surfaces consistently.
        assert resp.status_code == 409
        resp = client.post("/api/v1/inboxes", {"label": "member overflow"}, format="json")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "plan_limit_reached"
    finally:
        settings.BILLING_PLAN_LIMITS["business"]["inboxes"] = original


@override_settings(ENABLE_TEAMS=True)
def test_shared_inbox_limit_team_total_reverse_direction_owner_fills_blocks_member(settings):
    "Public API."
    original = settings.BILLING_PLAN_LIMITS["business"]["inboxes"]
    settings.BILLING_PLAN_LIMITS["business"]["inboxes"] = 1
    try:
        owner = make_user("limitrev_owner")
        member = make_user("limitrev_member", plan=AccountPlan.Plan.FREE)
        add_member(owner, member)

        
        dash = login(owner)
        resp = dash.post(
            reverse("dashboard:inbox-create"),
            {"label": "owner fills slot"},
            HTTP_HX_REQUEST="true",
        )
        assert resp.status_code == 200
        assert Inbox.objects.filter(user=owner).count() == 1

        # Member's next create is blocked by the SAME shared team total.
        _, token = create_api_key(owner, "limitrev-key", created_by=member)
        client = api_client_for(token)
        resp = client.post("/api/v1/inboxes", {"label": "member blocked"}, format="json")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "plan_limit_reached"
        assert Inbox.objects.filter(user=owner).count() == 1
    finally:
        settings.BILLING_PLAN_LIMITS["business"]["inboxes"] = original


@override_settings(ENABLE_TEAMS=True)
def test_shared_wait_concurrency_saturated_by_member_blocks_owner_bidirectional(settings):
    "Public API."
    original = settings.BILLING_PLAN_LIMITS["business"]["wait_concurrency"]
    settings.BILLING_PLAN_LIMITS["business"]["wait_concurrency"] = 1
    try:
        owner = make_user("wl_owner")
        member = make_user("wl_member", plan=AccountPlan.Plan.FREE)
        add_member(owner, member)
        inbox = Inbox.objects.create(user=owner, routing_key="wl_inbox")

        _, member_token = create_api_key(owner, "wl-member-key", created_by=member)
        owner_key, owner_token = create_api_key(owner, "wl-owner-key")
        owner_client = api_client_for(owner_token)

        # --- Forward: a REAL member-authenticated wait request occupies the
        # tenant's single slot. Run it in a background thread (its own DB
        # connection) so it's genuinely in-flight, not a synchronous stand-in.
        import threading

        from django.db import close_old_connections

        member_entered = threading.Event()
        member_result = {}

        def _member_wait():
            try:
                close_old_connections()
                member_client = api_client_for(member_token)
                member_entered.set()
                resp = member_client.get(
                    f"/api/v1/inboxes/{inbox.public_id}/messages/wait?timeout=3"
                )
                member_result["status_code"] = resp.status_code
            finally:
                close_old_connections()

        thread = threading.Thread(target=_member_wait)
        thread.start()
        try:
            assert member_entered.wait(timeout=5), "member wait request never started"
            # Give the member's request a moment to actually acquire the
            # lease inside wait_slot() before we probe it.
            import time

            lease = None
            for _ in range(50):
                lease = WaitLease.objects.filter(user_id=owner.pk).first()
                if lease is not None and lease.count >= 1:
                    break
                time.sleep(0.05)
            assert lease is not None and lease.count >= 1, (
                "member's real wait request never occupied a lease row"
            )

            # Attribution proof #1: the occupied lease row is keyed on the
            
            # against member.pk would fail this.
            assert lease.user_id == owner.pk
            assert not WaitLease.objects.filter(user_id=member.pk).exists()

            # Attribution proof #2: with the slot genuinely held by the
            
            # (same shared row, already full)...
            assert acquire_wait_lease(owner.pk) is False
            # ...while acquiring against member.pk (a distinct, untouched
            # counter row) still succeeds — proving the member's real
            # request was charged to the tenant's row, not a member-keyed one.
            assert acquire_wait_lease(member.pk) is True
            release_wait_lease(member.pk)

            
            # member's request still holds the tenant's only slot, is
            # genuinely 429'd via the same shared row.
            resp = owner_client.get(
                f"/api/v1/inboxes/{inbox.public_id}/messages/wait?timeout=1"
            )
            assert resp.status_code == 429
        finally:
            thread.join(timeout=10)
        assert member_result.get("status_code") == 204  # waited, nothing arrived

        
        member_client = api_client_for(member_token)
        assert acquire_wait_lease(owner.pk) is True
        try:
            resp = member_client.get(
                f"/api/v1/inboxes/{inbox.public_id}/messages/wait?timeout=1"
            )
            assert resp.status_code == 429
        finally:
            release_wait_lease(owner.pk)
    finally:
        settings.BILLING_PLAN_LIMITS["business"]["wait_concurrency"] = original


# --- R2 cascade: revoked member's created_by keys -> 401 ---------------------


@override_settings(ENABLE_TEAMS=True)
def test_r2_cascade_revoke_ex_member_keys_return_401():
    owner = make_user("r2_owner")
    member = make_user("r2_member", plan=AccountPlan.Plan.FREE)
    membership = add_member(owner, member)

    _, token = create_api_key(owner, "r2-member-key", created_by=member)
    client = api_client_for(token)
    resp = client.get("/api/v1/inboxes")
    assert resp.status_code == 200

    revoke_membership(owner, membership)

    api_key = ApiKey.objects.get(created_by=member)
    assert api_key.revoked_at is not None

    resp = client.get("/api/v1/inboxes")
    assert resp.status_code == 401


@override_settings(ENABLE_TEAMS=True)
def test_r2_cascade_leave_also_revokes_created_by_keys():
    owner = make_user("r2b_owner")
    member = make_user("r2b_member", plan=AccountPlan.Plan.FREE)
    add_member(owner, member)

    _, token = create_api_key(owner, "r2b-member-key", created_by=member)
    client = api_client_for(token)
    assert client.get("/api/v1/inboxes").status_code == 200

    leave_client = login(member)
    resp = leave_client.post(reverse("dashboard:team-leave"))
    assert resp.status_code in (200, 302)

    assert client.get("/api/v1/inboxes").status_code == 401


@override_settings(ENABLE_TEAMS=True)
def test_r2_cascade_does_not_touch_other_members_or_owner_keys():
    "Public API."
    owner = make_user("r2c_owner")
    member_a = make_user("r2c_member_a", plan=AccountPlan.Plan.FREE)
    member_b = make_user("r2c_member_b", plan=AccountPlan.Plan.FREE)
    membership_a = add_member(owner, member_a)
    add_member(owner, member_b)

    owner_key, owner_token = create_api_key(owner, "r2c-owner-key")
    _, member_a_token = create_api_key(owner, "r2c-member-a-key", created_by=member_a)
    member_b_key, member_b_token = create_api_key(owner, "r2c-member-b-key", created_by=member_b)

    owner_client = api_client_for(owner_token)
    a_client = api_client_for(member_a_token)
    b_client = api_client_for(member_b_token)

    assert owner_client.get("/api/v1/inboxes").status_code == 200
    assert a_client.get("/api/v1/inboxes").status_code == 200
    assert b_client.get("/api/v1/inboxes").status_code == 200

    revoke_membership(owner, membership_a)

    # member-A's key: real bearer auth now 401.
    assert a_client.get("/api/v1/inboxes").status_code == 401

    
    # just an unrevoked_at DB check) — proves the cascade filter is
    # created_by-scoped, not over-broad.
    owner_key.refresh_from_db()
    assert owner_key.revoked_at is None
    assert owner_client.get("/api/v1/inboxes").status_code == 200

    member_b_key.refresh_from_db()
    assert member_b_key.revoked_at is None
    assert b_client.get("/api/v1/inboxes").status_code == 200



# _domain_owner_required_response was wired ONLY into DomainListCreateView.post.


# DELETE or force-VERIFY the tenant's domain. Fixed by adding the same guard
# to both verbs. These four tests pin it down.


@override_settings(ENABLE_TEAMS=True, ENABLE_BYOD=True)
def test_p1_member_key_cannot_delete_tenant_domain():
    owner = make_user("p1del_owner")
    member = make_user("p1del_member", plan=AccountPlan.Plan.FREE)
    add_member(owner, member)
    domain = Domain.objects.create(user=owner, name="p1del-owned.example.com")

    _, member_token = create_api_key(owner, "p1del-member-key", created_by=member)
    client = api_client_for(member_token)
    resp = client.delete(f"/api/v1/domains/{domain.public_id}")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "account_forbidden"
    assert Domain.objects.filter(pk=domain.pk).exists()


@override_settings(ENABLE_TEAMS=True, ENABLE_BYOD=True)
def test_p1_member_key_cannot_verify_tenant_domain():
    owner = make_user("p1ver_owner")
    member = make_user("p1ver_member", plan=AccountPlan.Plan.FREE)
    add_member(owner, member)
    domain = Domain.objects.create(user=owner, name="p1ver-owned.example.com")
    assert domain.status != Domain.Status.VERIFIED

    _, member_token = create_api_key(owner, "p1ver-member-key", created_by=member)
    client = api_client_for(member_token)
    resp = client.post(f"/api/v1/domains/{domain.public_id}/verify")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "account_forbidden"
    domain.refresh_from_db()
    assert domain.status != Domain.Status.VERIFIED


@override_settings(ENABLE_TEAMS=True, ENABLE_BYOD=True)
def test_p1_owner_key_can_delete_and_verify_own_domain(monkeypatch):
    owner = make_user("p1owner_owner")
    member = make_user("p1owner_member", plan=AccountPlan.Plan.FREE)
    add_member(owner, member)

    class _FakeRecord:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    verify_domain = Domain.objects.create(user=owner, name="p1owner-verify.example.com")

    class _FakeResolver:
        def resolve(self, qname, rdtype):
            if rdtype == "TXT":
                return [_FakeRecord(strings=[verify_domain.verification_token.encode()])]
            return [_FakeRecord(exchange="mx.mailsocket.app.")]

    monkeypatch.setattr("apps.domains.services._default_resolver", lambda: _FakeResolver())

    owner_key, owner_token = create_api_key(owner, "p1owner-key")
    client = api_client_for(owner_token)

    resp = client.post(f"/api/v1/domains/{verify_domain.public_id}/verify")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "verified"

    delete_domain_row = Domain.objects.create(user=owner, name="p1owner-delete.example.com")
    resp = client.delete(f"/api/v1/domains/{delete_domain_row.public_id}")
    assert resp.status_code == 204
    assert not Domain.objects.filter(pk=delete_domain_row.pk).exists()


@override_settings(ENABLE_TEAMS=True, ENABLE_BYOD=True)
def test_p1_member_gets_404_not_403_on_foreign_team_domain_delete_and_verify():
    "Public API."
    team_t_owner = make_user("p1x_t_owner")
    team_u_owner = make_user("p1x_u_owner")
    member_t = make_user("p1x_member_t", plan=AccountPlan.Plan.FREE)
    add_member(team_t_owner, member_t)

    u_domain = Domain.objects.create(user=team_u_owner, name="p1x-foreign.example.com")

    _, member_token = create_api_key(team_t_owner, "p1x-member-key", created_by=member_t)
    client = api_client_for(member_token)

    resp = client.delete(f"/api/v1/domains/{u_domain.public_id}")
    assert resp.status_code == 404
    assert Domain.objects.filter(pk=u_domain.pk).exists()

    resp = client.post(f"/api/v1/domains/{u_domain.public_id}/verify")
    assert resp.status_code == 404


@override_settings(ENABLE_TEAMS=False)
def test_p1_flag_off_domain_owner_gate_is_unconditionally_true():
    """With ENABLE_TEAMS off, _mint_api_key always sets created_by=user, so
    _api_key_is_owner_minted (and therefore _domain_owner_required_response)
    is unconditionally True for every key — zero behavior change flag-off."""
    from apps.api.views import _api_key_is_owner_minted

    user = make_user("p1flagoff_user")
    _, token = create_api_key(user, "p1flagoff-key")
    key = ApiKey.objects.get(user=user)
    assert key.created_by_id is None or key.created_by_id == key.user_id

    class FakeRequest:
        auth = key

    assert _api_key_is_owner_minted(FakeRequest()) is True

    domain = Domain.objects.create(user=user, name="p1flagoff.example.com")
    client = api_client_for(token)
    with override_settings(ENABLE_BYOD=True):
        resp = client.delete(f"/api/v1/domains/{domain.public_id}")
    assert resp.status_code == 204


# --- Role matrix: member forbidden actions -----------------------------------


@override_settings(ENABLE_TEAMS=True)
def test_role_matrix_member_forbidden_domain_create_invite_foreign_key_revoke():
    owner = make_user("role_owner")
    member = make_user("role_member", plan=AccountPlan.Plan.FREE)
    add_member(owner, member)

    assert require_owner_for(member, owner) is False

    _, member_token = create_api_key(owner, "role-member-key", created_by=member)
    client = api_client_for(member_token)
    with override_settings(ENABLE_BYOD=True):
        resp = client.post("/api/v1/domains", {"name": "role-forbidden.example.com"}, format="json")
        assert resp.status_code == 403

    dash = login(member)
    resp = dash.post(reverse("dashboard:team-invite-create"), {"email": "nope@example.test"})
    assert resp.status_code == 403

    owner_key, _ = create_api_key(owner, "role-owner-key")
    resp = dash.post(reverse("dashboard:api-key-revoke", kwargs={"public_id": owner_key.public_id}))
    assert resp.status_code == 404  # not owned by created_by=member -> no oracle


def require_owner_for(user, owner):
    """Helper mirroring require_owner()'s membership check without needing a
    live request object, for the role-matrix assertion above."""
    return not TeamMembership.objects.filter(user_id=user.pk, revoked_at__isnull=True).exists()


@override_settings(ENABLE_TEAMS=True)
def test_role_matrix_member_can_revoke_own_key_not_others():
    owner = make_user("role2_owner")
    member = make_user("role2_member", plan=AccountPlan.Plan.FREE)
    add_member(owner, member)

    own_key, _ = create_api_key(owner, "role2-own-key", created_by=member)
    other_key, _ = create_api_key(owner, "role2-other-key")

    dash = login(member)
    resp = dash.post(reverse("dashboard:api-key-revoke", kwargs={"public_id": own_key.public_id}))
    assert resp.status_code == 200
    own_key.refresh_from_db()
    assert own_key.revoked_at is not None

    resp = dash.post(reverse("dashboard:api-key-revoke", kwargs={"public_id": other_key.public_id}))
    assert resp.status_code == 404
    other_key.refresh_from_db()
    assert other_key.revoked_at is None


@override_settings(ENABLE_TEAMS=True)
def test_role_matrix_owner_can_revoke_any_team_key():
    owner = make_user("role3_owner")
    member = make_user("role3_member", plan=AccountPlan.Plan.FREE)
    add_member(owner, member)

    member_key, _ = create_api_key(owner, "role3-member-key", created_by=member)

    dash = login(owner)
    resp = dash.post(reverse("dashboard:api-key-revoke", kwargs={"public_id": member_key.public_id}))
    assert resp.status_code == 200
    member_key.refresh_from_db()
    assert member_key.revoked_at is not None


# --- Poll cursor: tenant-bound, same-team ok, other-team fails --------------


@override_settings(ENABLE_TEAMS=True)
def test_poll_cursor_member_a_validates_for_member_b_same_team_fails_other_team():
    from apps.dashboard.views import _decode_cursor, _encode_cursor

    owner = make_user("cursor_owner")
    member_a = make_user("cursor_member_a", plan=AccountPlan.Plan.FREE)
    member_b = make_user("cursor_member_b", plan=AccountPlan.Plan.FREE)
    other_team_owner = make_user("cursor_other_owner")
    other_team_member = make_user("cursor_other_member", plan=AccountPlan.Plan.FREE)
    add_member(owner, member_a)
    add_member(owner, member_b)
    add_member(other_team_owner, other_team_member)

    inbox = Inbox.objects.create(user=owner, routing_key="cursor_inbox")
    message = make_message(inbox, "cursor")

    class FakeRequest:
        def __init__(self, user):
            self.user = user

    req_a = FakeRequest(member_a)
    cursor = _encode_cursor(req_a, message, "inbox:all")

    req_b = FakeRequest(member_b)
    decoded_b = _decode_cursor(req_b, cursor, "inbox:all")
    assert decoded_b is not None

    req_other = FakeRequest(other_team_member)
    decoded_other = _decode_cursor(req_other, cursor, "inbox:all")
    assert decoded_other is None


# --- Flag-off regression: tenant() == request.user, full existing suite ------


@override_settings(ENABLE_TEAMS=False)
def test_flag_off_tenant_equals_request_user_even_with_live_membership():
    owner = make_user("flagoff_tenant_owner")
    member = make_user("flagoff_tenant_member", plan=AccountPlan.Plan.FREE)
    add_member(owner, member)

    class FakeRequest:
        def __init__(self, user):
            self.user = user

    assert tenant(FakeRequest(member)) == member
    assert tenant(FakeRequest(owner)) == owner


@override_settings(ENABLE_TEAMS=False)
def test_flag_off_member_dashboard_and_api_see_only_own_personal_data():
    owner = make_user("flagoff_regress2_owner")
    member = make_user("flagoff_regress2_member", plan=AccountPlan.Plan.FREE)
    add_member(owner, member)

    owner_inbox = Inbox.objects.create(user=owner, routing_key="flagoff_owner_inbox")
    member_inbox = Inbox.objects.create(user=member, routing_key="flagoff_member_inbox")

    client = login(member)
    resp = client.get(reverse("dashboard:inboxes"))
    assert list(resp.context["inboxes"]) == [member_inbox]

    _, token = create_api_key(member, "flagoff-member-key")
    api_client = api_client_for(token)
    resp = api_client.get("/api/v1/inboxes")
    ids = {row["id"] for row in resp.json()["data"]}
    assert ids == {member_inbox.public_id}
    assert owner_inbox.public_id not in ids


# --- 2-way proof: these tests FAIL under a mis-wired tenant() ---------------
#
# Documented, not separately executed (mis-wiring tenant() is a code change,
# not a runtime toggle):
#
# 1. If tenant(request) were mis-wired to return the ACTOR (request.user)
#    unconditionally (ignoring membership), then:
#      - test_r3_member_created_key_lists_team_inboxes_not_member_personal
#        and test_r3_member_own_api_key_mint_binds_to_tenant_by_construction
#        would FAIL: the member-minted key's ApiKey.user would be the

#        member_personal_inbox instead of team_inbox (assertion flips).
#      - test_r1a_personal_inbox_not_in_team_view_and_vice_versa_after_leave
#        would FAIL: the member's dashboard inbox list would show their
#        personal inbox, not the team inbox, while still a live member.
#      - test_usage_attributed_to_tenant_monthlyusage_row_not_member would
#        FAIL: MonthlyUsage would accrue on the member's row, not the

#
# 2. If a single dashboard scope site were left reading request.user
#    directly instead of tenant(request) (e.g. _owned_inboxes, or the
#    :1074-equivalent api_key_create binding), the corresponding test above
#    (test_r1a_..., test_r3_...) fails identically to case 1 for that one
#    site, isolating exactly which flip was missed — the same reasoning

