"Public API."

import hashlib
import json

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, AccountPlan
from apps.accounts.services import create_api_key
from apps.inboxes.models import Inbox, Message
from apps.webhooks.services import (
    build_event_payload,
    derive_signing_secret,
    event_body_bytes,
    signature_header,
    verify_signature,
)

pytestmark = pytest.mark.django_db(transaction=True)


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
    _, token = create_api_key(user, "metadata-test")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def _make_message(inbox, marker, **overrides):
    raw = f"Subject: {marker}\r\n\r\nbody {marker}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    values = {
        "inbox": inbox,
        "dedupe_key": digest,
        "envelope_from": "security@acme.test",
        "envelope_recipient": inbox.address,
        "header_from": "Security <security@acme.test>",
        "header_to": [inbox.address],
        "header_cc": [],
        "subject": marker,
        "text_body": f"Your verification code is 482193. {marker}",
        "html_body": "",
        "raw_bytes": raw,
        "raw_sha256": digest,
        "size_bytes": len(raw),
        "ingest_status": Message.IngestStatus.PARSED,
        "otp": "482193",
        "otp_confidence": 0.95,
        "detected_otp": "482193",
    }
    values.update(overrides)
    return Message.objects.create(**values)





def test_m1_metadata_echoed_in_create_get_list_and_message_reads():
    user = make_user("m1_owner")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/inboxes",
        {"routing_key": "m1inbox", "metadata": {"run": "ci-123"}},
        format="json",
    )
    assert resp.status_code == 201
    assert resp.data["data"]["metadata"] == {"run": "ci-123"}
    inbox = Inbox.objects.get(routing_key="m1inbox", domain__isnull=True)

    resp_list = client.get("/api/v1/inboxes")
    assert resp_list.status_code == 200
    row = next(r for r in resp_list.data["data"] if r["id"] == inbox.public_id)
    assert row["metadata"] == {"run": "ci-123"}

    resp_get = client.get(f"/api/v1/inboxes/{inbox.public_id}")
    assert resp_get.status_code == 200
    assert resp_get.data["data"]["metadata"] == {"run": "ci-123"}

    message = _make_message(inbox, "m1 message")
    resp_msgs = client.get(f"/api/v1/inboxes/{inbox.public_id}/messages")
    assert resp_msgs.status_code == 200
    msg_row = next(r for r in resp_msgs.data["data"] if r["id"] == message.public_id)
    assert msg_row["metadata"] == {"run": "ci-123"}

    resp_latest = client.get(f"/api/v1/inboxes/{inbox.public_id}/messages/latest")
    assert resp_latest.status_code == 200
    assert resp_latest.data["data"]["metadata"] == {"run": "ci-123"}


def test_m1_metadata_echoed_for_random_routing_key_create_branch():
    user = make_user("m1b_owner")
    client = auth_client(user)
    resp = client.post("/api/v1/inboxes", {"metadata": {"run": "ci-456"}}, format="json")
    assert resp.status_code == 201
    assert resp.data["data"]["metadata"] == {"run": "ci-456"}


# --- M2: webhook payload contains metadata AND signature still verifies ----


def test_m2_webhook_payload_contains_metadata_and_signature_verifies():
    user = make_user("m2_owner")
    inbox = Inbox.objects.create(
        user=user, label="", routing_key="m2inbox", metadata={"run": "ci-789"}
    )
    message = _make_message(inbox, "m2 message")

    class _FakeWebhook:
        public_id = "wh_fake"
        secret_salt = "fakesalt"

    payload = build_event_payload(message, "evt_test123")
    assert payload["data"]["message"]["metadata"] == {"run": "ci-789"}

    secret = derive_signing_secret(_FakeWebhook())
    body = event_body_bytes(payload)
    header = signature_header(secret, 1234567890, body)
    assert verify_signature(secret, header, body) is True


# --- M3: size / type / key-count limits -------------------------------------


def test_m3_exactly_4096_bytes_is_accepted():
    user = make_user("m3a_owner")
    client = auth_client(user)
    # Build a compact-JSON payload of exactly 4096 bytes.
    padding_len = 4096 - len('{"k":""}')
    value = {"k": "x" * padding_len}
    assert len(json.dumps(value, separators=(",", ":")).encode("utf-8")) == 4096
    resp = client.post(
        "/api/v1/inboxes", {"routing_key": "m3ainbox", "metadata": value}, format="json"
    )
    assert resp.status_code == 201


def test_m3_4097_bytes_is_rejected():
    user = make_user("m3b_owner")
    client = auth_client(user)
    padding_len = 4097 - len('{"k":""}')
    value = {"k": "x" * padding_len}
    assert len(json.dumps(value, separators=(",", ":")).encode("utf-8")) == 4097
    resp = client.post(
        "/api/v1/inboxes", {"routing_key": "m3binbox", "metadata": value}, format="json"
    )
    assert resp.status_code == 422
    assert resp.data["error"]["code"] == "metadata_too_large"


def test_m3_non_dict_metadata_is_rejected():
    user = make_user("m3c_owner")
    client = auth_client(user)
    for bad in (["a", "b"], "a string", 42, True):
        resp = client.post(
            "/api/v1/inboxes", {"routing_key": f"m3cinbox{id(bad)}", "metadata": bad}, format="json"
        )
        assert resp.status_code == 422
        assert resp.data["error"]["code"] == "metadata_must_be_object"


def test_m3_51_top_level_keys_is_rejected():
    user = make_user("m3d_owner")
    client = auth_client(user)
    value = {f"k{i}": i for i in range(51)}
    resp = client.post(
        "/api/v1/inboxes", {"routing_key": "m3dinbox", "metadata": value}, format="json"
    )
    assert resp.status_code == 422
    assert resp.data["error"]["code"] == "metadata_too_large"


def test_m3_50_top_level_keys_is_accepted():
    user = make_user("m3e_owner")
    client = auth_client(user)
    value = {f"k{i}": i for i in range(50)}
    resp = client.post(
        "/api/v1/inboxes", {"routing_key": "m3einbox", "metadata": value}, format="json"
    )
    assert resp.status_code == 201
    # audit 2026-10-11 sweep: stored and echoed exactly
    assert resp.json()["data"]["metadata"] == value
    assert Inbox.objects.get(user=user, routing_key="m3einbox").metadata == value


# --- M4: omitted metadata defaults to {} everywhere -------------------------


def test_m4_omitted_metadata_defaults_to_empty_dict():
    user = make_user("m4_owner")
    client = auth_client(user)
    resp = client.post("/api/v1/inboxes", {"routing_key": "m4inbox"}, format="json")
    assert resp.status_code == 201
    assert resp.data["data"]["metadata"] == {}


def test_m4_pre_migration_style_row_reads_back_as_empty_dict():
    user = make_user("m4b_owner")
    # Simulate a pre-Batch-1a row: never set metadata explicitly, relying on
    # the model default exactly like an old row would after the migration.
    inbox = Inbox.objects.create(user=user, label="", routing_key="m4binbox")
    inbox.refresh_from_db()
    assert inbox.metadata == {}


# --- M5: unicode bytes, not chars -------------------------------------------


def test_m5_unicode_value_counted_by_utf8_bytes_not_chars():
    user = make_user("m5_owner")
    client = auth_client(user)
    
    # under naive limits but whose UTF-8 byte length exceeds 4096.
    char = "\u1ebf"
    # compact json overhead for {"k":"...."} is 8 bytes; want body > 4096 bytes.
    repeat = (4096 - 8) // len(char.encode("utf-8")) + 5
    value = {"k": char * repeat}
    byte_len = len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    assert byte_len > 4096
    resp = client.post(
        "/api/v1/inboxes", {"routing_key": "m5inbox", "metadata": value}, format="json"
    )
    assert resp.status_code == 422
    assert resp.data["error"]["code"] == "metadata_too_large"





def test_m6_metadata_absent_from_public_payload_even_when_set():
    from django.urls import reverse

    user = make_user("m6_owner")
    inbox = Inbox.objects.create(
        user=user,
        label="",
        routing_key="m6inbox",
        is_public=True,
        metadata={"secret": "leak-check"},
    )
    _make_message(inbox, "m6 message")

    url = reverse("api:public-inbox-messages", kwargs={"email": inbox.address})
    resp = APIClient().get(url)
    assert resp.status_code == 200
    body = str(resp.data)
    assert "metadata" not in resp.data["data"][0]
    assert "leak-check" not in body
