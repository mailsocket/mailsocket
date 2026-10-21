from django.urls import path

from apps.dashboard.redirects import slash_redirect

from . import views

app_name = "status"

urlpatterns = [
    path("status", views.status_page, name="page"),
    path("status.json", views.status_json, name="json"),
    # ux3 N9: canonical URLs have no trailing slash (APPEND_SLASH only adds
    # one), so the slashed forms used to 404. 301 them, keeping the query.
    path("status/", slash_redirect("status:page")),
    path("status.json/", slash_redirect("status:json")),
]
