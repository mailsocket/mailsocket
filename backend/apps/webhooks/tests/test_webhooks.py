import hashlib
import logging
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import close_old_connections
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan
from apps.inboxes.models import Inbox, Message
from apps.ingest.parser import parse_message
from apps.webhooks.engine import _finalize, claim_batch, deliver_claim, drain
from apps.webhooks.models import Webhook, WebhookDelivery
from apps.webhooks.security import SSRFValidationError, validate_webhook_target
from apps.webhooks.services import (
    build_event_payload,
    create_webhook,
    derive_signing_secret,
    enqueue_message_delivery,
    event_body_bytes,
    rotate_signing_secret,
    signature_header,
    verify_signature,
)
from apps.webhooks.transport import DeliveryResponse, post_json_pinned

pytestmark = pytest.mark.django_db(transaction=True)
PUBLIC = ["93.184.216.34"]


def public_resolver(_hostname):
    return PUBLIC


@pytest.fixture
def owner(user):
    Account.objects.create(user=user, email_normalized=user.email, email_verified_at=timezone.now())
    AccountPlan.objects.create(user=user, plan=AccountPlan.Plan.PAID)
    return user


@pytest.fixture
def inbox(owner):
    return Inbox.objects.create(user=owner, routing_key="webhook_box", label="Webhook")


@pytest.fixture
def webhook(inbox):
    webhook, _ = create_webhook(inbox, "https://receiver.example/hook?source=test", resolver=public_resolver)
    return webhook


def message_values(inbox, *, suffix="one", parsed=True):
    raw = f"Subject: {suffix}\r\n\r\nCode 482193".encode()
    digest = hashlib.sha256(raw).hexdigest()
    return {
        "inbox": inbox,
        "dedupe_key": digest,
        "envelope_from": "sender@example.com",
        "envelope_recipient": inbox.address,
        "raw_bytes": raw,
        "raw_sha256": digest,
        "size_bytes": len(raw),
        "ingest_status": Message.IngestStatus.PARSED if parsed else Message.IngestStatus.RECEIVED,
        "header_from": "Sender <sender@example.com>",
        "header_to": [inbox.address],
        "header_cc": [],
        "subject": suffix,
        "text_body": "Code 482193",
        "html_body": "<p>Code 482193</p>",
        "otp": "482193",
        "otp_confidence": 0.95,
        "magic_link": None,
        "links": [],
        "detected_otp": "482193",
        "parsed_at": timezone.now() if parsed else None,
    }


def make_delivery(webhook, suffix="one"):
    message = Message.objects.create(**message_values(webhook.inbox, suffix=suffix))
    delivery, created = enqueue_message_delivery(message)
    assert created
    return delivery


def test_case_30_parse_completion_enqueues_exactly_once_outside_store(inbox, webhook):
    message = Message.objects.create(**message_values(inbox, parsed=False))
    first = parse_message(message.pk)
    second = parse_message(message.pk)
    assert first.ingest_status == second.ingest_status == Message.IngestStatus.PARSED
    assert WebhookDelivery.objects.filter(message=message, webhook=webhook).count() == 1
    delivery, created = enqueue_message_delivery(second)
    assert not created
    assert delivery.pk == WebhookDelivery.objects.get().pk


def test_case_31_payload_reuses_frozen_message_serializer_and_event_id(webhook):
    delivery = make_delivery(webhook)
    expected = build_event_payload(delivery.message, delivery.event_id)
    assert delivery.payload["data"] == expected["data"]
    assert set(delivery.payload) == {"id", "event", "created_at", "data"}
    assert set(delivery.payload["data"]["message"]) == {
        "id", "inbox_id", "from", "to", "cc", "subject", "received_at", "status",
        "otp", "otp_confidence", "magic_link", "links", "detected_otp",
        "envelope_from", "envelope_recipient", "text", "html",
        "otp_context", "otp_candidates", "provider_hint", "read", "metadata",
    }
    assert delivery.payload["id"] == delivery.event_id
    assert "raw_bytes" not in str(delivery.payload)


def test_case_32_exact_bytes_signature_and_rotation(webhook):
    delivery = make_delivery(webhook)
    body = event_body_bytes(delivery.payload)
    timestamp = 1_795_000_000
    first_secret = derive_signing_secret(webhook)
    header = signature_header(first_secret, timestamp, body)
    assert verify_signature(first_secret, header, body)
    assert not verify_signature(first_secret, header, body + b" ")
    assert not verify_signature(first_secret, header.replace(str(timestamp), str(timestamp + 1)), body)
    assert not verify_signature("whsec_wrong", header, body)
    assert not verify_signature(first_secret, header, body, expected_version="2")
    shown = rotate_signing_secret(webhook)
    webhook.refresh_from_db()
    assert shown == derive_signing_secret(webhook)
    assert shown != first_secret
    assert signature_header(shown, timestamp, body) != header


def test_case_33_retry_success_permanent_and_attempt_cap(webhook, settings):
    settings.WEBHOOK_RETRY_JITTER_SECONDS = 0
    success = make_delivery(webhook, "success")
    drain(transport=lambda *_: DeliveryResponse(204))
    success.refresh_from_db()
    assert success.status == WebhookDelivery.Status.SUCCEEDED

    retry = make_delivery(webhook, "retry")
    drain(transport=lambda *_: DeliveryResponse(429, retry_after_seconds=10))
    retry.refresh_from_db()
    assert retry.status == WebhookDelivery.Status.PENDING
    assert 1 <= (retry.next_attempt_at - retry.last_attempt_at).total_seconds() <= 60

    permanent = make_delivery(webhook, "permanent")
    drain(transport=lambda *_: DeliveryResponse(400))
    permanent.refresh_from_db()
    assert permanent.status == WebhookDelivery.Status.EXHAUSTED

    capped = make_delivery(webhook, "cap")
    capped.attempt_count = settings.WEBHOOK_MAX_ATTEMPTS
    capped.save(update_fields=["attempt_count"])
    assert claim_batch(batch_size=10) == []
    capped.refresh_from_db()
    assert capped.status == WebhookDelivery.Status.EXHAUSTED


def test_case_34_two_drainers_expiry_and_stale_fence(webhook):
    delivery = make_delivery(webhook)
    barrier = threading.Barrier(2)

    def claim():
        close_old_connections()
        barrier.wait(timeout=10)
        value = claim_batch(batch_size=1)
        close_old_connections()
        return value

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda _: claim(), range(2)))
    assert sum(len(value) for value in claims) == 1
    old_token = claims[0][0][1] if claims[0] else claims[1][0][1]
    WebhookDelivery.objects.filter(pk=delivery.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    recovered = claim_batch(batch_size=1)
    assert len(recovered) == 1 and recovered[0][1] != old_token
    assert _finalize(delivery.pk, old_token, outcome="success", response_code=200) == "fenced"
    assert deliver_claim(delivery.pk, old_token, transport=lambda *_: pytest.fail("stale lease sent")) == "fenced"
    delivery.refresh_from_db()
    assert delivery.status == WebhookDelivery.Status.DELIVERING


def test_case_34_command_database_failure_is_nonzero(webhook):
    make_delivery(webhook)
    with patch("apps.webhooks.management.commands.deliver_webhooks.drain", side_effect=__import__("django.db").db.DatabaseError("down")):
        with pytest.raises(Exception, match="Webhook delivery database failure"):
            call_command("deliver_webhooks", "--once", stdout=StringIO())


def test_case_34_command_process_database_failure_exits_nonzero(settings):
    environment = __import__("os").environ.copy()
    environment.update({
        "DB_NAME": "mailsocket_ci_m3d_missing",
        "DB_HOST": "127.0.0.1",
        "DB_PORT": "1",
        "DB_USERNAME": "mailsocket",
        "DB_PASS": "",
        "DJANGO_SETTINGS_MODULE": "config.settings.testing",
    })
    completed = subprocess.run(
        [sys.executable, "manage.py", "deliver_webhooks", "--once"],
        cwd=settings.BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode != 0
    assert "CommandError: Webhook delivery database failure" in completed.stderr
    assert "payload" not in completed.stderr and "secret" not in completed.stderr


@pytest.mark.parametrize("url", [
    "http://example.com/hook", "https://user:pass@example.com/hook", "https://example.com:444/hook",
    "https://example.com/hook#fragment", "https://localhost/hook", "https://127.0.0.1/hook",
    "https://10.0.0.1/hook", "https://172.16.0.1/hook", "https://192.168.0.1/hook",
    "https://169.254.1.1/hook", "https://169.254.169.254/hook", "https://[::1]/hook",
    "https://[fc00::1]/hook", "https://[fe80::1]/hook", "https://[fd00:ec2::254]/hook",
    "https://[64:ff9b::a9fe:a9fe]/hook", "https://[64:ff9b::a00:1]/hook",
    "https://[64:ff9b::7f00:1]/hook", "https://[64:ff9b::ac10:1]/hook",
    "https://[::10.0.0.1]/hook", "https://[::ffff:10.0.0.1]/hook",
    "https://2130706433/hook", "https://0x7f000001/hook", "https://0177.0.0.1/hook",
    "https://192.0.2.1/hook", "https://198.18.0.1/hook",
])
def test_case_35_ssrf_matrix_rejects_without_socket(url):
    socket_calls = []
    with pytest.raises(SSRFValidationError):
        validate_webhook_target(url, resolver=lambda _: ["93.184.216.34"])
    assert socket_calls == []


def test_case_35_mixed_dns_and_redirect_are_rejected_without_following():
    with pytest.raises(SSRFValidationError, match="ssrf_non_public_address"):
        validate_webhook_target("https://mixed.example/hook", resolver=lambda _: ["93.184.216.34", "10.0.0.1"])
    delivery = DeliveryResponse(302)
    assert delivery.status_code == 302
    # The raw client returns the 302; the engine permanently exhausts it and never follows Location.


def test_case_35_nat64_resolver_answer_is_rejected():
    with pytest.raises(SSRFValidationError, match="ssrf_non_public_address"):
        validate_webhook_target(
            "https://attacker.example/hook",
            resolver=lambda _: ["64:ff9b::a9fe:a9fe"],
        )


def test_case_35_public_ipv6_is_allowed():
    target = validate_webhook_target("https://[2606:4700:4700::1111]/hook")
    assert target.addresses == ("2606:4700:4700::1111",)


def test_case_36_rebinding_revalidated_and_pinned_tls_hostname(webhook):
    validate_webhook_target(webhook.url, resolver=public_resolver)
    socket_calls = []

    def no_socket(*args):
        socket_calls.append(args)
        raise AssertionError("connection attempted")

    with pytest.raises(SSRFValidationError):
        post_json_pinned(webhook.url, b"{}", {}, resolver=lambda _: ["10.0.0.1"], socket_factory=no_socket)
    assert socket_calls == []

    class FakeSocket:
        def settimeout(self, value): pass
        def connect(self, address): socket_calls.append(address)
        def close(self): pass

    class FakeTLS(FakeSocket):
        def sendall(self, value): self.request = value
        def makefile(self, mode): return __import__("io").BytesIO(b"HTTP/1.1 204 No Content\r\n\r\n")

    fake_tls = FakeTLS()
    sni = []

    class FakeContext:
        def wrap_socket(self, sock, server_hostname):
            sni.append(server_hostname)
            return fake_tls

    response = post_json_pinned(
        webhook.url, b"{}", {}, resolver=lambda _: ["93.184.216.34"],
        socket_factory=lambda *_: FakeSocket(), ssl_context=FakeContext(),
    )
    assert response.status_code == 204
    assert socket_calls == [("93.184.216.34", 443)]
    assert sni == ["receiver.example"]
    assert b"Host: receiver.example\r\n" in fake_tls.request


def test_case_37_response_body_discarded_and_not_logged_or_stored(webhook, caplog, settings):
    delivery = make_delivery(webhook)
    secret_body = b"response-secret-never-store"

    class FakeSocket:
        def settimeout(self, value): pass
        def connect(self, address): pass
        def close(self): pass

    class FakeTLS(FakeSocket):
        def sendall(self, value): pass
        def makefile(self, mode):
            return __import__("io").BytesIO(b"HTTP/1.1 500 Failure\r\nContent-Length: 999999\r\n\r\n" + secret_body)

    class FakeContext:
        def wrap_socket(self, sock, server_hostname): return FakeTLS()

    def transport(url, body, headers):
        return post_json_pinned(url, body, headers, resolver=public_resolver, socket_factory=lambda *_: FakeSocket(), ssl_context=FakeContext())

    caplog.set_level(logging.INFO)
    token = claim_batch(batch_size=1)[0][1]
    deliver_claim(delivery.pk, token, transport=transport)
    delivery.refresh_from_db()
    assert delivery.last_error_code == "http_500"
    assert secret_body.decode() not in caplog.text
    assert secret_body.decode() not in str(WebhookDelivery.objects.values().get(pk=delivery.pk))
    field_names = {field.name for field in WebhookDelivery._meta.fields}
    assert "response_body" not in field_names and "secret" not in field_names


def test_dashboard_owner_scope_entitlement_and_secret_once(client, owner, inbox, monkeypatch):
    client.force_login(owner)
    monkeypatch.setattr("apps.webhooks.services.validate_webhook_target", lambda url, **kwargs: validate_webhook_target(url, resolver=public_resolver))
    response = client.post(f"/app/inboxes/{inbox.public_id}/webhook/save/", {"url": "https://receiver.example/hook", "is_enabled": "on"})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert b"whsec_" in response.content
    assert b"whsec_" not in client.get("/app/webhooks/").content

    other = __import__("django.contrib.auth").contrib.auth.get_user_model().objects.create_user("other")
    Account.objects.create(user=other, email_normalized="other@example.test", email_verified_at=timezone.now())
    AccountPlan.objects.create(user=other, plan=AccountPlan.Plan.PAID)
    foreign = Inbox.objects.create(user=other, routing_key="foreign_webhook")
    assert client.post(f"/app/inboxes/{foreign.public_id}/webhook/disable/").status_code == 404


def test_dashboard_free_plan_cannot_create_or_enable(client, owner, inbox, monkeypatch):
    AccountPlan.objects.filter(user=owner).update(plan=AccountPlan.Plan.FREE)
    client.force_login(owner)
    monkeypatch.setattr("apps.webhooks.services.validate_webhook_target", lambda url, **kwargs: validate_webhook_target(url, resolver=public_resolver))
    response = client.post(
        f"/app/inboxes/{inbox.public_id}/webhook/save/",
        {"url": "https://receiver.example/hook", "is_enabled": "on"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 422
    assert not Webhook.objects.filter(inbox=inbox).exists()
    # qafix2 #4: a plain (no-JS) POST lands on the full Webhooks page with the
    # same message instead of a bare partial.
    response = client.post(
        f"/app/inboxes/{inbox.public_id}/webhook/save/",
        {"url": "https://receiver.example/hook", "is_enabled": "on"},
        follow=True,
    )
    assert response.redirect_chain == [("/app/webhooks/", 302)]
    assert b"Your plan cannot configure or enable webhooks." in response.content
    assert not Webhook.objects.filter(inbox=inbox).exists()
