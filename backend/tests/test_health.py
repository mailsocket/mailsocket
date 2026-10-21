import pytest
from django.db import connection


@pytest.mark.django_db
def test_healthz_ok(api_client):
    assert connection.vendor == "postgresql"
    response = api_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_healthz_db_failure_returns_503(api_client, monkeypatch):
    """Spec §3.2: DB failure -> 503. Simulate by making SELECT 1 raise."""
    from config import views

    class _BoomCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            raise Exception("db down")

        def fetchone(self):
            return None

    monkeypatch.setattr(views.connection, "cursor", lambda: _BoomCursor())
    response = api_client.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {"status": "error"}
