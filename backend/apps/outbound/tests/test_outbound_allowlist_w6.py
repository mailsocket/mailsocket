"Public API."
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.accounts.models import Account, AccountPlan
from apps.outbound.models import ApprovedRecipient, OutboundEmail, SuppressionEntry
from apps.outbound.tests.test_outbound import (
    _bomb_transport,
    _error_without_request_id,
    auth_client,
    eligible_domain,
    make_user,
    member_auth_client,
    send_body,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _enable_outbound(settings):
    settings.ENABLE_OUTBOUND = True


def enable_allowlist(user):
    Account.objects.filter(pk=user.pk).update(outbound_allowlist_enabled=True)


def approve(user, email):
    from apps.accounts.services import normalize_email

    return ApprovedRecipient.objects.create(
        user=user, email_normalized=normalize_email(email), created_by=user
    )


# --- (1) Default-OFF regression ----------------------------------------------


def test_default_off_regression_send_unchanged():
    "Public API."
    user = make_user("w6_default_off")
    domain = eligible_domain(user, "w6-default-off.example.com")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}", to=["anyone@example.test"]), format="json"
    )
    assert resp.status_code == 201, resp.json()
    account = Account.objects.get(pk=user.pk)
    assert account.outbound_allowlist_enabled is False


# --- (2) Mode ON + all approved -> 201 -> drain sends ------------------------


def test_mode_on_all_approved_sends():
    from apps.outbound.engine import claim_one, deliver_claim

    user = make_user("w6_all_approved")
    domain = eligible_domain(user, "w6-all-approved.example.com")
    enable_allowlist(user)
    approve(user, "dest@example.test")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}", to=["dest@example.test"]), format="json"
    )
    assert resp.status_code == 201, resp.json()

    calls = []
    pk, token = claim_one()
    result = deliver_claim(pk, token, transport=lambda **kw: calls.append(kw))
    assert result == "sent"
    # LOW: a no-op lambda would silently pass even if send never actually
    # reached transport — proving it WAS invoked tightens the always-422
    # decoy risk in this positive-drain path.
    assert len(calls) == 1


# --- (3) Mode ON + one unapproved among to/cc/bcc -> 422 generic ------------


def test_mode_on_unapproved_recipient_rejected_422_generic_body():
    user = make_user("w6_unapproved")
    domain = eligible_domain(user, "w6-unapproved.example.com")
    enable_allowlist(user)
    approve(user, "approved@example.test")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails",
        send_body(f"hello@{domain.name}", to=["approved@example.test", "notapproved@example.test"]),
        format="json",
    )
    assert resp.status_code == 422
    # Byte-exact envelope, not just "the two known addresses are absent" —
    # a leaked recipient count, position, or an unexpected extra key/field
    # in the error envelope would fail this even if neither literal address
    # string appeared anywhere in the body.
    assert _error_without_request_id(resp) == {
        "error": {
            "code": "recipient_not_approved",
            "message": "One or more recipients are not on the approved-recipients allowlist.",
            "fields": {},
        }
    }
    dumped = resp.content.decode()
    assert "notapproved@example.test" not in dumped
    assert "approved@example.test" not in dumped
    assert OutboundEmail.objects.filter(user=user).count() == 0


def test_unapproved_cc_rejected_422():
    """M1 decoy coverage: to/bcc are tested elsewhere — an unapproved CC
    address must be caught too (all three recipient fields feed the same
    allowlist gate; a cc-only bug would otherwise slip through untested)."""
    user = make_user("w6_cc")
    domain = eligible_domain(user, "w6-cc.example.com")
    enable_allowlist(user)
    approve(user, "to@example.test")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails",
        send_body(f"hello@{domain.name}", to=["to@example.test"], cc=["cc-leak@example.test"]),
        format="json",
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "recipient_not_approved"
    assert OutboundEmail.objects.filter(user=user).count() == 0


# --- (4) EMPTY allowlist + mode ON -> every send 422 (load-bearing) ---------


def test_empty_allowlist_mode_on_rejects_every_send():
    user = make_user("w6_empty_allowlist")
    domain = eligible_domain(user, "w6-empty-allowlist.example.com")
    enable_allowlist(user)
    assert ApprovedRecipient.objects.filter(user=user).count() == 0
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}", to=["anyone@example.test"]), format="json"
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "recipient_not_approved"
    assert OutboundEmail.objects.filter(user=user).count() == 0


# --- (5) bcc coverage: approved to + unapproved bcc -> 422 ------------------


def test_unapproved_bcc_rejected_422():
    user = make_user("w6_bcc")
    domain = eligible_domain(user, "w6-bcc.example.com")
    enable_allowlist(user)
    approve(user, "to@example.test")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails",
        send_body(f"hello@{domain.name}", to=["to@example.test"], bcc=["exfil@example.test"]),
        format="json",
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "recipient_not_approved"


# --- (6) Suppression precedence: suppressed AND unapproved -> recipient_rejected


def test_suppression_precedence_over_allowlist():
    user = make_user("w6_suppression_precedence")
    domain = eligible_domain(user, "w6-suppression.example.com")
    enable_allowlist(user)
    # deliberately NOT approved either, but suppression must win.
    SuppressionEntry.objects.create(
        email_normalized="suppressed@example.test", reason=SuppressionEntry.Reason.MANUAL
    )
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}", to=["suppressed@example.test"]), format="json"
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "recipient_rejected"


# --- (7) Cross-tenant enforcement: B's entry doesn't approve A's send -------


def test_cross_tenant_approval_does_not_leak():
    user_a = make_user("w6_cross_a")
    user_b = make_user("w6_cross_b")
    domain_a = eligible_domain(user_a, "w6-cross-a.example.com")
    enable_allowlist(user_a)
    approve(user_b, "shared@example.test")  # B approves it, not A.
    client_a = auth_client(user_a)
    resp = client_a.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain_a.name}", to=["shared@example.test"]), format="json"
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "recipient_not_approved"


# --- (8) Cross-tenant CRUD: A GET/DELETE B's apr_id -> 404 byte-identical ---


def test_cross_tenant_crud_404_byte_identical_with_nonexistent():
    user_a = make_user("w6_crud_a")
    user_b = make_user("w6_crud_b")
    row_b = approve(user_b, "target@example.test")
    client_a = auth_client(user_a)

    resp_wrong_tenant = client_a.delete(f"/api/v1/outbound_allowlist/{row_b.public_id}")
    resp_nonexistent = client_a.delete("/api/v1/outbound_allowlist/apr_doesnotexist000000000000")

    assert resp_wrong_tenant.status_code == 404
    assert resp_nonexistent.status_code == 404
    assert _error_without_request_id(resp_wrong_tenant) == _error_without_request_id(resp_nonexistent)
    # B's row must still exist (A's DELETE never touched it).
    assert ApprovedRecipient.objects.filter(pk=row_b.pk).exists()


def test_cross_tenant_get_404_byte_identical_with_nonexistent():
    """M2: DELETE's no-oracle guarantee was covered, but GET has no per-id
    detail endpoint of its own — the closest analog is listing, which never
    leaks another tenant's row either way. This proves the same _owned_
    approved_recipient no-oracle helper, exercised via the actual list
    response, gives A zero visibility into B's row: A's list is empty
    (byte-identical to a tenant with literally zero approved recipients),
    matching the DELETE-side guarantee that wrong-tenant and nonexistent
    are indistinguishable."""
    user_a = make_user("w6_get_crud_a")
    user_b = make_user("w6_get_crud_b")
    approve(user_b, "target@example.test")
    client_a = auth_client(user_a)
    client_never_approved = auth_client(make_user("w6_get_crud_never"))

    resp_a = client_a.get("/api/v1/outbound_allowlist")
    resp_never = client_never_approved.get("/api/v1/outbound_allowlist")

    assert resp_a.status_code == 200
    assert resp_never.status_code == 200
    assert resp_a.json()["data"] == []
    assert resp_never.json()["data"] == []
    assert resp_a.content == resp_never.content


# --- (9) Normalization: approve Bar.COM -> send to bar.com passes; +tag 422 -


def test_normalization_case_and_plus_tag():
    user = make_user("w6_normalization")
    domain = eligible_domain(user, "w6-normalization.example.com")
    enable_allowlist(user)
    client = auth_client(user)

    approve_resp = client.post(
        "/api/v1/outbound_allowlist", {"email": "Foo@Bar.COM"}, format="json"
    )
    assert approve_resp.status_code == 201, approve_resp.json()
    assert approve_resp.json()["data"]["email"] == "foo@bar.com"

    resp_case = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}", to=["FOO@bar.com"]), format="json"
    )
    assert resp_case.status_code == 201, resp_case.json()

    resp_plus_tag = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}", to=["foo+tag@bar.com"]), format="json"
    )
    assert resp_plus_tag.status_code == 422
    assert resp_plus_tag.json()["error"]["code"] == "recipient_not_approved"


# --- (10) Duplicate POST -> 409; entries cap -> 422 -------------------------


def test_duplicate_post_409():
    user = make_user("w6_dup")
    client = auth_client(user)
    resp1 = client.post("/api/v1/outbound_allowlist", {"email": "dup@example.test"}, format="json")
    assert resp1.status_code == 201
    resp2 = client.post("/api/v1/outbound_allowlist", {"email": "dup@example.test"}, format="json")
    assert resp2.status_code == 409
    assert resp2.json()["error"]["code"] == "approved_recipient_exists"


def test_entries_cap_422(settings):
    settings.OUTBOUND_ALLOWLIST_MAX_ENTRIES = 2
    user = make_user("w6_cap")
    client = auth_client(user)
    assert client.post("/api/v1/outbound_allowlist", {"email": "a@example.test"}, format="json").status_code == 201
    assert client.post("/api/v1/outbound_allowlist", {"email": "b@example.test"}, format="json").status_code == 201
    resp = client.post("/api/v1/outbound_allowlist", {"email": "c@example.test"}, format="json")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "outbound_allowlist_limit_reached"


# --- (11) Tier wall + demo/agent + member-key + flag-off on all 4 endpoints -


@pytest.mark.parametrize("plan", [AccountPlan.Plan.FREE, AccountPlan.Plan.PAID, AccountPlan.Plan.SCALE])
def test_tier_wall_non_business_403_on_all_four_endpoints(plan):
    """M3: real matrix. The tenant is on a real free/paid/scale plan, and we
    prove the DELETE wall fires identically whether the target id is an
    EXISTING row this tenant actually owns (created directly via the ORM,
    bypassing the POST tier wall, exactly like a Business->downgrade
    scenario would leave rows behind) or a row that never existed at all —
    after the CODE TWEAK reordering DELETE's walls before the row lookup,
    both must be the SAME 403 outbound_allowlist_entitlement_required, with
    no oracle on which case fired."""
    user = make_user(f"w6_tier_{plan}", plan=plan)
    client = auth_client(user)
    owned_row = approve(user, f"owned-{plan}@example.test")

    resp_get = client.get("/api/v1/outbound_allowlist")
    assert resp_get.status_code == 403
    assert resp_get.json()["error"]["code"] == "outbound_allowlist_entitlement_required"

    resp_post = client.post("/api/v1/outbound_allowlist", {"email": "x@example.test"}, format="json")
    assert resp_post.status_code == 403
    assert resp_post.json()["error"]["code"] == "outbound_allowlist_entitlement_required"

    resp_mode = client.post("/api/v1/outbound_allowlist/mode", {"enabled": True}, format="json")
    assert resp_mode.status_code == 403
    assert resp_mode.json()["error"]["code"] == "outbound_allowlist_entitlement_required"

    # DELETE now runs the tier wall BEFORE the owned-row lookup (post-tweak
    # order), so an EXISTING owned id and a nonexistent one are byte-for-
    # byte the same 403 — no existence oracle survives the tier wall.
    resp_delete_existing = client.delete(f"/api/v1/outbound_allowlist/{owned_row.public_id}")
    resp_delete_missing = client.delete("/api/v1/outbound_allowlist/apr_doesnotexist000000000000")
    assert resp_delete_existing.status_code == 403
    assert resp_delete_existing.json()["error"]["code"] == "outbound_allowlist_entitlement_required"
    assert resp_delete_missing.status_code == 403
    assert resp_delete_missing.json()["error"]["code"] == "outbound_allowlist_entitlement_required"
    assert _error_without_request_id(resp_delete_existing) == _error_without_request_id(resp_delete_missing)
    # The wall, not the mutation, stopped it — row must still exist.
    assert ApprovedRecipient.objects.filter(pk=owned_row.pk).exists()


def test_demo_or_agent_403_on_allowlist_endpoints(monkeypatch):
    user = make_user("w6_demo_agent")
    client = auth_client(user)
    monkeypatch.setattr(
        "apps.accounts.team_services.is_demo_or_agent_user", lambda u: True
    )
    resp = client.get("/api/v1/outbound_allowlist")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "outbound_entitlement_required"


def test_real_demo_user_403_on_all_four_allowlist_endpoints():
    """M3: a genuine get_demo_user() account (not a monkeypatched fixture),
    forced onto Business so ONLY the demo/agent wall (not the tier wall)
    could plausibly explain the 403 — proving that wall is truly
    independent of tier, across all 4 routes including DELETE on a real
    owned row."""
    from apps.accounts.services import get_demo_user

    demo_user = get_demo_user()
    AccountPlan.objects.filter(user=demo_user).update(plan=AccountPlan.Plan.BUSINESS)
    client = auth_client(demo_user)
    owned_row = approve(demo_user, "demo-owned@example.test")

    resp_get = client.get("/api/v1/outbound_allowlist")
    assert resp_get.status_code == 403
    assert resp_get.json()["error"]["code"] == "outbound_entitlement_required"

    resp_post = client.post("/api/v1/outbound_allowlist", {"email": "x@example.test"}, format="json")
    assert resp_post.status_code == 403
    assert resp_post.json()["error"]["code"] == "outbound_entitlement_required"

    resp_mode = client.post("/api/v1/outbound_allowlist/mode", {"enabled": True}, format="json")
    assert resp_mode.status_code == 403
    assert resp_mode.json()["error"]["code"] == "outbound_entitlement_required"

    resp_delete = client.delete(f"/api/v1/outbound_allowlist/{owned_row.public_id}")
    assert resp_delete.status_code == 403
    assert resp_delete.json()["error"]["code"] == "outbound_entitlement_required"
    assert ApprovedRecipient.objects.filter(pk=owned_row.pk).exists()


def test_real_agent_register_key_403_on_all_four_allowlist_endpoints():
    """M3: a genuine /api/v1/agents/register-minted key (not a hand-rolled
    agent user), forced onto Business — same independent-wall proof as the
    demo user above, but for the self-registered agent account path."""
    from apps.accounts.services import register_agent

    agent_user, _api_key, token = register_agent("w6-wall-test-agent")
    AccountPlan.objects.filter(user=agent_user).update(plan=AccountPlan.Plan.BUSINESS)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    owned_row = approve(agent_user, "agent-owned@example.test")

    resp_get = client.get("/api/v1/outbound_allowlist")
    assert resp_get.status_code == 403
    assert resp_get.json()["error"]["code"] == "outbound_entitlement_required"

    resp_post = client.post("/api/v1/outbound_allowlist", {"email": "x@example.test"}, format="json")
    assert resp_post.status_code == 403
    assert resp_post.json()["error"]["code"] == "outbound_entitlement_required"

    resp_mode = client.post("/api/v1/outbound_allowlist/mode", {"enabled": True}, format="json")
    assert resp_mode.status_code == 403
    assert resp_mode.json()["error"]["code"] == "outbound_entitlement_required"

    resp_delete = client.delete(f"/api/v1/outbound_allowlist/{owned_row.public_id}")
    assert resp_delete.status_code == 403
    assert resp_delete.json()["error"]["code"] == "outbound_entitlement_required"
    assert ApprovedRecipient.objects.filter(pk=owned_row.pk).exists()


def test_member_key_403_account_forbidden_on_mutation_endpoints():
    owner = make_user("w6_member_owner")
    member = get_user_model().objects.create_user(
        username="w6_member_only", email="w6_member_only@example.test", password="password"
    )
    member_client = member_auth_client(owner, member)
    owned_row = approve(owner, "member-wall-owned@example.test")

    resp_post = member_client.post("/api/v1/outbound_allowlist", {"email": "x@example.test"}, format="json")
    assert resp_post.status_code == 403
    assert resp_post.json()["error"]["code"] == "account_forbidden"

    resp_mode = member_client.post("/api/v1/outbound_allowlist/mode", {"enabled": True}, format="json")
    assert resp_mode.status_code == 403
    assert resp_mode.json()["error"]["code"] == "account_forbidden"

    
    # runs on EVERY endpoint, GET included — a member key is refused there
    # too. Enforcement of the allowlist ON SENDS still applies to member
    # keys (see the enqueue gate tests above), only CONFIGURING/reading the
    
    resp_get = member_client.get("/api/v1/outbound_allowlist")
    assert resp_get.status_code == 403
    assert resp_get.json()["error"]["code"] == "account_forbidden"

    
    # lookup, so a member key gets the SAME 403 account_forbidden whether
    
    # existed — pre-tweak, an EXISTING owned id used to leak a distinct
    # 403 vs. 404-for-nonexistent (an intra-tenant existence oracle); that
    # signal is now gone.
    resp_delete_existing = member_client.delete(f"/api/v1/outbound_allowlist/{owned_row.public_id}")
    resp_delete_missing = member_client.delete("/api/v1/outbound_allowlist/apr_doesnotexist000000000000")
    assert resp_delete_existing.status_code == 403
    assert resp_delete_existing.json()["error"]["code"] == "account_forbidden"
    assert resp_delete_missing.status_code == 403
    assert resp_delete_missing.json()["error"]["code"] == "account_forbidden"
    assert _error_without_request_id(resp_delete_existing) == _error_without_request_id(resp_delete_missing)
    assert ApprovedRecipient.objects.filter(pk=owned_row.pk).exists()


def test_flag_off_404_on_all_four_allowlist_routes(settings):
    settings.ENABLE_OUTBOUND = False
    user = make_user("w6_flag_off")
    client = auth_client(user)
    assert client.get("/api/v1/outbound_allowlist").status_code == 404
    assert client.post("/api/v1/outbound_allowlist", {"email": "x@example.test"}, format="json").status_code == 404
    assert client.delete("/api/v1/outbound_allowlist/apr_doesnotexist000000000000").status_code == 404
    assert client.post("/api/v1/outbound_allowlist/mode", {"enabled": True}, format="json").status_code == 404


# --- (12) Revoke-mid-queue: DELETE entry -> drain REJECTED, bomb transport --


def test_revoke_mid_queue_drain_rejects_bomb_transport_never_called():
    "Public API."
    from apps.outbound.engine import claim_one, deliver_claim

    user = make_user("w6_revoke_mid_queue")
    domain = eligible_domain(user, "w6-revoke-mid-queue.example.com")
    enable_allowlist(user)
    row = approve(user, "victim@example.test")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}", to=["victim@example.test"]), format="json"
    )
    assert resp.status_code == 201, resp.json()

    # Revoke AFTER enqueue, BEFORE drain (key-compromise scenario) via the
    # real authenticated DELETE endpoint, not an ORM shortcut.
    delete_resp = client.delete(f"/api/v1/outbound_allowlist/{row.public_id}")
    assert delete_resp.status_code == 204
    assert not ApprovedRecipient.objects.filter(pk=row.pk).exists()

    pk, token = claim_one()
    result = deliver_claim(pk, token, transport=_bomb_transport)
    assert result == "rejected"
    outbound_row = OutboundEmail.objects.get(pk=pk)
    assert outbound_row.status == OutboundEmail.Status.REJECTED
    assert outbound_row.error_code == "recipient_not_approved"


# --- (13) Enable-mid-queue: flip ON (empty) -> drain rejects ----------------


def test_enable_mid_queue_empty_list_drain_rejects():
    from apps.outbound.engine import claim_one, deliver_claim

    user = make_user("w6_enable_mid_queue")
    domain = eligible_domain(user, "w6-enable-mid-queue.example.com")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}", to=["dest@example.test"]), format="json"
    )
    assert resp.status_code == 201, resp.json()

    # Flip mode ON with an EMPTY allowlist AFTER enqueue, BEFORE drain.
    enable_allowlist(user)

    pk, token = claim_one()
    result = deliver_claim(pk, token, transport=_bomb_transport)
    assert result == "rejected"
    outbound_row = OutboundEmail.objects.get(pk=pk)
    assert outbound_row.error_code == "recipient_not_approved"


# --- (14) Mode OFF-mid-queue: flip OFF + delete entry -> still sends --------


def test_mode_off_mid_queue_permissive_still_sends():
    from apps.outbound.engine import claim_one, deliver_claim

    user = make_user("w6_off_mid_queue")
    domain = eligible_domain(user, "w6-off-mid-queue.example.com")
    enable_allowlist(user)
    row = approve(user, "dest@example.test")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}", to=["dest@example.test"]), format="json"
    )
    assert resp.status_code == 201, resp.json()

    # Flip mode OFF and delete the entry — explicit permissive tenant action.
    Account.objects.filter(pk=user.pk).update(outbound_allowlist_enabled=False)
    row.delete()

    calls = []
    pk, token = claim_one()
    result = deliver_claim(pk, token, transport=lambda **kw: calls.append(kw))
    assert result == "sent"
    # LOW: prove the send actually happened (call-spy), not just a no-op
    # lambda that would pass regardless of whether transport ran.
    assert len(calls) == 1





def test_no_quota_or_velocity_consumption_on_rejection():
    "Public API."
    from datetime import timedelta

    from django.db.models import Sum
    from django.utils import timezone as django_timezone

    from apps.accounts.models import RateLimitBucket
    from apps.accounts.services import _bucket_hash

    user = make_user("w6_no_quota_consumption")
    domain = eligible_domain(user, "w6-no-quota.example.com")
    enable_allowlist(user)
    client = auth_client(user)

    # Prime the per-key rate-limit buckets (BearerAPIView.initial runs on
    # EVERY request regardless of outcome) with a harmless GET first, so the
    # before/after delta below isolates the OUTBOUND-specific velocity
    # bucket (outbound:enqueue:account:hour), which gate #7.5 must never
    # reach on rejection.
    client.get("/api/v1/outbound_allowlist")
    bucket_count_before = RateLimitBucket.objects.count()

    velocity_bucket_hash = _bucket_hash("outbound:enqueue:account:hour", str(user.pk))
    velocity_bucket_before = RateLimitBucket.objects.filter(bucket_hash=velocity_bucket_hash).first()
    velocity_counter_before = velocity_bucket_before.count if velocity_bucket_before else 0

    now = django_timezone.now()
    quota_sum_before = (
        OutboundEmail.objects.filter(user=user, created_at__gte=now - timedelta(hours=24))
        .aggregate(total=Sum("recipient_count"))["total"]
        or 0
    )

    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}", to=["anyone@example.test"]), format="json"
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "recipient_not_approved"
    assert OutboundEmail.objects.filter(user=user).count() == 0
    # The velocity/queue-depth bucket (outbound:enqueue:account:hour) is
    
    # must never reach it, so no NEW bucket row appears, AND (the numeric
    # check) no existing bucket's counter moves.
    assert RateLimitBucket.objects.count() == bucket_count_before
    velocity_bucket_after = RateLimitBucket.objects.filter(bucket_hash=velocity_bucket_hash).first()
    velocity_counter_after = velocity_bucket_after.count if velocity_bucket_after else 0
    assert velocity_counter_after == velocity_counter_before

    quota_sum_after = (
        OutboundEmail.objects.filter(user=user, created_at__gte=now - timedelta(hours=24))
        .aggregate(total=Sum("recipient_count"))["total"]
        or 0
    )
    assert quota_sum_after == quota_sum_before
