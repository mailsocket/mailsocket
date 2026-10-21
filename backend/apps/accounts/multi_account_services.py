"Public API."
import secrets

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .entitlements import _locked_plan, plan_limits
from .models import AccountOwnership
from .services import create_product_user
from .team_services import is_demo_or_agent_user


class NotEntitled(Exception):
    "Public API."


def is_workspace_user(user) -> bool:
    """Mirror is_demo_or_agent_user's email-suffix idiom (team_services.py:68):
    a workspace ("owned account") user is identifiable purely by its
    non-deliverable, reserved email domain — no new flag needed."""
    email = (getattr(user, "email", "") or "").lower()
    return email.endswith(f"@{settings.WORKSPACE_ACCOUNT_DOMAIN}")


def create_owned_account(owner_user, name: str) -> AccountOwnership:
    "Public API."
    if not settings.ENABLE_MULTI_ACCOUNT:
        raise NotEntitled()
    if is_demo_or_agent_user(owner_user) or is_workspace_user(owner_user):
        raise NotEntitled()

    with transaction.atomic():
        plan = _locked_plan(owner_user)
        existing_count = AccountOwnership.objects.filter(owner_user=owner_user).count()
        limit = plan_limits(plan.plan)["owned_accounts"] if plan is not None else 0
        
        # can_invite_member, ...) a non-ACTIVE plan (past_due, suspended)
        # gets no new workspace.
        if existing_count >= limit or plan.status != plan.Status.ACTIVE:
            raise NotEntitled()

        handle = secrets.token_urlsafe(12).lower().replace("-", "").replace("_", "")
        ws_user = create_product_user(
            email=f"ws-{handle}@{settings.WORKSPACE_ACCOUNT_DOMAIN}", password=None
        )
        ws_user.account.email_verified_at = timezone.now()
        ws_user.account.save(update_fields=["email_verified_at", "updated_at"])

        return AccountOwnership.objects.create(
            owner_user=owner_user, account_user=ws_user, name=name
        )
