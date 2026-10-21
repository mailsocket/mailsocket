import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(
        username="testuser", email="user@example.test", password="test-password"
    )


@pytest.fixture
def admin_user(db):
    return get_user_model().objects.create_superuser(
        username="admin", email="admin@example.test", password="test-password"
    )


@pytest.fixture
def authenticated_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


@pytest.fixture(autouse=True)
def _reset_api_auth_fail_memo():
    """apps.api.auth_failures keeps a per-process memo of over-limit IPs; tests
    share one process, so clear it to keep every test independent."""
    from apps.api import auth_failures

    auth_failures._reset_memo()
    yield
    auth_failures._reset_memo()
