from unittest import mock

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.entitlements import plan_limits
from apps.accounts.models import Account, AccountPlan, ApiKey
from apps.accounts.services import register_pending_signup
from apps.accounts.tokens import make_verification_token
from apps.inboxes.models import Inbox, Message
from apps.inboxes.services import provision_onboarding

pytestmark = pytest.mark.django_db

PASSWORD = "correct horse battery staple 90210"
WELCOME_OTP = "481920"


def _register_and_verify(client, email="onboard@example.com"):
    user = register_pending_signup(email)
    token = make_verification_token(user)
    response = client.post(
        reverse("accounts:verify-email", kwargs={"token": token}),
        {"new_password1": PASSWORD, "new_password2": PASSWORD},
    )
    # Verification rotates the password; refresh so the object's auth hash
    # (session _auth_user_hash) matches the DB row on the next request.
    user.refresh_from_db()
    return user, response


def test_first_verification_auto_creates_inbox_and_welcome_message(client):
    user, response = _register_and_verify(client)

    assert response.status_code == 302
    account = Account.objects.get(pk=user.pk)
    assert account.email_verified_at is not None

    inboxes = Inbox.objects.filter(user=user, deleted_at__isnull=True)
    assert inboxes.count() == 1
    inbox = inboxes.get()
    assert inbox.label == "My first inbox"

    messages = Message.objects.filter(inbox=inbox)
    assert messages.count() == 1
    message = messages.get()
    assert message.ingest_status == Message.IngestStatus.PARSED
    assert message.detected_otp == WELCOME_OTP
    assert message.otp == WELCOME_OTP
    assert message.magic_link
    assert "example.com/auth/magic" in message.magic_link


def test_provision_onboarding_is_idempotent():
    user = register_pending_signup("idem@example.com")
    user.account.email_verified_at = timezone.now()
    user.account.save(update_fields=["email_verified_at", "updated_at"])

    provision_onboarding(user)
    provision_onboarding(user)

    assert Inbox.objects.filter(user=user, deleted_at__isnull=True).count() == 1
    inbox = Inbox.objects.get(user=user, deleted_at__isnull=True)
    assert Message.objects.filter(inbox=inbox).count() == 1


def test_second_verify_does_not_create_second_inbox_or_message(client):
    user, first = _register_and_verify(client)
    assert first.status_code == 302

    # A re-send/re-verify path cannot re-activate: the account is now verified
    # so the pending signup is gone, and re-provisioning is guarded.
    provision_onboarding(user)

    assert Inbox.objects.filter(user=user, deleted_at__isnull=True).count() == 1
    inbox = Inbox.objects.get(user=user, deleted_at__isnull=True)
    assert Message.objects.filter(inbox=inbox).count() == 1


def test_auto_provision_respects_free_plan_inbox_limit(client):
    user, response = _register_and_verify(client)
    assert response.status_code == 302
    # Auto-provision creates exactly one inbox; it counts toward the Free cap
    
    assert AccountPlan.objects.get(user=user).plan == AccountPlan.Plan.FREE
    assert Inbox.objects.filter(user=user, deleted_at__isnull=True).count() == 1
    cap = plan_limits(AccountPlan.Plan.FREE)["inboxes"]

    browser = Client()
    browser.force_login(user)
    for i in range(cap - 1):
        ok = browser.post(
            reverse("dashboard:inbox-create"), {"label": f"extra-{i}"}, HTTP_HX_REQUEST="true"
        )
        assert ok.status_code == 200, i
    create = browser.post(
        reverse("dashboard:inbox-create"),
        {"label": "over"},
        HTTP_HX_REQUEST="true",
    )
    assert create.status_code == 409
    assert Inbox.objects.filter(user=user, deleted_at__isnull=True).count() == cap


def test_onboarding_api_key_one_click_reveals_plaintext_once(client):
    user, _ = _register_and_verify(client)

    browser = Client()
    browser.force_login(user)
    response = browser.post(
        reverse("dashboard:api-key-onboarding"),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    body = response.content.decode()
    assert "ms_live_" in body
    token = body.split("ms_live_", 1)[1].split("<", 1)[0]
    token = f"ms_live_{token}"

    key = ApiKey.objects.get(user=user)
    assert key.name == "Onboarding key"
    assert key.key_hash
    # Plaintext is never persisted.
    assert token not in key.__dict__.values()

    # A refresh (GET overview) does not re-reveal the plaintext.
    overview = browser.get(reverse("dashboard:overview"))
    assert overview.status_code == 200
    assert token.encode() not in overview.content


def test_seed_failure_is_fail_silent_account_still_verified(client):
    user = register_pending_signup("failseed@example.com")
    token = make_verification_token(user)

    with mock.patch(
        "apps.inboxes.services.seed_welcome_message",
        side_effect=RuntimeError("seed boom"),
    ):
        response = client.post(
            reverse("accounts:verify-email", kwargs={"token": token}),
            {"new_password1": PASSWORD, "new_password2": PASSWORD},
        )

    assert response.status_code == 302
    account = Account.objects.get(pk=user.pk)
    assert account.email_verified_at is not None
    # Inbox still provisioned; seeding failed silently.
    assert Inbox.objects.filter(user=user, deleted_at__isnull=True).count() == 1
    inbox = Inbox.objects.get(user=user, deleted_at__isnull=True)
    assert Message.objects.filter(inbox=inbox).count() == 0

    # Verification signed the user in; sign out, then password login works.
    client.post(reverse("accounts:logout"))
    login = client.post(
        reverse("accounts:login"),
        {"email": "failseed@example.com", "password": PASSWORD},
    )
    assert login.status_code == 302


def test_snippet_endpoint_matches_real_wait_route(client):
    user, _ = _register_and_verify(client)
    inbox = Inbox.objects.get(user=user, deleted_at__isnull=True)

    browser = Client()
    browser.force_login(user)
    response = browser.post(
        reverse("dashboard:api-key-onboarding"),
        HTTP_HX_REQUEST="true",
    )

    body = response.content.decode()
    expected_path = f"/api/v1/inboxes/{inbox.public_id}/messages/wait"
    assert expected_path in body
    # The rendered route is the real wait endpoint, not a hard-coded guess.
    real_route = reverse("api:inbox-message-wait", kwargs={"public_id": inbox.public_id})
    assert real_route == expected_path
    assert "Authorization: Bearer ms_live_" in body
