"""Dashboard message-list filtering matches the API and never leaks foreign rows."""

import hashlib

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan
from apps.inboxes.models import Inbox, Message

pytestmark = pytest.mark.django_db(transaction=True)


def make_user(name):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user,
        email_normalized=f"{name}@example.test",
        email_verified_at=timezone.now(),
    )
    AccountPlan.objects.create(user=user, plan=AccountPlan.Plan.PAID)
    return user


def make_message(inbox, marker, *, otp=None, subject=None, header_from=None):
    raw = f"Subject: {subject or marker}\r\n\r\nbody {marker}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    values = {
        "inbox": inbox,
        "dedupe_key": digest,
        "envelope_from": "sender@example.test",
        "envelope_recipient": inbox.address,
        "header_from": header_from or f"Sender <sender-{marker}@example.test>",
        "header_to": [inbox.address],
        "header_cc": [],
        "subject": subject or marker,
        "text_body": f"text {marker}",
        "html_body": f"<p>{marker}</p>",
        "otp": otp,
        "otp_confidence": 0.9 if otp else None,
        "raw_bytes": raw,
        "raw_sha256": digest,
        "size_bytes": len(raw),
        "ingest_status": Message.IngestStatus.PARSED,
    }
    return Message.objects.create(**values)


@pytest.fixture
def dashboard_data():
    alice = make_user("dash_filter_alice")
    bob = make_user("dash_filter_bob")
    alice_inbox = Inbox.objects.create(user=alice, routing_key="dash_filter_alice_box")
    bob_inbox = Inbox.objects.create(user=bob, routing_key="dash_filter_bob_box")

    make_message(alice_inbox, "otp-login", otp="123456", subject="Alice login code", header_from="Security <security@bank.test>")
    make_message(alice_inbox, "news", subject="Alice newsletter", header_from="News <news@example.test>")

    # Foreign rows that must never appear for Alice, even though they match her filter terms.
    make_message(bob_inbox, "bob-otp", otp="999999", subject="Alice login code", header_from="Security <security@bank.test>")

    return alice, bob, alice_inbox, bob_inbox


def _subjects(response):
    return {message.subject for message in response.context["messages"]}


def test_has_otp_filter_renders_only_otp_rows(dashboard_data):
    alice, _, _, _ = dashboard_data
    client = Client()
    client.force_login(alice)

    response = client.get(reverse("dashboard:messages"), {"has_otp": "1"})

    assert response.status_code == 200
    assert _subjects(response) == {"Alice login code"}


def test_subject_contains_filter_renders_only_matching_rows(dashboard_data):
    alice, _, _, _ = dashboard_data
    client = Client()
    client.force_login(alice)

    response = client.get(reverse("dashboard:messages"), {"subject_contains": "news"})

    assert response.status_code == 200
    assert _subjects(response) == {"Alice newsletter"}


def test_from_filter_renders_only_matching_rows(dashboard_data):
    alice, _, _, _ = dashboard_data
    client = Client()
    client.force_login(alice)

    response = client.get(reverse("dashboard:messages"), {"from": "bank.test"})

    assert response.status_code == 200
    assert _subjects(response) == {"Alice login code"}


def test_another_users_messages_never_appear_even_when_filtering(dashboard_data):
    alice, _, _, bob_inbox = dashboard_data
    client = Client()
    client.force_login(alice)

    # Alice filters by subject/from that Bob ALSO has; Bob's rows must not leak.
    response = client.get(
        reverse("dashboard:messages"),
        {"subject_contains": "login code", "from": "bank.test"},
    )

    assert response.status_code == 200
    assert _subjects(response) == {"Alice login code"}
    body = response.content.decode()
    assert "999999" not in body  # Bob's OTP never rendered

    # qafix2 #14: Bob's inbox id is ignored like an unknown id (no 404 page),
    # and Bob's mail still never renders.
    foreign = client.get(reverse("dashboard:messages"), {"inbox": bob_inbox.public_id})
    assert foreign.status_code == 200
    assert "999999" not in foreign.content.decode()


def test_blank_filter_params_return_all_owned_rows(dashboard_data):
    alice, _, _, _ = dashboard_data
    client = Client()
    client.force_login(alice)

    response = client.get(
        reverse("dashboard:messages"),
        {"subject_contains": "", "from": "", "has_otp": ""},
    )

    assert response.status_code == 200
    assert _subjects(response) == {"Alice login code", "Alice newsletter"}
