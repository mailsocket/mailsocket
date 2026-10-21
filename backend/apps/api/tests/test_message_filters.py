"""Message-list filtering (`has_otp`, `subject_contains`, `from`) on the API.

Verifies the filters behave exactly as the OpenAPI spec describes, that cursors
are bound to the active filter (cannot cross filter boundaries), and that no
IDOR regression is introduced by the filter path.
"""

import hashlib

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, AccountPlan
from apps.accounts.services import create_api_key
from apps.api.tests.support import body_without_request_id
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


def client_for(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


@pytest.fixture
def fixture_data():
    alice = make_user("filter_alice")
    bob = make_user("filter_bob")
    _, alice_token = create_api_key(alice, "Alice filter CI")
    _, bob_token = create_api_key(bob, "Bob filter CI")
    alice_inbox = Inbox.objects.create(user=alice, routing_key="filter_alice_box")
    bob_inbox = Inbox.objects.create(user=bob, routing_key="filter_bob_box")

    # Alice: mixed OTP / no-OTP, mixed subject/from.
    make_message(alice_inbox, "otp-login", otp="123456", subject="Your login code", header_from="Security <security@bank.test>")
    make_message(alice_inbox, "otp-reset", otp="654321", subject="Password reset code", header_from="Security <security@bank.test>")
    make_message(alice_inbox, "no-otp-news", subject="Your weekly newsletter", header_from="News <news@example.test>")
    make_message(alice_inbox, "no-otp-receipt", subject="Order receipt", header_from="Shop <orders@shop.test>")

    # Bob's inbox (must never leak into Alice's results).
    make_message(bob_inbox, "bob-secret", otp="999999", subject="Bob secret code", header_from="Security <security@bank.test>")

    return {
        "alice_inbox": alice_inbox,
        "bob_inbox": bob_inbox,
        "alice_token": alice_token,
        "bob_token": bob_token,
    }


def _subjects(response):
    return {row["subject"] for row in response.json()["data"]}


def _list_url(inbox):
    return f"/api/v1/inboxes/{inbox.public_id}/messages"


def test_has_otp_true_returns_only_otp_rows(fixture_data):
    client = client_for(fixture_data["alice_token"])
    inbox = fixture_data["alice_inbox"]

    response = client.get(f"{_list_url(inbox)}?has_otp=true")

    assert response.status_code == 200
    subjects = _subjects(response)
    assert subjects == {"Your login code", "Password reset code"}
    for row in response.json()["data"]:
        assert row["otp"] is not None and row["otp"] != ""


def test_subject_contains_filters(fixture_data):
    client = client_for(fixture_data["alice_token"])
    inbox = fixture_data["alice_inbox"]

    response = client.get(f"{_list_url(inbox)}?subject_contains=code")

    assert response.status_code == 200
    assert _subjects(response) == {"Your login code", "Password reset code"}


def test_from_filters(fixture_data):
    client = client_for(fixture_data["alice_token"])
    inbox = fixture_data["alice_inbox"]

    response = client.get(f"{_list_url(inbox)}?from=shop.test")

    assert response.status_code == 200
    assert _subjects(response) == {"Order receipt"}


def test_combined_filters_are_anded(fixture_data):
    client = client_for(fixture_data["alice_token"])
    inbox = fixture_data["alice_inbox"]

    response = client.get(f"{_list_url(inbox)}?has_otp=1&subject_contains=login")

    assert response.status_code == 200
    assert _subjects(response) == {"Your login code"}

    # Same sender + OTP narrows to two.
    response = client.get(f"{_list_url(inbox)}?has_otp=1&from=security@bank.test")
    assert _subjects(response) == {"Your login code", "Password reset code"}


def test_foreign_inbox_still_404s(fixture_data):
    client = client_for(fixture_data["alice_token"])
    bob_inbox = fixture_data["bob_inbox"]

    # Filter params must not change the ownership check.
    response = client.get(f"{_list_url(bob_inbox)}?has_otp=1&subject_contains=code&from=security")

    assert response.status_code == 404
    body = body_without_request_id(response)
    assert body == {"error": {"code": "not_found", "message": "Resource not found.", "fields": {}}}


def test_blank_and_garbage_params_are_graceful(fixture_data):
    client = client_for(fixture_data["alice_token"])
    inbox = fixture_data["alice_inbox"]

    response = client.get(
        f"{_list_url(inbox)}?has_otp=maybe&subject_contains=&from="
    )

    assert response.status_code == 200
    # No filters applied -> all four of Alice's messages.
    assert len(response.json()["data"]) == 4

    # A 200-char subject cap still returns 200 (and does not 500).
    response = client.get(f"{_list_url(inbox)}?subject_contains={'x' * 500}")
    assert response.status_code == 200


def test_filtered_pagination_cursor_stays_within_filter(fixture_data):
    client = client_for(fixture_data["alice_token"])
    inbox = fixture_data["alice_inbox"]

    # Seed enough OTP rows to force pagination with limit=1.
    for index in range(3):
        make_message(inbox, f"otp-bulk-{index}", otp=f"00000{index}", subject=f"bulk {index}")

    first = client.get(f"{_list_url(inbox)}?has_otp=1&limit=1")
    assert first.status_code == 200
    assert len(first.json()["data"]) == 1
    cursor = first.json()["pagination"]["next_cursor"]
    assert cursor is not None

    # Continuing with the same filter keeps returning only OTP rows.
    seen = {first.json()["data"][0]["subject"]}
    while cursor:
        page = client.get(f"{_list_url(inbox)}?has_otp=1&limit=1&cursor={cursor}")
        assert page.status_code == 200
        rows = page.json()["data"]
        for row in rows:
            assert row["otp"] is not None and row["otp"] != ""
            seen.add(row["subject"])
        cursor = page.json()["pagination"]["next_cursor"]

    # All OTP rows (5: 2 seeded in fixture + 3 bulk) and no non-OTP rows.
    assert len(seen) == 5


def test_cursor_cannot_cross_filter_boundaries(fixture_data):
    client = client_for(fixture_data["alice_token"])
    inbox = fixture_data["alice_inbox"]

    for index in range(30):
        make_message(inbox, f"otp-cross-{index}", otp=f"1111{index}", subject=f"otp cross {index}")

    first = client.get(f"{_list_url(inbox)}?has_otp=1&limit=10")
    cursor = first.json()["pagination"]["next_cursor"]
    assert cursor is not None

    # Reusing the cursor under a DIFFERENT filter (no has_otp) must be rejected,
    # because the active_filter embedded in the cursor no longer matches.
    wrong_filter = client.get(f"{_list_url(inbox)}?limit=10&cursor={cursor}")
    assert wrong_filter.status_code == 422

    # Reusing the cursor under the SAME filter still works.
    same_filter = client.get(f"{_list_url(inbox)}?has_otp=1&limit=10&cursor={cursor}")
    assert same_filter.status_code == 200





class _Params(dict):
    pass


@pytest.mark.parametrize(
    "a, b",
    [
        ({"subject_contains": "a|from=b"}, {"subject_contains": "a", "from": "b"}),
        ({"from": "x|q=y"}, {"from": "x", "q": "y"}),
        ({"subject_contains": "s|read=true"}, {"subject_contains": "s", "read": "true"}),
        ({"subject_contains": "has_otp=1"}, {"has_otp": "1"}),
    ],
)
def test_distinct_filter_sets_never_share_a_signature(a, b):
    from apps.inboxes.filters import apply_message_filters

    _, sig_a = apply_message_filters(Message.objects.none(), _Params(a))
    _, sig_b = apply_message_filters(Message.objects.none(), _Params(b))
    assert sig_a and sig_b
    assert sig_a != sig_b


def test_no_filter_signature_stays_empty():
    from apps.inboxes.filters import apply_message_filters

    _, sig = apply_message_filters(Message.objects.none(), _Params({"read": "all"}))
    assert sig == ""


def test_filter_collision_cursor_does_not_replay_across_filters(fixture_data):
    "Public API."
    from urllib.parse import quote

    client = client_for(fixture_data["alice_token"])
    inbox = fixture_data["alice_inbox"]
    for index in range(4):
        make_message(
            inbox, f"col-{index}", subject=f"a|from=b {index}", header_from="b <b@x.test>"
        )
    first = client.get(f"{_list_url(inbox)}?subject_contains={quote('a|from=b')}&limit=1")
    assert first.status_code == 200
    cursor = first.json()["pagination"]["next_cursor"]
    assert cursor
    replay = client.get(f"{_list_url(inbox)}?subject_contains=a&from=b&limit=1&cursor={cursor}")
    assert replay.status_code == 422
    same = client.get(
        f"{_list_url(inbox)}?subject_contains={quote('a|from=b')}&limit=1&cursor={cursor}"
    )
    assert same.status_code == 200
