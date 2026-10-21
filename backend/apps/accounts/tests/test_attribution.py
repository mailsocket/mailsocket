"Public API."

import re
from datetime import timedelta
from urllib.parse import urlencode

import pytest
from django.core import signing
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts import attribution
from apps.accounts.models import Account

pytestmark = pytest.mark.django_db

COOKIE = "ms_src"
EMAIL = "person@example.com"

PUBLIC_PAGES = [
    "/",
    "/docs/",
    "/docs/try/",
    "/docs/guides/",
    "/docs/compare/mailpit",
    "/pricing/",
    "/legal/privacy",
    "/legal/terms",
    "/security",
    "/status",
    "/llms.txt",
    "/register/",
    "/login/",
]


def _account():
    return Account.objects.get(email_normalized=EMAIL)


def _register(client, path="/register/", data=None, **extra):
    return client.post(path, {"email": EMAIL, **(data or {})}, **extra)


# --- no cookie before signup ---------------------------------------------------


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_no_ms_src_set_cookie_on_any_public_page(path):
    client = Client()
    response = client.get(
        path, {"ref": "hn", "utm_source": "devto"}, HTTP_REFERER="https://news.ycombinator.com/item?id=1"
    )
    assert response.status_code < 400, path
    assert COOKIE not in response.cookies
    assert COOKIE not in client.cookies


def test_signup_post_sets_no_attribution_cookie():
    client = Client()
    response = _register(client, "/register/?ref=hn", HTTP_REFERER="https://dev.to/x")
    assert response.status_code == 200
    assert COOKIE not in response.cookies


def test_public_page_sets_no_cookie_at_all_for_anonymous_visit():
    # Consent-free: an anonymous visit to a public page sets nothing.
    for path in ("/", "/docs/", "/pricing/", "/legal/privacy"):
        response = Client().get(path + "?ref=hn", HTTP_REFERER="https://reddit.com/r/x")
        assert not response.cookies, (path, list(response.cookies))


# --- legacy cookie sweep -------------------------------------------------------


def test_old_cookie_gets_expired_on_any_response():
    client = Client()
    for path in ("/", "/docs/", "/register/", "/app/", "/healthz"):
        client.cookies[COOKIE] = "old-signed-value"
        morsel = client.get(path).cookies[COOKIE]
        assert morsel.value == ""
        assert int(morsel["max-age"]) == 0
        assert "1970" in morsel["expires"]
        assert morsel["path"] == "/"


def test_old_cookie_expired_on_parent_domain_in_prod(settings):
    settings.ALLOWED_HOSTS = ["mailsocket.app", "dash.mailsocket.app", "testserver"]
    for host in ("mailsocket.app", "dash.mailsocket.app"):
        client = Client()
        client.cookies[COOKIE] = "old"
        morsel = client.get("/healthz", HTTP_HOST=host, secure=True).cookies[COOKIE]
        assert morsel["domain"] == "mailsocket.app"  # must match the old Set-Cookie
        assert int(morsel["max-age"]) == 0


def test_old_cookie_sweep_stops_after_cutoff(settings):
    settings.LEGACY_MS_SRC_SWEEP_UNTIL = (timezone.now().date() - timedelta(days=1)).isoformat()
    client = Client()
    client.cookies[COOKIE] = "old"
    assert COOKIE not in client.get("/").cookies
    settings.LEGACY_MS_SRC_SWEEP_UNTIL = ""
    assert COOKIE not in client.get("/").cookies


@override_settings(LEGACY_MS_SRC_SWEEP_UNTIL="not-a-date")
def test_bad_sweep_date_disables_sweep_instead_of_crashing():
    client = Client()
    client.cookies[COOKIE] = "old"
    response = client.get("/")
    assert response.status_code == 200
    assert COOKIE not in response.cookies


def test_old_cookie_is_ignored_at_signup():
    client = Client()
    client.cookies[COOKIE] = signing.dumps(
        {"ref": "hn", "referrer_host": "news.ycombinator.com", "landing_path": "/"},
        salt="mailsocket.signup-attribution.v1",
        compress=True,
    )
    _register(client)
    account = _account()
    assert account.src_ref is None and account.src_referrer_host is None
    assert account.src_first_seen is None


# --- ref carried landing -> register -> Account ----------------------------------


@pytest.mark.parametrize(
    "path", ["/", "/docs/", "/docs/guides/playwright/", "/pricing/", "/docs/try/", "/docs/compare/mailpit"]
)
def test_public_page_carries_valid_ref_into_every_signup_link(path):
    plain = Client().get(path)
    assert plain.status_code == 200, path
    plain = plain.content.decode()
    links = re.findall(r'href="(/register/[^"]*)"', plain)
    assert links, path
    assert "ref=hn" not in plain
    tagged = Client().get(path, {"ref": "HN"}).content.decode()
    tagged_links = re.findall(r'href="(/register/[^"]*)"', tagged)
    assert len(tagged_links) == len(links)
    for link in tagged_links:
        assert link.endswith("ref=hn"), link


def test_pricing_paid_tier_link_keeps_next_and_adds_ref(settings):
    settings.ENABLE_BILLING = True
    body = Client().get("/pricing/?ref=devto").content.decode()
    assert 'href="/register/?next=/app/billing/&amp;ref=devto"' in body
    assert 'href="/register/?next=/app/billing/"' in Client().get("/pricing/").content.decode()


@pytest.mark.parametrize("path", ["/", "/docs/", "/pricing/"])
def test_page_without_ref_renders_byte_identical_to_invalid_ref(path):
    # No reflection: an invalid ref changes nothing on the page.
    clean = Client().get(path).content
    for bad in ("<script>", "a b", "hn.com", 'x"y', "x" * 3 + "/"):
        assert Client().get(path, {"ref": bad}).content == clean, bad


def test_ref_carried_landing_to_register_to_account():
    client = Client()
    landing = client.get("/docs/?ref=hn").content.decode()
    assert 'href="/register/?ref=hn"' in landing
    form = client.get("/register/?ref=hn")
    assert form.status_code == 200
    # The form posts back to its own URL (no action), so ?ref= survives.
    assert '<form method="post">' in form.content.decode()
    response = _register(client, "/register/?ref=hn")
    assert response.status_code == 200
    account = _account()
    assert account.src_ref == "hn"
    assert account.src_referrer_host is None
    assert account.src_landing_path is None
    assert account.src_first_seen is not None
    assert timezone.now() - account.src_first_seen < timedelta(minutes=1)


def test_auth_pages_carry_ref_into_their_own_signup_links():
    body = Client().get("/login/?ref=hn").content.decode()
    assert 'href="/register/?ref=hn">New here? Start free' in body
    body = Client().get("/login/?ref=hn&next=/app/billing/").content.decode()
    assert 'href="/register/?next=/app/billing/&amp;ref=hn">New here? Start free' in body
    assert 'href="/register/">New here? Start free' in Client().get("/login/").content.decode()


def test_utm_source_used_when_no_ref():
    _register(Client(), "/register/?utm_source=devto")
    assert _account().src_ref == "devto"


@pytest.mark.parametrize(
    "ref", ["<script>alert(1)</script>", "a b", "hn.com", "%3Cscript%3E", "ré", ""]
)
def test_invalid_ref_dropped(ref):
    client = Client()
    body = client.get("/", {"ref": ref}).content.decode()
    assert 'href="/register/"' in body
    _register(client, "/register/?" + urlencode({"ref": ref}))
    account = _account()
    assert account.src_ref is None and account.src_first_seen is None


def test_invalid_ref_dropped_but_external_referer_kept():
    _register(Client(), "/register/?ref=%3Cscript%3E", HTTP_REFERER="https://dev.to/post")
    account = _account()
    assert account.src_ref is None
    assert account.src_referrer_host == "dev.to"


def test_ref_length_capped_at_40():
    _register(Client(), "/register/?ref=" + "x" * 200)
    assert _account().src_ref == "x" * 40


def test_sanitize_ref_unit():
    assert attribution.sanitize_ref("N8N_Launch-1") == "n8n_launch-1"
    assert attribution.sanitize_ref("<script>") is None
    assert attribution.sanitize_ref(None) is None
    assert len(attribution.sanitize_ref("a" * 41)) == 40


# --- Referer host ------------------------------------------------------------------


def test_external_referer_on_register_get_carried_to_post():
    client = Client()
    form = client.get("/register/", HTTP_REFERER="https://News.YCombinator.com:443/item?id=123#c")
    body = form.content.decode()
    assert '<input type="hidden" name="src_host" value="news.ycombinator.com">' in body
    assert "id=123" not in body
    # The browser's POST Referer is the register page itself (own host).
    _register(client, data={"src_host": "news.ycombinator.com"}, HTTP_REFERER="http://testserver/register/")
    account = _account()
    assert account.src_referrer_host == "news.ycombinator.com"
    assert account.src_ref is None


def test_external_referer_on_register_post_recorded_host_only():
    _register(Client(), HTTP_REFERER="https://www.reddit.com/r/selfhosted/comments/abc?x=1")
    account = _account()
    assert account.src_referrer_host == "www.reddit.com"
    assert account.src_ref is None
    assert account.src_landing_path is None


@pytest.mark.parametrize(
    "referer",
    [
        "https://mailsocket.app/pricing/",
        "https://dash.mailsocket.app/app/",
        "https://www.mailsocket.app/",
        "http://testserver/docs/",  # same host as the request
    ],
)
def test_own_host_referrer_ignored(referer):
    client = Client()
    assert 'name="src_host"' not in client.get("/register/", HTTP_REFERER=referer).content.decode()
    _register(client, HTTP_REFERER=referer)
    account = _account()
    assert account.src_referrer_host is None and account.src_first_seen is None


@pytest.mark.parametrize(
    "posted", ["mailsocket.app", "dash.mailsocket.app", "testserver", "a/b", "<x>", "x" * 300]
)
def test_forged_or_own_src_host_field_dropped(posted):
    _register(Client(), data={"src_host": posted})
    assert _account().src_referrer_host is None


def test_lookalike_host_is_not_treated_as_own():
    _register(Client(), HTTP_REFERER="https://evilmailsocket.app/x")
    assert _account().src_referrer_host == "evilmailsocket.app"


def test_non_http_referer_ignored():
    _register(Client(), HTTP_REFERER="javascript:alert(1)")
    assert _account().src_first_seen is None


# --- landing path -------------------------------------------------------------------


def test_landing_path_from_allowlisted_next_only():
    _register(Client(), "/register/?ref=hn&next=/pricing/")
    assert _account().src_landing_path == "/pricing/"


@pytest.mark.parametrize(
    "next_value", ["/app/billing/", "/verify-email/abc/", "/docs/try/inbox/x", "https://evil.example/"]
)
def test_landing_path_empty_unless_allowlisted(next_value):
    _register(Client(), "/register/?ref=hn", data={"next": next_value})
    account = _account()
    assert account.src_ref == "hn"
    assert account.src_landing_path is None


def test_landing_path_from_from_param():
    _register(Client(), "/register/?ref=hn&from=/docs/")
    assert _account().src_landing_path == "/docs/"


def test_landing_path_alone_is_not_attribution():
    _register(Client(), "/register/?next=/pricing/")
    assert _account().src_first_seen is None


# --- first touch / no signal -----------------------------------------------------------


def test_signup_without_signal_leaves_fields_null():
    _register(Client())
    account = _account()
    assert account.src_ref is None and account.src_referrer_host is None
    assert account.src_landing_path is None and account.src_first_seen is None


def test_resubmitted_pending_signup_keeps_first_attribution():
    _register(Client(), "/register/?ref=hn")
    _register(Client(), "/register/?ref=reddit", HTTP_REFERER="https://reddit.com/r/x")
    account = _account()
    assert account.src_ref == "hn"
    assert account.src_referrer_host is None


# --- agent self-register -------------------------------------------------------------


def test_agent_register_tagged():
    response = APIClient().post(
        "/api/v1/agents/register?ref=hn", {"name": "bot"}, format="json",
        HTTP_REFERER="https://news.ycombinator.com/",
    )
    assert response.status_code == 201
    assert COOKIE not in response.cookies
    account = Account.objects.get(email_normalized__startswith="agent_")
    assert account.src_ref == "agent-register"
    assert account.src_referrer_host is None
    assert account.src_first_seen is not None


def test_register_url_reverse_unchanged():
    assert reverse("accounts:register") == "/register/"


@pytest.mark.django_db
def test_register_form_rejects_reserved_internal_domains():
    from apps.accounts.forms import RegisterForm

    for email in ("demo@mailsocket.internal", "x@agent.mailsocket.internal", "a@workspace.mailsocket.internal", "A@Foo.INTERNAL"):
        f = RegisterForm(data={"email": email})
        assert not f.is_valid() and "email" in f.errors, email
    ok = RegisterForm(data={"email": "dev@example.com"})
    assert ok.is_valid(), ok.errors
