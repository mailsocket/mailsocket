"""MTA-STS policy host (RFC 8461) for the brand domain.

Receiving MTAs fetch ``https://mta-sts.<domain>/.well-known/mta-sts.txt``.
The inbound MX of mailsocket.app is Cloudflare Email Routing, so the policy
lists those MX hosts. Mode and max_age come from settings so the coordinator
can flip ``testing`` -> ``enforce`` with an env change only.

``MtaStsMiddleware`` answers EVERY request on settings.MTA_STS_HOST itself,
before the canonical-host redirects, SecurityMiddleware, sessions, CSRF and
auth: the policy path gets the policy, any other path a plain 404. No
redirect, no cookie, nothing else of the site is reachable on that host. It
matches the raw Host header (normalised like the canonical middleware), so it
never calls request.get_host().
"""

from django.conf import settings
from django.http import HttpResponse

POLICY_PATH = "/.well-known/mta-sts.txt"
VALID_MODES = ("testing", "enforce", "none")
# RFC 8461 3.2: max_age is at most 31557600 seconds (about 1 year).
MAX_AGE_LIMIT = 31557600


def policy_text() -> str:
    lines = ["version: STSv1", f"mode: {settings.MTA_STS_MODE}"]
    lines += [f"mx: {mx}" for mx in settings.MTA_STS_MX]
    lines.append(f"max_age: {int(settings.MTA_STS_MAX_AGE)}")
    return "\n".join(lines) + "\n"


def _host(request) -> str:
    return request.META.get("HTTP_HOST", "").split(":", 1)[0].strip().lower().rstrip(".")


def _plain(body: str, status: int) -> HttpResponse:
    response = HttpResponse(body, content_type="text/plain", status=status)
    response["X-Content-Type-Options"] = "nosniff"
    return response


class MtaStsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = (getattr(settings, "MTA_STS_HOST", "") or "").lower()
        if not host or _host(request) != host:
            return self.get_response(request)
        if request.path != POLICY_PATH:
            return _plain("Not found\n", 404)
        if request.method not in ("GET", "HEAD"):
            response = _plain("Method not allowed\n", 405)
            response["Allow"] = "GET, HEAD"
            return response
        response = _plain(policy_text(), 200)
        response["Cache-Control"] = "max-age=300"
        return response
