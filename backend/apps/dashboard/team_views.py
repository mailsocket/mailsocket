"Public API."
from django.conf import settings
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET

from apps.accounts.entitlements import can_invite_member, team_seat_usage
from apps.accounts.models import TeamInvite, TeamMembership
from apps.accounts.services import RateLimitExceeded, increment_rate_limit
from apps.accounts.team_services import (
    AlreadyInTeam,
    DuplicateInvite,
    InviteNotUsable,
    NotEntitled,
    accept_invite,
    create_invite,
    is_demo_or_agent_user,
    leave_team,
    resolve_invite,
    revoke_invite,
    revoke_membership,
)
from apps.accounts.tenancy import require_owner

from .decorators import post_only
from .forms import TeamInviteForm
from .partials import render_partial
from .views import _is_htmx, dashboard_required, lowest_plan_name_with

TEAM_INVITE_RATE_NAME = "dashboard:team-invite:hour"
TEAM_INVITE_RATE_LIMIT = 10
TEAM_INVITE_RATE_WINDOW = 3600


def _teams_enabled(view):
    def wrapped(request, *args, **kwargs):
        if not settings.ENABLE_TEAMS:
            raise Http404("Teams are not enabled.")
        return view(request, *args, **kwargs)

    return wrapped


def _absolute_url(request, path: str) -> str:
    return settings.PUBLIC_BASE_URL.rstrip("/") + path


def _team_context(request):
    return {
        "memberships": TeamMembership.objects.filter(
            account_id=request.user.pk, revoked_at__isnull=True
        ).select_related("user").order_by("created_at", "id"),
        "invites": TeamInvite.objects.filter(
            account_id=request.user.pk, accepted_at__isnull=True, revoked_at__isnull=True
        ).order_by("created_at", "id"),
        # qafix2 #8: "Seats: N of M used" — same counting rule as
        
        "seats": team_seat_usage(request.user),
    }


def _team_rows(request, flash):
    """Success partial for #team-rows. ``clear_result`` also empties the
    #team-result error slot out-of-band (qafix2 #10: no stale error next to
    a later success)."""
    return render_partial(request, "dashboard/partials/team_rows.html", {
        **_team_context(request),
        "flash": flash,
        "clear_result": True,
    })


@_teams_enabled
@dashboard_required
@require_GET
def team(request):
    "Public API."
    context = _team_context(request)
    return render(request, "dashboard/team.html", {
        **context,
        "can_invite": can_invite_member(request.user, lock=False),
        
        # neutral upgrade card (the server-side seat check is unchanged).
        "plan_has_team_seats": context["seats"]["total"] > 1,
        "team_plan_name": lowest_plan_name_with(lambda limits: limits.get("seats", 1) > 1),
        "form": TeamInviteForm(),
    })


@_teams_enabled
@dashboard_required
@post_only("dashboard:team")
def team_invite_create(request):
    if not require_owner(request):
        return render_partial(request, "dashboard/partials/flash.html", {
            "flash": "Only the team owner can invite members.",
        }, status=403)

    form = TeamInviteForm(request.POST)
    if not form.is_valid():
        return render_partial(request, "dashboard/partials/flash.html", {
            "flash": "Enter a valid email address.",
        }, status=422)

    if is_demo_or_agent_user(request.user):
        return render_partial(request, "dashboard/partials/flash.html", {
            "flash": "This account cannot invite team members.",
        }, status=403)

    try:
        increment_rate_limit(
            TEAM_INVITE_RATE_NAME,
            str(request.user.pk),
            limit=TEAM_INVITE_RATE_LIMIT,
            window_seconds=TEAM_INVITE_RATE_WINDOW,
        )
    except RateLimitExceeded:
        return render_partial(request, "dashboard/partials/flash.html", {
            "flash": "Too many invites sent. Please wait and try again.",
        }, status=429)

    try:
        invite, raw_token = create_invite(request.user, form.cleaned_data["email"])
    except NotEntitled:
        return render_partial(request, "dashboard/partials/flash.html", {
            "flash": "Your plan does not have an available team seat.",
        }, status=403)
    except DuplicateInvite:
        return render_partial(request, "dashboard/partials/flash.html", {
            "flash": "There is already a pending invite for that address.",
        }, status=400)

    from apps.accounts.team_services import _send_invite_mail

    invite_url = _absolute_url(request, f"/team/accept/{raw_token}")
    _send_invite_mail(
        to_email=invite.email_normalized,
        invite_url=invite_url,
        inviter_email=getattr(request.user, "email", ""),
    )

    return _team_rows(request, "Invite sent.")


@_teams_enabled
@dashboard_required
@post_only("dashboard:team")
def team_invite_revoke(request, invite_id):
    if not require_owner(request):
        return render_partial(request, "dashboard/partials/flash.html", {
            "flash": "Only the team owner can revoke invites.",
        }, status=403)

    invite = get_object_or_404(
        TeamInvite.objects.filter(account_id=request.user.pk), pk=invite_id
    )
    revoke_invite(request.user, invite)
    return _team_rows(request, "Invite revoked.")


@_teams_enabled
@dashboard_required
@post_only("dashboard:team")
def team_member_revoke(request, membership_id):
    if not require_owner(request):
        return render_partial(request, "dashboard/partials/flash.html", {
            "flash": "Only the team owner can remove members.",
        }, status=403)

    membership = get_object_or_404(
        TeamMembership.objects.filter(account_id=request.user.pk), pk=membership_id
    )
    revoke_membership(request.user, membership)
    return _team_rows(request, "Member removed.")


@_teams_enabled
@dashboard_required
@post_only("dashboard:team")
def team_leave(request):
    membership = get_object_or_404(TeamMembership, user_id=request.user.pk)
    leave_team(request.user, membership)
    if _is_htmx(request):
        response = HttpResponse(status=200)
        response.headers["HX-Redirect"] = "/app/"
        return response
    return redirect("dashboard:overview")


@_teams_enabled
@require_GET
def team_accept(request, token):
    """GET /team/accept/<token> — resolve + render a confirm screen.

    Uniform 410 on expired/revoked/accepted/unknown (no oracle). Logged-out
    visitors are sent through the existing login/signup path first (D-flow
    §5 step 2) and land back here — the token itself carries the state, so
    no session storage is needed.
    """
    try:
        invite = resolve_invite(token)
    except InviteNotUsable:
        return render(request, "dashboard/team_invite_expired.html", status=410)

    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login

        return redirect_to_login(request.get_full_path())

    return render(request, "dashboard/team_accept.html", {"invite": invite, "token": token})


@_teams_enabled
@post_only("dashboard:team-accept", url_kwargs=('token',))
def team_accept_confirm(request, token):
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login

        return redirect_to_login(request.get_full_path())

    try:
        accept_invite(token, request.user)
    except InviteNotUsable:
        return render(request, "dashboard/team_invite_expired.html", status=410)
    except NotEntitled:
        return render(request, "dashboard/confirmation.html", {
            "message": "This team no longer has an available seat.",
        }, status=403)
    except AlreadyInTeam:
        return render(request, "dashboard/confirmation.html", {
            "message": "You already belong to a team.",
        }, status=409)

    return render(request, "dashboard/confirmation.html", {
        "message": "You've joined the team.",
    })
