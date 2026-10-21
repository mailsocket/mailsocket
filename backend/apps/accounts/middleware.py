import ipaddress
import time

from django.conf import settings
from django.http import HttpResponsePermanentRedirect

from .client_ip import trusted_cf_ip

# QA #2: Cloudflare Web Analytics auto-injects
# <script src="https://static.cloudflareinsights.com/beacon.min.js"> on every
# proxied HTML page and the beacon reports to https://cloudflareinsights.com.
# Allow EXACTLY those two origins in the two directives they need; every other
# directive (and default-src 'self' for everything else) is unchanged, and no
# 'unsafe-inline' / 'unsafe-eval' / wildcard is introduced.
CLOUDFLARE_INSIGHTS_SCRIPT_ORIGIN = "https://static.cloudflareinsights.com"
CLOUDFLARE_INSIGHTS_CONNECT_ORIGIN = "https://cloudflareinsights.com"


# HOTFIX P0 (checkout-form-action): /app/billing/ "Upgrade" is
# a plain POST form to /app/billing/checkout/, which answers 303 -> Lemon
# Squeezy's hosted checkout at settings.LS_CHECKOUT_ORIGIN (and the LS
# customer-portal redirect, apps.billing.views.portal, lands on that same
# store-subdomain origin — LS docs: the customer portal is served from the
# store's own subdomain, never app.lemonsqueezy.com, so one origin covers
# both). Without it in form-action, Chrome blocks the cross-origin redirect
# of the form submission and checkout/portal silently do nothing (0 orders).
# Exactly that one origin, no wildcard; every other directive byte-identical.
def _default_csp():
    return (
        "default-src 'self'; "
        f"script-src 'self' {CLOUDFLARE_INSIGHTS_SCRIPT_ORIGIN}; "
        f"connect-src 'self' {CLOUDFLARE_INSIGHTS_CONNECT_ORIGIN}; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; "
        f"form-action 'self' {settings.LS_CHECKOUT_ORIGIN}"
    )


class WwwRedirectMiddleware:
    """301 ``www.<APEX_HOST>`` -> ``https://<APEX_HOST>`` (same path + query).

    Reads the raw Host header instead of request.get_host(), so the redirect
    fires even when the www host is NOT in ALLOWED_HOSTS (otherwise Django
    would answer 400 DisallowedHost first). The target origin is a fixed
    setting, never derived from the request, so this cannot become an open
    redirect. Every other host passes through untouched, so it is inert until
    DNS for www points at us.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        apex = getattr(settings, "APEX_HOST", "")
        if apex:
            host = request.META.get("HTTP_HOST", "").split(":", 1)[0].strip().lower().rstrip(".")
            if host == "www." + apex:
                return HttpResponsePermanentRedirect("https://" + apex + request.get_full_path())
        return self.get_response(request)


def _normalised_host(request) -> str:
    """Raw Host header, lower-cased, port and trailing dot stripped (same
    normalisation as WwwRedirectMiddleware)."""
    return request.META.get("HTTP_HOST", "").split(":", 1)[0].strip().lower().rstrip(".")


def _matches(path: str, prefixes) -> bool:
    """``/app`` and ``/app/...`` both match the prefix ``/app/``."""
    return any(path == p.rstrip("/") or path.startswith(p) for p in prefixes)



# Checked FIRST: never redirected on either host. Machine/API traffic (clients
# may not follow a cross-host redirect with their Authorization header or
# body), Postfix ingest, health probes (the compose healthcheck sends
# Host: mailsocket.app), static files, the LS webhook receiver, security.txt,
# the /docs/try JSON endpoints and the machine-readable files tools import
# from either host (llms.txt advertises <dash>/docs/openapi.yaml).
CANONICAL_NEVER_PREFIXES = (
    "/api/",
    "/internal/",
    "/healthz",
    "/static/",
    "/media/",
    "/billing/",
    "/.well-known/",
    "/docs/try/inbox",
    "/instant/",
)
CANONICAL_NEVER_EXACT = frozenset({"/docs/openapi.yaml", "/status.json", "/llms.txt"})
# apex -> dash: the product (sessions/cookies live on dash only).
PRODUCT_PATH_PREFIXES = (
    "/app/",
    "/login/",
    "/register/",
    "/logout/",
    "/verify-email/",
    "/password-reset/",
    "/team/accept/",
    "/admin/",
)
# dash -> apex: the public site. "/" is deliberately absent: on dash it keeps
# its host-aware behaviour (anonymous -> /login/, signed in -> /app/).
PUBLIC_PATH_PREFIXES = ("/docs/", "/pricing/", "/legal/")
PUBLIC_PATH_EXACT = frozenset({"/security", "/status", "/sitemap.xml", "/robots.txt"})
CANONICAL_REDIRECT_METHODS = frozenset({"GET", "HEAD"})


def canonical_redirect_host(host: str, method: str, path: str) -> str | None:
    """Return the host ``path`` must be served from, or None to serve it here.

    Pure function of (normalised request host, method, path) and fixed
    settings; the returned host is always settings.APEX_HOST or
    settings.DASHBOARD_HOST, never anything request-derived. Only GET/HEAD
    are redirected: a wrong-host POST is served where it landed (see
    CanonicalHostRedirectMiddleware).
    """
    if not getattr(settings, "CANONICAL_HOST_REDIRECTS", False):
        return None
    apex = (getattr(settings, "APEX_HOST", "") or "").lower()
    dash = (getattr(settings, "DASHBOARD_HOST", "") or "").lower()
    if not apex or not dash or apex == dash or host not in (apex, dash):
        return None
    if method not in CANONICAL_REDIRECT_METHODS:
        return None
    if path in CANONICAL_NEVER_EXACT or path.startswith(CANONICAL_NEVER_PREFIXES):
        return None
    if host == apex and _matches(path, PRODUCT_PATH_PREFIXES):
        return dash
    if host == dash and (path in PUBLIC_PATH_EXACT or _matches(path, PUBLIC_PATH_PREFIXES)):
        return apex
    return None


class CanonicalHostRedirectMiddleware:
    """301 a GET/HEAD for a product page on the apex to DASHBOARD_HOST, and a
    public-site page on the dashboard host to APEX_HOST (same path + query).

    Same safety rules as WwwRedirectMiddleware: raw Host header, exact host
    match only (port / trailing dot normalised), fixed target from settings,
    so a forged Host can never steer the Location. Behind
    settings.CANONICAL_HOST_REDIRECTS (env kill switch).

    Non-GET/HEAD requests are never redirected (no 308): session and CSRF
    cookies are host-only, so a form re-POSTed to the other host by a 308
    would fail CSRF (403) and lose the typed input. Forms are now only
    rendered on their canonical host, so a wrong-host POST can only come
    from a page opened before the switch or a script; serving it where it
    landed is the one option that never breaks it.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        target = canonical_redirect_host(_normalised_host(request), request.method, request.path)
        if target:
            return HttpResponsePermanentRedirect("https://" + target + request.get_full_path())
        return self.get_response(request)


class BrowserSecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", _default_csp())
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
        )
        if request.path.startswith(("/register/", "/login/", "/verify-email/", "/password-reset/")):
            response.headers["Cache-Control"] = "no-store"
        return response


# D3 (N1): module-level monotonic memo shared
# by every request in this worker process — throttles the anomaly counter's
# DB writes to at most 1 per 60s per worker (3 workers => <=3 writes/min even
# in a total edge outage; no hot-row storm).
_last_edge_anomaly_write = 0.0


def _is_exempt(address) -> bool:
    """True if ``address`` is in settings.EDGE_MONITOR_EXEMPT_CIDRS.

    Malformed entries are ignored (fail toward counting, i.e. toward the
    alarm). Parsed per call: this runs only on the rare header-less path.
    """
    for value in settings.EDGE_MONITOR_EXEMPT_CIDRS.split(","):
        value = value.strip()
        if not value:
            continue
        try:
            if address in ipaddress.ip_network(value, strict=True):
                return True
        except ValueError:
            continue
    return False


class EdgeHeaderMonitorMiddleware:
    """Counts requests reaching web with no CF-Connecting-IP header, whose
    REMOTE_ADDR is not loopback, not in EDGE_MONITOR_EXEMPT_CIDRS (known
    internal peers such as the mcp container) and whose path isn't
    /internal/ingest (all legitimate header-less traffic — see design §D3). Active only when
    settings.EDGE_HEADER_MONITOR is True (False on base.py/tests, True on
    production.py) — inert by construction everywhere else.

    Normal-path cost is a single dict lookup; on an anomaly, at most one DB
    write per 60s per worker process via a module-level monotonic memo, so a
    sustained edge outage can never turn into a write storm.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.EDGE_HEADER_MONITOR:
            self._maybe_count_anomaly(request)
        return self.get_response(request)

    @staticmethod
    def _maybe_count_anomaly(request) -> None:
        if trusted_cf_ip(request):
            return
        if request.path == "/internal/ingest":
            return
        remote_addr = request.META.get("REMOTE_ADDR", "")
        try:
            address = ipaddress.ip_address(remote_addr)
        except ValueError:
            address = None
        if address is not None:
            if address.is_loopback:
                return
            if _is_exempt(address):
                # Known internal peer (e.g. the mcp container): header-less by
                # design, not an edge anomaly. Monitor-only exemption.
                return
        global _last_edge_anomaly_write
        now = time.monotonic()
        if now - _last_edge_anomaly_write < 60:
            return
        _last_edge_anomaly_write = now
        try:
            from .services import increment_rate_limit

            increment_rate_limit(
                "ops:edge-missing", "global", limit=10**9, window_seconds=7200
            )
        except Exception:
            pass
