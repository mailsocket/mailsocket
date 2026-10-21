"Public API."
import pathlib
import re

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.admin import AccountAdmin
from apps.accounts.models import Account, AccountPlan, TeamInvite, TeamMembership
from apps.accounts.multi_account_services import create_owned_account
from apps.accounts.services import create_api_key, create_product_user, register_agent
from apps.accounts.team_services import NotEntitled, accept_invite, create_invite
from apps.accounts.tenancy import SESSION_KEY
from apps.inboxes.models import Inbox

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "correct horse battery staple 90210"


def make_user(name, *, plan=AccountPlan.Plan.BUSINESS):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password=PASSWORD
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


def suspend(user):
    Account.objects.filter(user=user).update(is_suspended=True)


def add_member(owner, member):
    return TeamMembership.objects.create(
        account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER
    )


@pytest.fixture
def business_five_seats(settings):
    # BILLING_PLAN_LIMITS is nested; the settings fixture does not restore
    # in-place mutation (same caveat as test_teams.py) — restore explicitly.
    original = settings.BILLING_PLAN_LIMITS["business"]["seats"]
    settings.BILLING_PLAN_LIMITS["business"]["seats"] = 5
    yield
    settings.BILLING_PLAN_LIMITS["business"]["seats"] = original


# --- 1. P2-2 seat cap at accept ---------------------------------------------


@override_settings(ENABLE_TEAMS=True)
def test_business_5_seats_all_4_invites_accept_then_5th_invite_refused(business_five_seats):
    owner = make_user("seat5_owner")
    tokens = [create_invite(owner, f"seat5_m{i}@example.test")[1] for i in range(4)]

    # 5th invite refused at CREATE (members + pending = 4, not < 4).
    with pytest.raises(NotEntitled):
        create_invite(owner, "seat5_overflow@example.test")

    for i, token in enumerate(tokens):
        member = make_user(f"seat5_member{i}", plan=AccountPlan.Plan.FREE)
        accept_invite(token, member)  # must NOT raise NotEntitled

    assert TeamMembership.objects.filter(account_id=owner.pk, revoked_at__isnull=True).count() == 4
    
    with pytest.raises(NotEntitled):
        create_invite(owner, "seat5_overflow2@example.test")


@override_settings(ENABLE_TEAMS=True)
def test_accept_via_dashboard_at_cap_edge_succeeds(business_five_seats):
    """Same bug through the real view: 4 pending invites, first accept -> 200."""
    owner = make_user("seatview_owner")
    tokens = [create_invite(owner, f"seatview_m{i}@example.test")[1] for i in range(4)]
    member = make_user("seatview_member", plan=AccountPlan.Plan.FREE)
    resp = login(member).post(
        reverse("dashboard:team-accept-confirm", kwargs={"token": tokens[0]})
    )
    assert resp.status_code == 200
    assert TeamMembership.objects.filter(user=member, account_id=owner.pk).exists()


@override_settings(ENABLE_TEAMS=True)
def test_accept_refused_when_members_already_full(business_five_seats):
    """Live members already fill seats-1: an accept (of an invite that was
    issued before the members arrived) is still refused — cap holds."""
    owner = make_user("seatfull_owner")
    _, token = create_invite(owner, "seatfull_late@example.test")
    for i in range(4):
        add_member(owner, make_user(f"seatfull_m{i}", plan=AccountPlan.Plan.FREE))

    late = make_user("seatfull_late", plan=AccountPlan.Plan.FREE)
    with pytest.raises(NotEntitled):
        accept_invite(token, late)
    assert not TeamMembership.objects.filter(user=late).exists()
    assert TeamInvite.objects.get(account_id=owner.pk).accepted_at is None


# --- 2a. dashboard_required: suspension follows the human --------------------


@override_settings(ENABLE_TEAMS=True)
def test_dashboard_refuses_suspended_team_member_session():
    owner = make_user("dash_team_owner")
    member = make_user("dash_team_member", plan=AccountPlan.Plan.FREE)
    add_member(owner, member)
    client = login(member)
    assert client.get(reverse("dashboard:inboxes")).status_code == 200  # control

    suspend(member)
    resp = client.get(reverse("dashboard:inboxes"))
    assert resp.status_code == 302
    assert "/login/" in resp["Location"]
    htmx = client.get(reverse("dashboard:inboxes"), HTTP_HX_REQUEST="true")
    assert htmx.status_code == 401 and htmx["HX-Redirect"] == "/login/"
    
    assert login(owner).get(reverse("dashboard:inboxes")).status_code == 200


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_dashboard_refuses_suspended_owner_switched_into_workspace():
    owner = make_user("dash_ws_owner")
    link = create_owned_account(owner, "WS")
    client = login(owner)
    session = client.session
    session[SESSION_KEY] = link.account_user_id
    session.save()
    assert client.get(reverse("dashboard:inboxes")).status_code == 200  # control

    suspend(owner)
    resp = client.get(reverse("dashboard:inboxes"))
    assert resp.status_code == 302
    assert "/login/" in resp["Location"]


# --- 2b. Bearer auth: suspension follows the human ---------------------------


@override_settings(ENABLE_TEAMS=True)
def test_bearer_rejects_member_minted_key_when_member_suspended():
    owner = make_user("api_team_owner")
    member = make_user("api_team_member", plan=AccountPlan.Plan.FREE)
    add_member(owner, member)
    _, member_token = create_api_key(owner, "member key", created_by=member)
    _, owner_token = create_api_key(owner, "owner key")
    assert api_client_for(member_token).get("/api/v1/inboxes").status_code == 200  # control

    suspend(member)
    resp = api_client_for(member_token).get("/api/v1/inboxes")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "account_forbidden"
    
    assert api_client_for(owner_token).get("/api/v1/inboxes").status_code == 200


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_bearer_rejects_workspace_key_when_owner_suspended():
    owner = make_user("api_ws_owner")
    link = create_owned_account(owner, "WS")
    _, ws_token = create_api_key(link.account_user, "ws key")
    assert api_client_for(ws_token).get("/api/v1/inboxes").status_code == 200  # control

    suspend(owner)
    resp = api_client_for(ws_token).get("/api/v1/inboxes")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "account_forbidden"


def test_bearer_suspension_checks_ride_the_key_lookup_join():
    """Hot path: the human-suspension checks use the existing select_related
    key-lookup JOIN — no standalone AccountOwnership/created_by query."""
    owner = make_user("api_hot_owner")
    _, token = create_api_key(owner, "k")
    client = api_client_for(token)
    client.get("/api/v1/inboxes")  # warm last_used_at
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as ctx:
        assert client.get("/api/v1/inboxes").status_code == 200
    ownership_sql = [q["sql"] for q in ctx.captured_queries if "accounts_accountownership" in q["sql"]]
    assert ownership_sql, "expected the ownership JOIN on the key lookup"
    assert all('FROM "accounts_apikey"' in sql for sql in ownership_sql)


# --- 2c. admin suspend action -------------------------------------------------


@override_settings(ENABLE_TEAMS=True, ENABLE_MULTI_ACCOUNT=True)
def test_admin_suspend_revokes_created_by_keys_and_suspends_owned_workspaces():
    team_owner = make_user("adm_team_owner")
    human = make_user("adm_human")
    add_member(team_owner, human)
    member_key, _ = create_api_key(team_owner, "human's team key", created_by=human)
    team_owner_key, _ = create_api_key(team_owner, "owner's own key")

    link = create_owned_account(human, "Human WS")
    ws = link.account_user
    ws_inbox = Inbox.objects.create(user=ws, routing_key="adm_ws_inbox")
    bystander = make_user("adm_bystander")
    bystander_inbox = Inbox.objects.create(user=bystander, routing_key="adm_bystander_inbox")

    AccountAdmin(Account, admin.site).suspend_and_disable_inboxes(
        None, Account.objects.filter(user=human)
    )

    assert Account.objects.get(user=human).is_suspended is True
    member_key.refresh_from_db()
    assert member_key.revoked_at is not None
    assert Account.objects.get(user=ws).is_suspended is True
    ws_inbox.refresh_from_db()
    assert ws_inbox.is_enabled is False and ws_inbox.deleted_at is not None

    
    assert Account.objects.get(user=team_owner).is_suspended is False
    team_owner_key.refresh_from_db()
    assert team_owner_key.revoked_at is None
    assert Account.objects.get(user=bystander).is_suspended is False
    bystander_inbox.refresh_from_db()
    assert bystander_inbox.is_enabled is True and bystander_inbox.deleted_at is None


# --- r2 / 1. admin suspend turns off catch-all before disabling inboxes ------


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_admin_suspend_owner_turns_off_workspace_catch_all_and_stops_routing():
    "Public API."
    from apps.domains.models import Domain
    from apps.ingest.services import IngestError, resolve_inbox
    from apps.ingest.tests.test_stage_c_catch_all import _run_recipient_map_sql

    owner = make_user("ca_ws_owner")
    ws = create_owned_account(owner, "CA WS").account_user
    domain = Domain.objects.create(user=ws, name="ca-ws-suspend.example.com")
    domain.status = Domain.Status.VERIFIED
    domain.save(update_fields=["status"])
    sentinel = Inbox.objects.create(user=ws, routing_key="catchall", domain=domain)
    domain.catch_all = True
    domain.catch_all_inbox = sentinel
    domain.save(update_fields=["catch_all", "catch_all_inbox"])
    # Bystander catch-all on another tenant stays on.
    bystander = make_user("ca_bystander")
    other = Domain.objects.create(user=bystander, name="ca-bystander.example.com")
    other.status = Domain.Status.VERIFIED
    other.save(update_fields=["status"])
    other_sentinel = Inbox.objects.create(user=bystander, routing_key="catchall", domain=other)
    other.catch_all = True
    other.catch_all_inbox = other_sentinel
    other.save(update_fields=["catch_all", "catch_all_inbox"])

    # Control: routed before suspension.
    assert _run_recipient_map_sql("anything", domain.name) == [("anything",)]
    assert resolve_inbox("anything", domain.name).pk == sentinel.pk

    AccountAdmin(Account, admin.site).suspend_and_disable_inboxes(
        None, Account.objects.filter(user=owner)
    )

    domain.refresh_from_db()
    assert domain.catch_all is False and domain.catch_all_inbox_id is None
    sentinel.refresh_from_db()
    assert sentinel.is_enabled is False and sentinel.deleted_at is not None
    assert Account.objects.get(user=ws).is_suspended is True
    # No longer routed: RCPT map refuses, ingest refuses.
    assert _run_recipient_map_sql("anything", domain.name) == []
    assert _run_recipient_map_sql("catchall", domain.name) == []
    with pytest.raises(IngestError):
        resolve_inbox("anything", domain.name)
    with pytest.raises(IngestError):
        resolve_inbox("catchall", domain.name)

    # Bystander untouched.
    other.refresh_from_db()
    assert other.catch_all is True and other.catch_all_inbox_id == other_sentinel.pk
    assert _run_recipient_map_sql("anything", other.name) == [("anything",)]


# --- 3. user delete revokes keys they minted ---------------------------------


def _strip_protected_rows(user):
    TeamMembership.objects.filter(user=user).delete()
    AccountPlan.objects.filter(user=user).delete()
    Account.objects.filter(user=user).delete()


@override_settings(ENABLE_TEAMS=True)
@pytest.mark.parametrize("via_queryset", [False, True], ids=["instance", "queryset"])
def test_deleting_member_user_revokes_their_minted_keys(via_queryset):
    tag = "qs" if via_queryset else "inst"
    owner = make_user(f"del_owner_{tag}")
    member = make_user(f"del_member_{tag}", plan=AccountPlan.Plan.FREE)
    add_member(owner, member)
    member_key, member_token = create_api_key(owner, "member key", created_by=member)
    owner_key, _ = create_api_key(owner, "owner key")

    _strip_protected_rows(member)
    if via_queryset:  # admin "delete selected" path
        get_user_model().objects.filter(pk=member.pk).delete()
    else:  # admin change-form delete path
        get_user_model().objects.get(pk=member.pk).delete()

    member_key.refresh_from_db()
    assert member_key.created_by_id is None  # SET_NULL still applied...
    assert member_key.revoked_at is not None  # ...but only on a dead key
    resp = api_client_for(member_token).get("/api/v1/inboxes")
    assert resp.status_code == 401
    owner_key.refresh_from_db()
    assert owner_key.revoked_at is None


# --- 4. password reset: never for workspace / agent / demo users -------------


def _reset(client, email):
    return client.post(reverse("accounts:password-reset"), {"email": email})


def _assert_discarded(outbox_before):
    from django.conf import settings

    assert len(mail.outbox) == outbox_before + 1
    last = mail.outbox[-1]
    assert last.to == [settings.AUTH_DISCARD_EMAIL]
    assert "password-reset/confirm" not in last.body and "discarded" in last.body


def test_reset_control_real_user_gets_link(client):
    user = create_product_user("reset_ctrl@example.com", PASSWORD)
    user.account.email_verified_at = timezone.now()
    user.account.save(update_fields=["email_verified_at", "updated_at"])
    _reset(client, "reset_ctrl@example.com")
    assert mail.outbox[-1].to == ["reset_ctrl@example.com"]


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_reset_refused_for_workspace_user_same_response(client):
    owner = make_user("reset_ws_owner")
    ws = create_owned_account(owner, "WS").account_user
    unknown = _reset(client, "nobody-here@example.com")
    before = len(mail.outbox)
    resp = _reset(client, ws.account.email_normalized)
    assert resp.status_code == unknown.status_code == 200
    # ux3 N4: only the echoed typed address differs.
    assert resp.content.replace(ws.account.email_normalized.encode(), b"<typed>") == (
        unknown.content.replace(b"nobody-here@example.com", b"<typed>")
    )
    _assert_discarded(before)


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_reset_refused_for_ownership_linked_user_even_off_reserved_domain(client):
    """The AccountOwnership(account_user=...) arm is load-bearing on its own:
    if WORKSPACE_ACCOUNT_DOMAIN is later changed, pre-existing workspace users
    no longer match the suffix check but must still never get a reset.
    (Nested override_settings, NOT the pytest `settings` fixture: mixing the
    fixture with the decorator leaks ENABLE_MULTI_ACCOUNT=True into later
    tests because the fixture restores a snapshot taken inside the decorator.)"""
    owner = make_user("reset_link_owner")
    ws = create_owned_account(owner, "WS").account_user
    with override_settings(WORKSPACE_ACCOUNT_DOMAIN="some-other-reserved.internal"):
        before = len(mail.outbox)
        _reset(client, ws.account.email_normalized)
        _assert_discarded(before)


def test_reset_refused_for_agent_user(client):
    agent, _, _ = register_agent("probe")
    before = len(mail.outbox)
    resp = _reset(client, agent.account.email_normalized)
    assert resp.status_code == 200
    _assert_discarded(before)


def test_workspace_domain_default_is_reserved_internal_tld():
    source = (
        pathlib.Path(__file__).resolve().parents[3] / "config" / "settings" / "base.py"
    ).read_text()
    match = re.search(
        r'WORKSPACE_ACCOUNT_DOMAIN\s*=\s*os\.environ\.get\(\s*"WORKSPACE_ACCOUNT_DOMAIN"\s*,\s*"([^"]+)"',
        source,
    )
    assert match is not None
    default = match.group(1)
    assert default.endswith(".internal")
    assert "mailsocket.dev" not in default
    from apps.accounts.services import AGENT_ACCOUNT_DOMAIN

    assert default != AGENT_ACCOUNT_DOMAIN


# --- r2 / 2. password_reset_confirm refuses workspace / agent users ----------


def _confirm_url(user, token=None):
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    return reverse(
        "accounts:password-reset-confirm",
        kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
            "token": token or default_token_generator.make_token(user),
        },
    )


NEW_PW = {"new_password1": "brand new pass phrase 4242", "new_password2": "brand new pass phrase 4242"}


def test_reset_confirm_control_real_user_can_set_password(client):
    user = make_user("confirm_ctrl")
    resp = client.post(_confirm_url(user), NEW_PW)
    assert resp.status_code == 200
    user.refresh_from_db()
    assert user.check_password(NEW_PW["new_password1"])


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_reset_confirm_refuses_workspace_user_with_valid_token(client):
    owner = make_user("confirm_ws_owner")
    ws = create_owned_account(owner, "WS").account_user
    invalid = client.post(_confirm_url(ws, token="bogus-token"), NEW_PW)
    for method in ("get", "post"):
        resp = getattr(client, method)(_confirm_url(ws), NEW_PW if method == "post" else None)
        assert resp.status_code == invalid.status_code == 400
        assert resp.content == invalid.content
    ws.refresh_from_db()
    assert not ws.has_usable_password()


def test_reset_confirm_refuses_agent_user_with_valid_token(client):
    agent, _, _ = register_agent("probe")
    invalid = client.post(_confirm_url(agent, token="bogus-token"), NEW_PW)
    resp = client.post(_confirm_url(agent), NEW_PW)
    assert resp.status_code == 400 and resp.content == invalid.content
    agent.refresh_from_db()
    assert not agent.check_password(NEW_PW["new_password1"])


# --- r2 / 3. suspended human cannot accept a team invite ---------------------


@override_settings(ENABLE_TEAMS=True)
def test_suspended_human_cannot_accept_invite():
    from apps.accounts.team_services import InviteNotUsable

    owner = make_user("acc_susp_owner")
    _, token = create_invite(owner, "acc_susp_member@example.test")
    member = make_user("acc_susp_member", plan=AccountPlan.Plan.FREE)
    suspend(member)

    with pytest.raises(InviteNotUsable):
        accept_invite(token, member)
    # Through the real view: the same uniform 410 as an expired/unknown link.
    resp = login(member).post(reverse("dashboard:team-accept-confirm", kwargs={"token": token}))
    unknown = login(member).post(
        reverse("dashboard:team-accept-confirm", kwargs={"token": "no-such-token"})
    )
    assert resp.status_code == unknown.status_code == 410
    assert not TeamMembership.objects.filter(user=member).exists()
    assert TeamInvite.objects.get(account_id=owner.pk).accepted_at is None

    # Control: once unsuspended, the same invite is accepted.
    Account.objects.filter(user=member).update(is_suspended=False)
    accept_invite(token, member)
    assert TeamMembership.objects.filter(user=member, account_id=owner.pk).exists()
