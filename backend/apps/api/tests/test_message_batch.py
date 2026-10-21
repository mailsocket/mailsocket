"Public API."

import hashlib

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, AccountPlan, RateLimitBucket
from apps.accounts.services import create_api_key
from apps.inboxes.models import Inbox, Message
from apps.webhooks.models import Webhook, WebhookDelivery
from apps.webhooks.services import enqueue_message_delivery

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
    alice = make_user("batch_alice")
    bob = make_user("batch_bob")
    _, alice_token = create_api_key(alice, "Alice batch CI")
    _, bob_token = create_api_key(bob, "Bob batch CI")
    alice_inbox = Inbox.objects.create(user=alice, routing_key="batch_alice_box")
    bob_inbox = Inbox.objects.create(user=bob, routing_key="batch_bob_box")
    return {
        "alice": alice,
        "bob": bob,
        "alice_token": alice_token,
        "bob_token": bob_token,
        "alice_inbox": alice_inbox,
        "bob_inbox": bob_inbox,
    }


BATCH_URL = "/api/v1/messages/batch"


def test_1_mark_read_three_valid_ids_are_idempotent(tenants):
    client = client_for(tenants["alice_token"])
    inbox = tenants["alice_inbox"]
    msgs = [make_message(inbox, f"m{i}") for i in range(3)]
    ids = [m.public_id for m in msgs]

    response = client.post(BATCH_URL, {"action": "mark_read", "ids": ids}, format="json")

    assert response.status_code == 200
    results = response.json()["data"]["results"]
    assert [r["status"] for r in results] == ["ok", "ok", "ok"]
    for m in msgs:
        m.refresh_from_db()
        assert m.read_at is not None

    
    first_read_ats = []
    for m in msgs:
        m.refresh_from_db()
        first_read_ats.append(m.read_at)

    # idempotent replay
    replay = client.post(BATCH_URL, {"action": "mark_read", "ids": ids}, format="json")
    assert replay.status_code == 200
    assert [r["status"] for r in replay.json()["data"]["results"]] == ["ok", "ok", "ok"]

    # ...and assert they are UNCHANGED after the replay (idempotent, not rewritten).
    for m, first_read_at in zip(msgs, first_read_ats, strict=True):
        m.refresh_from_db()
        assert m.read_at == first_read_at


def test_2_fetch_returns_correct_payload_in_input_order(tenants):
    client = client_for(tenants["alice_token"])
    inbox = tenants["alice_inbox"]
    first = make_message(inbox, "first")
    second = make_message(inbox, "second")
    ids = [second.public_id, first.public_id]

    response = client.post(BATCH_URL, {"action": "fetch", "ids": ids}, format="json")

    assert response.status_code == 200
    results = response.json()["data"]["results"]
    assert [r["id"] for r in results] == ids
    assert results[0]["message"]["subject"] == "second"
    assert results[1]["message"]["subject"] == "first"


def test_3_delete_removes_message_and_webhook_delivery_no_protected_error(tenants):
    client = client_for(tenants["alice_token"])
    inbox = tenants["alice_inbox"]
    # Create a real webhook directly via the model (bypassing the URL-target
    # network validation) so we can attach a WebhookDelivery to the message.
    webhook = Webhook(inbox=inbox, url="https://receiver.example/hook", is_enabled=True)
    webhook.save(_url_validated=True)
    message = make_message(inbox, "with-delivery")
    delivery, created = enqueue_message_delivery(message)
    assert created
    assert WebhookDelivery.objects.filter(message=message).count() == 1

    response = client.post(BATCH_URL, {"action": "delete", "ids": [message.public_id]}, format="json")

    assert response.status_code == 200
    assert response.json()["data"]["results"] == [{"id": message.public_id, "status": "deleted"}]
    assert not Message.objects.filter(pk=message.pk).exists()
    assert WebhookDelivery.objects.filter(message_id=message.pk).count() == 0


def test_4_ids_empty_or_over_100_returns_422(tenants):
    client = client_for(tenants["alice_token"])

    empty = client.post(BATCH_URL, {"action": "fetch", "ids": []}, format="json")
    assert empty.status_code == 422

    too_many = client.post(
        BATCH_URL, {"action": "fetch", "ids": [f"msg_{i}" for i in range(101)]}, format="json"
    )
    assert too_many.status_code == 422


def test_4b_serializer_rejects_101_ids_at_validation_level(tenants):
    "Public API."
    from apps.api.serializers import MessageBatchSerializer

    serializer = MessageBatchSerializer(
        data={"action": "fetch", "ids": [f"msg_{i}" for i in range(101)]}
    )

    assert serializer.is_valid() is False
    assert "ids" in serializer.errors

    ok_serializer = MessageBatchSerializer(
        data={"action": "fetch", "ids": [f"msg_{i}" for i in range(100)]}
    )
    assert ok_serializer.is_valid() is True


def test_5_partial_success_real_and_garbage_ids_mixed(tenants):
    client = client_for(tenants["alice_token"])
    inbox = tenants["alice_inbox"]
    real = make_message(inbox, "real-one")

    response = client.post(
        BATCH_URL, {"action": "fetch", "ids": [real.public_id, "msg_totally_made_up"]}, format="json"
    )

    assert response.status_code == 200
    results = response.json()["data"]["results"]
    assert results[0]["status"] == "ok"
    assert results[1]["status"] == "not_found"


def test_6_free_plan_gets_402_and_db_is_untouched(tenants):
    free_user = make_user("batch_free", plan=AccountPlan.Plan.FREE)
    _, free_token = create_api_key(free_user, "Free batch CI")
    inbox = Inbox.objects.create(user=free_user, routing_key="batch_free_box")
    message = make_message(inbox, "should-not-move")
    client = client_for(free_token)

    response = client.post(
        BATCH_URL, {"action": "mark_read", "ids": [message.public_id]}, format="json"
    )

    assert response.status_code == 402
    assert response.json()["error"]["code"] == "plan_upgrade_required"
    message.refresh_from_db()
    assert message.read_at is None


def test_7_rate_limit_returns_429_with_retry_after(tenants):
    client = client_for(tenants["alice_token"])
    inbox = tenants["alice_inbox"]
    message = make_message(inbox, "rate-limited")

    client.post(BATCH_URL, {"action": "fetch", "ids": [message.public_id]}, format="json")
    bucket = RateLimitBucket.objects.order_by("-expires_at").first()
    RateLimitBucket.objects.filter(pk=bucket.pk).update(count=60)

    response = client.post(BATCH_URL, {"action": "fetch", "ids": [message.public_id]}, format="json")

    assert response.status_code == 429
    assert "Retry-After" in response


def test_8_tenant_negative_batch_cannot_touch_foreign_message(tenants):
    alice_client = client_for(tenants["alice_token"])
    bob_inbox = tenants["bob_inbox"]
    bob_message = make_message(bob_inbox, "bob-secret")

    delete_resp = alice_client.post(
        BATCH_URL, {"action": "delete", "ids": [bob_message.public_id]}, format="json"
    )
    assert delete_resp.json()["data"]["results"] == [{"id": bob_message.public_id, "status": "not_found"}]
    assert Message.objects.filter(pk=bob_message.pk).exists()

    mark_resp = alice_client.post(
        BATCH_URL, {"action": "mark_read", "ids": [bob_message.public_id]}, format="json"
    )
    assert mark_resp.json()["data"]["results"] == [{"id": bob_message.public_id, "status": "not_found"}]
    bob_message.refresh_from_db()
    assert bob_message.read_at is None

    fetch_resp = alice_client.post(
        BATCH_URL, {"action": "fetch", "ids": [bob_message.public_id]}, format="json"
    )
    result = fetch_resp.json()["data"]["results"][0]
    assert result == {"id": bob_message.public_id, "status": "not_found"}
    assert "message" not in result


def test_9_no_existence_oracle_foreign_and_missing_ids_are_identical(tenants):
    alice_client = client_for(tenants["alice_token"])
    bob_inbox = tenants["bob_inbox"]
    bob_message = make_message(bob_inbox, "bob-exists")

    foreign = alice_client.post(
        BATCH_URL, {"action": "fetch", "ids": [bob_message.public_id]}, format="json"
    )
    missing = alice_client.post(
        BATCH_URL, {"action": "fetch", "ids": ["msg_definitely_does_not_exist"]}, format="json"
    )

    foreign_result = foreign.json()["data"]["results"][0]
    missing_result = missing.json()["data"]["results"][0]
    # ids differ (as they must, to echo the input), but the SHAPE is
    # identical: status="not_found" with no extra field hinting existence.
    assert foreign_result["status"] == missing_result["status"] == "not_found"
    assert set(foreign_result) == set(missing_result) == {"id", "status"}


def test_route_ordering_batch_is_not_captured_by_message_detail(tenants):
    """R5: 'messages/batch' must resolve to MessageBatchView, not be treated
    as a public_id by 'messages/<public_id>'. A GET here would 405, not 404,
    if routing were broken (batch only supports POST)."""
    client = client_for(tenants["alice_token"])

    response = client.get(BATCH_URL)

    assert response.status_code == 405  # method not allowed on MessageBatchView, not 404 on MessageDetailView
