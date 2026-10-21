"Public API."

import hashlib
import io
import logging
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.services import create_api_key
from apps.api.tests.test_webhook_test_endpoint import make_user
from apps.inboxes.models import Inbox, Message
from apps.webhooks import engine
from apps.webhooks.models import Webhook, WebhookDelivery
from apps.webhooks.security import SSRFValidationError, parse_webhook_url, validate_webhook_target
from apps.webhooks.services import (
    create_webhook,
    derive_signing_secret,
    enqueue_message_delivery,
    update_webhook,
)
from apps.webhooks.transport import DeliveryResponse, DeliveryTransportError, post_json_pinned

pytestmark = pytest.mark.django_db(transaction=True)

PUBLIC_IP = "93.184.216.34"


def public_resolver(_hostname):
    return [PUBLIC_IP]


def make_webhook(name, url="https://receiver.example/hook"):
    user = make_user(name)
    inbox = Inbox.objects.create(user=user, routing_key=f"{name}_box")
    webhook, _ = create_webhook(inbox, url, resolver=public_resolver)
    return user, inbox, webhook


def poison(webhook, url):
    """Simulate a row that ALREADY holds a non-ASCII URL (saved before the fix)."""
    Webhook.objects.filter(pk=webhook.pk).update(url=url)
    webhook.refresh_from_db()
    assert webhook.url == url
    return webhook


def make_delivery(webhook, suffix):
    raw = f"Subject: {suffix}\r\n\r\nCode 482193".encode()
    digest = hashlib.sha256(raw).hexdigest()
    message = Message.objects.create(
        inbox=webhook.inbox,
        dedupe_key=digest,
        envelope_from="sender@example.com",
        envelope_recipient=webhook.inbox.address,
        raw_bytes=raw,
        raw_sha256=digest,
        size_bytes=len(raw),
        ingest_status=Message.IngestStatus.PARSED,
        header_from="Sender <sender@example.com>",
        header_to=[webhook.inbox.address],
        header_cc=[],
        subject=suffix,
        text_body="Code 482193",
        parsed_at=timezone.now(),
    )
    delivery, created = enqueue_message_delivery(message)
    assert created
    return delivery


class FakeSocket:
    def settimeout(self, value):
        pass

    def connect(self, address):
        pass

    def close(self):
        pass


class FakeTLS(FakeSocket):
    sent: list[bytes] = []

    def sendall(self, value):
        FakeTLS.sent.append(value)

    def makefile(self, mode):
        return io.BytesIO(b"HTTP/1.1 204 No Content\r\n\r\n")


class FakeContext:
    def wrap_socket(self, sock, server_hostname):
        return FakeTLS()


@pytest.fixture
def network_edge(monkeypatch):
    """Replace only the network edge (DNS, TCP, TLS) so the REAL
    post_json_pinned / engine / views run end to end without I/O."""
    FakeTLS.sent = []
    opened = []

    def fake_open(ip, *, timeout, socket_factory=None):
        opened.append(ip)
        return FakeSocket()

    monkeypatch.setattr("apps.webhooks.security._default_resolver", public_resolver)
    monkeypatch.setattr("apps.webhooks.transport._open_pinned_socket", fake_open)
    monkeypatch.setattr("apps.webhooks.transport.ssl.create_default_context", lambda: FakeContext())
    return opened


# --- validation: refuse non-ASCII path / query, keep IDNA hosts ---------------


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/café",
        "https://example.com/hook?name=café",
        "https://example.com/hook?q=\u2603",
        "https://bücher.example/pfad/ä",  # IDNA host is fine, the path is not
        "https://example.com/\u202ehook",  # bidi override
    ],
)
def test_non_ascii_path_or_query_is_refused_as_invalid_url(url):
    with pytest.raises(SSRFValidationError) as exc_info:
        parse_webhook_url(url)
    assert exc_info.value.code == "ssrf_invalid_url"


def test_internationalised_hostname_still_accepted_via_idna():
    target = validate_webhook_target("https://bücher.example/hook?x=1", resolver=public_resolver)
    assert target.url == "https://xn--bcher-kva.example/hook?x=1"
    assert target.hostname == "xn--bcher-kva.example"
    assert target.request_target == "/hook?x=1"


def test_percent_encoded_path_and_query_still_accepted():
    target = validate_webhook_target("https://example.com/caf%C3%A9?n=%E2%98%83", resolver=public_resolver)
    assert target.request_target == "/caf%C3%A9?n=%E2%98%83"


def test_create_and_update_webhook_service_refuse_non_ascii_url():
    user = make_user("nb_service")
    inbox = Inbox.objects.create(user=user, routing_key="nb_service_box")
    with pytest.raises(SSRFValidationError) as exc_info:
        create_webhook(inbox, "https://example.com/café", resolver=public_resolver)
    assert exc_info.value.code == "ssrf_invalid_url"
    assert not Webhook.objects.filter(inbox=inbox).exists()

    webhook, _ = create_webhook(inbox, "https://receiver.example/hook", resolver=public_resolver)
    with pytest.raises(SSRFValidationError) as exc_info:
        update_webhook(webhook, "https://receiver.example/hook?q=é", enabled=True, resolver=public_resolver)
    assert exc_info.value.code == "ssrf_invalid_url"
    assert Webhook.objects.get(pk=webhook.pk).url == "https://receiver.example/hook"


def test_model_save_refuses_non_ascii_url(network_edge):
    _, _, webhook = make_webhook("nb_model")
    webhook.url = "https://receiver.example/ünicode"
    with pytest.raises(SSRFValidationError) as exc_info:
        webhook.save()
    assert exc_info.value.code == "ssrf_invalid_url"
    assert Webhook.objects.get(pk=webhook.pk).url == "https://receiver.example/hook"


@pytest.mark.parametrize("url", ["https://example.com/café", "https://example.com/hook?q=é"])
def test_dashboard_form_create_and_update_non_ascii_is_422(client, network_edge, url):
    user = make_user("nb_dash")
    inbox = Inbox.objects.create(user=user, routing_key="nb_dash_box")
    client.force_login(user)
    save_url = f"/app/inboxes/{inbox.public_id}/webhook/save/"

    response = client.post(save_url, {"url": url, "is_enabled": "on"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 422
    assert b"Webhook URL is not an allowed public HTTPS target." in response.content
    assert not Webhook.objects.filter(inbox=inbox).exists()

    ok = client.post(
        save_url, {"url": "https://receiver.example/hook", "is_enabled": "on"}, HTTP_HX_REQUEST="true"
    )
    assert ok.status_code == 200
    response = client.post(save_url, {"url": url, "is_enabled": "on"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 422
    assert Webhook.objects.get(inbox=inbox).url == "https://receiver.example/hook"


# --- belt-and-braces: a row that ALREADY holds a non-ASCII URL -----------------


def test_poisoned_row_fails_alone_and_next_tenant_row_is_still_delivered(network_edge):
    _, _, bad_hook = make_webhook("nb_bad")
    _, _, good_hook = make_webhook("nb_good")
    poison(bad_hook, "https://receiver.example/café")
    bad = make_delivery(bad_hook, "bad")  # older row: claimed first
    good = make_delivery(good_hook, "good")

    result = engine.drain(batch_size=10)

    bad.refresh_from_db()
    good.refresh_from_db()
    assert result.claimed == 2
    assert good.status == WebhookDelivery.Status.SUCCEEDED
    assert good.last_response_code == 204
    assert bad.status == WebhookDelivery.Status.EXHAUSTED  # per-row permanent failure
    assert bad.last_error_code == "ssrf_invalid_url"
    assert bad.attempt_count == 1
    assert network_edge == [PUBLIC_IP]  # only the good row opened a connection


def test_deliver_webhooks_command_does_not_abort_on_poisoned_row(network_edge):
    _, _, bad_hook = make_webhook("nb_cmd_bad")
    _, _, good_hook = make_webhook("nb_cmd_good")
    poison(bad_hook, "https://receiver.example/hook?q=é")
    make_delivery(bad_hook, "bad")
    good = make_delivery(good_hook, "good")
    out = StringIO()

    call_command("deliver_webhooks", "--once", stdout=out)

    assert "claimed=2 succeeded=1 retrying=0 exhausted=1" in out.getvalue()
    good.refresh_from_db()
    assert good.status == WebhookDelivery.Status.SUCCEEDED


def test_webhook_test_api_with_poisoned_row_returns_clean_400(network_edge):
    user, inbox, webhook = make_webhook("nb_api_test")
    poison(webhook, "https://receiver.example/café")
    _, token = create_api_key(user, "nb test key")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    response = client.post(f"/api/v1/inboxes/{inbox.public_id}/webhook/test")

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "webhook_ssrf_rejected"
    assert body["error"]["fields"] == {"url": ["ssrf_invalid_url"]}
    assert network_edge == []


def test_webhook_test_dashboard_with_poisoned_row_is_not_500(client, network_edge):
    user, inbox, webhook = make_webhook("nb_dash_test")
    poison(webhook, "https://receiver.example/café")
    client.force_login(user)

    response = client.post(f"/app/inboxes/{inbox.public_id}/webhook/test/", HTTP_HX_REQUEST="true")

    assert response.status_code == 400
    assert b"Webhook URL is not an allowed public HTTPS target." in response.content


def test_transport_unencodable_request_is_a_transport_error_before_any_socket(network_edge):
    # Any request that cannot be ASCII-encoded must surface as a normal
    # DeliveryTransportError (never a raw UnicodeEncodeError) and must not
    # open a connection first.
    with pytest.raises(DeliveryTransportError) as exc_info:
        post_json_pinned("https://receiver.example/hook", b"{}", {"X-Note": "café"})
    assert exc_info.value.code == "invalid_request_encoding"
    assert network_edge == []


# --- catch-all: no unexpected exception from one row kills the pass ------------


def test_unexpected_exception_in_one_row_is_recorded_and_pass_continues(caplog):
    _, _, bad_hook = make_webhook("nb_boom_bad", url="https://receiver.example/boom-hook")
    _, _, good_hook = make_webhook("nb_boom_good")
    bad = make_delivery(bad_hook, "bad")
    good = make_delivery(good_hook, "good")
    secret = derive_signing_secret(bad_hook)

    def transport(url, body, headers):
        if "boom-hook" in url:
            raise RuntimeError(f"kaboom {url} {headers['X-Mailsocket-Signature']}")
        return DeliveryResponse(204)

    caplog.set_level(logging.INFO)
    result = engine.drain(batch_size=10, transport=transport)

    bad.refresh_from_db()
    good.refresh_from_db()
    assert result.claimed == 2
    assert result.succeeded == 1
    assert result.retrying == 1
    assert good.status == WebhookDelivery.Status.SUCCEEDED
    # A recorded failed attempt: lease released, retry scheduled, error code set.
    assert bad.status == WebhookDelivery.Status.PENDING
    assert bad.attempt_count == 1
    assert bad.lease_token is None
    assert bad.next_attempt_at is not None
    assert bad.last_error_code == "internal_error"
    assert Webhook.objects.get(pk=bad_hook.pk).last_failure_at is not None
    # Logged with ids + exception type only: no URL, no secret, no message text.
    assert "webhook_delivery_internal_error" in caplog.text
    assert bad.public_id in caplog.text
    assert "RuntimeError" in caplog.text
    assert "boom-hook" not in caplog.text
    assert "receiver.example" not in caplog.text
    assert "kaboom" not in caplog.text
    assert secret not in caplog.text
    assert "v1=" not in caplog.text


def test_unexpected_exception_on_last_attempt_exhausts(settings):
    _, _, hook = make_webhook("nb_boom_last")
    delivery = make_delivery(hook, "last")
    WebhookDelivery.objects.filter(pk=delivery.pk).update(attempt_count=settings.WEBHOOK_MAX_ATTEMPTS - 1)

    def transport(url, body, headers):
        raise ValueError("unexpected")

    engine.drain(batch_size=1, transport=transport)

    delivery.refresh_from_db()
    assert delivery.status == WebhookDelivery.Status.EXHAUSTED
    assert delivery.last_error_code == "internal_error"


def test_database_error_still_stops_the_pass():
    # The per-row catch-all must not swallow a database outage: the
    # deliver_webhooks command relies on it to exit non-zero.
    from django.db import DatabaseError

    _, _, hook = make_webhook("nb_db_down")
    make_delivery(hook, "db")

    def transport(url, body, headers):
        raise DatabaseError("down")

    with pytest.raises(DatabaseError):
        engine.drain(batch_size=1, transport=transport)
