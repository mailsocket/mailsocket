"Public API."
import re

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan
from apps.accounts.views import _safe_next

HOST = "mailsocket.app"

REJECTED = [
    "https://mailsocket.app/app/",  # same-host absolute
    "http://mailsocket.app/app/",
    "//mailsocket.app/app/",  # scheme-relative, same host
    "/\\evil.example",  # "/\" is read as "//" by browsers
    "/\\mailsocket.app/app/",
    "\\t/app",  # literal backslash-t
    "\t/app",  # leading TAB
    "/\t/evil.example",  # browsers strip TAB -> "//evil.example"
    "/app/\nx",
    "/app/ billing/",
    "/app/\x00",
    "javascript:alert(1)",
    "javascript://mailsocket.app/%0Aalert(1)",
    "app/billing/",  # no leading slash
    "https://evil.example/x",
]



BYPASS_VECTORS = [
    # "//", "/\", "\\" and friends
    "//evil.com",
    "/\\evil.com",
    "\\\\evil.com",
    "\\/evil.com",
    "///evil.com",
    "/app/\\evil",  # backslash anywhere
    # percent-encoded separators
    "%2F%2Fevil.com",
    "/%2F%2Fevil.com",
    "/%2f%2fevil.com",
    "/%2Fevil.com",  # decodes to "//evil.com"
    "/%5Cevil.com",
    "/%5cevil.com",
    "/%5C/evil.com",
    "/%255Cevil.com",  # double-encoded backslash
    "/%25255Cevil.com",  # triple-encoded
    "/%252F%252Fevil.com",
    "/app/%2F%2Fevil.com",  # inner "//" after decode
    "/%2525252F%2525252Fevil.com",  # beyond the decode cap -> refused
    # tab / newline / CR (raw and encoded)
    "/\tevil.com",
    "/\n/evil.com",
    "/\r/evil.com",
    "/\r\n/evil.com",
    "/%09/evil.com",
    "/%0D%0A/evil.com",
    "/app/%0d%0aSet-Cookie:x=1",
    # unicode slash / backslash lookalikes
    "/\uff0fevil.com",  # FULLWIDTH SOLIDUS
    "/\u2044evil.com",  # FRACTION SLASH
    "/\u2215evil.com",  # DIVISION SLASH
    "/\u29f8evil.com",  # BIG SOLIDUS
    "/\uff3cevil.com",  # FULLWIDTH REVERSE SOLIDUS
    "/\u29f5evil.com",  # REVERSE SOLIDUS OPERATOR
    "/\ufe68evil.com",  # SMALL REVERSE SOLIDUS
    "/\u2216evil.com",  # SET MINUS
    "/%EF%BC%8Fevil.com",  # encoded FULLWIDTH SOLIDUS
    "/%E2%81%84evil.com",  # encoded FRACTION SLASH
    # whitespace inside
    "/ /evil.com",
    "/ /evil",
    "/%20/evil.com",
    "/\u3000/evil.com",
    "/\xa0/evil.com",
    "/\u2028/evil.com",
    "/\x85/evil.com",
    # schemes
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "https:evil.com",
    "https:/evil.com",
    # dot segments
    "/.//evil.com",
    "/..//evil.com",
    "/./evil.com",
    "/../evil.com",
    "/app/../billing/",
    "/app/./billing/",
    "/app/..",
    "/%2e%2e/%2fevil.com",
    "/app/%2E%2E/billing/",
]
ACCEPTED = [
    "/app/billing/?x=1",
    "/app/billing/",
    "/app/",
    "/app/inboxes/?q=a%20b",
    "/app/#frag",
    "/app/?next=//evil",  # inner next is only a query value
    "/app/...hidden/",  # "..." is not a dot segment
]

pytestmark = pytest.mark.django_db
override_host = override_settings(ALLOWED_HOSTS=[HOST, "testserver"])


def _req(method, value, secure):
    rf = RequestFactory()
    if method == "get":
        return rf.get("/login/", {"next": value}, HTTP_HOST=HOST, secure=secure)
    return rf.post("/login/", {"next": value}, HTTP_HOST=HOST, secure=secure)



# scheme-relative "//mailsocket.app/app/" whenever require_https was off.
@override_host
@pytest.mark.parametrize("secure", [True, False], ids=["https", "http"])
@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("value", REJECTED)
def test_safe_next_rejects_everything_but_a_relative_path(method, value, secure):
    assert _safe_next(_req(method, value, secure)) == ""


@override_host
@pytest.mark.parametrize("value", BYPASS_VECTORS)
def test_safe_next_rejects_decoded_and_confusable_bypasses(value):
    "Public API."
    for method in ("get", "post"):
        for secure in (True, False):
            assert _safe_next(_req(method, value, secure)) == "", (method, secure)


@override_host
@pytest.mark.parametrize("secure", [True, False], ids=["https", "http"])
@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("value", ACCEPTED)
def test_safe_next_accepts_a_same_site_relative_path(method, value, secure):
    assert _safe_next(_req(method, value, secure)) == value


def _user(name, pw):
    user = get_user_model().objects.create_user(username=name, email=f"{name}@example.test", password=pw)
    Account.objects.create(user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now())
    AccountPlan.objects.create(user=user, plan=AccountPlan.Plan.FREE)
    return user


@override_host
@pytest.mark.parametrize("value", REJECTED + BYPASS_VECTORS)
def test_login_never_redirects_to_a_non_relative_next(value, settings):
    pw = "pw-correct-horse-1"
    _user("f1login", pw)
    c = Client(HTTP_HOST=HOST)
    page = c.get(reverse("accounts:login"), {"next": value}, secure=True).content.decode()
    assert 'name="next"' not in page
    resp = c.post(
        reverse("accounts:login"),
        {"email": "f1login@example.test", "password": pw, "next": value},
        secure=True,
    )
    assert resp.status_code == 302
    assert resp["Location"] == settings.LOGIN_REDIRECT_URL


@override_host
def test_login_honours_a_relative_next_with_query():
    pw = "pw-correct-horse-1"
    _user("f1loginok", pw)
    c = Client(HTTP_HOST=HOST)
    resp = c.post(
        reverse("accounts:login"),
        {"email": "f1loginok@example.test", "password": pw, "next": "/app/billing/?x=1"},
        secure=True,
    )
    assert resp.status_code == 302 and resp["Location"] == "/app/billing/?x=1"


@override_host
@pytest.mark.parametrize("value", ["https://mailsocket.app/app/", "//mailsocket.app/app/", "/\\evil", "javascript:alert(1)"])
def test_register_and_verify_drop_a_non_relative_next(value, settings):
    pw = "correct horse battery staple 90210"
    c = Client(HTTP_HOST=HOST)
    reg_page = c.get(reverse("accounts:register"), {"next": value}, secure=True).content.decode()
    assert 'name="next"' not in reg_page
    c.post(
        reverse("accounts:register"),
        {"email": "f1reg@example.test", "next": value},
        secure=True,
    )
    link = mail.outbox[-1].body.strip().splitlines()[-1]
    assert "next=" not in link
    verify_path = re.sub(r"^https?://[^/]+", "", link)
    page = c.get(verify_path, {"next": value}, secure=True).content.decode()
    assert 'name="next"' not in page
    done = c.post(
        verify_path + "?next=x",
        {"new_password1": pw, "new_password2": pw, "next": value},
        secure=True,
    )
    assert done.status_code == 302
    assert done["Location"] == settings.LOGIN_REDIRECT_URL


@override_host
def test_login_honours_a_relative_next_with_encoded_query():
    pw = "pw-correct-horse-1"
    _user("f1loginq", pw)
    c = Client(HTTP_HOST=HOST)
    resp = c.post(
        reverse("accounts:login"),
        {"email": "f1loginq@example.test", "password": pw, "next": "/app/inboxes/?q=a%20b"},
        secure=True,
    )
    assert resp.status_code == 302 and resp["Location"] == "/app/inboxes/?q=a%20b"





def _local(link):
    return re.sub(r"^https?://[^/]+", "", link)


def _register_then_resend(c, email, pw, register_next, resend_next):
    c.post(
        reverse("accounts:register"),
        {"email": email, "next": register_next},
        secure=True,
    )
    first_link = mail.outbox[-1].body.strip().splitlines()[-1]
    body = {"email": email}
    if resend_next is not None:
        body["next"] = resend_next
    resend = c.post(reverse("accounts:resend-verification"), body, secure=True)
    assert resend.status_code == 200
    return first_link, mail.outbox[-1].body.strip().splitlines()[-1], resend.content.decode()


@override_host
def test_resend_carries_a_relative_next_through_verify_and_login():
    pw = "correct horse battery staple 90210"
    email = "f1resend@example.test"
    c = Client(HTTP_HOST=HOST)
    first_link, link, resend_page = _register_then_resend(c, email, pw, "/app/billing/", "/app/billing/")
    assert link != first_link  # a NEW token was minted
    assert "?next=%2Fapp%2Fbilling%2F" in link
    assert 'href="/login/?next=/app/billing/"' in resend_page
    verify = _local(link)
    page = c.get(verify, secure=True).content.decode()
    assert '<input type="hidden" name="next" value="/app/billing/">' in page
    done = c.post(verify, {"new_password1": pw, "new_password2": pw, "next": "/app/billing/"}, secure=True)
    # Verification signs the user in and lands on the validated next.
    assert done.status_code == 302 and done["Location"] == "/app/billing/"
    assert "_auth_user_id" in c.session


@override_host
@pytest.mark.parametrize(
    "value",
    ["https://evil.example/x", "//evil.example/x", "/\\evil.example", "/%2F%2Fevil.example", "/..//evil.example",
     "javascript:alert(1)"],
)
def test_resend_drops_a_foreign_next(value, settings):
    pw = "correct horse battery staple 90210"
    email = "f1resendevil@example.test"
    c = Client(HTTP_HOST=HOST)
    _, link, resend_page = _register_then_resend(c, email, pw, "/app/billing/", value)
    assert "next=" not in link
    assert "evil.example" not in resend_page and 'next=' not in resend_page
    verify = _local(link)
    done = c.post(verify, {"new_password1": pw, "new_password2": pw, "next": value}, secure=True)
    assert done.status_code == 302 and done["Location"] == settings.LOGIN_REDIRECT_URL


@override_host
def test_resend_without_next_emits_no_next():
    pw = "correct horse battery staple 90210"
    c = Client(HTTP_HOST=HOST)
    _, link, resend_page = _register_then_resend(c, "f1resendnone@example.test", pw, "", None)
    assert "next=" not in link and "next=" not in resend_page
