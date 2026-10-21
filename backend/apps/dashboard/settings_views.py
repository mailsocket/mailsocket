"Public API."
from typing import Any

from django.contrib import messages
from django.contrib.auth import logout, update_session_auth_hash
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.accounts.forms import SetPasswordForm
from apps.accounts.models import (
    Account,
    AccountOwnership,
    AccountPlan,
    ApiKey,
    MonthlyUsage,
    TeamInvite,
    TeamMembership,
)
from apps.accounts.services import RateLimitExceeded, enforce_auth_rate_limits
from apps.accounts.team_services import is_demo_or_agent_user
from apps.accounts.tenancy import require_owner
from apps.accounts.views import _client_ip
from apps.billing.views import _ls_delete
from apps.domains.models import Domain
from apps.inboxes.models import Inbox, Message
from apps.outbound.models import ApprovedRecipient, OutboundEmail
from apps.webhooks.models import Webhook, WebhookDelivery

from .decorators import post_only
from .views import dashboard_required

WORKSPACE_DELETE_VIA_SUPPORT = (
    "Your account owns workspaces, which cannot be deleted from the dashboard yet. "
    "Email support@mailsocket.app from this address and we will delete the "
    "workspaces and your account for you."
)
GENERIC_REAUTH_ERROR = "Unable to verify your credentials."
NOT_AVAILABLE_ERROR = "Not available for this account."
MISSING_CURRENT_PASSWORD = "Enter your current password."
MISSING_NEW_PASSWORD = "Enter the new password in both fields."
MISSING_CONFIRM_EMAIL = "Type your account email to confirm."
LS_CANCEL_FAILED = (
    "We couldn't cancel your subscription. Nothing was "
    "deleted — cancel it in the billing portal first, "
    "then retry."
)


def _back(request, section: str, text: str, *, level=messages.ERROR):
    """qafix2 #13: POST-redirect-GET. Every outcome of a Settings POST is a
    redirect back to /app/settings/ (a reload never re-POSTs); the message
    is tagged ``inline <section>`` so the page renders it inside the card
    it belongs to (and the global flash bar skips it)."""
    messages.add_message(request, level, text, extra_tags=f"inline {section}")
    return redirect(reverse("dashboard:settings") + f"#settings-{section}")


def _inline_messages(request) -> dict:
    """Split this request's inline Settings messages by card."""
    context: dict[str, Any] = {"password_errors": [], "password_success": "", "delete_errors": []}
    for message in messages.get_messages(request):
        tags = (message.extra_tags or "").split()
        if "inline" not in tags:
            continue
        if "password" in tags:
            if message.level == messages.SUCCESS:
                context["password_success"] = str(message)
            else:
                context["password_errors"].append(str(message))
        elif "delete" in tags:
            context["delete_errors"].append(str(message))
    return context


def _rate_limited(request, action: str) -> bool:
    try:
        enforce_auth_rate_limits(action, request.user.email, _client_ip(request))
    except RateLimitExceeded:
        return True
    return False


def _settings_context(request) -> dict:
    """Section A (account info) context, shared by GET and every POST
    re-render — computed from request.user ONLY (see module docstring)."""
    account = request.user.account
    plan = request.user.account_plan
    is_member = TeamMembership.objects.filter(
        user_id=request.user.pk, revoked_at__isnull=True
    ).exists()
    owner_is_self = require_owner(request)
    return {
        "title": "Settings",
        "account": account,
        "plan": plan,
        "is_demo_or_agent": is_demo_or_agent_user(request.user),
        "is_member": is_member,
        "can_show_danger_zone": owner_is_self and not is_demo_or_agent_user(request.user),
        "owner_is_self": owner_is_self,
    }


@dashboard_required
@require_GET
def settings_page(request):
    return render(request, "dashboard/settings.html", {
        **_settings_context(request),
        **_inline_messages(request),
    })


@dashboard_required
@post_only("dashboard:settings")
def settings_password(request):
    if is_demo_or_agent_user(request.user):
        return _back(request, "password", NOT_AVAILABLE_ERROR)

    if _rate_limited(request, "password-change"):
        return _back(request, "password", GENERIC_REAUTH_ERROR)

    current_password = request.POST.get("current_password", "")
    # Empty fields name the missing field; a WRONG password keeps the generic
    # message (no oracle). Checked before check_password, so an empty submit
    # never counts as a credential attempt.
    if not current_password:
        return _back(request, "password", MISSING_CURRENT_PASSWORD)
    if not request.POST.get("new_password1") or not request.POST.get("new_password2"):
        return _back(request, "password", MISSING_NEW_PASSWORD)
    if not request.user.check_password(current_password):
        return _back(request, "password", GENERIC_REAUTH_ERROR)

    # Same SetPasswordForm/AUTH_PASSWORD_VALIDATORS stack the reset flow
    # already uses (accounts/views.py:116,208) — no new validator surface.
    form = SetPasswordForm(request.user, request.POST)
    if not form.is_valid():
        for field in form:
            for error in field.errors:
                messages.add_message(request, messages.ERROR, error, extra_tags="inline password")
        for error in form.non_field_errors():
            messages.add_message(request, messages.ERROR, error, extra_tags="inline password")
        return redirect(reverse("dashboard:settings") + "#settings-password")

    form.save()  # set_password + save
    # THIS session survives; every OTHER session dies on its next request
    # (Django's session-auth-hash check) — the "kick the hijacker" behavior.
    update_session_auth_hash(request, request.user)
    return _back(request, "password", "Password changed.", level=messages.SUCCESS)


@dashboard_required
@post_only("dashboard:settings")
def settings_delete(request):
    # --- (a) ALL local pre-checks first (cheap, no side effects) — design
    # §4.1(a): never cancel a subscription then refuse the delete for a
    # reason we could have known upfront. ---
    if is_demo_or_agent_user(request.user):
        return _back(request, "delete", NOT_AVAILABLE_ERROR)

    if _rate_limited(request, "account-delete"):
        return _back(request, "delete", GENERIC_REAUTH_ERROR)

    if not require_owner(request):
        return _back(request, "delete", "Leave your team before deleting your account.")

    if TeamMembership.objects.filter(
        account_id=request.user.pk, revoked_at__isnull=True
    ).exists():
        return _back(request, "delete", "Remove your team members first.")

    if AccountOwnership.objects.filter(owner_user=request.user).exists():
        
        # old "Delete your workspaces first." was a dead end. Ops erases
        
        # workspaces"); tell the user how to ask.
        return _back(request, "delete", WORKSPACE_DELETE_VIA_SUPPORT)

    current_password = request.POST.get("current_password", "")
    confirm_email = (request.POST.get("confirm_email") or "").strip().lower()
    if not current_password:
        return _back(request, "delete", MISSING_CURRENT_PASSWORD)
    if not confirm_email:
        return _back(request, "delete", MISSING_CONFIRM_EMAIL)
    if not request.user.check_password(current_password):
        return _back(request, "delete", GENERIC_REAUTH_ERROR)

    if confirm_email != request.user.account.email_normalized:
        return _back(request, "delete", "Type your account email exactly to confirm.")

    # --- (b) LS cancel FIRST, fail-closed, OUTSIDE any DB transaction
    # (design §4.1: never call the LS API inside atomic()). ---
    plan = request.user.account_plan
    if plan.stripe_subscription_id:
        import logging
        import urllib.error

        logger = logging.getLogger(__name__)
        try:
            _ls_delete(f"/subscriptions/{plan.stripe_subscription_id}")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                logger.exception(
                    "settings_delete_ls_cancel_failed user_pk=%s", request.user.pk
                )
                return _back(request, "delete", LS_CANCEL_FAILED)
            # 404 = already cancelled at LS — proceed.
        except (urllib.error.URLError, TimeoutError, OSError):
            logger.exception(
                "settings_delete_ls_cancel_failed user_pk=%s", request.user.pk
            )
            return _back(request, "delete", LS_CANCEL_FAILED)

    # --- (c) the erase transaction — exact order, design §4.2 ---
    user_pk = request.user.pk
    with transaction.atomic():
        try:
            Account.objects.select_for_update().get(pk=user_pk)
        except Account.DoesNotExist:
            # Double-submit race: a concurrent request already erased this
            # account. Clean already-deleted handling, no 500.
            logout(request)
            return redirect(reverse("dashboard:root"))

        # 0. BREAKS THE FK CYCLE: Inbox.domain is PROTECT (inboxes/models.py:91)
        # and Domain.catch_all_inbox is PROTECT (domains/models.py:125) — two
        # PROTECT FKs pointing at each other. Nulling the catch-all pointer
        # first is the ONLY order that lets both sides delete cleanly below.
        # THIS STEP IS MANDATORY AND MUST RUN FIRST.
        Domain.objects.filter(user_id=user_pk).update(catch_all=False, catch_all_inbox=None)

        # 1-2. webhooks (PROTECT -> inbox/message)
        WebhookDelivery.objects.filter(webhook__inbox__user_id=user_pk).delete()
        Webhook.objects.filter(inbox__user_id=user_pk).delete()
        # 3. outbound audit rows (PROTECT -> user)
        OutboundEmail.objects.filter(user_id=user_pk).delete()
        # 4. allowlist rows (CASCADE anyway; explicit for a deterministic log)
        ApprovedRecipient.objects.filter(user_id=user_pk).delete()
        # 5-6. messages then inboxes (PROTECT -> inbox / user)
        Message.objects.filter(inbox__user_id=user_pk).delete()
        Inbox.objects.filter(user_id=user_pk).delete()
        # 7. domains (PROTECT -> user; no inboxes reference it anymore)
        Domain.objects.filter(user_id=user_pk).delete()
        # 8. api keys (PROTECT -> user; created_by=this user elsewhere is
        # SET_NULL and untouched — those keys belong to OTHER tenants)
        ApiKey.objects.filter(user_id=user_pk).delete()
        # 9-10. team rows this user owns as an account (would CASCADE from
        # Account, explicit for self-documentation / determinism)
        TeamInvite.objects.filter(account_id=user_pk).delete()
        TeamMembership.objects.filter(account_id=user_pk).delete()
        # 11. usage (PROTECT -> user)
        MonthlyUsage.objects.filter(user_id=user_pk).delete()
        # 12. WaitLease — CASCADE, automatic, nothing to do here.
        # 13-14. plan then account (PROTECT -> user)
        AccountPlan.objects.get(user_id=user_pk).delete()
        Account.objects.get(pk=user_pk).delete()
        # 15. the human row itself — SET_NULL fan-out fires here
        # automatically: BillingEvent.user, SuppressionEntry.source_user
        # (survives, protects the shared sending IP), ApiKey.created_by,
        # OutboundEmail.created_by, TeamMembership.invited_by,
        # TeamInvite.invited_by on OTHER tenants.
        request.user.delete()

    # After COMMIT (not before): flush this session, then send them to a
    # confirmation page on the public landing.
    logout(request)
    return render(
        request, "dashboard/confirmation.html",
        {"message": "Your account has been deleted."},
    )
