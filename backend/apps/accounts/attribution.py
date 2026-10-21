"""First-party signup source attribution (launch-channel tracking), consent-free.

No cookie, no session and no storage of any kind happen before signup. The
source is captured AT SIGNUP only, from what the register request itself
carries:

* ``?ref=<channel>`` (``?utm_source=`` as a fallback) on the register URL.
  Public pages carry a landing link's validated ``ref`` into every
  "Start free" link server-side (``signup_tags.signup_href``), and the
  register form posts back to its own URL, so the tag survives
  landing -> register -> POST without being stored anywhere.
* The ``Referer`` HOST of the register GET (carried to the POST in a hidden
  form field, re-validated there) or of the POST itself. Own hosts never
  count (internal navigation).
* Landing path = the register page's ``next`` / ``from`` only when already
  present and on the public allowlist (otherwise empty).

Values land on Account.src_* exactly as before; first touch wins across a
re-submitted pending signup (``record_signup_attribution``).

The old design set a signed 30-day ``ms_src`` cookie on the first anonymous
visit. ``LegacyAttributionCookieSweepMiddleware`` expires any such cookie a
returning browser still sends, until ``LEGACY_MS_SRC_SWEEP_UNTIL`` (30 days
after the cookie stopped being set); then it is inert and can be deleted.

Privacy posture (see legal/privacy): no third-party trackers, no JS, no full
URLs or query strings are ever stored — only a sanitised campaign tag, the
referring HOST, an allowlisted public PATH and a timestamp.
"""

import re
from datetime import date
from urllib.parse import urlsplit

from django.conf import settings
from django.utils import timezone

# Legacy cookie (no longer set; only expired when a browser still sends it).
COOKIE_NAME = "ms_src"

REF_MAX_LEN = 40
HOST_MAX_LEN = 253
PATH_MAX_LEN = 120

AGENT_REGISTER_REF = "agent-register"

# Hidden register-form field carrying the register GET's external Referer
# host to the POST (validated again on the POST; never trusted as is).
FORM_HOST_FIELD = "src_host"

_REF_RE = re.compile(r"^[a-z0-9_-]+$")
_HOST_RE = re.compile(r"^[a-z0-9.-]+$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9/._-]*$")

# Our own hosts never count as a referrer (internal navigation). Any
# subdomain of these (mcp., www., ...) is treated as ours too.
OWN_HOSTS = ("mailsocket.app", "dash.mailsocket.app")

# Allowlist of public, token-free pages that may be recorded as the landing
# path. Deliberately an allowlist: /verify-email/<token>,
# /password-reset/<uid>/..., /team/accept/<token> and
# /instant/email/<address>/ carry secrets or addresses in the PATH and must
# never be recorded.
_PUBLIC_EXACT = frozenset({"/", "/llms.txt"})
_PUBLIC_PREFIXES = ("/docs/", "/pricing/", "/legal/", "/register/", "/login/")
_PUBLIC_EXCLUDED_PREFIXES = ("/docs/try/inbox",)


def sanitize_ref(value) -> str | None:
    """Lower-cased campaign tag of ``[a-z0-9_-]`` only, capped at 40 chars.

    Anything containing another character (``<script>``, spaces, dots, ...)
    is rejected outright rather than "cleaned" into something misleading.
    """
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if not value or not _REF_RE.match(value):
        return None
    return value[:REF_MAX_LEN]


def sanitize_host(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower().rstrip(".")
    if not value or len(value) > HOST_MAX_LEN or not _HOST_RE.match(value):
        return None
    return value


def sanitize_path(value) -> str | None:
    if not isinstance(value, str) or not _PATH_RE.match(value):
        return None
    return value[:PATH_MAX_LEN]


def _is_own_host(host: str, request_host: str = "") -> bool:
    own: set[str] = set(OWN_HOSTS)
    if request_host:
        own.add(request_host.split(":", 1)[0].lower())
    return any(host == h or host.endswith("." + h) for h in own)


def _external_host(host, request) -> str | None:
    host = sanitize_host(host)
    if host is None or _is_own_host(host, request.get_host()):
        return None
    return host


def referrer_host(request) -> str | None:
    """Host part ONLY of an external Referer (path/query discarded)."""
    raw = request.META.get("HTTP_REFERER", "")
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        host = parts.hostname  # lower-cased, port stripped
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    return _external_host(host, request)


def is_public_landing_path(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    if path.startswith(_PUBLIC_EXCLUDED_PREFIXES):
        return False
    return path.startswith(_PUBLIC_PREFIXES)


def ref_from_request(request) -> str | None:
    """Validated campaign tag from the request's query string, or None."""
    return sanitize_ref(request.GET.get("ref")) or sanitize_ref(request.GET.get("utm_source"))


def form_referrer_host(request) -> str | None:
    """External Referer host carried by the register form (hidden field)."""
    if request.method != "POST":
        return None
    return _external_host(request.POST.get(FORM_HOST_FIELD), request)


def landing_path_from_request(request) -> str | None:
    """The register page's ``next`` / ``from``, only if public-allowlisted."""
    for key in ("next", "from"):
        raw = request.POST.get(key) if request.method == "POST" else None
        raw = raw or request.GET.get(key)
        path = sanitize_path(raw)
        if path and is_public_landing_path(path):
            return path
    return None


def attribution_from_request(request) -> dict | None:
    """Attribution carried by THIS register request (GET or POST), or None.

    None when there is neither a campaign tag nor an external referrer host
    (a direct signup): the landing path alone never counts.
    """
    ref = ref_from_request(request)
    host = referrer_host(request) or form_referrer_host(request)
    if ref is None and host is None:
        return None
    return {
        "ref": ref,
        "referrer_host": host,
        "landing_path": landing_path_from_request(request),
        "first_seen": timezone.now(),
    }


def account_fields(attribution: dict | None) -> dict:
    """Map an attribution dict onto Account.src_* field values."""
    if not attribution:
        return {}
    return {
        "src_ref": attribution.get("ref"),
        "src_referrer_host": attribution.get("referrer_host"),
        "src_landing_path": attribution.get("landing_path"),
        "src_first_seen": attribution.get("first_seen") or timezone.now(),
    }


def record_signup_attribution(user, attribution: dict | None) -> None:
    """Copy attribution onto ``user``'s Account — only if it has none yet
    (first touch wins across a re-submitted pending signup). Best-effort:
    attribution must never break signup."""
    fields = account_fields(attribution)
    if not fields or user is None:
        return
    from .models import Account

    try:
        Account.objects.filter(
            user=user, src_first_seen__isnull=True
        ).update(**fields)
    except Exception:  # pragma: no cover - never let analytics break signup
        pass


# --- legacy ms_src cookie sweep ------------------------------------------------


def _cookie_domain(request) -> str | None:
    """Domain the legacy cookie was set with: the parent domain
    (ATTRIBUTION_COOKIE_DOMAIN) when the request is on it, else host-only —
    the expiring Set-Cookie must match it or the browser keeps the cookie."""
    domain = (getattr(settings, "ATTRIBUTION_COOKIE_DOMAIN", "") or "").lstrip(".").lower()
    if not domain:
        return None
    host = request.get_host().split(":", 1)[0].lower()
    if host == domain or host.endswith("." + domain):
        return domain
    return None


def legacy_sweep_active(today: date | None = None) -> bool:
    until = getattr(settings, "LEGACY_MS_SRC_SWEEP_UNTIL", "")
    if not until:
        return False
    try:
        until_date = date.fromisoformat(str(until))
    except ValueError:
        return False
    return (today or timezone.now().date()) <= until_date


class LegacyAttributionCookieSweepMiddleware:
    """Expire a leftover ``ms_src`` cookie (old design) on any response.

    Sets nothing for a browser that does not send the cookie, so a new
    visitor never gets an ``ms_src`` Set-Cookie. Inert after
    settings.LEGACY_MS_SRC_SWEEP_UNTIL.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            if COOKIE_NAME in request.COOKIES and legacy_sweep_active():
                response.delete_cookie(
                    COOKIE_NAME, path="/", domain=_cookie_domain(request), samesite="Lax"
                )
        except Exception:  # pragma: no cover - cleanup is never load-bearing
            pass
        return response
