"Public API."

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts import attribution
from apps.accounts.models import Account, AccountPlan
from apps.accounts.services import create_api_key
from apps.inboxes.models import Inbox, Message

pytestmark = pytest.mark.django_db

APEX = "mailsocket.app"
DASH = "dash.mailsocket.app"
PASSWORD = "correct horse battery staple 90210"


@pytest.fixture
def canon(settings):
    settings.CANONICAL_HOST_REDIRECTS = True
    settings.APEX_HOST = APEX
    settings.DASHBOARD_HOST = DASH
    settings.ATTRIBUTION_COOKIE_DOMAIN = APEX
    settings.ALLOWED_HOSTS = [APEX, DASH, "web", "testserver"]
    return settings


def _get(path, host, method="get", **extra):
    return getattr(Client(), method)(path, HTTP_HOST=host, secure=True, **extra)


def _verified_user(name="canon"):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password=PASSWORD
    )
    Account.objects.create(
        user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user, plan=AccountPlan.Plan.FREE)
    return user


# --- redirect pairs ------------------------------------------------------------

APEX_TO_DASH = [
    "/app/",
    "/app",
    "/app/inboxes/?q=a&page=2",
    "/login/",
    "/login/?next=/app/api-keys/",
    "/register/?ref=hn&utm_source=x",
    "/logout/",
    "/verify-email/sometoken/",
    "/password-reset/",
    "/password-reset/confirm/uid/tok/",
    "/team/accept/tok",
    "/admin/",
    "/admin/login/?next=/admin/",
]

DASH_TO_APEX = [
    "/docs/",
    "/docs/api/?x=1",
    "/docs/guides/pytest/",
    "/docs/changelog",
    "/docs/try/",
    "/pricing/?ref=hn",
    "/legal/terms",
    "/legal/privacy/",
    "/security",
    "/status",
    "/sitemap.xml",
    "/robots.txt",
]


@pytest.mark.parametrize("path", APEX_TO_DASH)
@pytest.mark.parametrize("method", ["get", "head"])
def test_product_paths_on_apex_301_to_dash(canon, path, method):
    response = _get(path, APEX, method)
    assert response.status_code == 301
    assert response["Location"] == "https://" + DASH + path


@pytest.mark.parametrize("path", DASH_TO_APEX)
@pytest.mark.parametrize("method", ["get", "head"])
def test_public_paths_on_dash_301_to_apex(canon, path, method):
    response = _get(path, DASH, method)
    assert response.status_code == 301
    assert response["Location"] == "https://" + APEX + path


@pytest.mark.parametrize("path", ["/", "/docs/", "/pricing/", "/legal/terms", "/security", "/status", "/robots.txt", "/sitemap.xml"])
def test_public_paths_served_on_apex(canon, path):
    assert _get(path, APEX).status_code == 200


@pytest.mark.parametrize("path", ["/login/", "/register/", "/password-reset/"])
def test_product_paths_served_on_dash(canon, path):
    assert _get(path, DASH).status_code == 200


def test_dash_root_keeps_host_aware_behaviour(canon):
    """dash "/" is NOT sent to the landing: anonymous -> /login/, signed in -> /app/."""
    response = _get("/", DASH)
    assert response.status_code == 302 and response["Location"] == "/login/"
    client = Client()
    client.force_login(_verified_user())
    response = client.get("/", HTTP_HOST=DASH, secure=True)
    assert response.status_code == 302 and response["Location"] == "/app/"


# --- never redirected ------------------------------------------------------------

NEVER = [
    "/healthz",
    "/healthz/edge",
    "/.well-known/security.txt",
    "/docs/openapi.yaml",
    "/llms.txt",
    "/status.json",
    "/billing/webhook/ls/",
    "/static/dashboard/app.css",
    "/api/v1/inboxes",
    "/api/v1/nope",
    "/instant/email.md",
    "/docs/try/inbox/inbox_x/wait",
]


@pytest.mark.parametrize("path", NEVER)
@pytest.mark.parametrize("host", [APEX, DASH])
def test_never_redirect_list(canon, path, host):
    response = _get(path, host)
    assert not 300 <= response.status_code < 400, (path, host, response.status_code)


@pytest.mark.parametrize("host", [APEX, DASH])
def test_api_served_on_both_hosts_with_and_without_auth(canon, host):
    user = _verified_user("canon_api_" + host.split(".")[0])
    _key, token = create_api_key(user, "CI")
    anon = _get("/api/v1/inboxes", host)
    assert anon.status_code == 401
    authed = _get("/api/v1/inboxes", host, HTTP_AUTHORIZATION=f"Bearer {token}")
    assert authed.status_code == 200
    assert "data" in authed.json()
    # write path too: no redirect would drop the body
    created = Client().post(
        "/api/v1/inboxes", {"label": "x"}, content_type="application/json",
        HTTP_HOST=host, secure=True, HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    assert created.status_code in (200, 201), created.content


@pytest.mark.parametrize("host", [APEX, DASH])
def test_try_playground_post_unaffected_on_both_hosts(canon, host):
    response = Client().post("/docs/try/inbox", HTTP_HOST=host, secure=True)
    assert response.status_code == 201
    assert response.json()["id"].startswith("inbox_")


@pytest.mark.parametrize("path", ["/login/", "/register/"])
def test_wrong_host_post_is_served_not_redirected(canon, path):
    """A form POST that lands on the apex (stale tab) is handled, not 301/308'd
    into a CSRF failure on the other host."""
    response = Client().post(path, {"email": "x@example.test"}, HTTP_HOST=APEX, secure=True)
    assert response.status_code in (200, 429)


@override_settings(
    INGEST_SHARED_SECRET="canon-ingest-secret",
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    MAIL_DOMAIN="in.inboxpipe.net",
)
def test_postfix_ingest_on_web_host_unaffected(canon, user):
    Inbox.objects.create(user=user, routing_key="canon_key", label="Canon")
    raw = b"From: a@example.test\r\nSubject: hi\r\nMessage-ID: <canon@example.test>\r\n\r\nYour code is 482193\r\n"
    response = Client().post(
        "/internal/ingest", data=raw, content_type="application/octet-stream",
        HTTP_HOST="web:8000",
        HTTP_X_MAILSOCKET_INGEST_SECRET="canon-ingest-secret",
        HTTP_X_ENVELOPE_TO="canon_key@in.inboxpipe.net",
        HTTP_X_ENVELOPE_FROM="bounce@example.test",
        HTTP_X_POSTFIX_QUEUE_ID="Q1",
        HTTP_X_INTAKE_ID="a-1",
        REMOTE_ADDR="172.30.0.9",
    )
    assert response.status_code == 201, response.content
    assert Message.objects.count() == 1


# --- host header safety ------------------------------------------------------


@pytest.mark.parametrize("host", [
    "evil.test",
    "mailsocket.app.evil.test",
    "dash.mailsocket.app.evil.test",
    "evil.test/mailsocket.app",
])
def test_unknown_hosts_never_redirect(canon, host):
    response = _get("/app/", host)
    assert response.status_code == 400  # DisallowedHost, not a redirect


@pytest.mark.parametrize("host", ["MAILSOCKET.APP", "mailsocket.app.", "mailsocket.app:443", "Mailsocket.App.:8443"])
def test_host_normalisation_and_fixed_target(canon, host):
    response = Client().get(
        "/login/?next=/app/", HTTP_HOST=host, HTTP_X_FORWARDED_HOST="evil.test", secure=True
    )
    assert response.status_code == 301
    assert response["Location"] == "https://dash.mailsocket.app/login/?next=/app/"


def test_location_never_uses_request_derived_host(canon):
    """Path tricks cannot change the authority of the Location."""
    response = _get("//evil.test/app/", APEX)
    assert not 300 <= response.status_code < 400 or response["Location"].startswith("https://" + DASH + "/")


def test_other_hosts_untouched(canon):
    assert _get("/app/", "testserver").status_code == 302  # login_required, not canonical
    assert Client().get("/docs/", HTTP_HOST="web").status_code == 200
    assert Client().get("/docs/").status_code == 200


# --- kill switch ---------------------------------------------------------------


def test_flag_off_means_no_redirects_and_relative_links(canon):
    canon.CANONICAL_HOST_REDIRECTS = False
    for path in ("/app/", "/login/", "/admin/"):
        response = _get(path, APEX)
        assert response.status_code != 301, path
    for path in ("/docs/", "/pricing/", "/legal/terms"):
        assert _get(path, DASH).status_code == 200, path
    body = _get("/", APEX).content.decode()
    assert 'href="/login/"' in body and 'href="/register/"' in body
    assert "https://dash.mailsocket.app/login/" not in body


def test_flag_requires_distinct_hosts(canon):
    canon.DASHBOARD_HOST = APEX
    assert _get("/app/", APEX).status_code != 301
    canon.DASHBOARD_HOST = ""
    assert _get("/app/", APEX).status_code != 301


# --- cookies / login / attribution ---------------------------------------------


def test_login_end_to_end_on_dash(canon):
    _verified_user("canon_login")
    client = Client()
    # apex link -> 301 -> dash login form
    first = client.get("/login/?next=/app/inboxes/", HTTP_HOST=APEX, secure=True)
    assert first["Location"] == "https://dash.mailsocket.app/login/?next=/app/inboxes/"
    form = client.get("/login/?next=/app/inboxes/", HTTP_HOST=DASH, secure=True)
    assert form.status_code == 200
    response = client.post(
        "/login/?next=/app/inboxes/",
        {"email": "canon_login@example.test", "password": PASSWORD, "next": "/app/inboxes/"},
        HTTP_HOST=DASH, secure=True,
    )
    assert response.status_code == 302 and response["Location"] == "/app/inboxes/"
    session = response.cookies["sessionid"]
    assert session["domain"] == ""  # host-only: the session lives on dash only
    assert client.get("/app/inboxes/", HTTP_HOST=DASH, secure=True).status_code == 200


def test_attribution_survives_apex_visit_then_dash_signup(canon):
    """No cookie: the apex page carries ?ref= into its absolute dash
    "Start free" links, and the dash register request records it."""
    client = Client()
    landing = client.get("/?ref=hn", HTTP_HOST=APEX, secure=True,
                         HTTP_REFERER="https://news.ycombinator.com/item?id=1")
    assert landing.status_code == 200
    assert attribution.COOKIE_NAME not in landing.cookies
    body = landing.content.decode()
    assert body.count('href="https://dash.mailsocket.app/register/?ref=hn"') == 2
    assert 'href="https://dash.mailsocket.app/register/"' not in body

    # A stale apex /register/ link 301s to dash with the query intact.
    hop = client.get("/register/?ref=hn", HTTP_HOST=APEX, secure=True)
    assert hop.status_code == 301 and hop["Location"] == "https://dash.mailsocket.app/register/?ref=hn"

    # Browser Referer on the dash GET is the apex page (own host): ignored.
    form = client.get("/register/?ref=hn", HTTP_HOST=DASH, secure=True,
                      HTTP_REFERER="https://mailsocket.app/")
    assert form.status_code == 200
    assert attribution.COOKIE_NAME not in form.cookies
    assert 'name="src_host"' not in form.content.decode()
    response = client.post(
        "/register/?ref=hn",
        {"email": "canon_attr@example.com"},
        HTTP_HOST=DASH, secure=True, HTTP_REFERER="https://dash.mailsocket.app/register/?ref=hn",
    )
    assert response.status_code == 200
    account = Account.objects.get(email_normalized="canon_attr@example.com")
    assert account.src_ref == "hn"
    assert account.src_referrer_host is None
    assert account.src_landing_path is None


def test_external_referer_direct_to_dash_register_recorded(canon):
    client = Client()
    form = client.get("/register/", HTTP_HOST=DASH, secure=True,
                      HTTP_REFERER="https://dev.to/some/post?x=1")
    assert '<input type="hidden" name="src_host" value="dev.to">' in form.content.decode()
    client.post("/register/", {"email": "canon_ref@example.com", "src_host": "dev.to"},
                HTTP_HOST=DASH, secure=True, HTTP_REFERER="https://dash.mailsocket.app/register/")
    assert Account.objects.get(email_normalized="canon_ref@example.com").src_referrer_host == "dev.to"


def test_ref_query_preserved_through_redirect_no_cookie_on_dash(canon):
    client = Client()
    hop = client.get("/register/?ref=devto", HTTP_HOST=APEX, secure=True)
    assert hop["Location"] == "https://dash.mailsocket.app/register/?ref=devto"
    page = client.get("/register/?ref=devto", HTTP_HOST=DASH, secure=True)
    assert attribution.COOKIE_NAME not in page.cookies


# --- cross-host links ------------------------------------------------------------


def test_public_pages_link_product_routes_absolutely(canon):
    landing = _get("/", APEX).content.decode()
    assert 'href="https://dash.mailsocket.app/login/"' in landing  # header Sign in
    assert landing.count('href="https://dash.mailsocket.app/register/"') == 2  # Start free x2
    assert '<link rel="canonical" href="https://mailsocket.app/">' in landing
    docs = _get("/docs/", APEX).content.decode()
    assert 'href="https://dash.mailsocket.app/register/"' in docs
    assert 'href="https://dash.mailsocket.app/app/"' in docs
    assert 'href="/pricing/"' in docs  # same-host links stay relative
    pricing = _get("/pricing/", APEX).content.decode()
    assert 'href="https://dash.mailsocket.app/register/' in pricing
    assert 'href="https://dash.mailsocket.app/app/settings/"' in _get("/docs/faq", APEX).content.decode()
    assert 'href="https://dash.mailsocket.app/app/settings/"' in _get("/security", APEX).content.decode()
    assert 'href="https://dash.mailsocket.app/register/"' in _get("/docs/try/", APEX).content.decode()


def test_auth_pages_brand_links_to_apex(canon):
    body = _get("/login/", DASH).content.decode()
    assert 'class="brand" href="https://mailsocket.app/"' in body
    assert 'href="/register/"' in body  # same-host link stays relative


def test_dashboard_pages_link_public_routes_absolutely(canon):
    client = Client()
    client.force_login(_verified_user("canon_dash"))
    usage = client.get("/app/usage/", HTTP_HOST=DASH, secure=True)
    assert usage.status_code == 200
    body = usage.content.decode()
    assert 'href="https://mailsocket.app/pricing/"' in body
    # the sidebar brand stays on dash ("/" -> /app/)
    assert 'class="brand" href="/"' in body


def test_llms_txt_points_docs_at_apex_and_api_at_dash(canon):
    body = _get("/llms.txt", DASH).content.decode()
    assert "API base: `https://dash.mailsocket.app/api/v1`" in body
    assert "- API reference: https://mailsocket.app/docs/api/" in body
    assert "- Full SDK and MCP docs: https://mailsocket.app/docs/#sdks" in body
    # only the machine-readable spec (never redirected) is advertised on dash
    assert [line for line in body.splitlines() if "https://dash.mailsocket.app/docs/" in line] == [
        "- Machine-readable OpenAPI 3.1 spec: https://dash.mailsocket.app/docs/openapi.yaml"
    ]
    assert _get("/docs/openapi.yaml", DASH).status_code == 200


def test_e006_flags_missing_dash_host_when_redirects_on():
    from types import SimpleNamespace

    from apps.accounts.checks import canonical_host_errors

    bad = SimpleNamespace(CANONICAL_HOST_REDIRECTS=True, APEX_HOST="mailsocket.app", DASHBOARD_HOST="dash.mailsocket.app",
                          ALLOWED_HOSTS=["mailsocket.app"], CSRF_TRUSTED_ORIGINS=["https://mailsocket.app"])
    ids = [e.id for e in canonical_host_errors(bad)]
    assert ids.count("mailsocket.E006") == 2
    good = SimpleNamespace(CANONICAL_HOST_REDIRECTS=True, APEX_HOST="mailsocket.app", DASHBOARD_HOST="dash.mailsocket.app",
                           ALLOWED_HOSTS=["dash.mailsocket.app", "mailsocket.app"],
                           CSRF_TRUSTED_ORIGINS=["https://dash.mailsocket.app", "https://mailsocket.app"])
    assert canonical_host_errors(good) == []
    off = SimpleNamespace(**{**bad.__dict__, "CANONICAL_HOST_REDIRECTS": False})
    assert canonical_host_errors(off) == []


def test_env_example_satisfies_e006():
    from pathlib import Path
    from types import SimpleNamespace

    from django.conf import settings as dj

    from apps.accounts.checks import canonical_host_errors

    env = {}
    for line in (Path(dj.BASE_DIR).parent / ".env.example").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    s = SimpleNamespace(CANONICAL_HOST_REDIRECTS=env.get("CANONICAL_HOST_REDIRECTS", "true").lower() == "true",
                        APEX_HOST="mailsocket.app", DASHBOARD_HOST="dash.mailsocket.app",
                        ALLOWED_HOSTS=env["ALLOWED_HOSTS"].split(","), CSRF_TRUSTED_ORIGINS=env["CSRF_TRUSTED_ORIGINS"].split(","))
    assert canonical_host_errors(s) == []
