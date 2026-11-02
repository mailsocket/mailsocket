"Public API."

from datetime import timedelta as _timedelta

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.accounts.models import AccountPlan, ApiKey
from apps.accounts.services import (
    ApiKeyLimitExceeded,
    RateLimitExceeded,
    create_api_key,
    increment_rate_limit,
)
from apps.accounts.tenancy import require_owner, tenant

from .decorators import post_only
from .forms import ApiKeyCreateForm
from .partials import render_partial
from .views import _owned_inboxes, _snippet_api_base, dashboard_required


def _owned_api_keys(request):
    return ApiKey.objects.filter(user=tenant(request)).order_by("-created_at", "-id")


API_KEY_CREATE_RATE_LIMIT = 10
API_KEY_CREATE_RATE_WINDOW = 3600
API_KEY_CREATE_RATE_NAME = "dashboard:apikey-create:hour"
API_KEY_CAP_MESSAGE = "You have reached the maximum of 20 active API keys. Revoke an unused key first."
API_KEY_RATE_MESSAGE = "Too many API keys created. Please wait and try again."
API_KEY_DUPLICATE_WINDOW = _timedelta(seconds=5)
API_KEY_DUPLICATE_MESSAGE = "A key with this name was just created."


def _mint_api_key(request, name: str, *, expires_at=None, dedupe_name=False):
    """Rate-limit and cap-check the shared key-mint for both dashboard paths.

    Returns ``(api_key, token)`` on success, or an ``HttpResponse`` flash
    partial when the per-user active-key cap or the hourly create rate limit is
    hit. ``request`` carries the authenticated user; both failure shapes are
    rendered so the caller can return them verbatim.

    QA-DASH #4: a double-clicked "Create key" minted two keys (the first an
    orphan whose secret was never shown). With ``dedupe_name=True`` (the
    user-named Create-key form) a second create with the SAME name for the
    same tenant within ``API_KEY_DUPLICATE_WINDOW`` is refused (409). The
    onboarding path (fixed name) keeps its pre-existing contract.
    The check runs under the tenant's AccountPlan row lock (the same lock
    ``create_api_key`` takes), so two near-simultaneous requests serialize and
    the second one sees the first key. It runs before the rate-limit charge so
    a double-click does not burn hourly budget.
    """
    owner = tenant(request)
    with transaction.atomic():
        AccountPlan.objects.select_for_update().filter(user=owner).first()
        # A key revoked right after creation (e.g. "wrong expiry, redo it")
        
        if (
            dedupe_name
            and ApiKey.objects.filter(
                user=owner,
                name=name,
                revoked_at__isnull=True,
                created_at__gte=timezone.now() - API_KEY_DUPLICATE_WINDOW,
            ).exists()
        ):
            return render_partial(
                request,
                "dashboard/partials/flash.html",
                {
                    "flash": API_KEY_DUPLICATE_MESSAGE,
                },
                status=409,
            )
        try:
            increment_rate_limit(
                API_KEY_CREATE_RATE_NAME,
                str(owner.pk),
                limit=API_KEY_CREATE_RATE_LIMIT,
                window_seconds=API_KEY_CREATE_RATE_WINDOW,
            )
        except RateLimitExceeded:
            return render_partial(
                request,
                "dashboard/partials/flash.html",
                {
                    "flash": API_KEY_RATE_MESSAGE,
                },
                status=429,
            )
        try:
            return create_api_key(owner, name, created_by=request.user, expires_at=expires_at)
        except ApiKeyLimitExceeded:
            return render_partial(
                request,
                "dashboard/partials/flash.html",
                {
                    "flash": API_KEY_CAP_MESSAGE,
                },
                status=409,
            )


@dashboard_required
@require_GET
def api_keys(request):
    api_base = ("https://" + getattr(settings, "DASHBOARD_HOST", "dash.mailsocket.app")).rstrip("/")
    return render(
        request,
        "dashboard/api_keys.html",
        {
            "api_keys": _owned_api_keys(request),
            "api_base": api_base,
        },
    )


@dashboard_required
@post_only("dashboard:api-keys")
def api_key_create(request):
    form = ApiKeyCreateForm(request.POST)
    if not form.is_valid():
        # QA-DASH #5: report the field that actually failed; an out-of-range
        # expiry used to be reported as a name error.
        if "name" in form.errors:
            message = "Name is required and must be at most 80 characters."
        else:
            message = "Expiry must be between 1 and 3650 days."
        return render_partial(
            request,
            "dashboard/partials/flash.html",
            {
                "flash": message,
            },
            status=422,
        )
    expires_in_days = form.cleaned_data.get("expires_in_days")
    expires_at = timezone.now() + _timedelta(days=expires_in_days) if expires_in_days else None
    result = _mint_api_key(request, form.cleaned_data["name"], expires_at=expires_at, dedupe_name=True)
    if isinstance(result, HttpResponse):
        # Cap or rate-limit flash partial — return it verbatim.
        return result
    api_key, token = result
    response = render_partial(
        request,
        "dashboard/partials/api_key_created.html",
        {
            "api_key": api_key,
            "token": token,
            "api_keys": _owned_api_keys(request),
        },
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@dashboard_required
@post_only("dashboard:overview")
def onboarding_api_key_create(request):
    """One-click API key for the Overview onboarding card.

    A thin wrapper over the same ``create_api_key`` service: it mints the key
    with a fixed label, reveals the plaintext ONCE via the existing
    ``api_key_created`` partial (plaintext never persisted/re-shown), and swaps
    the onboarding card to the now-authorized code-snippet state. No secret is
    logged or stored.
    """
    inbox = _owned_inboxes(request).order_by("-created_at", "-id").first()
    result = _mint_api_key(request, "Onboarding key")
    if isinstance(result, HttpResponse):
        # Cap or rate-limit flash partial — return it verbatim (never a 500).
        return result
    api_key, token = result
    response = render_partial(
        request,
        "dashboard/partials/onboarding_snippet.html",
        {
            "api_key": api_key,
            "token": token,
            "inbox": inbox,
            "public_base_url": _snippet_api_base(),
        },
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@dashboard_required
@post_only("dashboard:api-keys")
def api_key_revoke(request, public_id):
    "Public API."
    queryset = ApiKey.objects.filter(user=tenant(request))
    if not require_owner(request):
        queryset = queryset.filter(created_by_id=request.user.pk)
    api_key = get_object_or_404(queryset, public_id=public_id)
    if api_key.revoked_at is None:
        api_key.revoked_at = timezone.now()
        api_key.save(update_fields=["revoked_at"])
    return render_partial(
        request,
        "dashboard/partials/api_key_rows.html",
        {
            "api_keys": _owned_api_keys(request),
            "flash": "API key revoked.",
        },
    )
