"Public API."

from django.contrib import messages as messages_framework
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET

from apps.accounts.entitlements import can_configure_webhook
from apps.accounts.services import RateLimitExceeded
from apps.accounts.tenancy import tenant
from apps.webhooks.models import Webhook, WebhookDelivery
from apps.webhooks.security import SSRFValidationError
from apps.webhooks.services import create_webhook, delete_webhook, rotate_signing_secret, update_webhook

from .decorators import post_only
from .forms import WebhookForm
from .partials import render_partial
from .views import _is_htmx, _owned_inbox, _owned_inboxes, dashboard_required, lowest_plan_name_with


def _owned_webhooks(request):
    return (
        Webhook.objects.filter(inbox__user=tenant(request))
        .select_related("inbox")
        .order_by("inbox__label", "id")
    )


def _webhook_page_context(request, *, flash="", secret="", is_error=False):
    return {
        "inboxes": _owned_inboxes(request).select_related("domain").order_by("label", "id"),
        "webhooks": _owned_webhooks(request),
        "can_configure": can_configure_webhook(tenant(request)),
        "webhook_plan_name": lowest_plan_name_with(lambda limits: limits.get("webhooks")),
        "flash": flash,
        "flash_is_error": is_error,
        "secret": secret,
    }


def _webhook_result(request, *, flash, status=200, secret="", is_error=None):
    """Every webhook mutation's response (qafix2 #4/#9).

    - htmx 4xx: ONLY the error flash, swapped into ``#webhook-result`` (never
      a second copy of the card list).
    - htmx success: the flash (+ the one-time secret) into ``#webhook-result``
      and the card list refreshed out-of-band in ``#webhook-rows``.
    - plain POST (no JS / htmx failed to load): never a bare partial — a
      success or error redirects back to the full Webhooks page with the
      flash; a one-time secret is rendered on the full page instead (it is
      never put in the message store / a cookie).
    """
    if is_error is None:
        is_error = status >= 400
    if not _is_htmx(request):
        if secret:
            response = render(
                request,
                "dashboard/webhooks.html",
                _webhook_page_context(request, flash=flash, secret=secret),
                status=status,
            )
            response.headers["Cache-Control"] = "private, no-store"
            return response
        (messages_framework.error if is_error else messages_framework.success)(request, flash)
        return redirect("dashboard:webhooks")
    if status >= 400:
        return render_partial(request, "dashboard/partials/flash.html", {"flash": flash}, status=status)
    template = "dashboard/partials/webhook_secret.html"
    response = render_partial(
        request,
        template,
        _webhook_page_context(request, flash=flash, secret=secret, is_error=is_error),
        status=status,
    )
    if secret:
        response.headers["Cache-Control"] = "private, no-store"
    return response


@dashboard_required
@require_GET
def webhooks(request):
    return render(request, "dashboard/webhooks.html", _webhook_page_context(request))


@dashboard_required
@post_only("dashboard:webhooks")
def webhook_save(request, public_id):
    inbox = _owned_inbox(request, public_id)
    form = WebhookForm(request.POST)
    if not form.is_valid():
        return _webhook_result(request, flash="Enter a valid HTTPS webhook URL.", status=422)
    existing = Webhook.objects.filter(inbox=inbox).first()
    enabling = form.cleaned_data["is_enabled"]
    if (existing is None or enabling) and not can_configure_webhook(tenant(request)):
        return _webhook_result(request, flash="Your plan cannot configure or enable webhooks.", status=422)
    try:
        if existing is None:
            webhook, secret = create_webhook(inbox, form.cleaned_data["url"], enabled=enabling)
            del webhook
            return _webhook_result(request, flash="Webhook created.", secret=secret)
        update_webhook(existing, form.cleaned_data["url"], enabled=enabling)
    except SSRFValidationError:
        return _webhook_result(
            request, flash="Webhook URL is not an allowed public HTTPS target.", status=422
        )
    return _webhook_result(request, flash="Webhook updated.")


@dashboard_required
@post_only("dashboard:webhooks")
def webhook_disable(request, public_id):
    inbox = _owned_inbox(request, public_id)
    webhook = get_object_or_404(Webhook.objects.filter(inbox=inbox), inbox=inbox)
    webhook.is_enabled = False
    webhook.save(update_fields=["is_enabled", "updated_at"])
    return _webhook_result(request, flash="Webhook disabled.")


@dashboard_required
@post_only("dashboard:webhooks")
def webhook_rotate(request, public_id):
    inbox = _owned_inbox(request, public_id)
    webhook = get_object_or_404(Webhook.objects.filter(inbox=inbox), inbox=inbox)
    secret = rotate_signing_secret(webhook)
    return _webhook_result(request, flash="Signing secret rotated.", secret=secret)


WEBHOOK_TEST_REFUSED_FLASH = {
    "webhook_entitlement_required": "Your plan cannot configure or enable webhooks.",
    "webhook_not_configured": "Configure a webhook for this inbox first.",
    "webhook_ssrf_rejected": "Webhook URL is not an allowed public HTTPS target.",
}


@dashboard_required
@post_only("dashboard:webhooks")
def webhook_test(request, public_id):
    """qafix2 #5: the dashboard "Send test" button.

    Reuses ``apps.api.views.send_webhook_test`` verbatim — the exact function
    behind POST /api/v1/inboxes/{id}/webhook/test — so the entitlement gate,
    rate-limit buckets, ``webhook_attempts`` metering and the SSRF-pinned
    transport are shared, not re-implemented. Tenant-scoped through
    ``_owned_inbox``; same permission rule as Disable (any dashboard user of
    the tenant).
    """
    from apps.api.views import WebhookTestRefused, send_webhook_test

    inbox = _owned_inbox(request, public_id)
    try:
        data, _rate_limit = send_webhook_test(inbox, tenant(request))
    except RateLimitExceeded:
        return _webhook_result(request, flash="Too many test sends. Please wait and try again.", status=429)
    except WebhookTestRefused as exc:
        if exc.code == "webhook_unreachable":
            # The request itself worked; the receiver did not answer. This is
            # a test RESULT, shown inline, not a dashboard request error.
            reason = (exc.fields or {}).get("error", ["error"])[0]
            return _webhook_result(
                request,
                flash=f"Test sent, but your endpoint could not be reached ({reason}).",
                is_error=True,
            )
        flash = WEBHOOK_TEST_REFUSED_FLASH.get(exc.code, "Could not send a test event.")
        return _webhook_result(request, flash=flash, status=exc.status)
    code = data["response_status"]
    if 200 <= code < 300:
        return _webhook_result(request, flash=f"Test event delivered — your endpoint answered HTTP {code}.")
    return _webhook_result(
        request,
        flash=f"Test event sent, but your endpoint answered HTTP {code}.",
        is_error=True,
    )


@dashboard_required
@post_only("dashboard:webhooks")
def webhook_delete(request, public_id):
    """qafix2 #5: delete the inbox's webhook and its delivery history.

    Same tenant scoping and permission rule as Disable. ``delete_webhook``
    removes the PROTECT-ed WebhookDelivery rows first, in one transaction.
    """
    inbox = _owned_inbox(request, public_id)
    webhook = get_object_or_404(Webhook.objects.filter(inbox=inbox), inbox=inbox)
    delete_webhook(webhook)
    return _webhook_result(request, flash="Webhook deleted.")


@dashboard_required
@require_GET
def webhook_deliveries(request, public_id):
    inbox = _owned_inbox(request, public_id)
    webhook = get_object_or_404(Webhook.objects.filter(inbox=inbox), inbox=inbox)
    deliveries = WebhookDelivery.objects.filter(webhook=webhook).order_by("-created_at", "-id")[:100]
    return render_partial(
        request,
        "dashboard/partials/webhook_delivery_rows.html",
        {
            "webhook": webhook,
            "deliveries": deliveries,
        },
    )
