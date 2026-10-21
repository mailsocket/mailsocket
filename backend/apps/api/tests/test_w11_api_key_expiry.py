"Public API."
from datetime import timedelta
from unittest import mock

import pytest
from django.test import Client
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import ApiKey
from apps.accounts.services import create_api_key
from apps.api.tests.support import body_without_request_id
from apps.api.tests.test_api_v1 import make_message, make_user
from apps.inboxes.models import Inbox

pytestmark = pytest.mark.django_db(transaction=True)


def client_for(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


@pytest.fixture
def alice_with_inbox():
    alice = make_user("w11_alice")
    inbox = Inbox.objects.create(user=alice, routing_key="w11_alice_box", label="Alice")
    make_message(inbox, "W11 message")
    return alice, inbox


# --- P0 -----------------------------------------------------------------


def test_expired_key_rejected_uniform(alice_with_inbox):
    alice, _inbox = alice_with_inbox
    expired_key, expired_token = create_api_key(alice, "Expired")
    expired_key.expires_at = timezone.now() - timedelta(seconds=1)
    expired_key.save(update_fields=["expires_at"])

    revoked_key, revoked_token = create_api_key(alice, "Revoked")
    revoked_key.revoked_at = timezone.now()
    revoked_key.save(update_fields=["revoked_at"])

    expired_response = client_for(expired_token).get("/api/v1/inboxes")
    revoked_response = client_for(revoked_token).get("/api/v1/inboxes")
    wrong_response = client_for("ms_live_wrong_wrong_wrong_wrong_wrong").get("/api/v1/inboxes")

    assert expired_response.status_code == revoked_response.status_code == wrong_response.status_code == 401
    expired_body = body_without_request_id(expired_response)
    revoked_body = body_without_request_id(revoked_response)
    wrong_body = body_without_request_id(wrong_response)
    assert expired_body == revoked_body == wrong_body
    assert expired_response.headers["WWW-Authenticate"] == "Bearer"
    assert expired_response.json()["error"]["code"] == "authentication_required"
    assert expired_response.json()["error"]["message"] == "Authentication required."


def test_null_expiry_never_expires(alice_with_inbox):
    alice, _inbox = alice_with_inbox
    _key, token = create_api_key(alice, "Never expires")
    response = client_for(token).get("/api/v1/inboxes")
    assert response.status_code == 200


def test_boundary_exactly_now_rejected(alice_with_inbox):
    """Pins the boundary at exactly `<= now` using a frozen clock (unittest.mock.patch
    on django.utils.timezone.now, the mechanism already used in
    apps/accounts/tests/test_auth.py) so expires_at and the authentication-time
    now() observe the IDENTICAL instant. Without freezing, expires_at is captured
    before the request and authenticate() calls timezone.now() strictly later, so
    the key is already strictly expired by request time — a `<` vs `<=` regression
    would not be caught. The companion test below (frozen_now + 1us) proves the
    other side of the same boundary.
    """
    alice, _inbox = alice_with_inbox
    key, token = create_api_key(alice, "Boundary")
    frozen_now = timezone.now()
    key.expires_at = frozen_now
    key.save(update_fields=["expires_at"])
    with mock.patch("django.utils.timezone.now", return_value=frozen_now):
        response = client_for(token).get("/api/v1/inboxes")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_boundary_just_after_now_accepted(alice_with_inbox):
    """Companion to test_boundary_exactly_now_rejected: same frozen clock, but
    expires_at = frozen_now + 1 microsecond (still in the "future" at auth time).
    Together the pair pins the boundary predicate as exactly `<= now`: this test
    would fail if the code regressed to reject anything not strictly greater than
    now, and the sibling test would fail if the code regressed to `< now` only.
    """
    alice, _inbox = alice_with_inbox
    key, token = create_api_key(alice, "Boundary plus epsilon")
    frozen_now = timezone.now()
    key.expires_at = frozen_now + timedelta(microseconds=1)
    key.save(update_fields=["expires_at"])
    with mock.patch("django.utils.timezone.now", return_value=frozen_now):
        response = client_for(token).get("/api/v1/inboxes")
    assert response.status_code == 200


def test_future_expiry_key_works(alice_with_inbox):
    alice, _inbox = alice_with_inbox
    key, token = create_api_key(alice, "Future")
    key.expires_at = timezone.now() + timedelta(hours=1)
    key.save(update_fields=["expires_at"])
    response = client_for(token).get("/api/v1/inboxes")
    assert response.status_code == 200
    assert response.json()["data"]





@pytest.mark.parametrize("bad_days", [0, -5, "abc", 99999])
def test_create_invalid_days_422(bad_days):
    alice = make_user("w11_invalid_days")
    client = Client()
    client.force_login(alice)
    before = ApiKey.objects.filter(user=alice).count()
    response = client.post(
        "/app/api-keys/create/",
        {"name": "Bad expiry", "expires_in_days": bad_days},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 422
    assert ApiKey.objects.filter(user=alice).count() == before


def test_create_blank_days_omitted_mints_null():
    alice = make_user("w11_blank_days_omit")
    client = Client()
    client.force_login(alice)
    response = client.post(
        "/app/api-keys/create/", {"name": "Blank expiry omitted"}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 200
    row = ApiKey.objects.get(user=alice, name="Blank expiry omitted")
    assert row.expires_at is None


def test_create_blank_days_empty_string_mints_null():
    """A real browser form always posts the expires_in_days field, empty or not
    (e.g. an unfilled <input>), rather than omitting it entirely. Exercises the
    actual blank-value coercion path (expires_in_days="") instead of only the
    field-omitted path.
    """
    alice = make_user("w11_blank_days_empty")
    client = Client()
    client.force_login(alice)
    response = client.post(
        "/app/api-keys/create/",
        {"name": "Blank expiry empty", "expires_in_days": ""},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    row = ApiKey.objects.get(user=alice, name="Blank expiry empty")
    assert row.expires_at is None


def test_expired_key_does_not_touch_last_used(alice_with_inbox):
    alice, _inbox = alice_with_inbox
    key, token = create_api_key(alice, "Expired no touch")
    key.expires_at = timezone.now() - timedelta(seconds=1)
    key.save(update_fields=["expires_at"])
    assert key.last_used_at is None

    response = client_for(token).get("/api/v1/inboxes")
    assert response.status_code == 401
    key.refresh_from_db()
    assert key.last_used_at is None


def test_expiry_isolation():
    alice = make_user("w11_isolation_alice")
    bob = make_user("w11_isolation_bob")
    alice_key, _alice_token = create_api_key(alice, "Alice active")
    bob_key, _bob_token = create_api_key(bob, "Bob expiring")
    bob_key.expires_at = timezone.now() + timedelta(days=1)
    bob_key.save(update_fields=["expires_at"])

    client = Client()
    client.force_login(alice)
    page = client.get("/app/api-keys/")
    assert page.status_code == 200
    assert b"Alice active" in page.content
    assert alice_key.public_id.encode() in page.content
    assert b"Bob expiring" not in page.content
    assert bob_key.public_id.encode() not in page.content


@pytest.mark.parametrize(
    "method,path_suffix",
    [
        ("get", "/inboxes"),
        ("get", "/inboxes/{inbox_id}/messages"),
        ("get", "/inboxes/{inbox_id}/messages/wait"),
    ],
)
def test_multiple_endpoints_expired(alice_with_inbox, method, path_suffix):
    alice, inbox = alice_with_inbox
    key, token = create_api_key(alice, "Expired multi")
    key.expires_at = timezone.now() - timedelta(seconds=1)
    key.save(update_fields=["expires_at"])

    path = "/api/v1" + path_suffix.format(inbox_id=inbox.public_id)
    response = getattr(client_for(token), method)(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert response.json()["error"]["message"] == "Authentication required."
