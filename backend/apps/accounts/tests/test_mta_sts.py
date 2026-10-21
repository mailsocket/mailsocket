"""MTA-STS policy host (apps.accounts.mta_sts, RFC 8461)."""

import pytest
from django.core.checks import Error
from django.test import Client, override_settings

from apps.accounts.checks import mta_sts_errors

pytestmark = pytest.mark.django_db

HOST = "mta-sts.mailsocket.app"
POLICY = (
    "version: STSv1\n"
    "mode: testing\n"
    "mx: route1.mx.cloudflare.net\n"
    "mx: route2.mx.cloudflare.net\n"
    "mx: route3.mx.cloudflare.net\n"
    "max_age: 86400\n"
)


@pytest.fixture
def prodlike(settings):
    """Production-like host setup: canonical redirects on, secure redirect
    on, ALLOWED_HOSTS without the MTA-STS host (the middleware answers it
    before Django's host validation ever runs)."""
    settings.CANONICAL_HOST_REDIRECTS = True
    settings.APEX_HOST = "mailsocket.app"
    settings.DASHBOARD_HOST = "dash.mailsocket.app"
    settings.ALLOWED_HOSTS = ["mailsocket.app", "dash.mailsocket.app", HOST, "testserver"]
    settings.SECURE_SSL_REDIRECT = True
    return settings


def _get(path, method="get", **extra):
    return getattr(Client(), method)(path, HTTP_HOST=HOST, secure=True, **extra)


def test_policy_exact_body_and_content_type(prodlike):
    response = _get("/.well-known/mta-sts.txt")
    assert response.status_code == 200
    assert response.content == POLICY.encode()
    assert response["Content-Type"] == "text/plain"
    assert not response.cookies
    assert "Set-Cookie" not in response
    assert "Location" not in response


def test_policy_over_plain_http_is_not_redirected(prodlike):
    response = Client().get("/.well-known/mta-sts.txt", HTTP_HOST=HOST)
    assert response.status_code == 200  # no SECURE_SSL_REDIRECT 301
    assert response.content == POLICY.encode()


def test_policy_with_port_and_trailing_dot_host(prodlike):
    for host in ("mta-sts.mailsocket.app:443", "MTA-STS.mailsocket.app."):
        response = Client().get("/.well-known/mta-sts.txt", HTTP_HOST=host, secure=True)
        assert response.status_code == 200, host
        assert response.content == POLICY.encode()


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/.well-known/mta-sts.txt/",
        "/.well-known/security.txt",
        "/.well-known/",
        "/docs/",
        "/login/",
        "/register/",
        "/app/",
        "/admin/",
        "/api/v1/inboxes",
        "/healthz",
        "/static/dashboard/app.css",
        "/robots.txt",
        "/.well-known/MTA-STS.TXT",
    ],
)
def test_other_paths_404_no_redirect_no_cookie(prodlike, path):
    response = _get(path)
    assert response.status_code == 404, path
    assert "Location" not in response
    assert not response.cookies
    assert response["Content-Type"] == "text/plain"


def test_query_string_still_serves_policy(prodlike):
    assert _get("/.well-known/mta-sts.txt?x=1").content == POLICY.encode()


def test_old_cookie_and_session_never_touched_on_policy_host(prodlike):
    client = Client()
    client.cookies["ms_src"] = "old"
    client.cookies["sessionid"] = "whatever"
    response = client.get("/.well-known/mta-sts.txt", HTTP_HOST=HOST, secure=True)
    assert response.status_code == 200
    assert not response.cookies


def test_post_refused_head_ok(prodlike):
    assert _get("/.well-known/mta-sts.txt", method="post").status_code == 405
    head = _get("/.well-known/mta-sts.txt", method="head")
    assert head.status_code == 200


def test_mode_and_max_age_from_settings(prodlike):
    prodlike.MTA_STS_MODE = "enforce"
    prodlike.MTA_STS_MAX_AGE = 604800
    body = _get("/.well-known/mta-sts.txt").content.decode()
    assert "mode: enforce\n" in body
    assert body.endswith("max_age: 604800\n")


def test_policy_only_on_mta_sts_host():
    # Other hosts: the policy path is not served by this middleware.
    for host in ("testserver",):
        response = Client().get("/.well-known/mta-sts.txt", HTTP_HOST=host)
        assert response.status_code == 404
        assert b"STSv1" not in response.content


def test_disabled_when_host_empty(settings):
    settings.MTA_STS_HOST = ""
    settings.ALLOWED_HOSTS = [HOST]
    response = Client().get("/.well-known/mta-sts.txt", HTTP_HOST=HOST)
    assert b"STSv1" not in response.content


def test_production_settings_accept_mta_sts_host():
    """production.py appends MTA_STS_HOST to the env-driven ALLOWED_HOSTS."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[3] / "config" / "settings" / "production.py").read_text()
    assert "ALLOWED_HOSTS.append(MTA_STS_HOST)" in src


@override_settings(MTA_STS_MODE="enforced")
def test_check_rejects_bad_mode():
    assert [e.id for e in mta_sts_errors()] == ["mailsocket.E007"]


@override_settings(MTA_STS_MAX_AGE=31557601)
def test_check_rejects_bad_max_age():
    assert [e.id for e in mta_sts_errors()] == ["mailsocket.E007"]


@override_settings(MTA_STS_MX=[])
def test_check_rejects_empty_mx():
    errors = mta_sts_errors()
    assert errors and all(isinstance(e, Error) for e in errors)


def test_check_passes_on_defaults():
    assert mta_sts_errors() == []
