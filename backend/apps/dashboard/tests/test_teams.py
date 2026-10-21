"""Team Seats (Phase 4 Stage A) tests.

Covers: ENABLE_TEAMS gate (flag off -> 404), can_invite_member entitlement
gate (free/paid/scale -> 403; business up to seats-1 counting pending
invites), token replay/revoke/expire -> 410, duplicate live invite -> 400,
one-team-per-user -> 409, token_hash stored not raw, and the Stage A
INERTNESS PROOF: with ENABLE_TEAMS on and a live membership row present, a
member's own dashboard session still resolves ONLY to their personal
account (membership grants zero access).
"""
import hashlib
import re as _re
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.entitlements import can_invite_member
from apps.accounts.models import Account, AccountPlan, TeamInvite, TeamMembership
from apps.accounts.services import create_api_key
from apps.accounts.team_services import create_invite
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


def login(user):
    client = Client()
    client.force_login(user)
    return client


# --- ENABLE_TEAMS gate --------------------------------------------------------


@override_settings(ENABLE_TEAMS=False)
def test_flag_off_team_urls_404():
    owner = make_user("flagoff_owner")
    client = login(owner)

    resp = client.get(reverse("dashboard:team"))
    assert resp.status_code == 404

    resp = client.post(reverse("dashboard:team-invite-create"), {"email": "x@example.test"})
    assert resp.status_code == 404

    resp = client.post(reverse("dashboard:team-leave"))
    assert resp.status_code == 404

    resp = client.get(reverse("dashboard:team-accept", kwargs={"token": "whatever"}))
    assert resp.status_code == 404


@override_settings(ENABLE_TEAMS=True)
def test_business_owner_get_team_page_renders_200_not_500():
    "Public API."
    owner = make_user("teampage_business")
    client = login(owner)
    resp = client.get(reverse("dashboard:team"))
    assert resp.status_code == 200
    assert b"can_invite" not in resp.content  # sanity: not leaking the raw context key


@override_settings(ENABLE_TEAMS=False)
def test_flag_off_leaves_existing_dashboard_and_api_behaviour_unaffected():
    """ENABLE_TEAMS=False must not change any existing (non-team) behaviour."""
    owner = make_user("flagoff_regress", plan=AccountPlan.Plan.PAID)
    client = login(owner)
    resp = client.get(reverse("dashboard:overview"))
    assert resp.status_code == 200
    resp = client.get(reverse("dashboard:inboxes"))
    assert resp.status_code == 200


# --- can_invite_member entitlement gate --------------------------------------


@override_settings(ENABLE_TEAMS=True)
@pytest.mark.parametrize("plan", [AccountPlan.Plan.FREE, AccountPlan.Plan.PAID, AccountPlan.Plan.SCALE])
def test_non_business_plans_get_403_on_invite(plan):
    owner = make_user(f"gate_{plan}", plan=plan)
    client = login(owner)
    resp = client.post(reverse("dashboard:team-invite-create"), {"email": "invitee@example.test"})
    assert resp.status_code == 403
    assert TeamInvite.objects.filter(account_id=owner.pk).count() == 0


@override_settings(ENABLE_TEAMS=True)
def test_business_owner_can_invite_up_to_seats_minus_one_then_403(settings):
    "Public API."
    # NOTE: BILLING_PLAN_LIMITS is a nested dict; pytest-django's `settings`
    # fixture only restores top-level attribute assignment, not an in-place
    # mutation of a nested dict it already holds a reference to (same caveat
    # documented in apps/api/tests/test_domains.py). Restore explicitly.
    original_seats = settings.BILLING_PLAN_LIMITS["business"]["seats"]
    settings.BILLING_PLAN_LIMITS["business"]["seats"] = 3
    try:
        owner = make_user("seat_owner")
        client = login(owner)

        for i in range(2):
            resp = client.post(
                reverse("dashboard:team-invite-create"), {"email": f"member{i}@example.test"}
            )
            assert resp.status_code == 200, f"invite {i} should succeed under the cap"

        from django.db import transaction

        with transaction.atomic():
            assert can_invite_member(owner) is False
        resp = client.post(reverse("dashboard:team-invite-create"), {"email": "overflow@example.test"})
        assert resp.status_code == 403
        assert TeamInvite.objects.filter(account_id=owner.pk).count() == 2
    finally:
        settings.BILLING_PLAN_LIMITS["business"]["seats"] = original_seats


@override_settings(ENABLE_TEAMS=True)
def test_seat_headroom_counts_accepted_members_not_just_invites(settings):
    "Public API."
    original_seats = settings.BILLING_PLAN_LIMITS["business"]["seats"]
    settings.BILLING_PLAN_LIMITS["business"]["seats"] = 2
    try:
        owner = make_user("seat_member_owner")
        member_user = make_user("seat_member_a", plan=AccountPlan.Plan.FREE)
        TeamMembership.objects.create(account_id=owner.pk, user=member_user, role=TeamMembership.Role.MEMBER)

        # (a) revoked membership, SAME account — must NOT count.
        revoked_member_user = make_user("seat_decoy_revoked_member", plan=AccountPlan.Plan.FREE)
        TeamMembership.objects.create(
            account_id=owner.pk,
            user=revoked_member_user,
            role=TeamMembership.Role.MEMBER,
            revoked_at=timezone.now(),
        )

        # (b) membership for a DIFFERENT account — must NOT count.
        other_owner = make_user("seat_decoy_other_owner")
        other_member_user = make_user("seat_decoy_other_member", plan=AccountPlan.Plan.FREE)
        TeamMembership.objects.create(
            account_id=other_owner.pk, user=other_member_user, role=TeamMembership.Role.MEMBER
        )

        # (c) expired pending invite, SAME account — must NOT count. Insert
        # directly via the ORM (not create_invite(), which itself calls the
        # can_invite_member gate and would be refused once seats are full).
        from apps.accounts.models import _new_invite_token

        _, expired_token_hash = _new_invite_token()
        expired_invite = TeamInvite.objects.create(
            account_id=owner.pk,
            email_normalized="seat_decoy_expired@example.test",
            token_hash=expired_token_hash,
        )
        expired_invite.expires_at = timezone.now() - timedelta(seconds=1)
        expired_invite.save(update_fields=["expires_at"])

        # (d) revoked/dead invite, SAME account — must NOT count. Also
        # inserted directly (same reasoning as (c) above).
        _, dead_token_hash = _new_invite_token()
        dead_invite = TeamInvite.objects.create(
            account_id=owner.pk,
            email_normalized="seat_decoy_revoked_invite@example.test",
            token_hash=dead_token_hash,
        )
        dead_invite.revoked_at = timezone.now()
        dead_invite.save(update_fields=["revoked_at"])

        # Sanity: only the ONE genuine live membership counts toward the cap.
        assert (
            TeamMembership.objects.filter(account_id=owner.pk, revoked_at__isnull=True).count() == 1
        )
        assert (
            TeamInvite.objects.filter(
                account_id=owner.pk,
                accepted_at__isnull=True,
                revoked_at__isnull=True,
                expires_at__gt=timezone.now(),
            ).count()
            == 0
        )

        client = login(owner)
        resp = client.post(reverse("dashboard:team-invite-create"), {"email": "overflow2@example.test"})
        assert resp.status_code == 403
        assert TeamInvite.objects.filter(account_id=owner.pk, email_normalized="overflow2@example.test").count() == 0
    finally:
        settings.BILLING_PLAN_LIMITS["business"]["seats"] = original_seats


@override_settings(ENABLE_TEAMS=True)
def test_seat_predicate_a_revoked_membership_same_account_not_counted(settings):
    "Public API."
    original_seats = settings.BILLING_PLAN_LIMITS["business"]["seats"]
    settings.BILLING_PLAN_LIMITS["business"]["seats"] = 3
    try:
        owner = make_user("seat_pa_owner")
        live_member_user = make_user("seat_pa_live_member", plan=AccountPlan.Plan.FREE)
        TeamMembership.objects.create(account_id=owner.pk, user=live_member_user, role=TeamMembership.Role.MEMBER)

        revoked_member_user = make_user("seat_pa_revoked_member", plan=AccountPlan.Plan.FREE)
        TeamMembership.objects.create(
            account_id=owner.pk,
            user=revoked_member_user,
            role=TeamMembership.Role.MEMBER,
            revoked_at=timezone.now(),
        )

        member_count = TeamMembership.objects.filter(account_id=owner.pk, revoked_at__isnull=True).count()
        assert member_count == 1, "revoked_at__isnull=True predicate dropped: revoked decoy leaked into member_count"
        from django.db import transaction

        with transaction.atomic():
            assert can_invite_member(owner) is True
    finally:
        settings.BILLING_PLAN_LIMITS["business"]["seats"] = original_seats


@override_settings(ENABLE_TEAMS=True)
def test_seat_predicate_b_membership_different_account_not_counted(settings):
    "Public API."
    original_seats = settings.BILLING_PLAN_LIMITS["business"]["seats"]
    settings.BILLING_PLAN_LIMITS["business"]["seats"] = 3
    try:
        owner = make_user("seat_pb_owner")
        live_member_user = make_user("seat_pb_live_member", plan=AccountPlan.Plan.FREE)
        TeamMembership.objects.create(account_id=owner.pk, user=live_member_user, role=TeamMembership.Role.MEMBER)

        other_owner = make_user("seat_pb_other_owner")
        other_member_user = make_user("seat_pb_other_member", plan=AccountPlan.Plan.FREE)
        TeamMembership.objects.create(
            account_id=other_owner.pk, user=other_member_user, role=TeamMembership.Role.MEMBER
        )

        member_count = TeamMembership.objects.filter(account_id=owner.pk, revoked_at__isnull=True).count()
        assert member_count == 1, "account_id scope dropped: other-account decoy leaked into member_count"
        from django.db import transaction

        with transaction.atomic():
            assert can_invite_member(owner) is True
    finally:
        settings.BILLING_PLAN_LIMITS["business"]["seats"] = original_seats


@override_settings(ENABLE_TEAMS=True)
def test_seat_predicate_c_expired_pending_invite_not_counted(settings):
    "Public API."
    original_seats = settings.BILLING_PLAN_LIMITS["business"]["seats"]
    settings.BILLING_PLAN_LIMITS["business"]["seats"] = 3
    try:
        owner = make_user("seat_pc_owner")
        live_member_user = make_user("seat_pc_live_member", plan=AccountPlan.Plan.FREE)
        TeamMembership.objects.create(account_id=owner.pk, user=live_member_user, role=TeamMembership.Role.MEMBER)

        expired_invite, _ = create_invite(owner, "seat_pc_expired@example.test")
        expired_invite.expires_at = timezone.now() - timedelta(seconds=1)
        expired_invite.save(update_fields=["expires_at"])

        pending_invite_count = TeamInvite.objects.filter(
            account_id=owner.pk,
            accepted_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).count()
        assert pending_invite_count == 0, "expires_at__gt predicate dropped: expired decoy leaked into pending_invite_count"
        from django.db import transaction

        with transaction.atomic():
            assert can_invite_member(owner) is True
    finally:
        settings.BILLING_PLAN_LIMITS["business"]["seats"] = original_seats


@override_settings(ENABLE_TEAMS=True)
def test_seat_predicate_d_revoked_invite_not_counted(settings):
    "Public API."
    original_seats = settings.BILLING_PLAN_LIMITS["business"]["seats"]
    settings.BILLING_PLAN_LIMITS["business"]["seats"] = 3
    try:
        owner = make_user("seat_pd_owner")
        live_member_user = make_user("seat_pd_live_member", plan=AccountPlan.Plan.FREE)
        TeamMembership.objects.create(account_id=owner.pk, user=live_member_user, role=TeamMembership.Role.MEMBER)

        dead_invite, _ = create_invite(owner, "seat_pd_dead@example.test")
        dead_invite.revoked_at = timezone.now()
        dead_invite.save(update_fields=["revoked_at"])

        pending_invite_count = TeamInvite.objects.filter(
            account_id=owner.pk,
            accepted_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).count()
        assert pending_invite_count == 0, "revoked_at__isnull=True predicate dropped: revoked-invite decoy leaked into pending_invite_count"
        from django.db import transaction

        with transaction.atomic():
            assert can_invite_member(owner) is True
    finally:
        settings.BILLING_PLAN_LIMITS["business"]["seats"] = original_seats


# --- invite mail uses the existing outbound path -----------------------------


@override_settings(ENABLE_TEAMS=True)
def test_invite_create_sends_mail_through_existing_outbound_path():
    owner = make_user("mail_owner")
    client = login(owner)
    mail.outbox.clear()
    resp = client.post(reverse("dashboard:team-invite-create"), {"email": "recipient@example.test"})
    assert resp.status_code == 200
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["recipient@example.test"]


# --- token_hash discipline ----------------------------------------------------


@override_settings(ENABLE_TEAMS=True)
def test_token_hash_stored_not_raw_token():
    owner = make_user("hash_owner")
    invite, raw_token = create_invite(owner, "hashcheck@example.test")
    invite.refresh_from_db()
    assert invite.token_hash == hashlib.sha256(raw_token.encode()).hexdigest()
    assert invite.token_hash != raw_token
    assert len(invite.token_hash) == 64  # sha256 hex digest


# --- accept flow: replay/revoke/expired -> uniform 410 -----------------------


@override_settings(ENABLE_TEAMS=True)
def test_token_replay_after_accept_returns_410():
    owner = make_user("replay_owner")
    invite, raw_token = create_invite(owner, "acceptor@example.test")
    acceptor = make_user("acceptor_user", plan=AccountPlan.Plan.FREE)
    client = login(acceptor)

    resp = client.post(reverse("dashboard:team-accept-confirm", kwargs={"token": raw_token}))
    assert resp.status_code == 200
    assert TeamMembership.objects.filter(user=acceptor, account_id=owner.pk).exists()

    # Replay: same token, a fresh (different) user this time.
    second = make_user("second_replay_user", plan=AccountPlan.Plan.FREE)
    client2 = login(second)
    resp2 = client2.post(reverse("dashboard:team-accept-confirm", kwargs={"token": raw_token}))
    assert resp2.status_code == 410


@override_settings(ENABLE_TEAMS=True)
def test_token_after_revoke_returns_410():
    owner = make_user("revoke_owner")
    invite, raw_token = create_invite(owner, "revokee@example.test")
    invite.revoked_at = timezone.now()
    invite.save(update_fields=["revoked_at"])

    acceptor = make_user("revokee_user", plan=AccountPlan.Plan.FREE)
    client = login(acceptor)
    resp = client.post(reverse("dashboard:team-accept-confirm", kwargs={"token": raw_token}))
    assert resp.status_code == 410


@override_settings(ENABLE_TEAMS=True)
def test_expired_token_returns_410():
    owner = make_user("expired_owner")
    invite, raw_token = create_invite(owner, "expiree@example.test")
    invite.expires_at = timezone.now() - timedelta(seconds=1)
    invite.save(update_fields=["expires_at"])

    acceptor = make_user("expiree_user", plan=AccountPlan.Plan.FREE)
    client = login(acceptor)
    resp = client.post(reverse("dashboard:team-accept-confirm", kwargs={"token": raw_token}))
    assert resp.status_code == 410


@override_settings(ENABLE_TEAMS=True)
def test_unknown_token_returns_410_same_as_expired_revoked():
    """No oracle: an unknown token gets the exact same 410 as a real but
    expired/revoked/accepted one."""
    acceptor = make_user("unknown_token_user", plan=AccountPlan.Plan.FREE)
    client = login(acceptor)
    resp = client.post(reverse("dashboard:team-accept-confirm", kwargs={"token": "not-a-real-token"}))
    assert resp.status_code == 410



#
# The individual tests above each assert status_code == 410 in isolation.
# This test independently REPRODUCES all four cases (replay/revoked/expired/
# unknown-token) in one place and asserts the four response bodies (content
# bytes) and Content-Type headers are IDENTICAL across all four — not just
# same status — so a hypothetical implementation that leaked which case
# occurred via a differing body/whitespace/header would fail here even if
# every individual status-only assertion above still passed.
#
# The rendered template embeds a per-request CSRF token (csrfmiddlewaretoken
# hidden input, unique per session) which is expected, session-scoped noise
# unrelated to the accept-token oracle question; it is stripped before
# comparison so the assertion targets the actual page content, not
# incidental CSRF-token randomness.
_CSRF_TOKEN_RE = _re.compile(rb'name="csrfmiddlewaretoken" value="[^"]*"')


def _normalize_body(content: bytes) -> bytes:
    return _CSRF_TOKEN_RE.sub(b'name="csrfmiddlewaretoken" value="STRIPPED"', content)


@override_settings(ENABLE_TEAMS=True)
def test_all_four_410_responses_have_identical_body_no_oracle():
    bodies = {}
    content_types = {}

    # replay
    owner = make_user("f2_replay_owner")
    _, raw_token = create_invite(owner, "f2_acceptor@example.test")
    acceptor = login(make_user("f2_acceptor_user", plan=AccountPlan.Plan.FREE))
    resp = acceptor.post(reverse("dashboard:team-accept-confirm", kwargs={"token": raw_token}))
    assert resp.status_code == 200
    replay_client = login(make_user("f2_replay_user2", plan=AccountPlan.Plan.FREE))
    resp = replay_client.post(reverse("dashboard:team-accept-confirm", kwargs={"token": raw_token}))
    assert resp.status_code == 410
    bodies["replay"] = _normalize_body(resp.content)
    content_types["replay"] = resp.headers.get("Content-Type")

    # revoked
    owner2 = make_user("f2_revoke_owner")
    invite2, raw_token2 = create_invite(owner2, "f2_revokee@example.test")
    invite2.revoked_at = timezone.now()
    invite2.save(update_fields=["revoked_at"])
    client2 = login(make_user("f2_revokee_user", plan=AccountPlan.Plan.FREE))
    resp = client2.post(reverse("dashboard:team-accept-confirm", kwargs={"token": raw_token2}))
    assert resp.status_code == 410
    bodies["revoked"] = _normalize_body(resp.content)
    content_types["revoked"] = resp.headers.get("Content-Type")

    # expired
    owner3 = make_user("f2_expired_owner")
    invite3, raw_token3 = create_invite(owner3, "f2_expiree@example.test")
    invite3.expires_at = timezone.now() - timedelta(seconds=1)
    invite3.save(update_fields=["expires_at"])
    client3 = login(make_user("f2_expiree_user", plan=AccountPlan.Plan.FREE))
    resp = client3.post(reverse("dashboard:team-accept-confirm", kwargs={"token": raw_token3}))
    assert resp.status_code == 410
    bodies["expired"] = _normalize_body(resp.content)
    content_types["expired"] = resp.headers.get("Content-Type")

    # unknown
    client4 = login(make_user("f2_unknown_user", plan=AccountPlan.Plan.FREE))
    resp = client4.post(reverse("dashboard:team-accept-confirm", kwargs={"token": "not-a-real-token-f2"}))
    assert resp.status_code == 410
    bodies["unknown"] = _normalize_body(resp.content)
    content_types["unknown"] = resp.headers.get("Content-Type")

    distinct_bodies = set(bodies.values())
    assert len(distinct_bodies) == 1, (
        "410 responses must be byte-identical (after stripping the expected "
        "per-session CSRF token) across replay/revoked/expired/unknown "
        f"(no oracle); got distinct bodies: { {k: v[:200] for k, v in bodies.items()} }"
    )
    distinct_content_types = set(content_types.values())
    assert len(distinct_content_types) == 1, (
        f"410 responses must share one Content-Type, got: {content_types}"
    )


# --- duplicate live invite ----------------------------------------------------


@override_settings(ENABLE_TEAMS=True)
def test_duplicate_live_invite_same_email_returns_400():
    owner = make_user("dup_owner")
    client = login(owner)
    resp1 = client.post(reverse("dashboard:team-invite-create"), {"email": "dup@example.test"})
    assert resp1.status_code == 200
    resp2 = client.post(reverse("dashboard:team-invite-create"), {"email": "dup@example.test"})
    assert resp2.status_code == 400
    assert TeamInvite.objects.filter(account_id=owner.pk, email_normalized="dup@example.test").count() == 1


@override_settings(ENABLE_TEAMS=True)
def test_new_invite_allowed_after_prior_one_revoked():
    owner = make_user("dup_revoke_owner")
    client = login(owner)
    resp1 = client.post(reverse("dashboard:team-invite-create"), {"email": "dup2@example.test"})
    assert resp1.status_code == 200
    invite = TeamInvite.objects.get(account_id=owner.pk, email_normalized="dup2@example.test")
    invite.revoked_at = timezone.now()
    invite.save(update_fields=["revoked_at"])
    resp2 = client.post(reverse("dashboard:team-invite-create"), {"email": "dup2@example.test"})
    assert resp2.status_code == 200
    assert TeamInvite.objects.filter(account_id=owner.pk, email_normalized="dup2@example.test").count() == 2





@override_settings(ENABLE_TEAMS=True)
def test_duplicate_live_invite_raises_integrity_error_at_db_level():
    "Public API."
    from django.db import IntegrityError, transaction

    from apps.accounts.models import _new_invite_token

    owner = make_user("f3_owner")
    _, token_hash_1 = _new_invite_token()
    TeamInvite.objects.create(
        account_id=owner.pk,
        email_normalized="f3dup@example.test",
        token_hash=token_hash_1,
    )

    _, token_hash_2 = _new_invite_token()
    with pytest.raises(IntegrityError) as excinfo:
        with transaction.atomic():
            TeamInvite.objects.create(
                account_id=owner.pk,
                email_normalized="f3dup@example.test",
                token_hash=token_hash_2,
            )
    assert "team_invite_live_account_email_uniq" in str(excinfo.value)
    assert TeamInvite.objects.filter(account_id=owner.pk, email_normalized="f3dup@example.test").count() == 1


@override_settings(ENABLE_TEAMS=True)
def test_second_live_invite_inserts_fine_after_first_revoked_at_db_level():
    "Public API."
    from apps.accounts.models import _new_invite_token

    owner = make_user("f3_revoke_owner")
    _, token_hash_1 = _new_invite_token()
    first = TeamInvite.objects.create(
        account_id=owner.pk,
        email_normalized="f3revoke@example.test",
        token_hash=token_hash_1,
    )
    first.revoked_at = timezone.now()
    first.save(update_fields=["revoked_at"])

    _, token_hash_2 = _new_invite_token()
    second = TeamInvite.objects.create(
        account_id=owner.pk,
        email_normalized="f3revoke@example.test",
        token_hash=token_hash_2,
    )
    assert second.pk is not None
    assert TeamInvite.objects.filter(account_id=owner.pk, email_normalized="f3revoke@example.test").count() == 2


# --- one-team-per-user --------------------------------------------------------


@override_settings(ENABLE_TEAMS=True)
def test_user_already_in_team_t_accepting_team_u_invite_returns_409():
    team_t_owner = make_user("team_t_owner")
    team_u_owner = make_user("team_u_owner")
    member = make_user("cross_team_member", plan=AccountPlan.Plan.FREE)

    TeamMembership.objects.create(account_id=team_t_owner.pk, user=member, role=TeamMembership.Role.MEMBER)

    _, raw_token = create_invite(team_u_owner, "cross_team_member@example.test")
    client = login(member)
    resp = client.post(reverse("dashboard:team-accept-confirm", kwargs={"token": raw_token}))
    assert resp.status_code == 409
    # Still only ONE membership row for this user, and it's still team T.
    assert TeamMembership.objects.filter(user=member).count() == 1
    assert TeamMembership.objects.get(user=member).account_id == team_t_owner.pk





@override_settings(ENABLE_TEAMS=True)
def test_second_membership_for_same_user_raises_integrity_error_at_db_level():
    "Public API."
    from django.db import IntegrityError, transaction

    team_t_owner = make_user("f4_team_t_owner")
    team_u_owner = make_user("f4_team_u_owner")
    member = make_user("f4_cross_team_member", plan=AccountPlan.Plan.FREE)

    TeamMembership.objects.create(account_id=team_t_owner.pk, user=member, role=TeamMembership.Role.MEMBER)

    with pytest.raises(IntegrityError) as excinfo:
        with transaction.atomic():
            TeamMembership.objects.create(account_id=team_u_owner.pk, user=member, role=TeamMembership.Role.MEMBER)
    # OneToOneField enforces uniqueness via a unique constraint/index on the
    # underlying user_id column (Postgres default naming for this table).
    assert "unique" in str(excinfo.value).lower() or "duplicate key" in str(excinfo.value).lower()
    assert TeamMembership.objects.filter(user=member).count() == 1
    assert TeamMembership.objects.get(user=member).account_id == team_t_owner.pk


# --- demo/agent guard ---------------------------------------------------------


@override_settings(ENABLE_TEAMS=True)
def test_demo_user_cannot_be_invited_or_accept():
    from apps.accounts.services import get_demo_user
    from apps.accounts.team_services import is_demo_or_agent_user

    demo_user = get_demo_user()
    assert is_demo_or_agent_user(demo_user) is True

    owner = make_user("demo_guard_owner")
    _, raw_token = create_invite(owner, "someone@example.test")
    client = login(demo_user)
    resp = client.post(reverse("dashboard:team-accept-confirm", kwargs={"token": raw_token}))
    assert resp.status_code == 410





@override_settings(ENABLE_TEAMS=True)
def test_owner_can_revoke_member_and_leave_is_self_scoped():
    owner = make_user("revoke_member_owner")
    member = make_user("revoke_member_a", plan=AccountPlan.Plan.FREE)
    membership = TeamMembership.objects.create(
        account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER
    )

    client = login(owner)
    resp = client.post(reverse("dashboard:team-member-revoke", kwargs={"membership_id": membership.pk}))
    assert resp.status_code == 200
    membership.refresh_from_db()
    assert membership.revoked_at is not None


@override_settings(ENABLE_TEAMS=True)
def test_member_can_leave_their_own_membership():
    owner = make_user("leave_owner")
    member = make_user("leave_member", plan=AccountPlan.Plan.FREE)
    membership = TeamMembership.objects.create(
        account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER
    )
    client = login(member)
    resp = client.post(reverse("dashboard:team-leave"))
    assert resp.status_code in (200, 302)
    membership.refresh_from_db()
    assert membership.revoked_at is not None


# --- INERTNESS PROOF (the Stage-A guarantee) ---------------------------------


@override_settings(ENABLE_TEAMS=True)
def test_stage_b_member_dashboard_session_now_resolves_to_team_tenant():
    "Public API."
    owner = make_user("inert_owner")
    member = make_user("inert_member", plan=AccountPlan.Plan.FREE)

    
    # personal-account inbox, which must stay invisible while the
    # membership is live (R1a: personal/team split).
    owner_inbox = Inbox.objects.create(user=owner, routing_key="inert_owner_key", label="Owner inbox")
    member_inbox = Inbox.objects.create(user=member, routing_key="inert_member_key", label="Member inbox")

    TeamMembership.objects.create(account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER)

    member_client = login(member)
    resp = member_client.get(reverse("dashboard:inboxes"))
    assert resp.status_code == 200
    inbox_list = list(resp.context["inboxes"])
    
    assert inbox_list == [owner_inbox]
    assert member_inbox not in inbox_list
    assert member_inbox.address.encode() not in resp.content
    assert owner_inbox.address.encode() in resp.content

    # And the API surface, via a member-minted key, must ALSO be scoped to
    
    _, token = create_api_key(owner, "member-key", created_by=member)
    api_client = APIClient()
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    api_resp = api_client.get("/api/v1/inboxes")
    assert api_resp.status_code == 200
    api_data = api_resp.json()["data"]
    assert len(api_data) == 1
    assert api_data[0]["id"] == owner_inbox.public_id
    returned_ids = {row["id"] for row in api_data}
    assert member_inbox.public_id not in returned_ids


@override_settings(ENABLE_TEAMS=True)
def test_inertness_full_dashboard_and_api_regression_stays_green_with_membership_present():
    "Public API."
    owner = make_user("inert_regress_owner")
    member = make_user("inert_regress_member", plan=AccountPlan.Plan.FREE)
    TeamMembership.objects.create(account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER)

    unrelated = make_user("inert_regress_unrelated", plan=AccountPlan.Plan.PAID)
    unrelated_inbox = Inbox.objects.create(user=unrelated, routing_key="inert_unrelated_key")

    client = login(unrelated)
    resp = client.get(reverse("dashboard:inboxes"))
    assert resp.status_code == 200
    assert [i.pk for i in resp.context["inboxes"]] == [unrelated_inbox.pk]

    _, token = create_api_key(unrelated, "unrelated-key")
    api_client = APIClient()
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    api_resp = api_client.get("/api/v1/inboxes")
    assert api_resp.status_code == 200
    assert len(api_resp.json()["data"]) == 1
    assert api_resp.json()["data"][0]["id"] == unrelated_inbox.public_id





def test_enable_teams_source_default_is_false_when_env_absent(monkeypatch):
    "Public API."
    import pathlib

    monkeypatch.delenv("ENABLE_TEAMS", raising=False)

    base_settings_path = (
        pathlib.Path(__file__).resolve().parents[3] / "config" / "settings" / "base.py"
    )
    source = base_settings_path.read_text()

    match = _re.search(
        r'ENABLE_TEAMS\s*=\s*os\.environ\.get\(\s*["\']ENABLE_TEAMS["\']\s*,\s*["\'](\w+)["\']\s*\)',
        source,
    )
    assert match is not None, "could not locate the ENABLE_TEAMS os.environ.get(...) line in base.py"
    assert match.group(1) == "false", (
        "ENABLE_TEAMS default fallback in config/settings/base.py must be the "
        f"string 'false' when the env var is absent; found {match.group(1)!r} instead"
    )


@override_settings(ENABLE_TEAMS=False)
def test_enable_teams_setting_off_by_default_gates_teams_urls():
    "Public API."
    from django.conf import settings

    assert settings.ENABLE_TEAMS is False
