"Public API."

import re

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.models import Account
from apps.accounts.multi_account_services import create_owned_account
from apps.accounts.services import create_product_user, register_agent, register_pending_signup
from apps.accounts.tokens import make_verification_token

pytestmark = pytest.mark.django_db

_CSRF_TOKEN_RE = re.compile(r'name="csrfmiddlewaretoken" value="[^"]*"')


def _normalise(response, typed_email):
    """Only CSRF + the typed address may differ between branches."""
    body = _CSRF_TOKEN_RE.sub('name="csrfmiddlewaretoken" value=""', response.content.decode())
    return body.replace(typed_email, "<typed>")


def _verified(email):
    user = create_product_user(email, "a long passphrase 4242")
    Account.objects.filter(user=user).update(email_verified_at=timezone.now())
    return user


def _reset(client, email, **extra):
    return client.post(reverse("accounts:password-reset"), {"email": email}, **extra)





def test_reset_confirmation_copy_names_typed_address():
    page = _reset(Client(), "Someone@Example.com").content.decode()
    assert "If the address can be used" not in page
    # The form normalises the address to lower case: the echo is that value.
    assert re.search(
        r"If there's an account for <strong class=\"check-email-address\"><!--email_off-->"
        r"someone@example.com<!--/email_off--></strong>, we sent a reset link\. "
        r"Check Spam/Junk if it doesn't arrive within a minute\.",
        page,
    ), page


def test_reset_confirmation_escapes_the_typed_address():
    # A syntactically valid but hostile-looking local part is escaped.
    page = _reset(Client(), '"<b>x</b>"@example.com').content.decode()
    assert "<b>x</b>" not in page


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_reset_confirmation_identical_across_every_branch():
    """No oracle: unknown / verified / pending / suspended / inactive /
    agent / workspace all render the same page apart from the echo."""
    verified = _verified("n4-verified@example.com")
    pending = register_pending_signup("n4-pending@example.com")
    suspended = _verified("n4-suspended@example.com")
    Account.objects.filter(user=suspended).update(is_suspended=True)
    inactive = _verified("n4-inactive@example.com")
    get_user_model().objects.filter(pk=inactive.pk).update(is_active=False)
    agent, _, _ = register_agent("n4")
    owner = _verified("n4-owner@example.com")
    workspace = create_owned_account(owner, "WS").account_user

    emails = [
        "n4-unknown@example.com",
        verified.email,
        pending.email,
        suspended.email,
        inactive.email,
        agent.account.email_normalized,
        workspace.account.email_normalized,
    ]
    responses = [(email, _reset(Client(), email)) for email in emails]
    statuses = {r.status_code for _, r in responses}
    assert statuses == {200}
    bodies = {_normalise(r, email) for email, r in responses}
    assert len(bodies) == 1, "reset confirmation differs between account states"


def test_reset_confirmation_rate_limited_body_is_identical_too():
    email = "n4-limited@example.com"
    _verified(email)
    client = Client()
    responses = [_reset(client, email) for _ in range(8)]
    limited = [r for r in responses if r.status_code == 429]
    assert limited, "rate limit never reached"
    assert _normalise(limited[-1], email) == _normalise(responses[0], email)


def test_reset_still_mails_only_real_accounts():
    _verified("n4-real@example.com")
    _reset(Client(), "n4-real@example.com")
    assert mail.outbox[-1].to == ["n4-real@example.com"]


# --- N5 ------------------------------------------------------------------------------


def _field_block(page, name):
    return re.search(rf'<p class="auth-field[^"]*">\s*<label for="id_{name}">.*?</p>', page, re.S).group(0)


def test_reset_form_uses_verify_form_labels_and_one_line_help():
    user = _verified("n5@example.com")
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    reset = Client().get(
        reverse("accounts:password-reset-confirm", kwargs={"uidb64": uid, "token": token})
    ).content.decode()

    pending = register_pending_signup("n5-verify@example.com")
    verify = Client().get(
        reverse("accounts:verify-email", kwargs={"token": make_verification_token(pending)})
    ).content.decode()

    for name in ("new_password1", "new_password2"):
        assert _field_block(reset, name) == _field_block(verify, name), name
    p1 = _field_block(reset, "new_password1")
    assert ">Password:</label>" in p1
    assert ">At least 8 characters. Avoid common passwords.</span>" in p1
    # Not Django's 4-line validator list.
    assert "too similar to your other personal information" not in reset
    assert "can’t be entirely numeric" not in reset and "can&#x27;t be entirely numeric" not in reset
    assert ">Confirm password:</label>" in _field_block(reset, "new_password2")


def test_reset_form_still_enforces_password_validators():
    user = _verified("n5-weak@example.com")
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    url = reverse("accounts:password-reset-confirm", kwargs={"uidb64": uid, "token": token})
    response = Client().post(url, {"new_password1": "12345678", "new_password2": "12345678"})
    assert response.status_code == 200
    assert "field-error" in response.content.decode()
    user.refresh_from_db()
    assert not user.check_password("12345678")
