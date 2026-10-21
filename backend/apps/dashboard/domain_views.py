"Public API."
from django.conf import settings
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.accounts.entitlements import can_add_domain, plan_limits
from apps.accounts.models import AccountPlan
from apps.accounts.services import RateLimitExceeded, increment_rate_limit
from apps.accounts.team_services import is_demo_or_agent_user
from apps.accounts.tenancy import require_owner, tenant
from apps.domains.services import (
    MX_TARGET_HOST,
    DomainCreateRefused,
    DomainDeleteRefused,
    create_domain,
    delete_domain,
    owned_domains,
    verify_domain,
)

from .decorators import post_only
from .partials import render_partial
from .views import _is_htmx, dashboard_required


def _refuse(request, flash, status):
    """qafix2 #3: an error must never leave the browser on a bare partial.

    htmx submits get the flash partial (swapped into ``#domain-result`` by
    app.js, which only swaps HX-Partial-marked 4xx); a plain (no-JS) POST is
    redirected back to the full Domains page with the same text as a flash.
    """
    if _is_htmx(request):
        return render_partial(request, "dashboard/partials/flash.html", {"flash": flash}, status=status)
    messages.error(request, flash)
    return redirect("dashboard:domains")


def _done(request, flash=""):
    """Success: back to the full Domains page (htmx follows HX-Redirect)."""
    if flash:
        messages.success(request, flash)
    if _is_htmx(request):
        response = HttpResponse(status=200)
        response.headers["HX-Redirect"] = reverse("dashboard:domains")
        return response
    return redirect("dashboard:domains")


def _byod_enabled(view):
    def wrapped(request, *args, **kwargs):
        if not settings.ENABLE_BYOD:
            raise Http404("Custom domains are not enabled.")
        return view(request, *args, **kwargs)

    return wrapped


def _entitlement_reason(user) -> str:
    "Public API."
    plan = AccountPlan.objects.filter(user=user).first()
    if plan is not None and plan_limits(plan.plan)["custom_domains"] == 0:
        return "Custom domains require a Scale or Business plan."
    return "Domain limit reached."


@dashboard_required
@_byod_enabled
@require_GET
def domains(request):
    """List + add form + DNS instructions. Members CAN view (design §1)."""
    owner = tenant(request)
    domain_rows = list(owned_domains(owner).order_by("-created_at", "-id"))
    show_owner_ui = require_owner(request) and not is_demo_or_agent_user(request.user)
    # can_add_domain's default read acquires select_for_update() (only valid
    # inside a transaction). This is a GET-time display hint only (hides the
    # add-form; the POST walls are the real enforcement) — use the
    # lock=False non-locking read so no transaction.atomic() wrapper is
    # needed on a plain page render (incident 2026-09-30: select_for_update
    # outside atomic() -> TransactionManagementError on /app/team/; swept
    # every GET-path caller of a _locked_plan helper for the same bug).
    can_add = can_add_domain(owner, lock=False) if show_owner_ui else False
    return render(request, "dashboard/domains.html", {
        "domains": domain_rows,
        "mx_target": MX_TARGET_HOST,
        "show_owner_ui": show_owner_ui,
        "can_add_domain": can_add,
    })


@dashboard_required
@_byod_enabled
@post_only("dashboard:domains")
def domain_create(request):
    if is_demo_or_agent_user(request.user):
        return _refuse(request, "Custom domains are not available for this account.", 403)
    if not require_owner(request):
        return _refuse(request, "Only the account owner can manage domains.", 403)

    owner = tenant(request)
    name = request.POST.get("name", "")

    with transaction.atomic():
        if not can_add_domain(owner):
            return _refuse(request, _entitlement_reason(owner), 403)
        try:
            domain = create_domain(owner, name)
        except DomainCreateRefused as exc:
            # create_domain normalizes a malformed-FQDN/reserved-apex
            # full_clean() ValidationError into
            # DomainCreateRefused(code="validation_error") — that must
            # come back as 422 (the documented API contract), never 409
            # (409 is reserved for the real "name already taken" case).
            status_code = 422 if exc.code == "validation_error" else 409
            return _refuse(request, exc.message, status_code)
        except IntegrityError:
            return _refuse(request, "That domain is already registered.", 409)

    return _done(request, f"Domain {domain.name} added. Add the DNS records below, then click Verify.")


@dashboard_required
@_byod_enabled
@post_only("dashboard:domains")
def domain_verify(request, public_id):
    if is_demo_or_agent_user(request.user):
        return _refuse(request, "Custom domains are not available for this account.", 403)
    if not require_owner(request):
        return _refuse(request, "Only the account owner can manage domains.", 403)

    domain = get_object_or_404(owned_domains(tenant(request)), public_id=public_id)

    try:
        # Reuse the SAME rate-limit buckets/keys as the API
        # (apps.api.views.DomainVerifyView) verbatim — UI and API share
        # one budget, so alternating surfaces can't double the resolver
        # load (design §1).
        increment_rate_limit(
            "api:domain-verify:domain:30s", domain.public_id, limit=1, window_seconds=30
        )
        increment_rate_limit(
            "api:domain-verify:account:hour", str(request.user.pk), limit=20, window_seconds=3600
        )
    except RateLimitExceeded:
        return _refuse(request, "Checked too recently — try again in a moment.", 429)

    verify_domain(domain)
    return _done(request)


@dashboard_required
@_byod_enabled
@post_only("dashboard:domains")
def domain_delete(request, public_id):
    if is_demo_or_agent_user(request.user):
        return _refuse(request, "Custom domains are not available for this account.", 403)
    if not require_owner(request):
        return _refuse(request, "Only the account owner can manage domains.", 403)

    domain = get_object_or_404(owned_domains(tenant(request)), public_id=public_id)

    try:
        delete_domain(domain)
    except DomainDeleteRefused:
        flash = "Remove or unbind its inboxes first."
        if domain.catch_all:
            flash += " Disable catch-all via the API first."
        return _refuse(request, flash, 409)

    return _done(request, f"Domain {domain.name} deleted.")
