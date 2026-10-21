import hashlib
import threading
import time
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, AccountPlan, WaitCapacity, WaitLease
from apps.accounts.services import (
    acquire_wait_capacity,
    acquire_wait_lease,
    create_api_key,
    release_wait_capacity,
    release_wait_lease,
)
from apps.api.tests.support import body_without_request_id
from apps.inboxes.models import Inbox, Message

pytestmark = pytest.mark.django_db(transaction=True)

NOT_FOUND = {"error": {"code": "not_found", "message": "Resource not found.", "fields": {}}}


def make_user(name, *, plan=AccountPlan.Plan.PAID):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user,
        email_normalized=f"{name}@example.test",
        email_verified_at=timezone.now(),
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
        "header_from": "Sender <sender@example.test>",
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


def auth_client(user):
    _, token = create_api_key(user, "wait-test")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def wait_url(inbox):
    return f"/api/v1/inboxes/{inbox.public_id}/messages/wait"


def test_parse_wait_timeout_clamps():
    from apps.api.views import _parse_wait_timeout

    assert _parse_wait_timeout(None) == 20
    assert _parse_wait_timeout("999") == 25
    assert _parse_wait_timeout("0") == 1
    assert _parse_wait_timeout("-5") == 1
    assert _parse_wait_timeout("abc") == 20
    assert _parse_wait_timeout("15") == 15
    assert _parse_wait_timeout("nan") == 20


def test_wait_returns_message_arriving_during_wait(monkeypatch):
    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    owner = make_user("wait_arrive")
    inbox = Inbox.objects.create(user=owner, routing_key="wait_arrive_box")
    client = auth_client(owner)

    def create_mid_wait():
        close_old_connections()
        time.sleep(0.3)
        make_message(inbox, "late-otp", otp="123456", otp_confidence=0.9)
        close_old_connections()

    thread = threading.Thread(target=create_mid_wait)
    thread.start()
    response = client.get(f"{wait_url(inbox)}?timeout=3")
    thread.join(timeout=5)

    assert response.status_code == 200
    assert response.json()["data"]["otp"] == "123456"
    assert not thread.is_alive()


def test_wait_times_out_with_204(monkeypatch):
    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    owner = make_user("wait_timeout")
    inbox = Inbox.objects.create(user=owner, routing_key="wait_timeout_box")
    client = auth_client(owner)

    response = client.get(f"{wait_url(inbox)}?timeout=1")

    assert response.status_code == 204
    assert response.content == b""


def test_wait_require_link_matches_link_not_otp(monkeypatch):
    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    owner = make_user("wait_link")
    inbox = Inbox.objects.create(user=owner, routing_key="wait_link_box")
    client = auth_client(owner)

    make_message(inbox, "otp-only", otp="111111", otp_confidence=0.9, magic_link=None, links=[])
    make_message(
        inbox,
        "link-only",
        otp=None,
        otp_confidence=None,
        magic_link="https://example.test/go?token=abcdefghijklmnopqrstuvwxyz",
        links=["https://example.test/go?token=abcdefghijklmnopqrstuvwxyz"],
    )

    response = client.get(f"{wait_url(inbox)}?require=link&since=0&timeout=1")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["magic_link"] == "https://example.test/go?token=abcdefghijklmnopqrstuvwxyz"
    assert data["otp"] is None


def test_wait_min_confidence_filters_low_confidence(monkeypatch):
    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    owner = make_user("wait_conf")
    inbox = Inbox.objects.create(user=owner, routing_key="wait_conf_box")
    client = auth_client(owner)

    make_message(inbox, "low", otp="222222", otp_confidence=0.35)
    make_message(inbox, "high", otp="333333", otp_confidence=0.95)

    matched = client.get(f"{wait_url(inbox)}?min_confidence=0.9&since=0&timeout=1")
    assert matched.status_code == 200
    assert matched.json()["data"]["otp"] == "333333"

    no_match = client.get(f"{wait_url(inbox)}?min_confidence=0.99&since=0&timeout=1")
    assert no_match.status_code == 204


def test_wait_default_since_excludes_existing_old_code(monkeypatch):
    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    owner = make_user("wait_since")
    inbox = Inbox.objects.create(user=owner, routing_key="wait_since_box")
    client = auth_client(owner)

    make_message(
        inbox,
        "old",
        otp="444444",
        otp_confidence=0.9,
        received_at=timezone.now() - timedelta(minutes=5),
    )

    # Default since = request start -> the pre-existing code is NOT returned.
    future_only = client.get(f"{wait_url(inbox)}?timeout=1")
    assert future_only.status_code == 204

    # since=0 (epoch) -> "any existing unseen code" -> the old code IS returned.
    any_existing = client.get(f"{wait_url(inbox)}?since=0&timeout=1")
    assert any_existing.status_code == 200
    assert any_existing.json()["data"]["otp"] == "444444"


def test_wait_since_iso8601_returns_newer_message(monkeypatch):
    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    owner = make_user("wait_iso")
    inbox = Inbox.objects.create(user=owner, routing_key="wait_iso_box")
    client = auth_client(owner)

    make_message(inbox, "recent", otp="555555", otp_confidence=0.9)
    cutoff = (timezone.now() - timedelta(seconds=30)).isoformat()

    response = client.get(f"{wait_url(inbox)}?since={cutoff}&timeout=1")

    assert response.status_code == 200
    assert response.json()["data"]["otp"] == "555555"


def test_wait_timeout_is_clamped_not_999(monkeypatch):
    monkeypatch.setattr("apps.api.views.WAIT_TIMEOUT_MAX", 1)
    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    owner = make_user("wait_clamp")
    inbox = Inbox.objects.create(user=owner, routing_key="wait_clamp_box")
    client = auth_client(owner)

    start = time.monotonic()
    response = client.get(f"{wait_url(inbox)}?timeout=999")
    elapsed = time.monotonic() - start

    assert response.status_code == 204
    assert elapsed < 5


def test_wait_ownership_returns_404(monkeypatch):
    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    owner = make_user("wait_owner")
    other = make_user("wait_foreign")
    inbox = Inbox.objects.create(user=other, routing_key="wait_foreign_box")
    client = auth_client(owner)

    response = client.get(f"{wait_url(inbox)}?timeout=1")

    assert response.status_code == 404
    assert body_without_request_id(response) == NOT_FOUND


def test_wait_concurrent_cap_returns_429(monkeypatch):
    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    monkeypatch.setattr("apps.accounts.services.WAIT_LEASE_LIMIT_FREE", 1)
    owner = make_user("wait_cap", plan=AccountPlan.Plan.FREE)
    inbox = Inbox.objects.create(user=owner, routing_key="wait_cap_box")
    client = auth_client(owner)

    # Occupy the single available slot.
    assert acquire_wait_lease(owner.pk) is True

    response = client.get(f"{wait_url(inbox)}?timeout=1")

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "too_many_wait_requests"


def test_wait_lease_release_frees_slot():
    owner = make_user("wait_lease", plan=AccountPlan.Plan.PAID)

    assert acquire_wait_lease(owner.pk) is True
    assert acquire_wait_lease(owner.pk) is True
    assert acquire_wait_lease(owner.pk) is True
    assert WaitLease.objects.get(user_id=owner.pk).count == 3

    release_wait_lease(owner.pk)
    assert WaitLease.objects.get(user_id=owner.pk).count == 2


def test_wait_lease_self_heals_after_grace(monkeypatch):
    owner = make_user("wait_heal")
    WaitLease.objects.create(
        user_id=owner.pk,
        count=5,
        expires_at=timezone.now() - timedelta(seconds=120),
    )

    # Expired row is reset on next acquire instead of refusing the slot.
    assert acquire_wait_lease(owner.pk) is True
    lease = WaitLease.objects.get(user_id=owner.pk)
    assert lease.count == 1


def test_wait_capacity_global_cap_returns_429(monkeypatch):
    from apps.accounts.services import WAIT_CAPACITY_KEY

    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    monkeypatch.setattr("apps.accounts.services.WAIT_CAPACITY_LIMIT", 3)
    # Hold 3 global slots (the monkeypatched cap) across two accounts.
    owner_a = make_user("cap_global_a")
    make_user("cap_global_b")
    assert acquire_wait_capacity() is True
    assert acquire_wait_capacity() is True
    assert acquire_wait_capacity() is True

    inbox = Inbox.objects.create(user=owner_a, routing_key="cap_global_box")
    client = auth_client(owner_a)
    response = client.get(f"{wait_url(inbox)}?timeout=1")

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "wait_capacity"

    # Release and a new acquire succeeds.
    release_wait_capacity()
    assert acquire_wait_capacity() is True
    assert WaitCapacity.objects.get(key=WAIT_CAPACITY_KEY).count == 3


def test_wait_capacity_global_cap_counts_across_accounts(monkeypatch):
    monkeypatch.setattr("apps.accounts.services.WAIT_CAPACITY_LIMIT", 2)
    owner_a = make_user("cap_sum_a", plan=AccountPlan.Plan.PAID)
    owner_b = make_user("cap_sum_b", plan=AccountPlan.Plan.PAID)

    # Each account is under its own per-account cap, but the SUM hits the
    # global cap, proving the global counter is not per-account.
    assert acquire_wait_lease(owner_a.pk) is True
    assert acquire_wait_capacity() is True
    assert acquire_wait_lease(owner_b.pk) is True
    assert acquire_wait_capacity() is True
    # A third wait (from a third account) would exceed the global cap of 2.
    owner_c = make_user("cap_sum_c", plan=AccountPlan.Plan.PAID)
    assert acquire_wait_lease(owner_c.pk) is True
    assert acquire_wait_capacity() is False


def test_wait_capacity_self_heals_after_grace():
    from apps.accounts.services import WAIT_CAPACITY_KEY

    WaitCapacity.objects.create(
        key=WAIT_CAPACITY_KEY,
        count=100,
        expires_at=timezone.now() - timedelta(seconds=120),
    )

    assert acquire_wait_capacity() is True
    assert WaitCapacity.objects.get(key=WAIT_CAPACITY_KEY).count == 1


def test_free_account_cap_is_2_and_paid_is_10():
    
    free = make_user("plan_free", plan=AccountPlan.Plan.FREE)
    paid = make_user("plan_paid", plan=AccountPlan.Plan.PAID)

    assert acquire_wait_lease(free.pk) is True
    assert acquire_wait_lease(free.pk) is True
    assert acquire_wait_lease(free.pk) is False  # free capped at 2
    assert WaitLease.objects.get(user_id=free.pk).count == 2

    for _ in range(10):
        assert acquire_wait_lease(paid.pk) is True
    assert acquire_wait_lease(paid.pk) is False  # paid capped at 10
    assert WaitLease.objects.get(user_id=paid.pk).count == 10


def test_wait_free_account_third_concurrent_returns_429(monkeypatch):
    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    free = make_user("wait_free_cap", plan=AccountPlan.Plan.FREE)
    inbox = Inbox.objects.create(user=free, routing_key="wait_free_cap_box")
    client = auth_client(free)

    assert acquire_wait_lease(free.pk) is True
    assert acquire_wait_lease(free.pk) is True

    response = client.get(f"{wait_url(inbox)}?timeout=1")

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "too_many_wait_requests"


def test_first_ever_wait_race_never_500s(monkeypatch):
    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    from concurrent.futures import ThreadPoolExecutor

    owner = make_user("wait_first_race", plan=AccountPlan.Plan.PAID)

    def first_wait(_):
        close_old_connections()
        try:
            return acquire_wait_lease(owner.pk)
        finally:
            close_old_connections()

    
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(first_wait, range(12)))

    # No IntegrityError surfaced; every acquire returned a clean boolean and
    # the account's cap (10 for paid) bounds the granted slots.
    assert all(isinstance(r, bool) for r in results)
    assert results.count(True) == 10
    assert WaitLease.objects.get(user_id=owner.pk).count == 10


def test_wait_capacity_first_ever_race_never_500s(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from apps.accounts.services import WAIT_CAPACITY_KEY

    monkeypatch.setattr("apps.accounts.services.WAIT_CAPACITY_LIMIT", 5)

    def first_acquire(_):
        close_old_connections()
        try:
            return acquire_wait_capacity()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(first_acquire, range(8)))

    assert all(isinstance(r, bool) for r in results)
    assert results.count(True) == 5
    assert WaitCapacity.objects.get(key=WAIT_CAPACITY_KEY).count == 5


def test_waitlease_user_delete_cascades():
    owner = make_user("wait_cascade")
    assert acquire_wait_lease(owner.pk) is True
    assert WaitLease.objects.filter(user_id=owner.pk).exists()

    # Account and AccountPlan legitimately PROTECT the user (they guard real
    # data); remove them first, then deleting the user must NOT be blocked by
    # the ephemeral WaitLease row — it cascades away.
    AccountPlan.objects.filter(user_id=owner.pk).delete()
    Account.objects.filter(user_id=owner.pk).delete()
    owner.delete()

    assert not WaitLease.objects.filter(user_id=owner.pk).exists()


def test_perform_wait_releases_lease_if_capacity_acquire_raises(monkeypatch):
    """ocr-found HIGH edge: if acquire_wait_capacity() raises an unexpected
    error (e.g. a DB deadlock, not the IntegrityError it handles internally),
    the lease acquired just before must still be released rather than leaked
    until its grace timeout."""
    from apps.accounts import services as services_mod
    from apps.api import views as views_mod

    owner = make_user("leakguard")
    inbox = Inbox.objects.create(user=owner, routing_key="leakguardbox0001")

    boom = RuntimeError("simulated deadlock in capacity acquire")

    def _raising_capacity(*args, **kwargs):
        raise boom

    monkeypatch.setattr(services_mod, "acquire_wait_capacity", _raising_capacity)

    # The lease should be free before and after (released on the raise path).
    assert not WaitLease.objects.filter(user=owner, count__gt=0).exists()

    with pytest.raises(RuntimeError):
        views_mod.perform_wait(
            user=owner,
            inbox=inbox,
            require="any",
            min_confidence=0.0,
            timeout=1,
            since=timezone.now(),
        )

    # A fresh lease can be acquired => the previous one was released, not leaked.
    assert acquire_wait_lease(owner.pk) is True
    release_wait_lease(owner.pk)
