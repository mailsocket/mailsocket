"Public API."
import hashlib
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.core import mail as django_mail
from django.db import IntegrityError, transaction
from django.utils import timezone

from .entitlements import can_accept_member, can_invite_member
from .models import Account, TeamInvite, TeamMembership
from .services import normalize_email

logger = logging.getLogger(__name__)


class TeamInviteError(Exception):
    """Base class for invite-flow failures the view layer maps to HTTP codes."""


class NotEntitled(TeamInviteError):
    "Public API."


class DuplicateInvite(TeamInviteError):
    """A live invite already exists for this (account, email) pair (400)."""


class AlreadyInTeam(TeamInviteError):
    """The accepting user already belongs to a (possibly different) team (409)."""


class InviteNotUsable(TeamInviteError):
    """Token unknown/expired/revoked/already-accepted — uniform 410, no oracle."""


# Reuses the SAME outbound path as welcome/verification mail
# (apps.accounts.views._send_auth_mail -> django.core.mail.send_mail). No new
# mail infrastructure: this module calls django.core.mail.send_mail directly,
# the identical primitive _send_auth_mail wraps, with fail_silently so a
# broken SMTP relay can never surface as an invite-creation failure.
def _send_invite_mail(*, to_email: str, invite_url: str, inviter_email: str) -> None:
    subject = "You've been invited to a mailsocket team"
    body = (
        f"{inviter_email} invited you to join their mailsocket team.\n\n"
        f"Accept the invite by visiting this link:\n\n{invite_url}\n\n"
        "This link expires in 7 days."
    )
    try:
        django_mail.send_mail(
            subject, body, settings.DEFAULT_FROM_EMAIL, [to_email], fail_silently=True
        )
    except Exception:
        logger.exception("team_invite_mail_failed to=%s", to_email)


def is_demo_or_agent_user(user) -> bool:
    """Guard (§Unresolved questions): the shared demo user and
    self-registered agent accounts must never hold or join a membership.
    Both are identifiable purely by email domain — no new flag needed."""
    from .services import AGENT_ACCOUNT_DOMAIN, DEMO_USER_EMAIL

    email = (getattr(user, "email", "") or "").lower()
    if email == DEMO_USER_EMAIL.lower():
        return True
    return email.endswith(f"@{AGENT_ACCOUNT_DOMAIN}")


def create_invite(owner_user, email: str) -> tuple[TeamInvite, str]:
    "Public API."
    from .multi_account_services import is_workspace_user

    if is_workspace_user(owner_user):
        raise NotEntitled()
    normalized = normalize_email(email)
    with transaction.atomic():
        if not can_invite_member(owner_user):
            raise NotEntitled()
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        try:
            invite = TeamInvite.objects.create(
                account_id=owner_user.pk,
                email_normalized=normalized,
                token_hash=token_hash,
                role=TeamMembership.Role.MEMBER,
                invited_by=owner_user,
                expires_at=timezone.now() + timedelta(days=7),
            )
        except IntegrityError as exc:
            raise DuplicateInvite() from exc
    return invite, raw_token


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def resolve_invite(raw_token: str) -> TeamInvite:
    """Hash-lookup a token; raise InviteNotUsable for ANY non-live state
    (unknown/expired/revoked/accepted) — uniform failure, no oracle (§5)."""
    token_hash = _hash_token(raw_token)
    invite = TeamInvite.objects.filter(token_hash=token_hash).first()
    if invite is None or not invite.is_pending:
        raise InviteNotUsable()
    return invite


def accept_invite(raw_token: str, accepting_user) -> TeamMembership:
    "Public API."
    from .multi_account_services import is_workspace_user

    if is_demo_or_agent_user(accepting_user) or is_workspace_user(accepting_user):
        raise InviteNotUsable()
    # sec-gap r2 ( P3-a): a suspended human (or one with no Account
    # row — fail-closed) can't take a seat. Same uniform InviteNotUsable as
    # every other non-usable state: no oracle.
    if not Account.objects.filter(user_id=accepting_user.pk, is_suspended=False).exists():
        raise InviteNotUsable()
    token_hash = _hash_token(raw_token)
    with transaction.atomic():
        invite = (
            TeamInvite.objects.select_for_update().filter(token_hash=token_hash).first()
        )
        if invite is None or not invite.is_pending:
            raise InviteNotUsable()

        owner_user_id = invite.account_id
        
        # can_accept_member (live members only), NOT can_invite_member: the
        # latter counts pending invites, including THIS one, which made the
        # last seat(s) unfillable (sec-gap P2-2). Use get_user_model() rather
        # than type(accepting_user) — request.user is a SimpleLazyObject,
        # whose type has no .objects manager.
        from django.contrib.auth import get_user_model

        owner = get_user_model().objects.get(pk=owner_user_id)
        if not can_accept_member(owner):
            raise NotEntitled()

        if TeamMembership.objects.filter(user_id=accepting_user.pk).exists():
            raise AlreadyInTeam()

        try:
            membership = TeamMembership.objects.create(
                account_id=owner_user_id,
                user=accepting_user,
                role=TeamMembership.Role.MEMBER,
                invited_by_id=invite.invited_by_id,
            )
        except IntegrityError as exc:
            raise AlreadyInTeam() from exc

        invite.accepted_at = timezone.now()
        invite.save(update_fields=["accepted_at"])
    return membership


def revoke_invite(owner_user, invite: TeamInvite) -> None:
    if invite.account_id != owner_user.pk:
        raise InviteNotUsable()
    if invite.revoked_at is None:
        invite.revoked_at = timezone.now()
        invite.save(update_fields=["revoked_at"])


def revoke_membership(owner_user, membership: TeamMembership) -> None:
    "Public API."
    if membership.account_id != owner_user.pk:
        raise InviteNotUsable()
    if membership.revoked_at is None:
        with transaction.atomic():
            membership.revoked_at = timezone.now()
            membership.save(update_fields=["revoked_at"])
            _cascade_revoke_member_keys(membership.user_id, membership.account_id)


def leave_team(member_user, membership: TeamMembership) -> None:
    "Public API."
    if membership.user_id != member_user.pk:
        raise InviteNotUsable()
    if membership.revoked_at is None:
        with transaction.atomic():
            membership.revoked_at = timezone.now()
            membership.save(update_fields=["revoked_at"])
            _cascade_revoke_member_keys(membership.user_id, membership.account_id)


def _cascade_revoke_member_keys(member_user_id, team_account_id) -> None:
    "Public API."
    from .models import ApiKey

    ApiKey.objects.filter(
        created_by_id=member_user_id,
        user_id=team_account_id,
        revoked_at__isnull=True,
    ).update(revoked_at=timezone.now())
