"Public API."

import hashlib

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, AccountPlan
from apps.accounts.services import create_api_key
from apps.inboxes.models import Inbox, Message

pytestmark = pytest.mark.django_db(transaction=True)


def make_user(name):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now()
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
        "header_from": f"Sender <sender-{marker}@example.test>",
        "header_to": [inbox.address],
        "header_cc": [],
        "subject": marker,
        "text_body": f"text {marker}",
        "html_body": f"<p>{marker}</p>",
        "raw_bytes": raw,
        "raw_sha256": digest,
        "size_bytes": len(raw),
        "ingest_status": Message.IngestStatus.PARSED,
    }
    values.update(overrides)
    return Message.objects.create(**values)


def client_for(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


@pytest.fixture
def tenants():
    alice = make_user("mr_alice")
    bob = make_user("mr_bob")
    _, alice_token = create_api_key(alice, "Alice mark-read CI")
    _, bob_token = create_api_key(bob, "Bob mark-read CI")
    alice_inbox = Inbox.objects.create(user=alice, routing_key="mr_alice_box")
    bob_inbox = Inbox.objects.create(user=bob, routing_key="mr_bob_box")
    alice_message = make_message(alice_inbox, "alice-msg")
    bob_message = make_message(bob_inbox, "bob-secret")
    return {
        "alice_token": alice_token,
        "bob_token": bob_token,
        "alice_inbox": alice_inbox,
        "bob_inbox": bob_inbox,
        "alice_message": alice_message,
        "bob_message": bob_message,
    }


def test_1_patch_read_true_sets_read_at(tenants):
    client = client_for(tenants["alice_token"])
    message = tenants["alice_message"]

    response = client.patch(
        f"/api/v1/messages/{message.public_id}", {"read": True}, format="json"
    )

    assert response.status_code == 200
    assert response.json()["data"]["read"] is True
    message.refresh_from_db()
    assert message.read_at is not None

    detail = client.get(f"/api/v1/messages/{message.public_id}")
    assert detail.json()["data"]["read"] is True


def test_2_patch_read_true_twice_is_idempotent(tenants):
    client = client_for(tenants["alice_token"])
    message = tenants["alice_message"]

    first = client.patch(f"/api/v1/messages/{message.public_id}", {"read": True}, format="json")
    message.refresh_from_db()
    first_read_at = message.read_at

    second = client.patch(f"/api/v1/messages/{message.public_id}", {"read": True}, format="json")
    message.refresh_from_db()

    assert first.status_code == second.status_code == 200
    assert message.read_at == first_read_at


def test_3_patch_read_false_after_true_clears_read_at(tenants):
    client = client_for(tenants["alice_token"])
    message = tenants["alice_message"]

    client.patch(f"/api/v1/messages/{message.public_id}", {"read": True}, format="json")
    response = client.patch(f"/api/v1/messages/{message.public_id}", {"read": False}, format="json")

    assert response.status_code == 200
    assert response.json()["data"]["read"] is False
    message.refresh_from_db()
    assert message.read_at is None


def test_4_patch_missing_or_non_bool_read_returns_422_not_500(tenants):
    client = client_for(tenants["alice_token"])
    message = tenants["alice_message"]

    missing = client.patch(f"/api/v1/messages/{message.public_id}", {}, format="json")
    assert missing.status_code == 422

    non_bool = client.patch(
        f"/api/v1/messages/{message.public_id}", {"read": "maybe"}, format="json"
    )
    assert non_bool.status_code == 422
    # audit 2026-10-11 sweep: a validation error envelope, and nothing written
    for resp in (missing, non_bool):
        assert resp.json()["error"]["code"] == "validation_error"
        assert "read" in resp.json()["error"]["fields"]
    message.refresh_from_db()
    assert message.read_at is None


def test_5_get_read_filter_true_false_all(tenants):
    client = client_for(tenants["alice_token"])
    inbox = tenants["alice_inbox"]
    read_msg = make_message(inbox, "read-one", read_at=timezone.now())
    unread_msg = tenants["alice_message"]

    only_read = client.get(f"/api/v1/inboxes/{inbox.public_id}/messages?read=true")
    assert {row["id"] for row in only_read.json()["data"]} == {read_msg.public_id}

    only_unread = client.get(f"/api/v1/inboxes/{inbox.public_id}/messages?read=false")
    assert {row["id"] for row in only_unread.json()["data"]} == {unread_msg.public_id}

    everything = client.get(f"/api/v1/inboxes/{inbox.public_id}/messages?read=all")
    assert {row["id"] for row in everything.json()["data"]} == {read_msg.public_id, unread_msg.public_id}

    default = client.get(f"/api/v1/inboxes/{inbox.public_id}/messages")
    assert {row["id"] for row in default.json()["data"]} == {read_msg.public_id, unread_msg.public_id}


def test_6_read_filter_pagination_cursor_stays_within_filter(tenants):
    client = client_for(tenants["alice_token"])
    inbox = tenants["alice_inbox"]
    for index in range(4):
        make_message(inbox, f"read-bulk-{index}", read_at=timezone.now())

    seen = set()
    cursor = None
    while True:
        url = f"/api/v1/inboxes/{inbox.public_id}/messages?read=true&limit=1"
        if cursor:
            url += f"&cursor={cursor}"
        page = client.get(url)
        assert page.status_code == 200
        rows = page.json()["data"]
        for row in rows:
            assert row["read"] is True
            seen.add(row["id"])
        cursor = page.json()["pagination"]["next_cursor"]
        if not cursor:
            break

    assert len(seen) == 4  # only the 4 bulk read messages, never the unread fixture message


def test_6b_cursor_minted_under_read_filter_rejected_on_replay_with_different_filter(tenants):
    "Public API."
    client = client_for(tenants["alice_token"])
    inbox = tenants["alice_inbox"]
    for index in range(2):
        make_message(inbox, f"read-cursor-{index}", read_at=timezone.now())

    minted = client.get(f"/api/v1/inboxes/{inbox.public_id}/messages?read=true&limit=1")
    assert minted.status_code == 200
    cursor = minted.json()["pagination"]["next_cursor"]
    assert cursor  # there must be a second page to mint a real cursor

    replay_false = client.get(
        f"/api/v1/inboxes/{inbox.public_id}/messages?read=false&limit=1&cursor={cursor}"
    )
    assert replay_false.status_code == 422
    assert replay_false.json()["error"]["code"] == "validation_error"

    replay_no_filter = client.get(
        f"/api/v1/inboxes/{inbox.public_id}/messages?limit=1&cursor={cursor}"
    )
    assert replay_no_filter.status_code == 422
    assert replay_no_filter.json()["error"]["code"] == "validation_error"


def test_7_tenant_negative_patch_foreign_message_returns_404_and_untouched(tenants):
    client = client_for(tenants["alice_token"])
    bob_message = tenants["bob_message"]

    response = client.patch(
        f"/api/v1/messages/{bob_message.public_id}", {"read": True}, format="json"
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    bob_message.refresh_from_db()
    assert bob_message.read_at is None


def test_8_tenant_negative_read_filter_never_leaks_foreign_messages(tenants):
    bob_inbox = tenants["bob_inbox"]

    response = client_for(tenants["alice_token"]).get(f"{'/api/v1/inboxes/' + bob_inbox.public_id}/messages?read=all")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
