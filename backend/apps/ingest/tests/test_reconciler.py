import hashlib
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, close_old_connections
from django.utils import timezone

from apps.inboxes.models import Inbox, Message
from apps.ingest.reconciler import reconcile

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def inbox(user):
    return Inbox.objects.create(user=user, routing_key="reconcile_box", label="Reconciler")


def make_message(inbox, suffix, **overrides):
    raw = f"Subject: message {suffix}\r\n\r\ncode 12{suffix:04d}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    values = {
        "inbox": inbox,
        "dedupe_key": digest,
        "envelope_from": "sender@example.test",
        "envelope_recipient": "reconcile_box@in.inboxpipe.net",
        "raw_bytes": raw,
        "raw_sha256": digest,
        "size_bytes": len(raw),
    }
    values.update(overrides)
    return Message.objects.create(**values)


def test_case_17_due_received_is_claimed_and_parsed(inbox):
    due = make_message(inbox, 17, next_parse_at=timezone.now() - timedelta(seconds=1))
    future = make_message(inbox, 117, next_parse_at=timezone.now() + timedelta(hours=1))

    result = reconcile(stale_seconds=300, batch_size=100)

    due.refresh_from_db()
    future.refresh_from_db()
    assert result.claimed == result.parsed == 1
    assert due.ingest_status == Message.IngestStatus.PARSED
    assert due.parse_attempts == 1
    assert future.ingest_status == Message.IngestStatus.RECEIVED
    assert future.parse_attempts == 0


def test_case_18_expired_parsing_reclaimed_nonexpired_untouched(inbox):
    now = timezone.now()
    expired = make_message(
        inbox,
        18,
        ingest_status=Message.IngestStatus.PARSING,
        parse_attempts=1,
        parse_started_at=now - timedelta(minutes=10),
        parse_lease_token=uuid4(),
        parse_lease_expires_at=now - timedelta(seconds=1),
    )
    active = make_message(
        inbox,
        118,
        ingest_status=Message.IngestStatus.PARSING,
        parse_attempts=1,
        parse_started_at=now,
        parse_lease_token=uuid4(),
        parse_lease_expires_at=now + timedelta(minutes=5),
    )

    result = reconcile(stale_seconds=300, batch_size=100)

    expired.refresh_from_db()
    active.refresh_from_db()
    assert result.claimed == 1
    assert expired.ingest_status == Message.IngestStatus.PARSED
    assert expired.parse_attempts == 2
    assert active.ingest_status == Message.IngestStatus.PARSING
    assert active.parse_attempts == 1
    assert active.parse_lease_token is not None


def test_case_19_two_real_reconcilers_do_not_double_claim(inbox):
    message = make_message(inbox, 19)
    barrier = threading.Barrier(2)

    def run_reconciler():
        close_old_connections()
        barrier.wait(timeout=10)
        result = reconcile(stale_seconds=300, batch_size=1)
        close_old_connections()
        return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run_reconciler(), range(2)))

    message.refresh_from_db()
    assert sum(result.claimed for result in results) == 1
    assert sum(result.parsed for result in results) == 1
    assert message.ingest_status == Message.IngestStatus.PARSED
    assert message.parse_attempts == 1


def test_case_20_batch_limit_and_order_are_deterministic(inbox):
    messages = [make_message(inbox, suffix) for suffix in (201, 202, 203)]
    base = timezone.now() - timedelta(hours=1)
    Message.objects.filter(pk=messages[0].pk).update(received_at=base + timedelta(seconds=2))
    Message.objects.filter(pk=messages[1].pk).update(received_at=base)
    Message.objects.filter(pk=messages[2].pk).update(received_at=base + timedelta(seconds=1))
    processed = []

    from apps.ingest import reconciler
    real_process = reconciler.process_claim

    def record_order(message_id, token):
        processed.append(message_id)
        return real_process(message_id, token)

    with patch("apps.ingest.reconciler.process_claim", side_effect=record_order):
        result = reconcile(stale_seconds=300, batch_size=2)

    assert result.claimed == 2
    assert processed == [messages[1].pk, messages[2].pk]
    assert Message.objects.get(pk=messages[0].pk).ingest_status == Message.IngestStatus.RECEIVED


def test_case_16_failed_is_not_hot_looped(inbox):
    message = make_message(
        inbox,
        216,
        ingest_status=Message.IngestStatus.FAILED,
        parse_attempts=5,
        parse_error="Parser resource limit.",
    )
    first = reconcile(stale_seconds=0, batch_size=100)
    second = reconcile(stale_seconds=0, batch_size=100)
    message.refresh_from_db()
    assert first.claimed == second.claimed == 0
    assert message.ingest_status == Message.IngestStatus.FAILED
    assert message.parse_attempts == 5


def test_expired_lease_at_attempt_budget_is_failed_without_parsing(inbox):
    message = make_message(
        inbox,
        217,
        ingest_status=Message.IngestStatus.PARSING,
        parse_attempts=5,
        parse_started_at=timezone.now() - timedelta(minutes=10),
        parse_lease_token=uuid4(),
        parse_lease_expires_at=timezone.now() - timedelta(seconds=1),
    )
    with patch("apps.ingest.reconciler.process_claim") as process:
        result = reconcile(stale_seconds=300, batch_size=100)
    message.refresh_from_db()
    process.assert_not_called()
    assert result.claimed == 0
    assert message.ingest_status == Message.IngestStatus.FAILED
    assert message.parse_attempts == 5
    assert message.parse_lease_token is None


def test_command_reports_counts_and_validates_bounded_batch(inbox):
    make_message(inbox, 220)
    stdout = StringIO()
    call_command("reconcile_ingest", "--once", stdout=stdout)
    assert "claimed=1 parsed=1 retryable=0 failed=0" in stdout.getvalue()
    with pytest.raises(Exception, match="batch-size must be between"):
        call_command("reconcile_ingest", batch_size=1001)


def test_case_20_database_infrastructure_failure_exits_nonzero():
    backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    environment = os.environ.copy()
    environment.update(
        {
            "DJANGO_SETTINGS_MODULE": "config.settings.testing",
            "DB_NAME": "mailsocket",
            "DB_USERNAME": environment.get("DB_USERNAME", os.environ.get("USER", "")),
            "DB_PASS": "",
            "DB_HOST": "127.0.0.1",
            "DB_PORT": "1",
        }
    )
    completed = subprocess.run(
        [sys.executable, "manage.py", "reconcile_ingest", "--once"],
        cwd=backend_dir,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode != 0
    assert "CommandError: Ingest reconciliation database failure" in completed.stderr
    assert "raw_bytes" not in completed.stderr


def test_finalize_database_error_surfaces_and_is_not_recorded_as_parse_failure(inbox):
    message = make_message(inbox, 221)
    with patch("apps.ingest.parser.finalize_success", side_effect=DatabaseError("write failed")):
        with pytest.raises(CommandError, match="database failure"):
            call_command("reconcile_ingest", "--once")
    message.refresh_from_db()
    assert message.ingest_status == Message.IngestStatus.PARSING
    assert message.parse_error == ""
