"Public API."

import re
from pathlib import Path
from unittest import mock

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan, RateLimitBucket
from apps.accounts.services import create_product_user, register_pending_signup
from apps.accounts.tokens import make_verification_token

pytestmark = pytest.mark.django_db

PASSWORD = "correct horse battery staple 90210"
REPO_ROOT = Path(__file__).resolve().parents[4]
_CSRF_RE = re.compile(r'name="csrfmiddlewaretoken" value="[^"]*"')


def _norm(body: bytes) -> str:
    return _CSRF_RE.sub('name="csrfmiddlewaretoken" value=""', body.decode())


def _link_path(message) -> str:
    link = message.body.strip().splitlines()[-1]
    return re.sub(r"^https?://[^/]+", "", link)


def _verified(email):
    user = create_product_user(email, PASSWORD)
    user.account.email_verified_at = timezone.now()
    user.account.save(update_fields=["email_verified_at", "updated_at"])
    return user


def _reset_state(email):
    users = get_user_model().objects.filter(email=email)
    AccountPlan.objects.filter(user__in=users).delete()
    Account.objects.filter(user__in=users).delete()
    users.delete()
    RateLimitBucket.objects.all().delete()


# --- A. register = email only ------------------------------------------------


def test_register_page_has_no_password_fields_and_new_copy(client):
    page = client.get(reverse("accounts:register")).content.decode()
    assert 'type="password"' not in page
    assert 'name="password1"' not in page and 'name="password2"' not in page
    assert "<h1>Create your account</h1>" in page
    assert ">Start free</button>" in page  
    assert "We&#x27;ll email you a link to set your password." in page


def test_register_post_with_email_only_creates_pending_user_with_unusable_password(client):
    response = client.post(reverse("accounts:register"), {"email": "only-email@example.com"})
    assert response.status_code == 200
    user = get_user_model().objects.get(email="only-email@example.com")
    assert not user.has_usable_password()
    assert user.account.email_verified_at is None
    assert user.account.verification_nonce
    assert len(mail.outbox) == 1
    assert "_auth_user_id" not in client.session


def test_register_still_blocks_internal_domains(client):
    response = client.post(reverse("accounts:register"), {"email": "x@agent.mailsocket.internal"})
    assert response.status_code == 200
    assert not Account.objects.exists()
    assert len(mail.outbox) == 0


# --- B. "Check your email" page ------------------------------------------------


def test_check_email_page_renders_resend_form_with_typed_email(client):
    page = client.post(
        reverse("accounts:register"), {"email": "typed@example.com", "next": "/app/billing/"}
    ).content.decode()
    assert "<h1>Check your email</h1>" in page
    assert "typed@example.com" in page and "check Spam/Junk or resend below" in page
    form = re.search(r'<form method="post" action="([^"]+)" class="resend-form">(.*?)</form>', page, re.S)
    assert form.group(1) == reverse("accounts:resend-verification")
    inner = form.group(2)
    assert 'name="csrfmiddlewaretoken"' in inner
    assert '<input type="hidden" name="email" value="typed@example.com">' in inner
    assert '<input type="hidden" name="next" value="/app/billing/">' in inner
    assert 'id="resend-button"' in inner and ">Resend email</button>" in inner
    assert "Go to sign in</a>" in page
    # countdown is an external static file (CSP: no inline script/style)
    assert re.search(r'<script\b[^>]*\bsrc="[^"]*/dashboard/resend-countdown\.min\.js"', page)
    assert "<script>" not in page and "style=" not in page
    js = (Path(settings.BASE_DIR) / "apps/dashboard/static/dashboard/resend-countdown.js").read_text()
    assert 'getElementById("resend-button")' in js
    assert "dataset.cooldown" in js
    assert "button.disabled = true" in js and "button.disabled = false" in js
    assert 'data-cooldown="60"' in page


def test_check_email_page_escapes_the_typed_email(client):
    # A syntactically valid address carrying markup characters in a quoted
    # local part is echoed escaped, never as raw HTML.
    page = client.post(
        reverse("accounts:register"), {"email": '"<b>x</b>"@example.com'}
    ).content.decode()
    assert "<b>x</b>" not in page
    assert "&lt;b&gt;x&lt;/b&gt;" in page


@pytest.mark.parametrize("url_name", ["register", "resend-verification"])
def test_check_email_page_is_byte_identical_across_branches(client, url_name):
    """Same typed email, every account state -> same status and body."""
    email = "branches@example.com"

    def setup_none():
        pass

    def setup_pending():
        register_pending_signup(email)

    def setup_verified():
        _verified(email)

    bodies, statuses = {}, {}
    for name, setup in (("unknown", setup_none), ("pending", setup_pending), ("verified", setup_verified)):
        _reset_state(email)
        setup()
        response = client.post(reverse(f"accounts:{url_name}"), {"email": email, "next": "/app/"})
        statuses[name] = response.status_code
        bodies[name] = _norm(response.content)
    assert set(statuses.values()) == {200}
    assert bodies["unknown"] == bodies["pending"] == bodies["verified"]
    assert "<h1>Check your email</h1>" in bodies["unknown"]
    assert 'name="email" value="branches@example.com"' in bodies["unknown"]


def test_resend_is_still_rate_limited(client):
    register_pending_signup("limited@example.com")
    for _ in range(5):
        assert client.post(
            reverse("accounts:resend-verification"),
            {"email": "limited@example.com"},
            REMOTE_ADDR="203.0.113.77",
        ).status_code == 200
    sent = len(mail.outbox)
    blocked = client.post(
        reverse("accounts:resend-verification"),
        {"email": "limited@example.com"},
        REMOTE_ADDR="203.0.113.77",
    )
    assert blocked.status_code == 429
    assert len(mail.outbox) == sent
    assert "<h1>Check your email</h1>" in blocked.content.decode()


def test_old_token_invalid_after_resend(client):
    client.post(reverse("accounts:register"), {"email": "rotate@example.com"})
    old = _link_path(mail.outbox[-1])
    client.post(reverse("accounts:resend-verification"), {"email": "rotate@example.com"})
    new = _link_path(mail.outbox[-1])
    assert old != new
    assert client.get(old).status_code == 400
    assert client.post(old, {"new_password1": PASSWORD, "new_password2": PASSWORD}).status_code == 400
    assert client.get(new).status_code == 200


# --- C. verify link page ---------------------------------------------------------


def test_verify_page_copy_and_single_help_line(client):
    user = register_pending_signup("copy@example.com")
    page = client.get(
        reverse("accounts:verify-email", kwargs={"token": make_verification_token(user)})
    ).content.decode()
    assert "<h1>Set your password</h1>" in page
    assert "Verify email and choose a password" not in page
    assert ">Set password and continue</button>" in page
    assert "At least 8 characters. Avoid common passwords." in page
    assert "<ul>" not in page  # Django's validator bullet list is gone
    assert '<p class="auth-for">for <strong>copy@example.com</strong></p>' in page


def test_verify_still_enforces_password_validators(client):
    user = register_pending_signup("weak@example.com")
    url = reverse("accounts:verify-email", kwargs={"token": make_verification_token(user)})
    response = client.post(url, {"new_password1": "password", "new_password2": "password"})
    assert response.status_code == 200
    assert 'class="field-errors"' in response.content.decode()
    user.refresh_from_db()
    assert user.account.email_verified_at is None
    assert not user.has_usable_password()


def test_verify_success_logs_in_rotates_session_and_redirects_to_safe_next(client):
    user = register_pending_signup("login@example.com")
    session = client.session
    session["pre_verify_marker"] = "x"
    session.save()
    old_key = session.session_key
    url = reverse("accounts:verify-email", kwargs={"token": make_verification_token(user)})
    with mock.patch("apps.inboxes.services.provision_onboarding") as onboarding:
        response = client.post(
            url, {"new_password1": PASSWORD, "new_password2": PASSWORD, "next": "/app/billing/"}
        )
    assert response.status_code == 302 and response["Location"] == "/app/billing/"
    assert client.session["_auth_user_id"] == str(user.pk)
    assert client.session.session_key != old_key
    assert "pre_verify_marker" not in client.session  # flushed, not just cycled
    onboarding.assert_called_once()
    assert client.get(reverse("dashboard:overview")).status_code == 200


@pytest.mark.parametrize("evil", ["https://evil.example/x", "//evil.example/x", "/\\evil.example"])
def test_verify_success_rejects_external_next(client, evil):
    user = register_pending_signup("evilnext@example.com")
    url = reverse("accounts:verify-email", kwargs={"token": make_verification_token(user)})
    response = client.post(url, {"new_password1": PASSWORD, "new_password2": PASSWORD, "next": evil})
    assert response.status_code == 302
    assert response["Location"] == settings.LOGIN_REDIRECT_URL


def test_verify_token_is_single_use(client):
    user = register_pending_signup("once@example.com")
    url = reverse("accounts:verify-email", kwargs={"token": make_verification_token(user)})
    assert client.post(url, {"new_password1": PASSWORD, "new_password2": PASSWORD}).status_code == 302
    other = Client()
    assert other.get(url).status_code == 400
    again = other.post(url, {"new_password1": "another horse 77", "new_password2": "another horse 77"})
    assert again.status_code == 400
    assert "_auth_user_id" not in other.session
    user.refresh_from_db()
    assert user.check_password(PASSWORD)


def test_expired_or_invalid_link_page_has_resend_form(client):
    response = client.get(reverse("accounts:verify-email", kwargs={"token": "not-a-token"}))
    assert response.status_code == 400
    page = response.content.decode()
    assert "This link has expired or was replaced by a newer one." in page
    form = re.search(r'<form method="post" action="([^"]+)" class="resend-form">(.*?)</form>', page, re.S)
    assert form.group(1) == reverse("accounts:resend-verification")
    assert '<input type="email" name="email"' in form.group(2)
    assert 'name="csrfmiddlewaretoken"' in form.group(2)


# --- D. sign-in page -> resend page -------------------------------------------


def test_sign_in_links_to_resend_page_and_get_resend_renders(client):
    login_page = client.get(reverse("accounts:login")).content.decode()
    href = reverse("accounts:resend-verification")
    assert f'<a href="{href}">Didn\'t get the verification email?</a>' in login_page
    response = client.get(href)
    assert response.status_code == 200
    page = response.content.decode()
    assert "<h1>Resend verification email</h1>" in page
    assert 'name="email"' in page and 'type="email"' in page
    assert re.search(r'<form method="post">', page)
    assert ">Resend email</button>" in page
    assert len(mail.outbox) == 0
    assert not RateLimitBucket.objects.exists()  # GET never touches limits


# --- E. postfix outbound -------------------------------------------------------


def test_postfix_outbound_envelope_and_tls():
    canonical = (REPO_ROOT / "deploy/postfix/sender-canonical").read_text().split()
    assert canonical == ["noreply@mailsocket.app", "bounce@mailsocket.app"]
    main_cf = (REPO_ROOT / "deploy/postfix/main.cf").read_text()
    assert re.search(r"^smtp_tls_security_level = may$", main_cf, re.M)
    assert re.search(r"^smtp_tls_CAfile = /etc/ssl/certs/ca-certificates.crt$", main_cf, re.M)
    assert re.search(r"^smtp_tls_loglevel = 1$", main_cf, re.M)
    # inbound TLS untouched
    assert re.search(r"^smtpd_tls_security_level = none$", main_cf, re.M)


def test_hidden_attribute_wins_over_auth_helptext_display():
    css = (Path(settings.BASE_DIR) / "apps/dashboard/static/dashboard/app.css").read_text()
    assert ".auth-card [hidden]{display:none}" in css
