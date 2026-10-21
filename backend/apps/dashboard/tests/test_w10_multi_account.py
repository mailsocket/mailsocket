"Public API."
import threading

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, AccountOwnership, AccountPlan, ApiKey, TeamMembership
from apps.accounts.multi_account_services import (
    NotEntitled,
    create_owned_account,
    is_workspace_user,
)
from apps.accounts.services import create_api_key
from apps.accounts.team_services import accept_invite, create_invite, revoke_membership
from apps.accounts.tenancy import SESSION_KEY, tenant
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


def _message_values(inbox, suffix):
    import hashlib

    raw = f"Subject: {suffix}\r\n\r\nbody".encode()
    digest = hashlib.sha256(raw).hexdigest()
    return {
        "inbox": inbox,
        "dedupe_key": digest,
        "envelope_recipient": inbox.address,
        "subject": suffix,
        "raw_bytes": raw,
        "raw_sha256": digest,
        "size_bytes": len(raw),
    }


def set_session(client, key, value):
    session = client.session
    session[key] = value
    session.save()


class FakeRequest:
    def __init__(self, user, session=None):
        self.user = user
        self.session = session if session is not None else {}


# --- Switch authorization (P0) ------------------------------------------------


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_t1_switch_to_owned_returns_200_and_tenant_resolves_workspace():
    owner = make_user("t1_owner")
    link = create_owned_account(owner, "Client A")

    client = login(owner)
    resp = client.post(reverse("dashboard:account-switch"), {"account": link.account_user_id})
    assert resp.status_code == 200

    session = client.session
    assert session[SESSION_KEY] == link.account_user_id

    req = FakeRequest(owner, session={SESSION_KEY: link.account_user_id})
    assert tenant(req).pk == link.account_user_id


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_t2_forged_other_humans_primary_pk_falls_through_to_self_no_500():
    owner = make_user("t2_owner")
    other_human = make_user("t2_other")

    req = FakeRequest(owner, session={SESSION_KEY: other_human.pk})
    assert tenant(req) == owner


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_t3_forged_other_humans_workspace_pk_falls_through_to_self():
    owner = make_user("t3_owner")
    other_owner = make_user("t3_other_owner")
    other_link = create_owned_account(other_owner, "Other WS")

    req = FakeRequest(owner, session={SESSION_KEY: other_link.account_user_id})
    assert tenant(req) == owner


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_t4_stale_link_deleted_falls_through_to_self_next_request():
    owner = make_user("t4_owner")
    link = create_owned_account(owner, "Temp WS")
    ws_pk = link.account_user_id

    req = FakeRequest(owner, session={SESSION_KEY: ws_pk})
    assert tenant(req).pk == ws_pk

    link.delete()

    req2 = FakeRequest(owner, session={SESSION_KEY: ws_pk})
    assert tenant(req2) == owner


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_t5_switch_unowned_or_nonexistent_pk_returns_uniform_404():
    owner = make_user("t5_owner")
    other_owner = make_user("t5_other_owner")
    other_link = create_owned_account(other_owner, "Not Yours")

    client = login(owner)
    resp_not_yours = client.post(
        reverse("dashboard:account-switch"), {"account": other_link.account_user_id}
    )
    assert resp_not_yours.status_code == 404

    resp_nonexistent = client.post(
        reverse("dashboard:account-switch"), {"account": 999999999}
    )
    assert resp_nonexistent.status_code == 404
    # Per-request noise only (own X-Request-Id + own path in the report link).
    assert strip_request_noise(resp_nonexistent.content) == strip_request_noise(resp_not_yours.content)


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_t6_switch_via_get_redirects_and_no_csrf_403():
    owner = make_user("t6_owner")
    link = create_owned_account(owner, "WS")

    client = login(owner)
    # QA follow-up: a browser GET on the POST-only endpoint redirects to the
    # overview (was a bare 405) and switches nothing.
    resp = client.get(reverse("dashboard:account-switch"))
    assert resp.status_code == 302
    assert resp["Location"] == reverse("dashboard:overview")
    assert client.put(reverse("dashboard:account-switch")).status_code == 405

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(owner)
    resp = csrf_client.post(
        reverse("dashboard:account-switch"), {"account": link.account_user_id}
    )
    assert resp.status_code == 403


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_t7_session_non_int_or_self_falls_through_no_crash():
    owner = make_user("t7_owner")

    req = FakeRequest(owner, session={SESSION_KEY: "not-an-int"})
    assert tenant(req) == owner

    req2 = FakeRequest(owner, session={SESSION_KEY: owner.pk})
    assert tenant(req2) == owner


# --- Isolation (P0) ------------------------------------------------------------


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_t8_active_workspace_scopes_inboxes_keys_webhooks_usage():
    "Public API."
    owner = make_user("t8_owner")
    link_a = create_owned_account(owner, "Workspace A")
    link_b = create_owned_account(owner, "Workspace B")
    ws_a = link_a.account_user
    ws_b = link_b.account_user

    inbox_a = Inbox.objects.create(user=ws_a, routing_key="t8_inbox_a", label="Inbox A label")
    inbox_b = Inbox.objects.create(user=ws_b, routing_key="t8_inbox_b", label="Inbox B label")

    key_a = ApiKey.objects.create(
        user=ws_a, name="a-key-unique-name", prefix="t8aaaaaaaaaa", key_hash="a" * 64,
    )
    key_b = ApiKey.objects.create(
        user=ws_b, name="b-key-unique-name", prefix="t8bbbbbbbbbb", key_hash="b" * 64,
    )

    def resolver(_):
        return ["93.184.216.34"]
    # Direct ORM create (bypassing create_webhook's plan-entitlement gate,
    # which the FREE-plan workspace user fails) — entitlement gating is out
    # of scope for this isolation test; Webhook.save() still runs SSRF
    # target validation via validate_webhook_target through the normal save
    # path, we just skip create_webhook's can_enable_webhook check.
    from apps.webhooks.models import Webhook as _Webhook
    from apps.webhooks.security import validate_webhook_target

    def _direct_webhook(inbox, url):
        target = validate_webhook_target(url, resolver=resolver)
        webhook = _Webhook(inbox=inbox, url=target.url, is_enabled=True)
        webhook.save(_url_validated=True)
        return webhook

    webhook_a = _direct_webhook(inbox_a, "https://receiver-a.example/hook")
    webhook_b = _direct_webhook(inbox_b, "https://receiver-b.example/hook")

    message_a = Message.objects.create(**_message_values(inbox_a, "t8-msg-a"))
    message_b = Message.objects.create(**_message_values(inbox_b, "t8-msg-b"))

    
    # one inbox, so inbox_count == 1 on the usage page passed whether the
    # page resolved A or B — the oracle proved nothing. Drive each
    # workspace's MonthlyUsage.api_requests to a distinct, uniquely
    # identifiable nonzero number (A=77, B=13) so the usage page's rendered
    # number can only match if it actually resolved A's tenant.
    from apps.accounts.entitlements import utc_month_start
    from apps.accounts.models import MonthlyUsage

    period_start = utc_month_start()
    MonthlyUsage.objects.create(user=ws_a, period_start=period_start, api_requests=77)
    MonthlyUsage.objects.create(user=ws_b, period_start=period_start, api_requests=13)

    client = login(owner)
    client.post(reverse("dashboard:account-switch"), {"account": ws_a.pk})

    # --- Inboxes page: A's inbox present (rendered bytes + context), B's absent.
    resp = client.get(reverse("dashboard:inboxes"))
    inbox_list = list(resp.context["inboxes"])
    assert inbox_list == [inbox_a]
    assert inbox_b not in inbox_list
    assert inbox_a.label.encode() in resp.content
    assert inbox_b.label.encode() not in resp.content
    assert inbox_b.public_id.encode() not in resp.content

    # --- API keys page: A's key name present, B's absent.
    resp = client.get(reverse("dashboard:api-keys"))
    key_list = list(resp.context["api_keys"])
    assert key_list == [key_a]
    assert key_b not in key_list
    assert key_a.name.encode() in resp.content
    assert key_b.name.encode() not in resp.content

    # --- Webhooks page: A's webhook (via its inbox) present, B's absent.
    resp = client.get(reverse("dashboard:webhooks"))
    webhook_list = list(resp.context["webhooks"])
    assert webhook_list == [webhook_a]
    assert webhook_b not in webhook_list
    assert webhook_a.url.encode() in resp.content
    assert webhook_b.url.encode() not in resp.content

    # --- Usage page: reflects A's own MonthlyUsage, not a global/other-tenant
    # one. A=77 api_requests, B=13 — a distinguishing pair, so this fails if
    # the page resolves B instead of A. Scoped to the rendered usage-card
    # markup (`<strong>N</strong>`) rather than a bare substring scan, so an
    # unrelated PK elsewhere on the page (e.g. a workspace-switch
    # `value="713"`) can't collide with "13" and produce a false positive.
    resp = client.get(reverse("dashboard:usage"))
    assert resp.status_code == 200
    assert resp.context["usage"]["api_requests"] == 77
    assert b">77<" in resp.content
    assert b">13<" not in resp.content

    # --- Foreign 404: a real Message public_id (not an Inbox public_id)
    # belonging to workspace B, requested while switched to A, must 404.
    resp = client.get(
        reverse("dashboard:message-detail", kwargs={"public_id": message_b.public_id})
    )
    assert resp.status_code == 404

    # --- Positive control: A's own message, same view, must resolve.
    resp = client.get(
        reverse("dashboard:message-detail", kwargs={"public_id": message_a.public_id})
    )
    assert resp.status_code == 200
    assert message_a.public_id.encode() in resp.content or message_a.subject.encode() in resp.content


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_t9_cross_tenant_id_while_switched_returns_404():
    owner = make_user("t9_owner")
    link = create_owned_account(owner, "WS")
    ws = link.account_user
    decoy_owner = make_user("t9_decoy")
    decoy_inbox = Inbox.objects.create(user=decoy_owner, routing_key="t9_decoy_inbox")

    client = login(owner)
    client.post(reverse("dashboard:account-switch"), {"account": ws.pk})

    resp = client.get(
        reverse("dashboard:inbox-rename", kwargs={"public_id": decoy_inbox.public_id})
    )
    # QA follow-up: GET on a POST-only endpoint redirects to the list page.
    assert resp.status_code == 302
    assert resp["Location"] == reverse("dashboard:inboxes")

    resp = client.post(
        reverse("dashboard:inbox-rename", kwargs={"public_id": decoy_inbox.public_id}),
        {"label": "pwned"},
    )
    assert resp.status_code == 404


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_t10_api_key_under_workspace_a_scoped_primary_unaffected_by_switch():
    owner = make_user("t10_owner")
    link = create_owned_account(owner, "Workspace A")
    ws = link.account_user

    ws_inbox = Inbox.objects.create(user=ws, routing_key="t10_ws_inbox")
    primary_inbox = Inbox.objects.create(user=owner, routing_key="t10_primary_inbox")

    _, ws_token = create_api_key(ws, "ws-key")
    _, primary_token = create_api_key(owner, "primary-key")

    ws_client = api_client_for(ws_token)
    resp = ws_client.get("/api/v1/inboxes")
    ids = {row["id"] for row in resp.json()["data"]}
    assert ws_inbox.public_id in ids
    assert primary_inbox.public_id not in ids

    dash = login(owner)
    dash.post(reverse("dashboard:account-switch"), {"account": ws.pk})

    primary_client = api_client_for(primary_token)
    resp = primary_client.get("/api/v1/inboxes")
    ids = {row["id"] for row in resp.json()["data"]}
    assert primary_inbox.public_id in ids
    assert ws_inbox.public_id not in ids


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_t11_usage_verify_buckets_charge_active_workspace_not_primary():
    "Public API."
    owner = make_user("t11_owner")
    link = create_owned_account(owner, "Workspace A")
    ws = link.account_user

    _, ws_token = create_api_key(ws, "ws-key")
    ws_client = api_client_for(ws_token)

    from apps.accounts.models import MonthlyUsage

    period_start = timezone.now().date().replace(day=1)

    # Baseline: nothing charged yet for either identity.
    assert MonthlyUsage.objects.filter(user=ws, period_start=period_start).first() is None
    assert MonthlyUsage.objects.filter(user=owner, period_start=period_start).first() is None

    resp = ws_client.get("/api/v1/inboxes")
    assert resp.status_code == 200

    resp = ws_client.post("/api/v1/verify", {"email": "someone@example.test"}, format="json")
    assert resp.status_code == 200

    ws_usage = MonthlyUsage.objects.filter(user=ws, period_start=period_start).first()
    owner_usage = MonthlyUsage.objects.filter(user=owner, period_start=period_start).first()

    assert ws_usage is not None
    assert ws_usage.api_requests >= 2  # the list call + the verify call both hit BearerAPIView
    assert ws_usage.verifications_used == 1
    assert owner_usage is None or (
        owner_usage.api_requests == 0 and owner_usage.verifications_used == 0
    )

    from apps.accounts.models import RateLimitBucket
    from apps.accounts.services import _bucket_hash

    # The verify buckets are keyed by identity (account bucket by user.pk),
    # so assert A's account bucket incremented and the primary human never
    # gets a bucket of its own from this traffic.
    account_bucket = RateLimitBucket.objects.filter(
        bucket_hash=_bucket_hash("api:verify:account:hour", str(ws.pk)),
    ).first()
    assert account_bucket is not None and account_bucket.count >= 1

    owner_account_bucket = RateLimitBucket.objects.filter(
        bucket_hash=_bucket_hash("api:verify:account:hour", str(owner.pk)),
    ).first()
    assert owner_account_bucket is None


# --- Flag-off identity (P0) -----------------------------------------------------


@override_settings(ENABLE_MULTI_ACCOUNT=False, ENABLE_TEAMS=False)
def test_t12_flag_off_tenant_equals_request_user_switch_and_new_404_switcher_absent():
    owner = make_user("t12_owner")

    # Rows + a session key exist, but with the flag off tenant() must ignore
    # them entirely.
    other_user = make_user("t12_other")
    AccountOwnership.objects.create(owner_user=owner, account_user=other_user, name="ghost")

    req = FakeRequest(owner, session={SESSION_KEY: other_user.pk})
    assert tenant(req) == owner

    client = login(owner)
    set_session(client, SESSION_KEY, other_user.pk)
    resp = client.post(reverse("dashboard:account-switch"), {"account": other_user.pk})
    assert resp.status_code == 404
    resp = client.post(reverse("dashboard:account-new"), {"name": "nope"})
    assert resp.status_code == 404

    
    # ms_multi_account context flag — a template that rendered the switcher
    # off a DIFFERENT, independently-controlled variable would still pass.
    # Assert the switcher is absent from the RENDERED bytes too: neither the
    # switch/new URLs nor the "ghost" workspace name may appear anywhere in
    # the response body.
    resp = client.get(reverse("dashboard:overview"))
    assert "ms_multi_account" not in resp.context or not resp.context["ms_multi_account"]
    switch_url = reverse("dashboard:account-switch").encode()
    new_url = reverse("dashboard:account-new").encode()
    assert switch_url not in resp.content
    assert new_url not in resp.content
    assert b"ghost" not in resp.content
    assert b"account-switcher" not in resp.content

    # Combined fast path: with BOTH flags off, tenant() never touches the DB.
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as ctx:
        result = tenant(FakeRequest(owner, session={SESSION_KEY: other_user.pk}))
    assert result == owner
    assert len(ctx.captured_queries) == 0





@override_settings(ENABLE_MULTI_ACCOUNT=True, ENABLE_TEAMS=True)
def test_t13_live_member_with_owned_workspaces_resolves_to_team_owner_switch_refused():
    team_owner = make_user("t13_team_owner")
    member = make_user("t13_member", plan=AccountPlan.Plan.FREE)
    add_member(team_owner, member)
    link = create_owned_account(member, "Member's WS")
    ws = link.account_user

    req = FakeRequest(member, session={SESSION_KEY: ws.pk})
    assert tenant(req) == team_owner

    client = login(member)
    resp = client.post(reverse("dashboard:account-switch"), {"account": ws.pk})
    assert resp.status_code == 404


@override_settings(ENABLE_MULTI_ACCOUNT=True, ENABLE_TEAMS=True)
def test_t14_member_leaves_then_switch_works_next_request():
    from apps.accounts.team_services import leave_team

    team_owner = make_user("t14_team_owner")
    member = make_user("t14_member", plan=AccountPlan.Plan.FREE)
    membership = add_member(team_owner, member)
    link = create_owned_account(member, "Member's WS")
    ws = link.account_user

    req = FakeRequest(member, session={SESSION_KEY: ws.pk})
    assert tenant(req) == team_owner

    leave_team(member, membership)

    req2 = FakeRequest(member, session={SESSION_KEY: ws.pk})
    assert tenant(req2).pk == ws.pk

    client = login(member)
    resp = client.post(reverse("dashboard:account-switch"), {"account": ws.pk})
    assert resp.status_code == 200


@override_settings(ENABLE_MULTI_ACCOUNT=True, ENABLE_TEAMS=True)
def test_t15_owner_with_members_and_workspaces_member_cannot_reach_owner_workspace():
    "Public API."
    owner = make_user("t15_owner")
    member = make_user("t15_member", plan=AccountPlan.Plan.FREE)
    add_member(owner, member)
    link = create_owned_account(owner, "Owner's WS")
    ws = link.account_user
    ws_inbox = Inbox.objects.create(user=ws, routing_key="t15_ws_inbox")

    owner_client = login(owner)
    resp = owner_client.post(reverse("dashboard:account-switch"), {"account": ws.pk})
    assert resp.status_code == 200

    member_req = FakeRequest(member)
    member_req.session = {}
    assert tenant(member_req) == owner

    member_client = Client(enforce_csrf_checks=True)
    member_client.force_login(member)
    
    # CSRF checks disabled entirely, so the eventual 404 proved nothing about
    # CSRF enforcement — the same 404 would appear whether or not CSRF was
    # even wired up. Mirror t6's CSRF-enforcing client
    # (Client(enforce_csrf_checks=True)), but unlike t6 (which asserts the
    # *missing*-token 403) we must supply a VALID token so the request
    # actually clears CSRF and the resulting 404 is unambiguously the
    # ownership check, not a masked CSRF rejection. A valid token is primed
    # by GETting a page that renders {% csrf_token %} (base.html's sign-out
    # form, always rendered), which makes CsrfViewMiddleware set the
    # csrftoken cookie; the same value is sent both as the cookie (via the
    # client's persistent cookie jar) and as the csrfmiddlewaretoken POST
    # field, exactly like a real browser submission.
    priming = member_client.get(reverse("dashboard:overview"))
    assert priming.status_code == 200
    csrf_token = member_client.cookies["csrftoken"].value

    resp = member_client.post(
        reverse("dashboard:inbox-rename", kwargs={"public_id": ws_inbox.public_id}),
        {"label": "pwned", "csrfmiddlewaretoken": csrf_token},
    )
    
    # ownership lookup runs and refuses with EXACTLY 404, never 405.
    assert resp.status_code == 404
    ws_inbox.refresh_from_db()
    assert ws_inbox.label != "pwned"


@override_settings(ENABLE_MULTI_ACCOUNT=True, ENABLE_TEAMS=True)
def test_t16_invite_accept_refused_for_workspace_users_and_unusable_password():
    "Public API."
    from apps.accounts.team_services import InviteNotUsable
    from apps.accounts.team_services import NotEntitled as TeamNotEntitled

    owner = make_user("t16_owner")
    link = create_owned_account(owner, "WS")
    ws = link.account_user

    assert is_workspace_user(ws) is True
    assert ws.has_usable_password() is False

    with pytest.raises(TeamNotEntitled):
        create_invite(ws, "someone@example.test")

    other_owner = make_user("t16_other_owner")
    invite, raw_token = create_invite(other_owner, "invitee@example.test")
    with pytest.raises(InviteNotUsable):
        accept_invite(raw_token, ws)


# --- Cap / mint ------------------------------------------------------------------


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_t17_cap_at_limit_raises_not_entitled_and_concurrent_double_post_one_row(settings):
    "Public API."
    original = settings.BILLING_PLAN_LIMITS["business"]["owned_accounts"]
    settings.BILLING_PLAN_LIMITS["business"]["owned_accounts"] = 1
    try:
        owner = make_user("t17_owner")
        create_owned_account(owner, "First")
        with pytest.raises(NotEntitled):
            create_owned_account(owner, "Second")

        owner2 = make_user("t17_owner2")
        N = 5
        barrier = threading.Barrier(N)
        results = [None] * N
        exceptions = [None] * N

        def _create(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    create_owned_account(owner2, f"race-{index}")
                    results[index] = "ok"
                except NotEntitled:
                    results[index] = "blocked"
            except BaseException as exc:  # noqa: BLE001 — capture, never swallow silently
                exceptions[index] = exc
            finally:
                close_old_connections()

        threads = [threading.Thread(target=_create, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        for i, t in enumerate(threads):
            assert not t.is_alive(), f"thread {i} did not terminate within the join timeout"
        for i, exc in enumerate(exceptions):
            assert exc is None, f"thread {i} raised an uncaught exception: {exc!r}"

        assert results.count("ok") == 1
        assert results.count("blocked") == N - 1
        assert None not in results
        assert AccountOwnership.objects.filter(owner_user=owner2).count() == 1
    finally:
        settings.BILLING_PLAN_LIMITS["business"]["owned_accounts"] = original


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_t18_mint_provisions_user_account_plan_sets_verified_and_key_authenticates():
    owner = make_user("t18_owner")
    link = create_owned_account(owner, "WS")
    ws = link.account_user

    assert Account.objects.filter(user=ws, email_verified_at__isnull=False).exists()
    assert AccountPlan.objects.filter(user=ws, plan=AccountPlan.Plan.FREE).exists()
    assert ws.has_usable_password() is False

    _, token = create_api_key(ws, "ws-key")
    client = api_client_for(token)
    resp = client.get("/api/v1/inboxes")
    assert resp.status_code == 200


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_t19_demo_agent_create_and_switch_refused_tenant_ignores_session_key():
    "Public API."
    from apps.accounts.services import get_demo_user, register_agent

    demo_user = get_demo_user()
    with pytest.raises(NotEntitled):
        create_owned_account(demo_user, "nope")

    other_owner = make_user("t19_other_owner")
    link = create_owned_account(other_owner, "Some WS")

    client = login(demo_user)
    resp = client.post(reverse("dashboard:account-new"), {"name": "nope"})
    assert resp.status_code == 404
    resp = client.post(reverse("dashboard:account-switch"), {"account": link.account_user_id})
    assert resp.status_code == 404

    req = FakeRequest(demo_user, session={SESSION_KEY: link.account_user_id})
    assert tenant(req) == demo_user

    # Self-registered agent account: same refusals, same tenant() ignore.
    agent_user, _api_key, _token = register_agent("t19-agent")

    with pytest.raises(NotEntitled):
        create_owned_account(agent_user, "nope-agent")

    agent_client = login(agent_user)
    resp = agent_client.post(reverse("dashboard:account-new"), {"name": "nope-agent"})
    assert resp.status_code == 404
    resp = agent_client.post(
        reverse("dashboard:account-switch"), {"account": link.account_user_id}
    )
    assert resp.status_code == 404

    agent_req = FakeRequest(agent_user, session={SESSION_KEY: link.account_user_id})
    assert tenant(agent_req) == agent_user


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_t20_no_nesting_new_account_created_under_human_workspace_create_raises():
    owner = make_user("t20_owner")
    link = create_owned_account(owner, "WS")
    ws = link.account_user

    with pytest.raises(NotEntitled):
        create_owned_account(ws, "nested")

    client = login(owner)
    client.post(reverse("dashboard:account-switch"), {"account": ws.pk})
    resp = client.post(reverse("dashboard:account-new"), {"name": "Second WS"})
    assert resp.status_code == 200

    new_link = AccountOwnership.objects.get(owner_user=owner, name="Second WS")
    assert new_link.owner_user_id == owner.pk


# --- P2: cascade revoke scoped to team keys, personal key survives -------------


@override_settings(ENABLE_TEAMS=True)
def test_p2_cascade_revoke_scoped_to_team_keys_personal_key_survives():
    owner = make_user("p2_owner")
    member = make_user("p2_member", plan=AccountPlan.Plan.FREE)
    membership = add_member(owner, member)

    personal_key, personal_token = create_api_key(member, "personal-key")
    team_key, team_token = create_api_key(owner, "team-key", created_by=member)

    personal_client = api_client_for(personal_token)
    team_client = api_client_for(team_token)
    assert personal_client.get("/api/v1/inboxes").status_code == 200
    assert team_client.get("/api/v1/inboxes").status_code == 200

    revoke_membership(owner, membership)

    personal_key.refresh_from_db()
    team_key.refresh_from_db()
    assert personal_key.revoked_at is None
    assert team_key.revoked_at is not None

    assert personal_client.get("/api/v1/inboxes").status_code == 200
    assert team_client.get("/api/v1/inboxes").status_code == 401





@override_settings(ENABLE_MULTI_ACCOUNT=True)
@pytest.mark.parametrize(
    "status", [AccountPlan.Status.PAST_DUE, AccountPlan.Status.SUSPENDED]
)
def test_g6_non_active_owner_cannot_create_workspace(status):
    owner = make_user(f"g6_{status}")
    AccountPlan.objects.filter(user=owner).update(status=status)
    with pytest.raises(NotEntitled):
        create_owned_account(owner, "While unpaid")
    assert AccountOwnership.objects.filter(owner_user=owner).count() == 0


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_g6_past_due_dashboard_create_gets_the_plan_slot_message():
    owner = make_user("g6_past_due_ui")
    AccountPlan.objects.filter(user=owner).update(status=AccountPlan.Status.PAST_DUE)
    resp = login(owner).post(reverse("dashboard:account-new"), {"name": "X"})
    assert resp.status_code == 403
    assert "Your plan does not have an available account slot." in resp.content.decode()
    assert AccountOwnership.objects.filter(owner_user=owner).count() == 0


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_g6_active_owner_still_creates_workspace():
    owner = make_user("g6_active")
    assert create_owned_account(owner, "Fine").owner_user_id == owner.pk
