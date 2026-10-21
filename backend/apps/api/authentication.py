import secrets
from datetime import timedelta

from django.core.exceptions import ObjectDoesNotExist
from django.db import models
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import APIException, AuthenticationFailed

from apps.accounts.models import AccountPlan, ApiKey
from apps.accounts.services import API_KEY_PREFIX_LENGTH, hash_api_key


class AccountForbidden(APIException):
    status_code = 403
    default_code = "account_forbidden"
    default_detail = "Account is not permitted to use the API."


def _account_missing_or_suspended(user) -> bool:
    """Fail-closed: a human with no Account row counts as suspended."""
    try:
        return user.account.is_suspended
    except ObjectDoesNotExist:
        return True


def _human_behind_key_is_suspended(api_key) -> bool:
    "Public API."
    if api_key.created_by_id is not None and api_key.created_by_id != api_key.user_id:
        if _account_missing_or_suspended(api_key.created_by):
            return True
    try:
        link = api_key.user.owned_by_link
    except ObjectDoesNotExist:
        link = None
    if link is not None and _account_missing_or_suspended(link.owner_user):
        return True
    return False


class BearerAuthentication(BaseAuthentication):
    keyword = "Bearer"

    def authenticate_header(self, request):
        return self.keyword

    def authenticate(self, request):
        value = request.headers.get("Authorization", "")
        parts = value.split()
        if len(parts) != 2 or parts[0] != self.keyword:
            raise AuthenticationFailed("API key required.", code="authentication_required")
        token = parts[1]
        if not token.startswith("ms_live_") or len(token) <= len("ms_live_") + API_KEY_PREFIX_LENGTH:
            raise AuthenticationFailed("Invalid API key.", code="authentication_required")

        prefix = token.removeprefix("ms_live_")[:API_KEY_PREFIX_LENGTH]
        presented_hash = hash_api_key(token)
        matched = None
        candidates = ApiKey.objects.select_related(
            "user",
            "user__account",
            "user__account_plan",
            # sec-gap P2-1: joined up-front so the human-suspension checks
            # below cost no extra queries on the hot path.
            "created_by__account",
            "user__owned_by_link__owner_user__account",
        ).filter(prefix=prefix)
        for candidate in candidates:
            if secrets.compare_digest(presented_hash, candidate.key_hash):
                matched = candidate
                break
        now = timezone.now()
        if (
            matched is None
            or matched.revoked_at is not None
            or (matched.expires_at is not None and matched.expires_at <= now)
        ):
            secrets.compare_digest(presented_hash, "0" * 64)
            raise AuthenticationFailed("Invalid API key.", code="authentication_required")

        account = matched.user.account
        plan = getattr(matched.user, "account_plan", None)
        if (
            not matched.user.is_active
            or account.is_suspended
            or account.email_verified_at is None
            or plan is None
            or plan.status == AccountPlan.Status.SUSPENDED
        ):
            raise AccountForbidden()
        if _human_behind_key_is_suspended(matched):
            raise AccountForbidden()
        cutoff = timezone.now() - timedelta(minutes=5)
        if matched.last_used_at is None or matched.last_used_at <= cutoff:
            ApiKey.objects.filter(pk=matched.pk).filter(
                models.Q(last_used_at__isnull=True) | models.Q(last_used_at__lte=cutoff)
            ).update(last_used_at=timezone.now())
        return matched.user, matched
