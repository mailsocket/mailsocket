import hashlib
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core import signing
from django.test import Client
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, AccountPlan, ApiKey, RateLimitBucket
from apps.accounts.services import create_api_key, hash_api_key
from apps.api.pagination import CURSOR_SALT
from apps.inboxes.models import Inbox, Message

pytestmark = pytest.mark.django_db(transaction=True)


INBOX_FIELDS = {"id", "label", "address", "is_enabled", "created_at", "updated_at", "metadata"}
SUMMARY_FIELDS = {
    "id", "inbox_id", "from", "to", "cc", "subject", "received_at", "status", "otp",
    "otp_confidence", "magic_link", "links", "detected_otp", "has_html",
    "otp_context", "otp_candidates", "provider_hint", "read", "metadata",
}
MESSAGE_FIELDS = {
    "id", "inbox_id", "envelope_from", "envelope_recipient", "from", "to", "cc",
    "subject", "text", "html", "received_at", "status", "otp", "otp_confidence",
    "magic_link", "links", "detected_otp",
    "otp_context", "otp_candidates", "provider_hint", "read", "metadata",
}
NOT_FOUND = {"error": {"code": "not_found", "message": "Resource not found.", "fields": {}}}


def _without_request_id(body):
    """#15: every error envelope now carries a
    per-request request_id, so comparisons against a fixed expected body
    assert on everything else and check request_id is present+non-empty
    separately."""
    error = dict(body.get("error", {}))
    request_id = error.pop("request_id", None)
    assert request_id, f"expected a request_id in the error envelope, got {body!r}"
    return {**body, "error": error}


def make_user(name, *, verified=True, suspended=False):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user,
        email_normalized=f"{name}@example.test",
        email_verified_at=timezone.now() if verified else None,
        is_suspended=suspended,
    )
    AccountPlan.objects.create(user=user, plan=AccountPlan.Plan.PAID)
    return user


def make_message(inbox, marker, **overrides):
    raw = f"Subject: {marker}\r\n\r\nbody {marker}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    values = {
        "inbox": inbox,
        "dedupe_key": digest,
        "envelope_from": "sender@example.test",
        "envelope_recipient": inbox.address,
        "header_from": "Sender <sender@example.test>",
        "header_to": [inbox.address],
        "header_cc": [],
        "subject": marker,
        "text_body": f"text {marker}",
        "html_body": f"<p>{marker}</p>",
        "otp": "482193",
        "otp_confidence": 0.95,
        "magic_link": "https://example.test/login?token=abcdefghijklmnopqrstuv",
        "links": ["https://example.test/login?token=abcdefghijklmnopqrstuv"],
        "detected_otp": "482193",
        "raw_bytes": raw,
        "raw_sha256": digest,
        "size_bytes": len(raw),
        "ingest_status": Message.IngestStatus.PARSED,
    }
    values.update(overrides)
    return Message.objects.create(**values)


@pytest.fixture
def tenants():
    alice = make_user("api_alice")
    bob = make_user("api_bob")
    alice_key, alice_token = create_api_key(alice, "Alice CI")
    bob_key, bob_token = create_api_key(bob, "Bob CI")
    alice_inbox = Inbox.objects.create(user=alice, routing_key="api_alice_box", label="Alice")
    bob_inbox = Inbox.objects.create(user=bob, routing_key="api_bob_box", label="Bob")
    alice_message = make_message(alice_inbox, "Alice message")
    bob_message = make_message(bob_inbox, "Bob secret message")
    return {
        "alice": alice, "bob": bob, "alice_key": alice_key, "bob_key": bob_key,
        "alice_token": alice_token, "bob_token": bob_token, "alice_inbox": alice_inbox,
        "bob_inbox": bob_inbox, "alice_message": alice_message, "bob_message": bob_message,
    }


def client_for(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def test_cases_19_20_key_creation_one_time_hash_only_and_dashboard_csrf(tenants):
    user = tenants["alice"]
    client = Client()
    client.force_login(user)
    response = client.post("/app/api-keys/create/", {"name": "Deploy"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    body = response.content.decode()
    token = body.split("ms_live_", 1)[1].split("<", 1)[0]
    token = f"ms_live_{token}"
    assert len(token.removeprefix("ms_live_")) >= 43
    row = ApiKey.objects.get(user=user, name="Deploy")
    assert row.key_hash == hash_api_key(token)
    assert token not in row.__dict__.values()
    assert token.encode() not in client.get("/app/api-keys/").content
    assert response.headers["Cache-Control"] == "private, no-store"

    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(user)
    assert csrf.post("/app/api-keys/create/", {"name": "No CSRF"}).status_code == 403


def test_cases_21_22_23_auth_forbidden_revoke_and_last_used_throttle(tenants):
    client = client_for(tenants["alice_token"])
    assert client.get("/api/v1/inboxes").status_code == 200
    key = tenants["alice_key"]
    key.refresh_from_db()
    first_used = key.last_used_at
    assert first_used is not None
    assert client.get("/api/v1/inboxes").status_code == 200
    key.refresh_from_db()
    assert key.last_used_at == first_used

    browser = Client()
    browser.force_login(tenants["alice"])
    assert browser.post(f"/app/api-keys/{key.public_id}/revoke/").status_code == 200
    assert browser.post(f"/app/api-keys/{tenants['bob_key'].public_id}/revoke/").status_code == 404
    response = client.get("/api/v1/inboxes")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert client_for(tenants["bob_token"]).get("/api/v1/inboxes").status_code == 200

    for header in (None, "Basic abc", "Bearer", "Bearer ms_live_unknown_unknown_unknown"):
        probe = APIClient()
        if header:
            probe.credentials(HTTP_AUTHORIZATION=header)
        result = probe.get("/api/v1/inboxes")
        assert result.status_code == 401
        assert result.json()["error"]["code"] == "authentication_required"
        assert result.headers["WWW-Authenticate"] == "Bearer"

    for kwargs in ({"verified": False}, {"suspended": True}):
        owner = make_user(f"blocked_{len(ApiKey.objects.all())}", **kwargs)
        _, token = create_api_key(owner, "Blocked")
        assert client_for(token).get("/api/v1/inboxes").status_code == 403


def test_api_key_without_account_plan_returns_403():
    owner = make_user("missing_plan")
    _, token = create_api_key(owner, "Missing plan")
    AccountPlan.objects.filter(user=owner).delete()

    response = client_for(token).get("/api/v1/inboxes")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "account_forbidden"


def test_case_24_full_idor_matrix_returns_identical_404(tenants):
    client = client_for(tenants["alice_token"])
    foreign_inbox = tenants["bob_inbox"].public_id
    foreign_message = tenants["bob_message"].public_id
    probes = [
        ("get", f"/api/v1/inboxes/{foreign_inbox}"),
        ("delete", f"/api/v1/inboxes/{foreign_inbox}"),
        ("get", f"/api/v1/inboxes/{foreign_inbox}/messages"),
        ("get", f"/api/v1/inboxes/{foreign_inbox}/messages/latest"),
        ("get", f"/api/v1/messages/{foreign_message}"),
    ]
    missing = [
        ("get", "/api/v1/inboxes/inbox_missing"),
        ("delete", "/api/v1/inboxes/inbox_missing"),
        ("get", "/api/v1/inboxes/inbox_missing/messages"),
        ("get", "/api/v1/inboxes/inbox_missing/messages/latest"),
        ("get", "/api/v1/messages/msg_missing"),
    ]
    for (method, url), (missing_method, missing_url) in zip(probes, missing, strict=True):
        foreign = getattr(client, method)(url)
        unknown = getattr(client, missing_method)(missing_url)
        assert foreign.status_code == unknown.status_code == 404
        assert _without_request_id(foreign.json()) == _without_request_id(unknown.json()) == NOT_FOUND
    tenants["bob_inbox"].refresh_from_db()
    assert tenants["bob_inbox"].deleted_at is None


def test_cases_25_26_contracts_soft_delete_and_no_internal_leaks(tenants):
    client = client_for(tenants["alice_token"])
    listing = client.get("/api/v1/inboxes")
    assert listing.status_code == 200
    assert set(listing.json()) == {"data", "pagination"}
    assert set(listing.json()["data"][0]) == INBOX_FIELDS

    created = client.post("/api/v1/inboxes", {"label": "CI signup"}, format="json")
    assert created.status_code == 201
    assert set(created.json()["data"]) == INBOX_FIELDS
    assert created.headers["Location"].endswith(created.json()["data"]["id"])
    inbox_id = created.json()["data"]["id"]
    detail = client.get(f"/api/v1/inboxes/{inbox_id}")
    assert set(detail.json()["data"]) == INBOX_FIELDS | {"message_count_month", "webhook_configured"}
    assert client.delete(f"/api/v1/inboxes/{inbox_id}").status_code == 204
    assert client.delete(f"/api/v1/inboxes/{inbox_id}").status_code == 404

    inbox = tenants["alice_inbox"]
    summary = client.get(f"/api/v1/inboxes/{inbox.public_id}/messages")
    assert set(summary.json()["data"][0]) == SUMMARY_FIELDS
    for url in (
        f"/api/v1/messages/{tenants['alice_message'].public_id}",
        f"/api/v1/inboxes/{inbox.public_id}/messages/latest",
    ):
        full = client.get(url)
        assert full.status_code == 200
        assert set(full.json()["data"]) == MESSAGE_FIELDS
        encoded = str(full.json())
        for forbidden in ("raw_bytes", "raw_sha256", "parse_error", "dedupe_key", "key_hash"):
            assert forbidden not in encoded
    empty = Inbox.objects.create(user=tenants["alice"], routing_key="api_empty_box")
    assert client.get(f"/api/v1/inboxes/{empty.public_id}/messages/latest").status_code == 404


def test_case_27_cursor_integrity_binding_expiry_and_stable_order(tenants, monkeypatch):
    inbox = tenants["alice_inbox"]
    stamp = timezone.now() - timedelta(minutes=1)
    for index in range(4):
        make_message(inbox, f"page-{index}", received_at=stamp)
    client = client_for(tenants["alice_token"])
    first = client.get(f"/api/v1/inboxes/{inbox.public_id}/messages?limit=2")
    cursor = first.json()["pagination"]["next_cursor"]
    second = client.get(f"/api/v1/inboxes/{inbox.public_id}/messages?limit=2&cursor={cursor}")
    ids = [row["id"] for row in first.json()["data"] + second.json()["data"]]
    assert len(ids) == len(set(ids)) == 4
    assert client.get(f"/api/v1/inboxes/{inbox.public_id}/messages?cursor={cursor}x").status_code == 422
    assert client.get(f"/api/v1/inboxes?cursor={cursor}").status_code == 422
    assert client_for(tenants["bob_token"]).get(f"/api/v1/inboxes/{tenants['bob_inbox'].public_id}/messages?cursor={cursor}").status_code == 422
    assert client.get("/api/v1/inboxes?limit=0").status_code == 422

    payload = signing.loads(cursor, salt=CURSOR_SALT)
    expired = signing.dumps(payload, salt=CURSOR_SALT)
    monkeypatch.setattr("apps.api.pagination.CURSOR_MAX_AGE", -1)
    assert client.get(f"/api/v1/inboxes/{inbox.public_id}/messages?cursor={expired}").status_code == 422


def test_case_28_error_shapes_rate_headers_429_and_per_key_isolation(tenants):
    client = client_for(tenants["alice_token"])
    response = client.get("/api/v1/inboxes")
    for header in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"):
        assert header in response
    minute = RateLimitBucket.objects.order_by("expires_at").first()
    RateLimitBucket.objects.filter(pk=minute.pk).update(count=120)
    limited = client.get("/api/v1/inboxes")
    assert limited.status_code == 429
    assert _without_request_id(limited.json()) == {
        "error": {"code": "rate_limited", "message": "Rate limit exceeded.", "fields": {}}
    }
    assert "Retry-After" in limited
    assert client_for(tenants["bob_token"]).get("/api/v1/inboxes").status_code == 200

    malformed = client_for(tenants["bob_token"]).post(
        "/api/v1/inboxes", data="{secret-token", content_type="application/json"
    )
    assert malformed.status_code == 422
    assert "secret-token" not in str(malformed.json())


def test_case_29_valid_dashboard_session_is_not_rest_auth(tenants):
    client = Client()
    client.force_login(tenants["alice"])
    response = client.get("/api/v1/inboxes")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_unsupported_method_returns_uniform_405(tenants):
    response = client_for(tenants["alice_token"]).delete("/api/v1/inboxes")

    assert response.status_code == 405
    assert _without_request_id(response.json()) == {
        "error": {"code": "method_not_allowed", "message": "Method not allowed.", "fields": {}}
    }


def test_inbox_plan_limit_returns_distinct_409(tenants, monkeypatch):
    monkeypatch.setattr("apps.api.views.can_create_inbox", lambda user: False)

    response = client_for(tenants["alice_token"]).post(
        "/api/v1/inboxes", {"label": "Over limit"}, format="json"
    )

    assert response.status_code == 409
    assert _without_request_id(response.json()) == {
        "error": {"code": "plan_limit_reached", "message": "Plan limit reached.", "fields": {}}
    }


def test_query_count_sanity_does_not_grow_with_rows(tenants, django_assert_max_num_queries):
    for index in range(20):
        make_message(tenants["alice_inbox"], f"query-{index}")
    client = client_for(tenants["alice_token"])
    with django_assert_max_num_queries(8):
        response = client.get(f"/api/v1/inboxes/{tenants['alice_inbox'].public_id}/messages?limit=20")
    assert response.status_code == 200
    # audit 2026-10-11 sweep: the bounded query count is for a FULL page
    rows = response.json()["data"]
    assert len(rows) == 20
    assert len({row["id"] for row in rows}) == 20
