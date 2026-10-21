"Public API."
from django.conf import settings
from django.http import Http404

from apps.accounts.models import AccountOwnership
from apps.accounts.multi_account_services import NotEntitled, create_owned_account
from apps.accounts.team_services import is_demo_or_agent_user
from apps.accounts.tenancy import SESSION_KEY

from .decorators import post_only
from .forms import AccountCreateForm
from .partials import render_partial
from .views import dashboard_required


def _multi_account_enabled(view):
    def wrapped(request, *args, **kwargs):
        if not settings.ENABLE_MULTI_ACCOUNT:
            raise Http404("Multi-account is not enabled.")
        return view(request, *args, **kwargs)

    return wrapped


def _has_live_membership(user) -> bool:
    from apps.accounts.models import TeamMembership

    if not settings.ENABLE_TEAMS:
        return False
    return TeamMembership.objects.filter(user_id=user.pk, revoked_at__isnull=True).exists()


def _switcher_refused(request) -> bool:
    """Server-side re-check: refuse demo/agent users and live team members,
    even though the switcher UI is hidden for them (defense in depth,
    design §3/§5)."""
    return is_demo_or_agent_user(request.user) or _has_live_membership(request.user)


@_multi_account_enabled
@dashboard_required
@post_only("dashboard:overview")
def account_switch(request):
    """POST /app/account/switch — body: account=<pk>|"self".

    "self" pops the session key. Otherwise: server-side re-check
    AccountOwnership(owner_user=request.user, account_user_id=pk).exists()
    -> set the session key; else a UNIFORM 404 (same status/body whether the
    pk exists-but-isn't-yours or doesn't exist at all — no oracle).
    """
    if _switcher_refused(request):
        raise Http404("Account switching is not available.")

    value = request.POST.get("account", "")
    if value == "self":
        request.session.pop(SESSION_KEY, None)
        return render_partial(request, "dashboard/partials/flash.html", {"flash": "Switched to Personal."})

    try:
        pk = int(value)
    except (TypeError, ValueError):
        raise Http404("Unknown account.") from None

    exists = AccountOwnership.objects.filter(
        owner_user_id=request.user.pk, account_user_id=pk
    ).exists()
    if not exists:
        raise Http404("Unknown account.")

    request.session[SESSION_KEY] = pk
    return render_partial(request, "dashboard/partials/flash.html", {"flash": "Account switched."})


@_multi_account_enabled
@dashboard_required
@post_only("dashboard:overview")
def account_create(request):
    "Public API."
    if is_demo_or_agent_user(request.user) or _has_live_membership(request.user):
        raise Http404("Account creation is not available.")

    form = AccountCreateForm(request.POST)
    if not form.is_valid():
        return render_partial(request, "dashboard/partials/flash.html", {
            "flash": "Enter a name (max 80 characters).",
        }, status=422)

    try:
        link = create_owned_account(request.user, form.cleaned_data["name"])
    except NotEntitled:
        return render_partial(request, "dashboard/partials/flash.html", {
            "flash": "Your plan does not have an available account slot.",
        }, status=403)

    request.session[SESSION_KEY] = link.account_user_id
    return render_partial(request, "dashboard/partials/flash.html", {
        "flash": f"Account \"{link.name}\" created and switched.",
    })
