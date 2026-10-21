import unicodedata

from django.contrib import admin
from django.db import connection
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseServerError, JsonResponse

from apps.accounts.client_ip import client_ip as _client_ip
from apps.accounts.client_ip import trusted_cf_ip
from apps.accounts.services import RateLimitExceeded, enforce_auth_rate_limits


def admin_login(request):
    if request.method == "POST":
        
        # throttling, same as Django's UsernameField.to_python — otherwise
        # a Unicode look-alike ("ａｄｍｉｎ" fullwidth etc.) hashes to a
        # fresh bucket and bypasses the ceiling entirely, while still
        # authenticating as the real "admin" once normalized by the form.
        username = unicodedata.normalize(
            "NFKC", request.POST.get("username", "")
        ).strip()
        try:
            enforce_auth_rate_limits("admin-login", username, _client_ip(request))
        except RateLimitExceeded:
            return HttpResponse("Too many login attempts.", status=429)
    return admin.site.login(request)


def healthz(request):
    """Unauthenticated liveness+DB probe. Spec §3.2: executes SELECT 1; DB failure -> 503."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return JsonResponse({"status": "error"}, status=503)
    return JsonResponse({"status": "ok"})


def healthz_edge(request):
    """D3 (N1): unauthenticated probe reporting
    whether CF-Connecting-IP reached this request. Never echoes the IP, no DB
    access, no rate limit."""
    if trusted_cf_ip(request):
        return JsonResponse({"edge": "ok"})
    return JsonResponse({"edge": "missing"}, status=503)


def api_not_found(request, exception=None):
    """#16: any unmatched path under /api/ returns the
    same JSON error envelope as every other API 404 ({"error":{"code":
    "not_found", ...}}, application/json) instead of the branded HTML 404
    page. Routes elsewhere (dashboard, legal, docs, ...) keep the HTML 404 —
    this is Django's global ``handler404``, dispatched by path prefix here
    rather than registered as a urlpattern.

    Built as a plain JsonResponse (not DRF's Response, which needs DRF's
    view dispatch to get rendered) with the same {code, message, fields,
    request_id} shape as apps.api.errors.error_response/MESSAGES, so SDKs
    parsing either 404 see an identical body.
    """
    if request.path.startswith("/api/"):
        from apps.api.errors import MESSAGES
        from apps.api.middleware import current_request_id

        error = {"code": "not_found", "message": MESSAGES["not_found"], "fields": {}}
        request_id = current_request_id.get()
        if request_id is not None:
            error["request_id"] = request_id
        return JsonResponse({"error": error}, status=404)
    from django.template.loader import render_to_string

    return HttpResponse(render_to_string("404.html", request=request), status=404)


def server_error(request, template_name="500.html"):
    """handler500: Django's default renders 500.html with an EMPTY context,
    so the page could not show the request id. Same self-contained contract
    (no request, so no context processors; 500.html loads no static): only
    the request id (from the RequestIdMiddleware contextvar) and the path
    are passed, both autoescaped/urlencoded by the template. ANY failure
    returns a fixed static page (no template, no context, no request data):
    Django's default handler only catches TemplateDoesNotExist and would
    raise again inside the 500 path."""
    try:
        from django.template import loader

        from apps.api.middleware import current_request_id, is_valid_request_id

        request_id = current_request_id.get() or getattr(request, "request_id", "")
        if not is_valid_request_id(request_id):
            request_id = ""
        path = getattr(request, "path", "") or ""
        if not path.startswith("/") or path.startswith("//") or "\\" in path or len(path) > 200:
            path = ""
        html = loader.get_template(template_name).render({"request_id": request_id, "path": path})
        return HttpResponse(html, status=500)
    except Exception:  # noqa: BLE001 — never fail while reporting a failure
        return _static_server_error()


STATIC_500_HTML = (
    "<!doctype html><html><head><meta charset=\"utf-8\"><title>Server error</title></head>"
    "<body><h1>Server error (500)</h1><p>Something went wrong on our side. Please retry.</p>"
    "</body></html>"
)


def _static_server_error():
    """Last-resort 500: a hard-coded string, no template/context/request."""
    try:
        return HttpResponseServerError(STATIC_500_HTML)
    except Exception:  # noqa: BLE001
        return HttpResponse(b"Server error (500)", status=500, content_type="text/plain")


def csrf_failure(request, reason="", template_name="403_csrf.html"):
    "Public API."
    try:
        from django.template import loader

        from apps.api.middleware import current_request_id, is_valid_request_id

        request_id = current_request_id.get() or getattr(request, "request_id", "")
        if not is_valid_request_id(request_id):
            request_id = ""
        path = getattr(request, "path", "") or ""
        if not path.startswith("/") or path.startswith("//") or "\\" in path or len(path) > 200:
            path = ""
        html = loader.get_template(template_name).render({"request_id": request_id, "path": path})
        return HttpResponseForbidden(html)
    except Exception:  # noqa: BLE001 — never fail while reporting a failure
        return _static_csrf_failure()


STATIC_CSRF_HTML = (
    "<!doctype html><html><head><meta charset=\"utf-8\"><title>Session expired</title></head>"
    "<body><h1>Your session expired.</h1><p>Reload the page and try again.</p>"
    "</body></html>"
)


def _static_csrf_failure():
    """Last-resort CSRF 403: a hard-coded string, no template/context/request."""
    try:
        return HttpResponseForbidden(STATIC_CSRF_HTML)
    except Exception:  # noqa: BLE001
        return HttpResponse(b"Session expired (403)", status=403, content_type="text/plain")
