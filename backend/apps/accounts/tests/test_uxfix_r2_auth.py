"Public API."

import re

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts import views as account_views
from apps.accounts.models import Account, RateLimitBucket
from apps.accounts.services import create_product_user, register_pending_signup
from apps.inboxes.models import Inbox, Message

pytestmark = pytest.mark.django_db

PASSWORD = "correct horse battery staple 90210"
NEW_PASSWORD = "brand new horse battery 4471"
INVALID = b"This reset link is invalid or expired."
_CSRF_RE = re.compile(r'name="csrfmiddlewaretoken" value="[^"]*"')


def _reset_url(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return reverse("accounts:password-reset-confirm", kwargs={"uidb64": uid, "token": token})


def _verified(email):
    user = create_product_user(email, PASSWORD)
    user.account.email_verified_at = timezone.now()
    user.account.save(update_fields=["email_verified_at", "updated_at"])
    return user


# --- 4. reset confirm refuses suspended / inactive ----------------------------


def _suspend(user):
    Account.objects.filter(user=user).update(is_suspended=True)


def _deactivate(user):
    get_user_model().objects.filter(pk=user.pk).update(is_active=False)


@pytest.mark.parametrize("disable", [_suspend, _deactivate], ids=["suspended", "inactive"])
def test_reset_confirm_refuses_suspended_or_inactive_unverified_account(client, disable):
    user = register_pending_signup("r2-susp@example.com")
    url = _reset_url(user)  # link issued BEFORE the suspension/deactivation
    disable(user)

    # Same 400 invalid-link response as a bad token, on GET and POST.
    bad_token = reverse(
        "accounts:password-reset-confirm",
        kwargs={"uidb64": urlsafe_base64_encode(force_bytes(user.pk)), "token": "bad-token"},
    )
    reference = client.get(bad_token)
    get = client.get(url)
    post = client.post(url, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
    for response in (get, post):
        assert response.status_code == 400
        assert INVALID in response.content
        assert response.content == reference.content

    account = Account.objects.get(user=user)
    assert account.email_verified_at is None  # not verified
    assert account.verification_nonce != ""
    assert not Inbox.objects.filter(user=user).exists()  # no onboarding
    assert not Message.objects.filter(inbox__user=user).exists()
    user.refresh_from_db()
    assert not user.has_usable_password()


def test_reset_confirm_refuses_suspension_that_lands_after_get_validation(client, monkeypatch):
    """The state is re-checked under the Account lock too."""
    user = register_pending_signup("r2-late-susp@example.com")
    url = _reset_url(user)
    real_complete = account_views._complete_password_reset

    def suspend_then_complete(form, form_user, token):
        _suspend(form_user)
        return real_complete(form, form_user, token)

    monkeypatch.setattr(account_views, "_complete_password_reset", suspend_then_complete)
    response = client.post(url, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
    assert response.status_code == 400 and INVALID in response.content
    assert Account.objects.get(user=user).email_verified_at is None
    assert not Inbox.objects.filter(user=user).exists()


def test_reset_confirm_on_active_unverified_account_still_verifies(client):
    "Public API."
    user = register_pending_signup("r2-ok@example.com")
    response = client.post(
        _reset_url(user), {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}
    )
    assert response.status_code == 200
    assert Account.objects.get(user=user).email_verified_at is not None
    assert Inbox.objects.filter(user=user).count() == 1


# --- 5. per-normalized-email cap on the already-registered mail ---------------


def _norm(body: bytes) -> str:
    return _CSRF_RE.sub('name="csrfmiddlewaretoken" value=""', body.decode())


def _register_from(client, email, ip):
    return client.post(reverse("accounts:register"), {"email": email}, HTTP_CF_CONNECTING_IP=ip)


def test_already_registered_mail_capped_per_email_across_ips(client):
    email = "r2-flood@example.com"
    _verified(email)
    cap = account_views.ALREADY_REGISTERED_MAIL_LIMIT
    assert cap == 3

    statuses, bodies, sends, recipients = [], [], [], []
    for i in range(cap + 3):  # every request from a fresh IP: per-IP limits never trip
        before = len(mail.outbox)
        response = _register_from(client, email, f"198.51.100.{i + 1}")
        statuses.append(response.status_code)
        bodies.append(_norm(response.content))
        sends.append(len(mail.outbox) - before)
        recipients.append(mail.outbox[-1].to)

    assert statuses == [200] * (cap + 3)
    assert len(set(bodies)) == 1  # no body oracle
    assert sends == [1] * (cap + 3)  # exactly one SMTP submission every time
    assert recipients[:cap] == [[email]] * cap
    assert recipients[cap:] == [[settings.AUTH_DISCARD_EMAIL]] * 3  # over the cap: discard sink
    assert sum(1 for m in mail.outbox if m.to == [email]) == cap


def test_already_registered_cap_is_no_oracle_against_unknown_and_pending(client):
    """Over the cap, the verified branch is indistinguishable from unknown and
    pending: same status, same body, one submission each."""
    verified, pending, unknown = "r2-v@example.com", "r2-p@example.com", "r2-u@example.com"
    _verified(verified)
    register_pending_signup(pending)
    for i in range(account_views.ALREADY_REGISTERED_MAIL_LIMIT):
        _register_from(client, verified, f"203.0.113.{i + 1}")

    results = {}
    for name, email, ip in (
        ("verified", verified, "192.0.2.10"),
        ("pending", pending, "192.0.2.11"),
        ("unknown", unknown, "192.0.2.12"),
    ):
        before = len(mail.outbox)
        response = _register_from(client, email, ip)
        results[name] = (
            response.status_code,
            _norm(response.content).replace(email, "<typed>"),
            len(mail.outbox) - before,
        )
    assert results["verified"] == results["pending"] == results["unknown"]
    assert results["verified"][0] == 200 and results["verified"][2] == 1


def test_already_registered_cap_not_charged_for_unknown_or_pending(client):
    """The cap only meters real nudge mails; it never charges other branches."""
    register_pending_signup("r2-pend-only@example.com")
    _register_from(client, "r2-pend-only@example.com", "192.0.2.50")
    _register_from(client, "r2-nobody@example.com", "192.0.2.51")
    from apps.accounts.services import _bucket_hash

    names = [
        _bucket_hash(account_views.ALREADY_REGISTERED_MAIL_RATE_NAME, e)
        for e in ("r2-pend-only@example.com", "r2-nobody@example.com")
    ]
    assert not RateLimitBucket.objects.filter(bucket_hash__in=names).exists()
