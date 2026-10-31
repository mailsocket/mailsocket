"Public API."
from django.conf import settings

from apps.accounts.models import AccountOwnership
from apps.accounts.team_services import is_demo_or_agent_user
from apps.accounts.tenancy import tenant


def feature_flags(request):
    """Settings-page nav gating (design §4 / engdev-settings.md §4): today no
    feature flag reaches base.html at all, so the sidebar cannot conditionally
    show Billing/Team links. Settings reads, plus one AccountPlan lookup for a
    signed-in user while PAID_PLANS_ON_SALE is false (decides the paused
    upgrade note); anonymous/error renders never query.
    """
    from apps.billing.kill_switch import PAUSED_MESSAGE, billing_changes_enabled

    paused = not billing_changes_enabled()
    paid_upgrades_paused = False
    if not getattr(settings, "PAID_PLANS_ON_SALE", True) and request.path.startswith("/app/"):
        user = getattr(request, "user", None)
        if getattr(user, "is_authenticated", False):
            from apps.accounts.models import AccountPlan

            paid_upgrades_paused = not AccountPlan.objects.filter(
                user=tenant(request), plan__in=("paid", "scale", "business")
            ).exists()
    return {
        "PAID_UPGRADES_PAUSED": paid_upgrades_paused,
        "PAID_UPGRADES_PAUSED_MESSAGE": "Paid plans are paused. Your Free plan remains available.",
        "PAID_PLANS_ON_SALE": settings.PAID_PLANS_ON_SALE,
        "ENABLE_BILLING": settings.ENABLE_BILLING,
        "ENABLE_TEAMS": settings.ENABLE_TEAMS,
        "ENABLE_BYOD": settings.ENABLE_BYOD,
        "UPGRADE_URL": upgrade_url(),
        # Billing kill switch: billing.html / pricing.html (signed-in only)
        # render the upgrade CTAs disabled; /app/fleet shows a staff banner.
        "BILLING_CHANGES_PAUSED": paused,
        "BILLING_PAUSED_MESSAGE": PAUSED_MESSAGE if paused else "",
    }


def upgrade_url() -> str:
    "Public API."
    from django.urls import reverse

    if settings.ENABLE_BILLING:
        return reverse("dashboard:billing-page")
    return site_origins(None)["APEX_ORIGIN"] + reverse("dashboard:pricing")


def site_origins(request):
    "Public API."
    apex = getattr(settings, "APEX_HOST", "")
    dash = getattr(settings, "DASHBOARD_HOST", "")
    if not (getattr(settings, "CANONICAL_HOST_REDIRECTS", False) and apex and dash and apex != dash):
        return {"APEX_ORIGIN": "", "DASH_ORIGIN": ""}
    return {"APEX_ORIGIN": "https://" + apex, "DASH_ORIGIN": "https://" + dash}


def multi_account(request):
    if not settings.ENABLE_MULTI_ACCOUNT:
        return {}
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {}
    if is_demo_or_agent_user(user):
        return {}

    if settings.ENABLE_TEAMS:
        from apps.accounts.models import TeamMembership

        if TeamMembership.objects.filter(user_id=user.pk, revoked_at__isnull=True).exists():
            return {}

    owned = list(
        AccountOwnership.objects.filter(owner_user_id=user.pk)
        .select_related("account_user")
        .order_by("name", "id")
    )
    active_pk = getattr(tenant(request), "pk", user.pk)
    active_name = "Personal"
    for link in owned:
        if link.account_user_id == active_pk:
            active_name = link.name
            break
    return {
        "ms_multi_account": {
            "owned": owned,
            "active_name": active_name,
            "is_switched": active_pk != user.pk,
        }
    }
