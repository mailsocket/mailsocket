"Public API."

import re

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.models import Account, AccountPlan, RateLimitBucket
from apps.accounts.services import create_product_user, register_pending_signup
from apps.accounts.tokens import make_verification_token
from apps.inboxes.models import Inbox, Message

pytestmark = pytest.mark.django_db

PASSWORD = "correct horse battery staple 90210"
NEW_PASSWORD = "brand new horse battery 4471"
_CSRF_RE = re.compile(r'name="csrfmiddlewaretoken" value="[^"]*"')


def _norm(body: bytes, typed: str = "") -> str:
    text = _CSRF_RE.sub('name="csrfmiddlewaretoken" value=""', body.decode())
    return text.replace(typed, "<typed>") if typed else text


def _verified(email):
    user = create_product_user(email, PASSWORD)
    user.account.email_verified_at = timezone.now()
    user.account.save(update_fields=["email_verified_at", "updated_at"])
    return user


def _reset_url(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return reverse("accounts:password-reset-confirm", kwargs={"uidb64": uid, "token": token})


def _reset_state(email):
    users = get_user_model().objects.filter(email=email)
    AccountPlan.objects.filter(user__in=users).delete()
    Account.objects.filter(user__in=users).delete()
    users.delete()
    RateLimitBucket.objects.all().delete()





def test_reset_on_unverified_account_verifies_it_and_login_then_works(client):
    user = register_pending_signup("lost-verify@example.com")
    old_verify_token = make_verification_token(user)
    assert user.account.email_verified_at is None

    response = client.post(
        _reset_url(user), {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}
    )
    assert response.status_code == 200
    assert b"Password reset complete." in response.content

    account = Account.objects.get(user=user)
    assert account.email_verified_at is not None
    assert account.verification_nonce == ""
    # The pending verification link from before the reset is dead now.
    assert client.get(
        reverse("accounts:verify-email", kwargs={"token": old_verify_token})
    ).status_code == 400

    login = client.post(
        reverse("accounts:login"), {"email": "lost-verify@example.com", "password": NEW_PASSWORD}
    )
    assert login.status_code == 302 and login.url == settings.LOGIN_REDIRECT_URL


def test_reset_on_unverified_account_runs_onboarding_exactly_once(client):
    user = register_pending_signup("onboard-reset@example.com")
    client.post(_reset_url(user), {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
    assert Inbox.objects.filter(user=user, deleted_at__isnull=True).count() == 1
    assert Message.objects.filter(inbox__user=user).count() == 1

    # A second reset (now verified) must not provision again.
    user.refresh_from_db()
    client.post(_reset_url(user), {"new_password1": PASSWORD, "new_password2": PASSWORD})
    assert Inbox.objects.filter(user=user, deleted_at__isnull=True).count() == 1
    assert Message.objects.filter(inbox__user=user).count() == 1


def test_reset_on_verified_account_is_unchanged(client):
    user = _verified("already-ok@example.com")
    verified_at = user.account.email_verified_at
    response = client.post(
        _reset_url(user), {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}
    )
    assert response.status_code == 200
    account = Account.objects.get(user=user)
    assert account.email_verified_at == verified_at
    # No onboarding for an account that was already verified.
    assert not Inbox.objects.filter(user=user).exists()
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)


def test_reset_onboarding_failure_never_breaks_the_reset(client, monkeypatch):
    def boom(_user):
        raise RuntimeError("seed down")

    monkeypatch.setattr("apps.inboxes.services.provision_onboarding", boom)
    user = register_pending_signup("boom-reset@example.com")
    response = client.post(
        _reset_url(user), {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}
    )
    assert response.status_code == 200
    assert Account.objects.get(user=user).email_verified_at is not None


def test_reset_token_rechecked_under_lock_after_concurrent_resend(client):
    """A resend between GET-validation and the save rotates the password hash;
    the token is re-checked under the Account lock, so the stale reset is
    refused and the account stays unverified."""
    user = register_pending_signup("race-reset@example.com")
    url = _reset_url(user)

    from apps.accounts import views as account_views

    real_complete = account_views._complete_password_reset

    def resend_then_complete(form, form_user, token):
        # Simulate the resend landing first: the password hash changes.
        from apps.accounts.services import refresh_pending_verification

        refresh_pending_verification(form_user)
        return real_complete(form, form_user, token)

    account_views._complete_password_reset = resend_then_complete
    try:
        response = client.post(url, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
    finally:
        account_views._complete_password_reset = real_complete
    assert response.status_code == 400
    account = Account.objects.get(user=user)
    assert account.email_verified_at is None
    user.refresh_from_db()
    assert not user.has_usable_password()





def test_signin_failure_shows_verify_hint_identically_for_every_branch(client):
    register_pending_signup("pending-login@example.com")
    _verified("known-login@example.com")
    cases = {
        "unknown": "nobody-login@example.com",
        "pending": "pending-login@example.com",
        "wrong_password": "known-login@example.com",
    }
    bodies = {}
    for name, email in cases.items():
        response = client.post(reverse("accounts:login"), {"email": email, "password": "not-it-123456"})
        assert response.status_code == 200
        bodies[name] = _norm(response.content, email)
    assert bodies["unknown"] == bodies["pending"] == bodies["wrong_password"]
    page = bodies["unknown"]
    assert "Unable to sign in with those credentials." in page
    assert "Just signed up? Verify your email first" in page
    hint = re.search(r'<p class="auth-hint" id="signin-verify-hint">(.*?)</p>', page, re.S).group(1)
    assert f'<a href="{reverse("accounts:resend-verification")}">Resend verification email</a>' in hint
    # Enumeration-safe: the link never carries the address.
    assert "email=" not in hint


def test_signin_hint_also_on_rate_limited_failure_and_absent_without_error(client):
    assert "signin-verify-hint" not in client.get(reverse("accounts:login")).content.decode()
    for _ in range(6):
        response = client.post(
            reverse("accounts:login"), {"email": "limited@example.com", "password": "nope-123456"}
        )
    assert response.status_code == 429
    assert "signin-verify-hint" in response.content.decode()


def test_signin_hint_keeps_validated_next(client):
    response = client.post(
        reverse("accounts:login"),
        {"email": "x@example.com", "password": "nope-123456", "next": "/app/billing/"},
    )
    hint = re.search(r'id="signin-verify-hint">(.*?)</p>', response.content.decode(), re.S).group(1)
    assert "?next=/app/billing/" in hint





def test_csrf_failure_renders_branded_page_with_request_id():
    csrf_client = Client(enforce_csrf_checks=True)
    response = csrf_client.post(reverse("accounts:login"), {"email": "a@example.com", "password": "x"})
    assert response.status_code == 403
    page = response.content.decode()
    assert "Your session expired." in page
    assert "Reload the page and try again." in page
    assert 'href="/login/">Reload the page</a>' in page
    assert "CSRF verification failed" not in page
    assert "Reference: <code>" in page
    assert response.headers["X-Request-Id"] in page
    # CSP: no inline script/style.
    assert "<script" not in page and "<style" not in page and "style=" not in page


def test_csrf_failure_view_never_raises(monkeypatch):
    from django.template import loader

    from config import views as config_views

    def broken(*args, **kwargs):
        raise RuntimeError("template engine down")

    monkeypatch.setattr(loader, "get_template", broken)
    response = Client(enforce_csrf_checks=True).post(reverse("accounts:login"), {"email": "a@example.com"})
    assert response.status_code == 403
    assert response.content.decode() == config_views.STATIC_CSRF_HTML


def test_csrf_failure_view_is_the_configured_one():
    assert settings.CSRF_FAILURE_VIEW == "config.views.csrf_failure"


def test_api_paths_are_unaffected_by_csrf():
    """The JSON API is token auth and csrf-exempt: a cookie-less POST without a
    CSRF token gets the API's own JSON 401, never the CSRF page."""
    response = Client(enforce_csrf_checks=True).post(
        "/api/v1/inboxes", data="{}", content_type="application/json"
    )
    assert response.status_code == 401
    assert response["Content-Type"].startswith("application/json")
    assert "session expired" not in response.content.decode().lower()





def test_register_for_verified_address_mails_sign_in_and_reset_links(client):
    _verified("returning@example.com")
    response = client.post(reverse("accounts:register"), {"email": "returning@example.com"})
    assert response.status_code == 200
    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert sent.to == ["returning@example.com"]
    assert sent.subject == "Your mailsocket account"
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    assert f"Sign in: {base}{reverse('accounts:login')}" in sent.body
    assert f"Forgot password: {base}{reverse('accounts:password-reset')}" in sent.body
    assert "You already have a mailsocket account" in sent.body
    assert "If you didn't request this, ignore this email." in sent.body
    # Plain text only, and no credential/token of any kind in it.
    assert not sent.alternatives
    assert "verify-email" not in sent.body and "confirm" not in sent.body
    # Nothing about the account changed.
    account = Account.objects.get(email_normalized="returning@example.com")
    assert account.verification_nonce == ""
    assert get_user_model().objects.get(pk=account.user_id).check_password(PASSWORD)


def test_register_body_and_status_identical_with_already_registered_mail(client):
    email = "three-ways@example.com"
    bodies, statuses, sends = {}, {}, {}
    for name, setup in (
        ("unknown", lambda: None),
        ("pending", lambda: register_pending_signup(email)),
        ("verified", lambda: _verified(email)),
    ):
        _reset_state(email)
        setup()
        before = len(mail.outbox)
        response = client.post(reverse("accounts:register"), {"email": email})
        statuses[name] = response.status_code
        bodies[name] = _norm(response.content)
        sends[name] = len(mail.outbox) - before
    assert set(statuses.values()) == {200}
    assert bodies["unknown"] == bodies["pending"] == bodies["verified"]
    # Exactly one SMTP submission per branch (same send shape).
    assert sends == {"unknown": 1, "pending": 1, "verified": 1}


def test_register_for_suspended_verified_account_still_discards(client):
    user = _verified("suspended-reg@example.com")
    user.account.is_suspended = True
    user.account.save(update_fields=["is_suspended"])
    client.post(reverse("accounts:register"), {"email": "suspended-reg@example.com"})
    assert mail.outbox[-1].to == [settings.AUTH_DISCARD_EMAIL]


def test_already_registered_mail_respects_register_rate_limit(client):
    _verified("limit-reg@example.com")
    statuses = [
        client.post(reverse("accounts:register"), {"email": "limit-reg@example.com"}).status_code
        for _ in range(6)
    ]
    assert statuses[-1] == 429
    assert len(mail.outbox) == 5  # 5/15m per email+IP, unchanged





def test_replaced_link_copy_and_resend_form(client):
    user = register_pending_signup("replaced@example.com")
    first = make_verification_token(user)
    client.post(reverse("accounts:resend-verification"), {"email": "replaced@example.com"})
    response = client.get(reverse("accounts:verify-email", kwargs={"token": first}))
    assert response.status_code == 400
    page = response.content.decode()
    assert (
        "This link has expired or was replaced by a newer one. Use the most recent email "
        "from us, or request a new link below."
    ) in page
    assert 'class="resend-form"' in page


def test_check_email_copy_and_start_over_link(client):
    page = client.post(
        reverse("accounts:register"), {"email": "typo@example.com", "next": "/app/billing/"}
    ).content.decode()
    assert "can be used" not in page
    assert re.search(
        r"We sent a verification link to <strong[^>]*><!--email_off-->typo@example.com<!--/email_off--></strong>\. "
        r"If it doesn't arrive within a minute, check Spam/Junk or resend below\.",
        page,
    )
    register = reverse("accounts:register")
    assert f'<a href="{register}?next=/app/billing/">Wrong address? Start over</a>' in page


def _assert_plain_auth_mail(message, *, expiry):
    assert not message.alternatives  # plain text only
    body = message.body
    assert body.startswith("mailsocket is an inbox API")
    assert f"expires in {expiry}" in body
    assert "If you didn't request this, ignore this email." in body
    lines = [line for line in body.strip().splitlines() if line]
    assert 4 <= len(lines) <= 5
    # The link stays the last line (existing tests and mail clients rely on it).
    assert lines[-1].startswith(settings.PUBLIC_BASE_URL.rstrip("/") + "/")


def test_verification_mail_is_short_plain_and_reads_expiry_from_settings(client, settings):
    client.post(reverse("accounts:register"), {"email": "mailcopy@example.com"})
    _assert_plain_auth_mail(mail.outbox[-1], expiry="24 hours")
    settings.EMAIL_VERIFICATION_TIMEOUT = 2 * 3600
    _reset_state("mailcopy@example.com")
    client.post(reverse("accounts:register"), {"email": "mailcopy@example.com"})
    _assert_plain_auth_mail(mail.outbox[-1], expiry="2 hours")
    assert "/verify-email/" in mail.outbox[-1].body.strip().splitlines()[-1]


def test_reset_mail_is_short_plain_and_reads_expiry_from_settings(client, settings):
    _verified("resetcopy@example.com")
    client.post(reverse("accounts:password-reset"), {"email": "resetcopy@example.com"})
    _assert_plain_auth_mail(mail.outbox[-1], expiry="1 hour")
    settings.PASSWORD_RESET_TIMEOUT = 30 * 60
    RateLimitBucket.objects.all().delete()
    client.post(reverse("accounts:password-reset"), {"email": "resetcopy@example.com"})
    _assert_plain_auth_mail(mail.outbox[-1], expiry="30 minutes")


def test_human_duration():
    from apps.accounts.views import _human_duration

    assert _human_duration(86400) == "24 hours"
    assert _human_duration(3600) == "1 hour"
    assert _human_duration(7200) == "2 hours"
    assert _human_duration(172800) == "2 days"
    assert _human_duration(1800) == "30 minutes"
    assert _human_duration(45) == "45 seconds"


@pytest.mark.parametrize("which", ["verify", "reset"])
def test_set_password_labels_and_confirm_help(client, which):
    if which == "verify":
        user = register_pending_signup("labels@example.com")
        url = reverse("accounts:verify-email", kwargs={"token": make_verification_token(user)})
    else:
        url = _reset_url(_verified("labels-reset@example.com"))
    page = client.get(url).content.decode()
    assert '<label for="id_new_password1">Password:</label>' in page
    assert '<label for="id_new_password2">Confirm password:</label>' in page
    assert "Type the same password again." in page
    assert "New password confirmation" not in page
    assert "Enter the same password as before" not in page


def test_mismatched_passwords_still_rejected_on_reset(client):
    user = _verified("mismatch@example.com")
    response = client.post(_reset_url(user), {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD + "x"})
    assert response.status_code == 200
    assert "didn" in response.content.decode() and "match" in response.content.decode()
    user.refresh_from_db()
    assert user.check_password(PASSWORD)





@pytest.mark.parametrize("url_name", ["register", "login"])
def test_authenticated_register_and_login_redirect_to_safe_next(client, url_name):
    user = _verified(f"authed-{url_name}@example.com")
    client.force_login(user)
    url = reverse(f"accounts:{url_name}")
    response = client.get(url + "?next=/app/billing/")
    assert response.status_code == 302 and response.url == "/app/billing/"
    # No next -> LOGIN_REDIRECT_URL (was "/").
    response = client.get(url)
    assert response.url == settings.LOGIN_REDIRECT_URL
    # Foreign / scheme-relative next is refused.
    for evil in ("https://evil.example/", "//evil.example/", "/\\evil.example"):
        response = client.get(url, {"next": evil})
        assert response.url == settings.LOGIN_REDIRECT_URL
