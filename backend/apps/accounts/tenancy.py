"Public API."
from django.conf import settings

SESSION_KEY = "ms_active_account"


def tenant(request):
    "Public API."
    if not settings.ENABLE_TEAMS and not settings.ENABLE_MULTI_ACCOUNT:
        return request.user

    user = request.user
    if not getattr(user, "is_authenticated", False):
        return user

    from .team_services import is_demo_or_agent_user

    if is_demo_or_agent_user(user):
        return user

    if settings.ENABLE_TEAMS:
        from .models import TeamMembership

        membership = (
            TeamMembership.objects.filter(user_id=user.pk, revoked_at__isnull=True)
            .select_related("account__user")
            .first()
        )
        if membership is not None:
            return membership.account.user

    if settings.ENABLE_MULTI_ACCOUNT:
        selected = request.session.get(SESSION_KEY)
        if isinstance(selected, int) and selected != user.pk:
            from .models import AccountOwnership

            link = (
                AccountOwnership.objects.filter(
                    owner_user_id=user.pk, account_user_id=selected
                )
                .select_related("account_user")
                .first()
            )
            if link is not None:
                return link.account_user

    return user


def require_owner(request) -> bool:
    "Public API."
    if not settings.ENABLE_TEAMS:
        return True

    user = request.user
    if not getattr(user, "is_authenticated", False):
        return False

    from .team_services import is_demo_or_agent_user

    if is_demo_or_agent_user(user):
        return True

    from .models import TeamMembership

    return not TeamMembership.objects.filter(
        user_id=user.pk, revoked_at__isnull=True
    ).exists()
