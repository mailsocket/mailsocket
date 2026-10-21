"Public API."
import hashlib
import hmac
import logging
from urllib.parse import urlsplit

from django.conf import settings
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from apps.accounts.models import Account, AccountPlan
from apps.accounts.services import RateLimitExceeded, increment_rate_limit
from apps.accounts.tenancy import tenant
from apps.accounts.views import _client_ip
from apps.dashboard.views import dashboard_required

from . import alerts
from .forms import FeedbackForm, clean_page_path, clean_request_id
from .models import MESSAGE_MAX_CHARS, USER_AGENT_MAX_CHARS, FeedbackReport

logger = logging.getLogger(__name__)

IP_RATE_NAME = "feedback:ip:hour"
IP_RATE_LIMIT = 5
ACCOUNT_RATE_NAME = "feedback:account:hour"
ACCOUNT_RATE_LIMIT = 10
RATE_WINDOW = 3600


def ip_hash(ip):
    secret = settings.AUTH_RATE_LIMIT_SECRET.encode()
    return hmac.new(secret, f"feedback-ip\0{ip}".encode(), hashlib.sha256).hexdigest()


def _plan_key(user):
    return (
        AccountPlan.objects.filter(user=user).values_list("plan", flat=True).first() or ""
    )


def _referer_path(request):
    """Path of a same-host Referer (the dashboard sidebar link carries no
    ?from=, so every page stays byte-identical whatever its URL). Never
    trusted beyond clean_page_path: path only, local, no query."""
    referer = request.META.get("HTTP_REFERER", "")
    if not referer:
        return ""
    parts = urlsplit(referer)
    try:
        host = request.get_host()
    except Exception:  # noqa: BLE001 — DisallowedHost etc.
        return ""
    if parts.scheme not in ("http", "https") or parts.netloc != host:
        return ""
    path = clean_page_path(parts.path)
    return "" if path == request.path else path


def _initial(request):
    return {
        "page_path": clean_page_path(request.GET.get("from", "")) or _referer_path(request),
        "request_id": clean_request_id(request.GET.get("rid", "")),
    }


def _context(form, *, signed_in, report=None, received=False, rate_limited=False):
    return {
        "title": "Report a problem",
        "form": form,
        "signed_in": signed_in,
        "report": report,
        "received": received,
        "rate_limited": rate_limited,
        "message_max": MESSAGE_MAX_CHARS,
    }


def _handle(request, template, *, signed_in):
    if request.method != "POST":
        form = FeedbackForm(initial=_initial(request), email_required=not signed_in)
        return render(request, template, _context(form, signed_in=signed_in))

    form = FeedbackForm(request.POST, email_required=not signed_in)
    if form.is_bot:
        # Honeypot: look exactly like a success to the bot, store nothing.
        return render(
            request, template, _context(FeedbackForm(email_required=not signed_in), signed_in=signed_in, received=True)
        )
    if not form.is_valid():
        return render(request, template, _context(form, signed_in=signed_in), status=400)

    user = tenant(request) if signed_in else None
    # Rate limit BEFORE the report row is written ().
    try:
        if user is not None:
            increment_rate_limit(
                ACCOUNT_RATE_NAME, str(user.pk), limit=ACCOUNT_RATE_LIMIT, window_seconds=RATE_WINDOW
            )
        else:
            increment_rate_limit(
                IP_RATE_NAME, _client_ip(request), limit=IP_RATE_LIMIT, window_seconds=RATE_WINDOW
            )
    except RateLimitExceeded:
        # Keep what they typed; nothing stored.
        return render(
            request, template, _context(form, signed_in=signed_in, rate_limited=True), status=429
        )

    data = form.cleaned_data
    report = FeedbackReport.objects.create(
        message=data["message"],
        email=data["email"],
        page_path=data["page_path"],
        user=user,
        plan=_plan_key(user) if user is not None else "",
        request_id=data["request_id"],
        user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:USER_AGENT_MAX_CHARS],
        ip_hash=ip_hash(_client_ip(request)),
    )
    alerts.notify_new_report(report)  # never raises
    return render(
        request,
        template,
        _context(FeedbackForm(email_required=not signed_in), signed_in=signed_in, report=report, received=True),
    )


@require_http_methods(["GET", "HEAD", "POST"])
def public_feedback(request):
    signed_in = bool(
        getattr(request.user, "is_authenticated", False)
        and Account.objects.filter(user=request.user, email_verified_at__isnull=False).exists()
    )
    return _handle(request, "feedback/public.html", signed_in=signed_in)


@dashboard_required
@require_http_methods(["GET", "HEAD", "POST"])
def app_feedback(request):
    return _handle(request, "feedback/app.html", signed_in=True)
