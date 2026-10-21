"Public API."

import pytest

from apps.accounts.models import WaitCapacity, WaitLease
from apps.api.tests.test_wait import auth_client, make_user
from apps.inboxes.models import Inbox

pytestmark = pytest.mark.django_db(transaction=True)


def _lease(user):
    row = WaitLease.objects.filter(user=user).first()
    return row.count if row else 0


def _global():
    row = WaitCapacity.objects.filter(key="global").first()
    return row.count if row else 0


def _open(name, monkeypatch):
    monkeypatch.setattr("apps.api.views.SSE_POLL_SECONDS", 0.01)
    owner = make_user(name)
    inbox = Inbox.objects.create(user=owner, routing_key=f"{name}_box")
    response = auth_client(owner).get(
        f"/api/v1/inboxes/{inbox.public_id}/stream", HTTP_ACCEPT="text/event-stream"
    )
    assert response.status_code == 200
    assert _lease(owner) == 1
    return owner, response


def test_close_before_first_chunk_releases_the_slot(monkeypatch):
    owner, response = _open("sse_close_early", monkeypatch)
    response.close()  # client gone before the server wrote anything
    assert _lease(owner) == 0
    assert _global() == 0


def test_close_mid_stream_releases_the_slot(monkeypatch):
    owner, response = _open("sse_close_mid", monkeypatch)
    stream = iter(response.streaming_content)
    assert next(stream).startswith(b"retry:")
    response.close()  # write failed -> server closes the iterable
    assert _lease(owner) == 0
    assert _global() == 0


def test_release_is_idempotent(monkeypatch):
    owner, response = _open("sse_close_twice", monkeypatch)
    other_owner, other = _open("sse_close_twice_other", monkeypatch)
    response.close()
    response.close()
    # the second close must not release someone else's global slot
    assert _global() == 1
    other.close()
    assert _global() == 0


def test_heartbeat_cadence_bounds_disconnect_detection():
    from apps.api import views

    assert views.SSE_HEARTBEAT_SECONDS <= 5
    assert views.SSE_HEARTBEAT_SECONDS < views.SSE_MAX_SECONDS
