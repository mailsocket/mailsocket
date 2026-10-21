"""Fail-closed branch of BearerAuthentication (audit 2026-10-11 #3, CP-3/CP-5).

``_account_missing_or_suspended`` treats a human with NO Account row as
suspended. Before these tests, flipping ``except ObjectDoesNotExist: return
True`` to ``return False`` kept every auth test green. Each case checks the
full 403 body (only ``error.request_id`` removed), that it is byte-identical
to the plain "tenant suspended" refusal (no oracle on WHY the key is refused),
and that the refused request did not touch ``last_used_at``.
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, AccountPlan, ApiKey, TeamMembership
from apps.accounts.multi_account_services import create_owned_account
from apps.accounts.services import create_api_key
from apps.api.authentication import _account_missing_or_suspended
from apps.api.tests.support import body_without_request_id

pytestmark = pytest.mark.django_db(transaction=True)

FORBIDDEN_BODY = {
    "error": {
        "code": "account_forbidden",
        "message": "Account is not permitted to use the API.",
        "fields": {},
    }
}


def make_user(name, *, plan=AccountPlan.Plan.BUSINESS):
    user = get_user_model().objects.create_user(username=name, email=f"{name}@example.test")
    Account.objects.create(
        user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user, plan=plan)
    return user


def get_inboxes(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client.get("/api/v1/inboxes")


def assert_forbidden_like_suspended_tenant(response):
    assert response.status_code == 403
    assert body_without_request_id(response) == FORBIDDEN_BODY
    # same bytes (minus request id) as an ordinary suspended tenant's key
    other = make_user(f"ref_suspended_{ApiKey.objects.count()}")
    _, ref_token = create_api_key(other, "ref")
    Account.objects.filter(user=other).update(is_suspended=True)
    reference = get_inboxes(ref_token)
    assert reference.status_code == 403
    assert body_without_request_id(reference) == body_without_request_id(response)
    assert "WWW-Authenticate" not in response.headers


def test_helper_treats_missing_account_as_suspended():
    user = get_user_model().objects.create_user(username="no_account", email="na@example.test")
    assert _account_missing_or_suspended(user) is True
    ok = make_user("has_account")
    assert _account_missing_or_suspended(ok) is False
    Account.objects.filter(user=ok).update(is_suspended=True)
    ok.refresh_from_db()
    assert _account_missing_or_suspended(ok) is True


@override_settings(ENABLE_TEAMS=True)
def test_member_minted_key_refused_when_creator_has_no_account():
    owner = make_user("fc_team_owner")
    member = make_user("fc_team_member", plan=AccountPlan.Plan.FREE)
    TeamMembership.objects.create(account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER)
    key, token = create_api_key(owner, "member key", created_by=member)
    _, owner_token = create_api_key(owner, "owner key")
    assert get_inboxes(token).status_code == 200  # control
    used_before = ApiKey.objects.get(pk=key.pk).last_used_at
    ApiKey.objects.filter(pk=key.pk).update(last_used_at=None)

    Account.objects.filter(user=member).delete()
    response = get_inboxes(token)

    assert_forbidden_like_suspended_tenant(response)
    assert used_before is not None
    assert ApiKey.objects.get(pk=key.pk).last_used_at is None  # refused before the usage stamp
    # the tenant's own key is unaffected
    assert get_inboxes(owner_token).status_code == 200


@override_settings(ENABLE_TEAMS=True)
def test_member_minted_key_refused_when_creator_suspended_full_body():
    owner = make_user("fs_team_owner")
    member = make_user("fs_team_member", plan=AccountPlan.Plan.FREE)
    TeamMembership.objects.create(account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER)
    _, token = create_api_key(owner, "member key", created_by=member)
    assert get_inboxes(token).status_code == 200  # control

    Account.objects.filter(user=member).update(is_suspended=True)

    assert_forbidden_like_suspended_tenant(get_inboxes(token))


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_workspace_key_refused_when_owner_has_no_account():
    owner = make_user("fc_ws_owner")
    link = create_owned_account(owner, "WS")
    _, token = create_api_key(link.account_user, "ws key")
    assert get_inboxes(token).status_code == 200  # control

    Account.objects.filter(user=owner).delete()

    assert_forbidden_like_suspended_tenant(get_inboxes(token))


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_workspace_key_refused_when_owner_suspended_full_body():
    owner = make_user("fs_ws_owner")
    link = create_owned_account(owner, "WS")
    _, token = create_api_key(link.account_user, "ws key")
    assert get_inboxes(token).status_code == 200  # control

    Account.objects.filter(user=owner).update(is_suspended=True)

    assert_forbidden_like_suspended_tenant(get_inboxes(token))
