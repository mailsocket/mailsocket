"Public API."
import re

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, AccountPlan
from apps.accounts.services import acquire_wait_capacity, acquire_wait_lease, create_api_key
from apps.inboxes.models import Inbox

pytestmark = pytest.mark.django_db(transaction=True)

_REQUEST_ID_RE = re.compile(r"^req_[A-Za-z0-9_-]{20}$")


def make_user(name, *, plan=AccountPlan.Plan.PAID):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user, plan=plan)
    return user


def auth_client(user):
    _, token = create_api_key(user, "trust-api-test")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


# --- #15: X-Request-Id on every response -------------------------------------


def test_request_id_generated_when_absent():
    owner = make_user("reqid_gen")
    client = auth_client(owner)

    response = client.get("/api/v1/inboxes")

    assert response.status_code == 200
    assert _REQUEST_ID_RE.match(response["X-Request-Id"])


def test_request_id_reused_when_valid():
    owner = make_user("reqid_reuse")
    client = auth_client(owner)
    incoming = "my-valid-request-id-123"

    response = client.get("/api/v1/inboxes", HTTP_X_REQUEST_ID=incoming)

    assert response["X-Request-Id"] == incoming


@pytest.mark.parametrize(
    "incoming",
    [
        "short",  # < 8 chars
        "has a space",
        "has/a/slash",
        "x" * 65,  # > 64 chars
        "",
    ],
)
def test_request_id_regenerated_when_invalid(incoming):
    owner = make_user(f"reqid_bad_{abs(hash(incoming))}")
    client = auth_client(owner)

    response = client.get("/api/v1/inboxes", HTTP_X_REQUEST_ID=incoming)

    assert response["X-Request-Id"] != incoming
    assert _REQUEST_ID_RE.match(response["X-Request-Id"])


@pytest.mark.parametrize(
    "incoming",
    [
        "abcdefgh\n",
        "abcdefgh\r\nX-Evil: 1",
        "abcdefgh\r",
    ],
)
def test_request_id_crlf_injection_rejected(incoming):
    """A trailing/embedded CR or LF must never be trusted (`$` matches before
    a final \\n; header injection via \\r\\n must not reach the response
    headers). Regenerated instead, and no BadHeaderError/500."""
    owner = make_user(f"reqid_crlf_{abs(hash(incoming))}")
    client = auth_client(owner)

    response = client.get("/api/v1/inboxes", HTTP_X_REQUEST_ID=incoming)

    assert response.status_code == 200
    assert response["X-Request-Id"] != incoming
    assert _REQUEST_ID_RE.match(response["X-Request-Id"])


def test_request_id_present_on_html_dashboard_response_too():
    """Non-API routes go through the same middleware, so they also get a
    (generated) X-Request-Id — just no request_id in a JSON error body since
    there isn't one."""
    client = Client()
    response = client.get("/")
    assert _REQUEST_ID_RE.match(response["X-Request-Id"])


def test_request_id_embedded_in_error_envelope_and_matches_header():
    client = APIClient()
    response = client.get("/api/v1/inboxes")  # no auth -> 401

    assert response.status_code == 401
    header_id = response["X-Request-Id"]
    body_id = response.json()["error"]["request_id"]
    assert header_id == body_id
    assert _REQUEST_ID_RE.match(body_id)


def test_request_id_distinct_per_request():
    owner = make_user("reqid_distinct")
    client = auth_client(owner)

    first = client.get("/api/v1/inboxes")["X-Request-Id"]
    second = client.get("/api/v1/inboxes")["X-Request-Id"]
    assert first != second


# --- #16: unmatched /api/* paths return the JSON envelope, not HTML 404 ------


def test_unmatched_api_path_returns_json_404():
    client = APIClient()
    response = client.get("/api/v1/this-route-does-not-exist")

    assert response.status_code == 404
    assert response["Content-Type"].startswith("application/json")
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert "request_id" in body["error"]


def test_unmatched_api_v2_path_returns_json_404():
    client = APIClient()
    response = client.get("/api/v2/inboxes")

    assert response.status_code == 404
    assert response["Content-Type"].startswith("application/json")
    assert response.json()["error"]["code"] == "not_found"


def test_unmatched_non_api_path_still_returns_html_404():
    client = Client()
    response = client.get("/this-route-does-not-exist-either")

    assert response.status_code == 404
    assert response["Content-Type"].startswith("text/html")
    assert b"Page not found" in response.content


# --- #36: Retry-After on both wait 429s + the SSE equivalent ----------------


def test_too_many_wait_requests_429_carries_retry_after(monkeypatch):
    monkeypatch.setattr("apps.accounts.services.WAIT_LEASE_LIMIT_FREE", 1)
    owner = make_user("retry_after_lease", plan=AccountPlan.Plan.FREE)
    inbox = Inbox.objects.create(user=owner, routing_key="retry_after_lease_box")
    client = auth_client(owner)

    assert acquire_wait_lease(owner.pk) is True  # occupy the sole slot

    response = client.get(f"/api/v1/inboxes/{inbox.public_id}/messages/wait?timeout=1")

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "too_many_wait_requests"
    assert "Retry-After" in response
    assert int(response["Retry-After"]) > 0


def test_wait_capacity_429_carries_retry_after(monkeypatch):
    monkeypatch.setattr("apps.accounts.services.WAIT_CAPACITY_LIMIT", 1)
    owner = make_user("retry_after_cap")
    inbox = Inbox.objects.create(user=owner, routing_key="retry_after_cap_box")
    client = auth_client(owner)

    assert acquire_wait_capacity() is True  # fill the sole global slot

    response = client.get(f"/api/v1/inboxes/{inbox.public_id}/messages/wait?timeout=1")

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "wait_capacity"
    assert "Retry-After" in response
    assert int(response["Retry-After"]) > 0


def test_sse_stream_wait_capacity_429_carries_retry_after(monkeypatch):
    monkeypatch.setattr("apps.accounts.services.WAIT_CAPACITY_LIMIT", 1)
    monkeypatch.setattr("apps.accounts.services.WAIT_CAPACITY_FREE_LIMIT", 1)
    owner = make_user("retry_after_sse", plan=AccountPlan.Plan.PAID)
    inbox = Inbox.objects.create(user=owner, routing_key="retry_after_sse_box")

    other = make_user("retry_after_sse_filler")
    assert acquire_wait_lease(other.pk) is True
    assert acquire_wait_capacity(free=False) is True  # fill the sole global slot

    client = auth_client(owner)
    response = client.get(
        f"/api/v1/inboxes/{inbox.public_id}/stream?since=0&require=otp",
        HTTP_ACCEPT="text/event-stream",
    )

    assert response.status_code == 429
    assert "Retry-After" in response
    assert int(response["Retry-After"]) > 0
