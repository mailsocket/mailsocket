import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import call, patch

import pytest
from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import override_settings
from rest_framework.test import APIClient

from apps.inboxes.models import Inbox, Message
from apps.ingest.services import build_dedupe_key, extract_message_id, store_message

pytestmark = pytest.mark.django_db(transaction=True)

SECRET = "test-ingest-secret"
RECIPIENT = "alpha_key@in.inboxpipe.net"
RAW = (
    b"From: Sender <sender@example.test>\r\n"
    b"To: visible@example.test\r\n"
    b"Message-ID: <Case-ID@example.test>\r\n"
    b"Subject: exact bytes\r\n"
    b"\r\n"
    b"body\x00\xff\r\n"
)


@pytest.fixture
def inbox(user):
    return Inbox.objects.create(user=user, routing_key="alpha_key", label="Alpha")


def ingest_headers(**overrides):
    headers = {
        "HTTP_X_MAILSOCKET_INGEST_SECRET": SECRET,
        "HTTP_X_ENVELOPE_TO": RECIPIENT,
        "HTTP_X_ENVELOPE_FROM": "bounce@example.test",
        "HTTP_X_POSTFIX_QUEUE_ID": "QUEUE123",
        "HTTP_X_INTAKE_ID": "attempt-1",
        "REMOTE_ADDR": "172.30.0.9",
    }
    headers.update(overrides)
    return headers


def post(client, raw=RAW, **headers):
    return client.post(
        "/internal/ingest",
        data=raw,
        content_type="application/octet-stream",
        **ingest_headers(**headers),
    )


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    MAIL_DOMAIN="in.inboxpipe.net",
)
def test_case_1_stores_exact_bytes_and_metadata(api_client, inbox):
    response = post(api_client)

    assert response.status_code == 201
    assert response.json() == {
        "public_id": Message.objects.get().public_id,
        "ingest_status": "parsed",
        "deduplicated": False,
    }
    message = Message.objects.get()
    assert bytes(message.raw_bytes) == RAW
    assert message.raw_sha256 == hashlib.sha256(RAW).hexdigest()
    assert message.size_bytes == len(RAW)
    assert message.envelope_recipient == RECIPIENT
    assert message.envelope_from == "bounce@example.test"
    assert message.postfix_queue_id == "QUEUE123"
    assert message.intake_id == "attempt-1"
    assert message.provider_message_id == "<Case-ID@example.test>"
    assert message.public_id.startswith("msg_")
    assert message.inbox == inbox


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    MAIL_DOMAIN="in.inboxpipe.net",
)
def test_case_2_bcc_routes_only_by_envelope_recipient(api_client, inbox):
    raw = b"To: somebody-else@example.test\r\nMessage-ID: <bcc@example.test>\r\n\r\nsecret"

    response = post(api_client, raw)

    assert response.status_code == 201
    message = Message.objects.get()
    assert message.inbox == inbox
    assert message.envelope_recipient == RECIPIENT
    assert b"somebody-else@example.test" in bytes(message.raw_bytes)
    assert message.header_to == ["somebody-else@example.test"]


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
)
def test_case_3_missing_or_wrong_secret_is_403_without_row(api_client, inbox):
    with patch("apps.ingest.views.secrets.compare_digest", wraps=__import__("secrets").compare_digest) as compare:
        missing = post(api_client, HTTP_X_MAILSOCKET_INGEST_SECRET="")
        wrong = post(api_client, HTTP_X_MAILSOCKET_INGEST_SECRET="wrong")

    assert missing.status_code == wrong.status_code == 403
    assert Message.objects.count() == 0
    assert compare.call_args_list == [
        call(b"", SECRET.encode()),
        call(b"wrong", SECRET.encode()),
    ]


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
)
def test_non_ascii_secret_is_403_not_500(api_client, inbox):
    # Regression (audit P2): compare_digest on str raises TypeError for
    # non-ASCII, which used to surface as HTTP 500. Must be a clean 403.
    # Uses an allowed source so it reaches the secret check (not the CIDR 404).
    response = post(api_client, HTTP_X_MAILSOCKET_INGEST_SECRET="é")
    assert response.status_code == 403
    assert Message.objects.count() == 0


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
)
def test_case_4_cidr_middleware_hides_route_from_public_source(api_client, inbox):
    denied = post(api_client, REMOTE_ADDR="172.29.0.5")
    allowed = post(api_client, REMOTE_ADDR="172.30.0.5")

    assert denied.status_code == 404
    assert allowed.status_code == 201
    assert Message.objects.count() == 1


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    MAIL_DOMAIN="in.inboxpipe.net",
)
@pytest.mark.parametrize(
    ("recipient", "expected_status"),
    [
        ("unknown@in.inboxpipe.net", 404),
        ("not-an-address", 400),
        # Stage B (Phase 3 BYOD): a well-formed recipient at a domain that is
        # not the shared MAIL_DOMAIN and not a verified custom Domain is no
        # longer a parse-time 400 — parse_envelope_recipient now treats the
        # domain as data (Postfix RCPT-time acceptance already gated on it);
        # resolve_inbox looks the domain up and fails closed with 404 (same
        # "not found" shape as an unknown routing_key), never a 500.
        ("alpha_key@example.test", 404),
        ("alpha_key@in.inboxpipe.net\r\nX-Evil: yes", 400),
        ("Display <alpha_key@in.inboxpipe.net>", 400),
        ("@route:alpha_key@in.inboxpipe.net", 400),
        ("alpha_key@in.inboxpipe.net,other@in.inboxpipe.net", 400),
        ("alpha_key@in.inboxpipe.nét", 400),
    ],
)
def test_case_5_invalid_recipients_create_no_row(api_client, inbox, recipient, expected_status):
    response = post(api_client, HTTP_X_ENVELOPE_TO=recipient)

    assert response.status_code == expected_status
    assert Message.objects.count() == 0


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    MAIL_DOMAIN="in.inboxpipe.net",
)
@pytest.mark.parametrize("recipient", ["Alpha_key@in.inboxpipe.net", "alpha_key@IN.INBOXPIPE.NET"])
def test_case_5_mixed_case_recipient_is_folded_and_stored(api_client, inbox, recipient):
    
    # RCPT case-insensitively, so ingest must fold, not reject (the 400 became
    # a 5-day deferral and a lost mail).
    response = post(api_client, HTTP_X_ENVELOPE_TO=recipient)

    assert response.status_code == 201
    message = Message.objects.get()
    assert message.inbox == inbox
    assert message.envelope_recipient == RECIPIENT


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
)
def test_case_5_disabled_inbox_returns_422(api_client, inbox):
    inbox.is_enabled = False
    inbox.save()

    response = post(api_client)

    assert response.status_code == 422
    assert Message.objects.count() == 0


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    MAX_INBOUND_BYTES=128,
)
def test_case_6_enforces_exact_byte_boundary(api_client, inbox):
    at_limit = b"x" * 128
    accepted = post(api_client, at_limit)
    too_large = post(api_client, b"y" * 129)

    assert accepted.status_code == 201
    assert too_large.status_code == 413
    message = Message.objects.get()
    assert bytes(message.raw_bytes) == at_limit
    assert message.raw_sha256 == hashlib.sha256(at_limit).hexdigest()
    assert Message.objects.count() == 1


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
)
def test_case_7_sequential_identical_posts_deduplicate(api_client, inbox):
    first = post(api_client)
    second = post(api_client)

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["public_id"] == second.json()["public_id"]
    assert second.json()["deduplicated"] is True
    assert Message.objects.count() == 1


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
)
def test_case_8_concurrent_identical_posts_use_unique_conflict_path(inbox):
    barrier = threading.Barrier(2)

    def concurrent_post():
        close_old_connections()
        client = APIClient()
        barrier.wait(timeout=10)
        response = post(client)
        close_old_connections()
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: concurrent_post(), range(2)))

    assert sorted(status for status, _ in results) == [200, 201]
    assert sum(body["deduplicated"] for _, body in results) == 1
    assert Message.objects.count() == 1


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
)
def test_case_9_message_id_variants_are_deterministic_and_raw_bound(api_client, inbox):
    variants = [
        b"Subject: absent\r\n\r\nbody",
        b"Message-ID: malformed value\r\n\r\nbody",
        b"Message-ID: <one@example.test>\r\nMessage-ID: <two@example.test>\r\n\r\nbody",
        b"Message-ID: <forged@example.test>\r\n\r\nbody-a",
    ]
    keys = []
    for raw in variants:
        first = post(api_client, raw)
        second = post(api_client, raw)
        assert first.status_code == 201
        assert second.status_code == 200
        message = Message.objects.get(raw_sha256=hashlib.sha256(raw).hexdigest())
        keys.append(message.dedupe_key)
        original, normalized = extract_message_id(raw)
        assert message.dedupe_key == build_dedupe_key(normalized, message.raw_sha256)
        if b"forged" in raw:
            assert original == "<forged@example.test>"

    changed = b"Message-ID: <forged@example.test>\r\n\r\nbody-b"
    changed_response = post(api_client, changed)
    assert changed_response.status_code == 201
    assert Message.objects.count() == 5
    assert len(set(keys)) == 4


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
)
def test_case_10_impossible_dedupe_mismatch_is_409_and_critical(api_client, inbox, caplog):
    existing_raw = b"Subject: existing\r\n\r\nexisting"
    existing_sha = hashlib.sha256(existing_raw).hexdigest()
    forced_key = "a" * 64
    Message.objects.create(
        inbox=inbox,
        dedupe_key=forced_key,
        envelope_from="",
        envelope_recipient=RECIPIENT,
        raw_bytes=existing_raw,
        raw_sha256=existing_sha,
        size_bytes=len(existing_raw),
    )

    with patch("apps.ingest.services.build_dedupe_key", return_value=forced_key):
        with caplog.at_level("CRITICAL", logger="apps.ingest.services"):
            response = post(api_client, b"Subject: conflicting\r\n\r\ndifferent")

    assert response.status_code == 409
    assert Message.objects.count() == 1
    assert "Dedupe integrity mismatch" in caplog.text


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
)
def test_non_post_is_405_and_wrong_content_type_is_400(api_client, inbox):
    get_response = api_client.get("/internal/ingest", REMOTE_ADDR="172.30.0.8")
    type_response = api_client.post(
        "/internal/ingest",
        data={"not": "raw"},
        format="json",
        **ingest_headers(),
    )

    assert get_response.status_code == 405
    assert type_response.status_code == 400
    assert Message.objects.count() == 0


def test_postgresql_is_authoritative_database():
    assert connection.vendor == "postgresql"


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
)
def test_case_14_parser_exception_after_commit_preserves_raw_and_response(api_client, inbox):
    observed = {}

    def fail_after_commit(raw_bytes):
        durable = Message.objects.get()
        observed["status"] = durable.ingest_status
        observed["raw"] = bytes(durable.raw_bytes)
        observed["sha"] = durable.raw_sha256
        raise RuntimeError("raw secret must never be persisted in error")

    with patch("apps.ingest.parser.parse_raw_message", side_effect=fail_after_commit):
        response = post(api_client)

    message = Message.objects.get()
    assert response.status_code == 201
    assert response.json()["ingest_status"] == Message.IngestStatus.RECEIVED
    assert observed == {
        "status": Message.IngestStatus.PARSING,
        "raw": RAW,
        "sha": hashlib.sha256(RAW).hexdigest(),
    }
    assert bytes(message.raw_bytes) == RAW
    assert message.raw_sha256 == hashlib.sha256(RAW).hexdigest()
    assert message.parse_attempts == 1
    assert message.parse_error == "Parser failure (RuntimeError)."
    assert "secret" not in message.parse_error


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
)
def test_case_15_kill_window_dedupes_then_reconciler_parses(api_client, inbox):
    stored = store_message(
        raw_bytes=RAW,
        headers={
            "X-Envelope-To": RECIPIENT,
            "X-Envelope-From": "bounce@example.test",
            "X-Postfix-Queue-Id": "QUEUE123",
            "X-Intake-Id": "attempt-1",
        },
    )
    message = stored.message
    assert stored.deduplicated is False
    assert message.ingest_status == Message.IngestStatus.RECEIVED
    assert message.parse_attempts == 0

    duplicate = post(api_client)
    assert duplicate.status_code == 200
    assert duplicate.json()["deduplicated"] is True
    assert Message.objects.count() == 1
    message.refresh_from_db()
    assert message.ingest_status == Message.IngestStatus.RECEIVED

    call_command("reconcile_ingest", "--once")
    message.refresh_from_db()
    assert message.ingest_status == Message.IngestStatus.PARSED
    assert message.parse_attempts == 1
