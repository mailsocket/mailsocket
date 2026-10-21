"""Idempotency-Key on create/mutate POSTs (trust audit #19).

Covers: header absent = unchanged; replay returns the identical body + the
Idempotent-Replayed header and creates nothing; different body same key ->
422 idempotency_key_reused; cross-tenant same key is independent;
concurrent same key -> exactly one create; invalid header -> 400; purge
removes records older than 24h; 429/5xx are not stored; agents/register
ignores the header.
"""
import threading
from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, AccountPlan, IdempotencyRecord
from apps.accounts.services import create_api_key
from apps.domains.models import Domain
from apps.inboxes.models import Inbox, Message

pytestmark = pytest.mark.django_db(transaction=True)

INBOXES = "/api/v1/inboxes"
BATCH = "/api/v1/messages/batch"
DOMAINS = "/api/v1/domains"


def make_user(name, *, plan=AccountPlan.Plan.PAID):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user, plan=plan)
    return user


def key_and_client(user, *, created_by=None):
    api_key, token = create_api_key(user, "idem-test", created_by=created_by)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return api_key, client


def client_for(user, *, created_by=None):
    return key_and_client(user, created_by=created_by)[1]


def clients_sharing_one_key(user, n):
    """n independent HTTP clients presenting the SAME API key (concurrent
    retries from one caller)."""
    _, token = create_api_key(user, "idem-shared")
    clients = []
    for _ in range(n):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        clients.append(client)
    return clients


def post(client, url, body, key=None):
    extra = {} if key is None else {"HTTP_IDEMPOTENCY_KEY": key}
    return client.post(url, body, format="json", **extra)


def make_message(inbox, marker):
    import hashlib

    raw = f"Subject: {marker}\r\n\r\nbody {marker}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    return Message.objects.create(
        inbox=inbox,
        dedupe_key=digest,
        envelope_recipient=inbox.address,
        raw_bytes=raw,
        raw_sha256=digest,
        size_bytes=len(raw),
        ingest_status=Message.IngestStatus.PARSED,
    )


# --- header absent: unchanged --------------------------------------------------


def test_absent_header_unchanged_creates_every_time_and_stores_nothing():
    user = make_user("idem_absent")
    client = client_for(user)
    first = post(client, INBOXES, {"label": "ci"})
    second = post(client, INBOXES, {"label": "ci"})
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["data"]["id"] != second.json()["data"]["id"]
    assert "Idempotent-Replayed" not in first.headers
    assert "Idempotent-Replayed" not in second.headers
    assert Inbox.objects.filter(user=user).count() == 2
    assert IdempotencyRecord.objects.count() == 0


# --- replay ---------------------------------------------------------------------


def test_replay_returns_identical_body_and_header_and_creates_nothing():
    user = make_user("idem_replay")
    client = client_for(user)
    first = post(client, INBOXES, {"label": "ci", "metadata": {"b": 1, "a": 2}}, key="run-123")
    assert first.status_code == 201
    assert "Idempotent-Replayed" not in first.headers
    assert Inbox.objects.filter(user=user).count() == 1

    # Same body, keys in a different order: canonical-JSON fingerprint matches.
    second = post(client, INBOXES, {"metadata": {"a": 2, "b": 1}, "label": "ci"}, key="run-123")
    assert second.status_code == 201
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.content == first.content
    assert second.headers["Location"] == first.headers["Location"]
    assert Inbox.objects.filter(user=user).count() == 1
    assert IdempotencyRecord.objects.filter(user=user).count() == 1

    # A new key creates a new inbox.
    third = post(client, INBOXES, {"label": "ci", "metadata": {"b": 1, "a": 2}}, key="run-124")
    assert third.status_code == 201
    assert "Idempotent-Replayed" not in third.headers
    assert Inbox.objects.filter(user=user).count() == 2


def test_replay_saves_plan_limit_under_ci_retry():
    """The audit's scenario: a retry must not burn a second slot of the
    plan's inbox limit."""
    user = make_user("idem_free", plan=AccountPlan.Plan.FREE)
    client = client_for(user)
    first = post(client, INBOXES, {}, key="retry-me")
    retry = post(client, INBOXES, {}, key="retry-me")
    assert first.status_code == 201 and retry.status_code == 201
    assert retry.content == first.content
    assert Inbox.objects.filter(user=user).count() == 1


def test_stored_4xx_is_replayed_with_this_requests_id():
    user = make_user("idem_4xx")
    client = client_for(user)
    first = post(client, INBOXES, {"label": "x" * 500}, key="bad-body")
    assert first.status_code == 422
    second = post(client, INBOXES, {"label": "x" * 500}, key="bad-body")
    assert second.status_code == 422
    assert second.headers["Idempotent-Replayed"] == "true"
    a, b = first.json(), second.json()
    assert a["error"]["request_id"] == first.headers["X-Request-Id"]
    assert b["error"]["request_id"] == second.headers["X-Request-Id"]
    a["error"].pop("request_id"), b["error"].pop("request_id")
    assert a == b
    assert Inbox.objects.filter(user=user).count() == 0


# --- reuse with a different body / endpoint -----------------------------------------


def test_same_key_different_body_is_422_and_creates_nothing():
    user = make_user("idem_reuse")
    client = client_for(user)
    assert post(client, INBOXES, {"label": "one"}, key="k1").status_code == 201
    resp = post(client, INBOXES, {"label": "two"}, key="k1")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "idempotency_key_reused"
    assert "Idempotent-Replayed" not in resp.headers
    assert Inbox.objects.filter(user=user).count() == 1


def test_same_key_different_endpoint_is_422():
    user = make_user("idem_reuse_path")
    client = client_for(user)
    inbox = Inbox.objects.create(user=user, routing_key="idem_reuse_path_box")
    message = make_message(inbox, "m1")
    assert post(client, INBOXES, {}, key="k-shared").status_code == 201
    resp = post(client, BATCH, {"action": "mark_read", "ids": [message.public_id]}, key="k-shared")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "idempotency_key_reused"
    message.refresh_from_db()
    assert message.read_at is None


# --- tenant isolation -------------------------------------------------------------


def test_cross_tenant_same_key_is_independent():
    alice, bob = make_user("idem_alice"), make_user("idem_bob")
    a_client, b_client = client_for(alice), client_for(bob)
    a = post(a_client, INBOXES, {"label": "same"}, key="shared-key")
    # Bob, same key AND same body: a fresh create of his own, never Alice's
    # stored response.
    b = post(b_client, INBOXES, {"label": "same"}, key="shared-key")
    assert a.status_code == 201 and b.status_code == 201
    assert "Idempotent-Replayed" not in b.headers
    assert b.json()["data"]["id"] != a.json()["data"]["id"]
    assert Inbox.objects.get(public_id=b.json()["data"]["id"]).user_id == bob.pk
    # Carol, same key, different body: not 422 (the others' records are invisible).
    carol = make_user("idem_carol")
    c = post(client_for(carol), INBOXES, {"label": "carol"}, key="shared-key")
    assert c.status_code == 201
    # Each tenant's retry replays its OWN response.
    a2 = post(a_client, INBOXES, {"label": "same"}, key="shared-key")
    b2 = post(b_client, INBOXES, {"label": "same"}, key="shared-key")
    assert a2.content == a.content and b2.content == b.content
    assert Inbox.objects.filter(user=alice).count() == 1
    assert Inbox.objects.filter(user=bob).count() == 1
    assert IdempotencyRecord.objects.filter(key="shared-key").count() == 3


def test_two_api_keys_of_same_tenant_are_independent():
    """r2: scope is the AUTHORIZING API key inside its tenant, so another
    key of the same tenant never replays this key's stored response."""
    user = make_user("idem_two_keys")
    one, two = client_for(user), client_for(user)
    first = post(one, INBOXES, {"label": "x"}, key="k")
    second = post(two, INBOXES, {"label": "x"}, key="k")
    assert first.status_code == 201 and second.status_code == 201
    assert "Idempotent-Replayed" not in second.headers
    assert second.json()["data"]["id"] != first.json()["data"]["id"]
    # Each key still replays its own response.
    again = post(one, INBOXES, {"label": "x"}, key="k")
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.content == first.content
    assert Inbox.objects.filter(user=user).count() == 2
    assert IdempotencyRecord.objects.filter(user=user, key="k").count() == 2


# --- concurrency ---------------------------------------------------------------------


def test_concurrent_same_key_creates_exactly_once(monkeypatch):
    user = make_user("idem_race")
    clients = clients_sharing_one_key(user, 4)

    # Hold the first create open so every other request arrives while it is
    # still in flight (the placeholder row is the lock).
    import apps.api.views as views

    real = views.create_random_inbox
    entered = threading.Event()
    release = threading.Event()

    def slow_create(*args, **kwargs):
        entered.set()
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(views, "create_random_inbox", slow_create)

    results = [None] * len(clients)

    def worker(i):
        try:
            results[i] = post(clients[i], INBOXES, {"label": "race"}, key="race-key")
        finally:
            connection.close()

    first = threading.Thread(target=worker, args=(0,))
    first.start()
    assert entered.wait(10)
    others = [threading.Thread(target=worker, args=(i,)) for i in range(1, len(clients))]
    for t in others:
        t.start()
    for t in others:
        t.join(15)
    release.set()
    first.join(15)

    statuses = sorted(r.status_code for r in results)
    assert statuses == [201, 409, 409, 409], statuses
    for r in results:
        if r.status_code == 409:
            assert r.json()["error"]["code"] == "idempotency_in_progress"
    assert Inbox.objects.filter(user=user).count() == 1

    # Once finished, the same key replays the single create.
    replay = post(clients[1], INBOXES, {"label": "race"}, key="race-key")
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.content == next(r for r in results if r.status_code == 201).content
    assert Inbox.objects.filter(user=user).count() == 1


def test_concurrent_same_key_without_stall_never_double_creates():
    user = make_user("idem_race_free")
    clients = clients_sharing_one_key(user, 6)
    barrier = threading.Barrier(len(clients))
    results = [None] * len(clients)

    def worker(i):
        try:
            barrier.wait(10)
            results[i] = post(clients[i], INBOXES, {"label": "race"}, key="race-free")
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(clients))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert all(r.status_code in (201, 409) for r in results), [r.status_code for r in results]
    assert Inbox.objects.filter(user=user).count() == 1
    created = {r.json()["data"]["id"] for r in results if r.status_code == 201}
    assert len(created) == 1


# --- header validation --------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "x" * 256, "caf\u00e9", "tab\there", "nl\nx"])
def test_invalid_key_is_400_validation_error_and_creates_nothing(bad):
    user = make_user("idem_invalid")
    client = client_for(user)
    resp = client.post(INBOXES, {}, format="json", HTTP_IDEMPOTENCY_KEY=bad)
    assert resp.status_code == 400
    body = resp.json()["error"]
    assert body["code"] == "validation_error"
    assert "Idempotency-Key" in body["fields"]
    assert Inbox.objects.filter(user=user).count() == 0
    assert IdempotencyRecord.objects.count() == 0


def test_boundary_keys_accepted():
    user = make_user("idem_boundary")
    client = client_for(user)
    assert post(client, INBOXES, {}, key="x" * 255).status_code == 201
    assert post(client, INBOXES, {}, key="~ !").status_code == 201


# --- not stored: 429 / 5xx -------------------------------------------------------------


def test_429_is_not_stored_so_retry_runs_again():
    user = make_user("idem_429")
    client = client_for(user)
    from apps.accounts.services import increment_rate_limit

    for _ in range(10):
        increment_rate_limit("api:user:inbox-create:hour", str(user.pk), limit=10, window_seconds=3600)
    resp = post(client, INBOXES, {}, key="rl-key")
    assert resp.status_code == 429
    assert IdempotencyRecord.objects.count() == 0


def test_5xx_is_not_stored_so_retry_runs_again(monkeypatch):
    import apps.api.views as views

    user = make_user("idem_5xx")
    client = client_for(user)
    real = views.create_random_inbox
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(views, "create_random_inbox", flaky)
    client.raise_request_exception = False
    first = post(client, INBOXES, {}, key="5xx-key")
    assert first.status_code == 500
    assert IdempotencyRecord.objects.count() == 0
    second = post(client, INBOXES, {}, key="5xx-key")
    assert second.status_code == 201
    assert "Idempotent-Replayed" not in second.headers
    assert Inbox.objects.filter(user=user).count() == 1


def test_abandoned_placeholder_is_reclaimed():
    user = make_user("idem_abandoned")
    api_key, client = key_and_client(user)
    IdempotencyRecord.objects.create(
        user=user, api_key=api_key, key="stuck", method="POST", path=INBOXES, request_fingerprint="0" * 64,
        created_at=timezone.now() - timedelta(minutes=10),
    )
    resp = post(client, INBOXES, {}, key="stuck")
    assert resp.status_code == 201
    assert Inbox.objects.filter(user=user).count() == 1


# --- messages/batch ------------------------------------------------------------------


def test_batch_delete_replay_and_fetch_ignored():
    user = make_user("idem_batch")
    client = client_for(user)
    inbox = Inbox.objects.create(user=user, routing_key="idem_batch_box")
    message = make_message(inbox, "m1")
    body = {"action": "delete", "ids": [message.public_id]}
    first = post(client, BATCH, body, key="del-1")
    assert first.json()["data"]["results"][0]["status"] == "deleted"
    second = post(client, BATCH, body, key="del-1")
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.content == first.content  # replay says "deleted", not "not_found"

    other = make_message(inbox, "m2")
    fetch = {"action": "fetch", "ids": [other.public_id]}
    a = post(client, BATCH, fetch, key="fetch-1")
    b = post(client, BATCH, fetch, key="fetch-1")
    assert "Idempotent-Replayed" not in a.headers and "Idempotent-Replayed" not in b.headers
    assert IdempotencyRecord.objects.filter(key="fetch-1").count() == 0


# --- domains --------------------------------------------------------------------------


@override_settings(ENABLE_BYOD=True)
def test_domain_create_replay():
    user = make_user("idem_domain", plan=AccountPlan.Plan.BUSINESS)
    client = client_for(user)
    first = post(client, DOMAINS, {"name": "idem.example.com"}, key="dom-1")
    assert first.status_code == 201, first.json()
    second = post(client, DOMAINS, {"name": "idem.example.com"}, key="dom-1")
    assert second.status_code == 201
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.content == first.content
    assert Domain.objects.filter(user=user).count() == 1


# --- agents/register: unsupported ------------------------------------------------------


def test_agent_register_ignores_header():
    client = APIClient()
    first = client.post("/api/v1/agents/register", {}, format="json", HTTP_IDEMPOTENCY_KEY="agent-1")
    second = client.post("/api/v1/agents/register", {}, format="json", HTTP_IDEMPOTENCY_KEY="agent-1")
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["data"]["api_key"] != second.json()["data"]["api_key"]
    assert "Idempotent-Replayed" not in second.headers
    assert IdempotencyRecord.objects.count() == 0
    # Even an invalid header is ignored here.
    third = client.post("/api/v1/agents/register", {}, format="json", HTTP_IDEMPOTENCY_KEY="")
    assert third.status_code == 201


# --- outbound off: header must not change the uniform 404 -------------------------------


@override_settings(ENABLE_OUTBOUND=False)
def test_outbound_off_header_does_not_change_404():
    user = make_user("idem_outbound_off", plan=AccountPlan.Plan.BUSINESS)
    client = client_for(user)
    plain = client.post("/api/v1/outbound_emails", {}, format="json")
    with_bad = client.post("/api/v1/outbound_emails", {}, format="json", HTTP_IDEMPOTENCY_KEY="")
    assert plain.status_code == with_bad.status_code == 404
    assert IdempotencyRecord.objects.count() == 0


# --- retention purge --------------------------------------------------------------------


def test_purge_removes_records_older_than_24h():
    user = make_user("idem_purge")
    api_key, _ = key_and_client(user)
    old = IdempotencyRecord.objects.create(
        user=user, api_key=api_key, key="old", method="POST", path=INBOXES, request_fingerprint="a" * 64,
        status_code=201, response_body="{}", created_at=timezone.now() - timedelta(hours=24, seconds=1),
    )
    fresh = IdempotencyRecord.objects.create(
        user=user, api_key=api_key, key="fresh", method="POST", path=INBOXES, request_fingerprint="b" * 64,
        status_code=201, response_body="{}", created_at=timezone.now() - timedelta(hours=23),
    )
    out = StringIO()
    call_command("purge_expired_messages", stdout=out)
    assert not IdempotencyRecord.objects.filter(pk=old.pk).exists()
    assert IdempotencyRecord.objects.filter(pk=fresh.pk).exists()
    assert "idempotency_records=1" in out.getvalue()


def test_expired_key_is_treated_as_new():
    user = make_user("idem_expired")
    client = client_for(user)
    first = post(client, INBOXES, {"label": "a"}, key="day-old")
    IdempotencyRecord.objects.filter(key="day-old").update(
        created_at=timezone.now() - timedelta(hours=25)
    )
    # Not purged yet, but past 24h: a different body is a new request, not 422.
    second = post(client, INBOXES, {"label": "b"}, key="day-old")
    assert second.status_code == 201
    assert "Idempotent-Replayed" not in second.headers
    assert second.json()["data"]["id"] != first.json()["data"]["id"]


# =====================================================================================

# =====================================================================================


class _WorkerKilled(BaseException):
    """Stands in for SIGKILL / gunicorn timeout: not an Exception, so nothing
    in the request path catches it, and the cleanup step never runs."""


# --- HIGH 1: business mutation and record completion commit together ------------------


def test_completion_failure_rolls_back_the_create_and_retry_creates_exactly_one(monkeypatch):
    """(i) The handler succeeded, then saving the record failed: the inbox
    must NOT be persisted (it would be orphaned and the retry would mint a
    second one), the placeholder is released, and the retry creates exactly
    one inbox."""
    import apps.api.idempotency as idem

    user = make_user("idem_complete_fail")
    client = client_for(user)
    real_complete = idem._complete
    calls = {"n": 0}

    def failing_complete(record, response):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db went away while saving the record")
        return real_complete(record, response)

    monkeypatch.setattr(idem, "_complete", failing_complete)
    client.raise_request_exception = False
    first = post(client, INBOXES, {"label": "atomic"}, key="atomic-1")
    assert first.status_code == 500
    assert Inbox.objects.filter(user=user).count() == 0
    assert IdempotencyRecord.objects.count() == 0

    retry = post(client, INBOXES, {"label": "atomic"}, key="atomic-1")
    assert retry.status_code == 201
    assert "Idempotent-Replayed" not in retry.headers
    assert Inbox.objects.filter(user=user).count() == 1
    replay = post(client, INBOXES, {"label": "atomic"}, key="atomic-1")
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.content == retry.content
    assert Inbox.objects.filter(user=user).count() == 1


def test_worker_death_after_handler_leaves_no_committed_work_and_reclaim_creates_once(monkeypatch):
    """(ii) The worker dies after the handler ran but before the completion
    committed (and before any cleanup). The handler's work must be
    impossible to have committed: no inbox, only the placeholder. While the
    placeholder is fresh, a retry gets 409; once it is stale (5 min), the
    reclaim creates exactly one inbox."""
    import apps.api.idempotency as idem

    user = make_user("idem_worker_death")
    client = client_for(user)
    real_complete, real_release = idem._complete, idem._release

    def killed(record, response):
        raise _WorkerKilled()

    monkeypatch.setattr(idem, "_complete", killed)
    monkeypatch.setattr(idem, "_release", lambda record: None)  # the dead worker cleans nothing
    with pytest.raises(_WorkerKilled):
        post(client, INBOXES, {"label": "death"}, key="death-1")
    monkeypatch.setattr(idem, "_complete", real_complete)
    monkeypatch.setattr(idem, "_release", real_release)

    assert Inbox.objects.filter(user=user).count() == 0
    placeholder = IdempotencyRecord.objects.get(user=user, key="death-1")
    assert placeholder.status_code is None

    busy = post(client, INBOXES, {"label": "death"}, key="death-1")
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "idempotency_in_progress"
    assert Inbox.objects.filter(user=user).count() == 0

    IdempotencyRecord.objects.filter(pk=placeholder.pk).update(
        created_at=timezone.now() - timedelta(minutes=6)
    )
    reclaimed = post(client, INBOXES, {"label": "death"}, key="death-1")
    assert reclaimed.status_code == 201
    assert Inbox.objects.filter(user=user).count() == 1
    again = post(client, INBOXES, {"label": "death"}, key="death-1")
    assert again.headers["Idempotent-Replayed"] == "true"
    assert Inbox.objects.filter(user=user).count() == 1


def test_on_commit_hooks_fire_only_after_completion_commits(monkeypatch):
    """Webhook/parse-style on_commit hooks registered by the handler run
    only when the outer transaction (handler + completion) commits, never
    for an attempt whose completion failed."""
    from django.db import transaction as db_transaction

    import apps.api.idempotency as idem
    import apps.api.views as views

    user = make_user("idem_on_commit")
    client = client_for(user)
    fired = []
    real_create = views.create_random_inbox

    def create_with_hook(*args, **kwargs):
        inbox = real_create(*args, **kwargs)
        db_transaction.on_commit(lambda: fired.append(inbox.public_id))
        return inbox

    monkeypatch.setattr(views, "create_random_inbox", create_with_hook)
    real_complete = idem._complete
    monkeypatch.setattr(idem, "_complete", lambda r, resp: (_ for _ in ()).throw(RuntimeError("x")))
    client.raise_request_exception = False
    assert post(client, INBOXES, {}, key="hook-1").status_code == 500
    assert fired == []
    monkeypatch.setattr(idem, "_complete", real_complete)
    ok = post(client, INBOXES, {}, key="hook-1")
    assert ok.status_code == 201
    assert fired == [ok.json()["data"]["id"]]


def test_lost_placeholder_rolls_back_instead_of_committing(monkeypatch):
    """If our placeholder was reclaimed while we were still running (we
    outlived IN_PROGRESS_TIMEOUT), the completion must not commit our work
    on top of whoever reclaimed it."""
    import apps.api.views as views

    user = make_user("idem_lost")
    client = client_for(user)
    real = views.create_random_inbox

    def create_then_lose_claim(*args, **kwargs):
        inbox = real(*args, **kwargs)
        IdempotencyRecord.objects.filter(key="lost-1").delete()  # another worker reclaimed it
        return inbox

    monkeypatch.setattr(views, "create_random_inbox", create_then_lose_claim)
    client.raise_request_exception = False
    resp = post(client, INBOXES, {}, key="lost-1")
    assert resp.status_code == 500
    assert Inbox.objects.filter(user=user).count() == 0


@pytest.mark.parametrize("action", ["delete", "mark_read"])
def test_batch_mutation_rolls_back_when_completion_fails(monkeypatch, action):
    import apps.api.idempotency as idem

    user = make_user(f"idem_batch_atomic_{action}")
    client = client_for(user)
    inbox = Inbox.objects.create(user=user, routing_key=f"idem_batch_atomic_{action}")
    message = make_message(inbox, "m1")
    monkeypatch.setattr(idem, "_complete", lambda r, resp: (_ for _ in ()).throw(RuntimeError("x")))
    client.raise_request_exception = False
    body = {"action": action, "ids": [message.public_id]}
    assert post(client, BATCH, body, key=f"b-{action}").status_code == 500
    message.refresh_from_db()  # still there, still unread
    assert message.read_at is None
    assert IdempotencyRecord.objects.count() == 0


@override_settings(ENABLE_BYOD=True)
def test_domain_create_rolls_back_when_completion_fails(monkeypatch):
    import apps.api.idempotency as idem

    user = make_user("idem_dom_atomic", plan=AccountPlan.Plan.BUSINESS)
    client = client_for(user)
    real_complete = idem._complete
    monkeypatch.setattr(idem, "_complete", lambda r, resp: (_ for _ in ()).throw(RuntimeError("x")))
    client.raise_request_exception = False
    assert post(client, DOMAINS, {"name": "atomic.example.com"}, key="d-1").status_code == 500
    assert Domain.objects.filter(user=user).count() == 0
    monkeypatch.setattr(idem, "_complete", real_complete)
    retry = post(client, DOMAINS, {"name": "atomic.example.com"}, key="d-1")
    assert retry.status_code == 201  # not 409 domain_taken by its own orphan
    assert Domain.objects.filter(user=user).count() == 1


# --- HIGH 2: authorization before replay ----------------------------------------------


def _owner_and_member():
    from apps.accounts.models import TeamMembership

    owner = make_user("idem_team_owner", plan=AccountPlan.Plan.BUSINESS)
    member = make_user("idem_team_member", plan=AccountPlan.Plan.FREE)
    TeamMembership.objects.create(account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER)
    return owner, member


@override_settings(ENABLE_TEAMS=True, ENABLE_BYOD=True)
def test_member_key_never_replays_owner_domain_create():
    "Public API."
    owner, member = _owner_and_member()
    owner_client = client_for(owner)
    member_client = client_for(owner, created_by=member)
    body = {"name": "team-idem.example.com"}

    stored = post(owner_client, DOMAINS, body, key="dom-team")
    assert stored.status_code == 201, stored.json()
    token = stored.json()["data"]["verification_token"]

    resp = post(member_client, DOMAINS, body, key="dom-team")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "account_forbidden"
    assert "Idempotent-Replayed" not in resp.headers
    assert token not in resp.content.decode()
    assert Domain.objects.filter(user=owner).count() == 1
    
    assert IdempotencyRecord.objects.filter(key="dom-team").count() == 1
    owner_again = post(owner_client, DOMAINS, body, key="dom-team")
    assert owner_again.headers["Idempotent-Replayed"] == "true"
    assert owner_again.content == stored.content


@override_settings(ENABLE_TEAMS=True)
def test_member_key_same_key_on_shared_endpoint_runs_its_own_request():
    owner, member = _owner_and_member()
    a = post(client_for(owner), INBOXES, {"label": "t"}, key="team-inbox")
    b = post(client_for(owner, created_by=member), INBOXES, {"label": "t"}, key="team-inbox")
    assert a.status_code == 201 and b.status_code == 201
    assert "Idempotent-Replayed" not in b.headers
    assert a.json()["data"]["id"] != b.json()["data"]["id"]


def test_gate_runs_before_replay_after_plan_downgrade():
    """A stored batch success is not replayed once the caller's plan no
    longer allows batch: the endpoint's own gate answers (402)."""
    user = make_user("idem_downgrade")
    client = client_for(user)
    inbox = Inbox.objects.create(user=user, routing_key="idem_downgrade_box")
    message = make_message(inbox, "m1")
    body = {"action": "mark_read", "ids": [message.public_id]}
    assert post(client, BATCH, body, key="dg-1").status_code == 200
    AccountPlan.objects.filter(user=user).update(plan=AccountPlan.Plan.FREE)
    resp = post(client, BATCH, body, key="dg-1")
    assert resp.status_code == 402
    assert resp.json()["error"]["code"] == "plan_upgrade_required"
    assert "Idempotent-Replayed" not in resp.headers


# --- P2-b: retryable outcomes are not stored --------------------------------------------


def test_plan_limit_reached_is_not_stored_retry_runs_again():
    from apps.accounts.entitlements import plan_limits

    user = make_user("idem_limit", plan=AccountPlan.Plan.FREE)
    client = client_for(user)
    # Fill the Free cap; the last one is freed below.
    for i in range(plan_limits(AccountPlan.Plan.FREE)["inboxes"] - 1):
        Inbox.objects.create(user=user, routing_key=f"idem_limit_fill_{i}")
    existing = Inbox.objects.create(user=user, routing_key="idem_limit_existing")
    refused = post(client, INBOXES, {}, key="lim-1")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "plan_limit_reached"
    assert IdempotencyRecord.objects.count() == 0
    # The user fixes it (frees a slot) and retries with the SAME key.
    existing.deleted_at = timezone.now()
    existing.save(update_fields=["deleted_at"])
    retry = post(client, INBOXES, {}, key="lim-1")
    assert retry.status_code == 201
    assert "Idempotent-Replayed" not in retry.headers


def test_conflict_is_not_stored(monkeypatch):
    import apps.api.views as views

    user = make_user("idem_conflict")
    client = client_for(user)
    real = views.create_random_inbox
    monkeypatch.setattr(views, "create_random_inbox", lambda *a, **k: None)  # allocation exhausted
    first = post(client, INBOXES, {}, key="cf-1")
    assert first.status_code == 409 and first.json()["error"]["code"] == "conflict"
    assert IdempotencyRecord.objects.count() == 0
    monkeypatch.setattr(views, "create_random_inbox", real)
    assert post(client, INBOXES, {}, key="cf-1").status_code == 201


def test_402_is_not_stored():
    user = make_user("idem_402", plan=AccountPlan.Plan.FREE)
    client = client_for(user)
    inbox = Inbox.objects.create(user=user, routing_key="idem_402_box")
    message = make_message(inbox, "m1")
    body = {"action": "delete", "ids": [message.public_id]}
    assert post(client, BATCH, body, key="p-1").status_code == 402
    assert IdempotencyRecord.objects.count() == 0
    AccountPlan.objects.filter(user=user).update(plan=AccountPlan.Plan.PAID)
    retry = post(client, BATCH, body, key="p-1")
    assert retry.status_code == 200
    assert retry.json()["data"]["results"][0]["status"] == "deleted"


@override_settings(ENABLE_BYOD=True)
def test_domain_limit_reached_is_not_stored(monkeypatch):
    import apps.api.views as views

    user = make_user("idem_dom_limit", plan=AccountPlan.Plan.BUSINESS)
    client = client_for(user)
    real = views.can_add_domain
    monkeypatch.setattr(views, "can_add_domain", lambda u: False)  # at the plan's cap
    body = {"name": "limit.example.com"}
    first = post(client, DOMAINS, body, key="dl-1")
    assert first.status_code == 403 and first.json()["error"]["code"] == "domain_limit_reached"
    assert IdempotencyRecord.objects.count() == 0
    monkeypatch.setattr(views, "can_add_domain", real)  # a slot was freed
    retry = post(client, DOMAINS, body, key="dl-1")
    assert retry.status_code == 201
    assert "Idempotent-Replayed" not in retry.headers


def test_validation_422_is_still_stored():
    """Deterministic validation errors stay stored (same body -> same answer)."""
    user = make_user("idem_422_stored")
    client = client_for(user)
    post(client, INBOXES, {"label": "x" * 500}, key="v-1")
    assert IdempotencyRecord.objects.get(key="v-1").status_code == 422


# --- P2-c: unreadable body is rejected before claiming ------------------------------------


@override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=64)
def test_too_large_body_with_key_is_400_and_never_claimed():
    user = make_user("idem_big")
    client = client_for(user)
    big = {"label": "x" * 200}
    resp = post(client, INBOXES, big, key="big-1")
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "validation_error" and "body" in err["fields"]
    assert IdempotencyRecord.objects.count() == 0
    assert Inbox.objects.filter(user=user).count() == 0
    # An empty-body request with the same key is a NEW request, not a
    # replay/mismatch against an "empty" fingerprint.
    small = post(client, INBOXES, {}, key="big-1")
    assert small.status_code == 201
    assert "Idempotent-Replayed" not in small.headers


# --- enabled outbound: success replay, member key, retryable 422 --------------------------


@override_settings(ENABLE_OUTBOUND=True)
def test_outbound_send_replays_and_enqueues_exactly_once():
    from apps.outbound.models import OutboundEmail
    from apps.outbound.tests.test_outbound import eligible_domain, send_body

    user = make_user("idem_out_ok", plan=AccountPlan.Plan.BUSINESS)
    domain = eligible_domain(user, "idem-out-ok.example.com")
    client = client_for(user)
    body = send_body(f"hello@{domain.name}", to=["dest@example.test"])
    first = post(client, "/api/v1/outbound_emails", body, key="send-1")
    assert first.status_code == 201, first.json()
    second = post(client, "/api/v1/outbound_emails", body, key="send-1")
    assert second.status_code == 201
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.content == first.content
    assert OutboundEmail.objects.filter(user=user).count() == 1


@override_settings(ENABLE_OUTBOUND=True, ENABLE_TEAMS=True)
def test_outbound_member_key_gets_403_not_owner_replay():
    from apps.outbound.models import OutboundEmail
    from apps.outbound.tests.test_outbound import eligible_domain, send_body

    owner, member = _owner_and_member()
    domain = eligible_domain(owner, "idem-out-team.example.com")
    body = send_body(f"hello@{domain.name}", to=["dest@example.test"])
    assert post(client_for(owner), "/api/v1/outbound_emails", body, key="send-t").status_code == 201
    resp = post(client_for(owner, created_by=member), "/api/v1/outbound_emails", body, key="send-t")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "account_forbidden"
    assert "Idempotent-Replayed" not in resp.headers
    assert OutboundEmail.objects.filter(user=owner).count() == 1


@override_settings(ENABLE_OUTBOUND=True)
def test_outbound_recipient_not_approved_is_not_stored():
    from apps.accounts.services import normalize_email
    from apps.outbound.models import ApprovedRecipient, OutboundEmail
    from apps.outbound.tests.test_outbound import eligible_domain, send_body

    user = make_user("idem_out_allow", plan=AccountPlan.Plan.BUSINESS)
    domain = eligible_domain(user, "idem-out-allow.example.com")
    Account.objects.filter(pk=user.pk).update(outbound_allowlist_enabled=True)
    client = client_for(user)
    body = send_body(f"hello@{domain.name}", to=["dest@example.test"])
    first = post(client, "/api/v1/outbound_emails", body, key="send-a")
    assert first.status_code == 422 and first.json()["error"]["code"] == "recipient_not_approved"
    assert IdempotencyRecord.objects.count() == 0
    ApprovedRecipient.objects.create(
        user=user, email_normalized=normalize_email("dest@example.test"), created_by=user
    )
    retry = post(client, "/api/v1/outbound_emails", body, key="send-a")
    assert retry.status_code == 201
    assert "Idempotent-Replayed" not in retry.headers
    assert OutboundEmail.objects.filter(user=user).count() == 1


def test_reclaim_never_deletes_a_placeholder_that_completed_meanwhile(monkeypatch):
    "Public API."
    from apps.api import idempotency as idem

    user = make_user("idem_reclaim_race")
    api_key, client = key_and_client(user)
    old = timezone.now() - timedelta(minutes=10)
    rec = IdempotencyRecord.objects.create(
        user=user, api_key=api_key, key="race", method="POST", path=INBOXES,
        request_fingerprint=idem._parse_body(b"{}")[1],
        created_at=old,
    )
    inbox = Inbox.objects.create(user=user, routing_key="idem_race_original")
    real_filter = IdempotencyRecord.objects.filter
    fired = {"done": False}

    def filter_then_complete(*args, **kwargs):
        # The first filter keyed on created_at is the reclaim DELETE; complete
        # the row just before it runs, as the original worker would.
        if not fired["done"] and "created_at" in kwargs:
            fired["done"] = True
            real_filter(pk=rec.pk).update(status_code=201, response_body='{"data":{"id":"%s"}}' % inbox.public_id, response_headers={})
        return real_filter(*args, **kwargs)

    monkeypatch.setattr(IdempotencyRecord.objects, "filter", filter_then_complete)
    resp = post(client, INBOXES, {}, key="race")
    monkeypatch.undo()
    assert fired["done"]
    assert IdempotencyRecord.objects.filter(pk=rec.pk, status_code=201).exists()
    assert Inbox.objects.filter(user=user).count() == 1  # mutation not repeated
    assert resp.status_code == 201
    assert resp.headers.get("Idempotent-Replayed") == "true"
