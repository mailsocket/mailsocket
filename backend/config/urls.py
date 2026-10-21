from django.contrib import admin
from django.urls import include, path

from .views import admin_login, api_not_found, healthz, healthz_edge, server_error

handler404 = api_not_found
handler500 = server_error

urlpatterns = [
    path("admin/login/", admin_login, name="admin-login"),
    path("admin/", admin.site.urls),
    path("", include("apps.accounts.urls")),
    path("", include("apps.dashboard.urls")),
    # Report a problem: /feedback/ (public) and /app/feedback/ (dashboard).
    path("", include("apps.feedback.urls")),
    path("api/v1/", include("config.api_urls")),
    path("", include("apps.status.urls")),
    path("healthz", healthz, name="healthz"),
    # D3 (N1): unauthenticated edge probe used
    # by the hourly check_edge_health command (and any external monitor).
    path("healthz/edge", healthz_edge, name="healthz-edge"),
    # Defense in depth: this route is outside the public API and CIDR-gated by
    # InternalIngestNetworkMiddleware. Turn B also isolates it at the network layer.
    path("internal/ingest", include("apps.ingest.urls")),
    # Lemon Squeezy billing webhook. Always REGISTERED (mirrors the ENABLE_
    # OUTBOUND/ENABLE_TEAMS pattern: reverse() never breaks); the view itself
    # raises Http404 when settings.ENABLE_BILLING is False.
    path("billing/", include("apps.billing.urls")),
]
