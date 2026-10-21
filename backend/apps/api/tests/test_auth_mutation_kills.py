"Public API."

import secrets
from datetime import timedelta
from unittest import mock

import pytest
from django.test import RequestFactory, override_settings
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIClient

from apps.accounts.models import AccountPlan, ApiKey, TeamMembership
from apps.accounts.multi_account_services import create_owned_account
from apps.accounts.services import API_KEY_PREFIX_LENGTH, create_api_key, hash_api_key
from apps.api.authentication import BearerAuthentication
from apps.api.tests.support import body_without_request_id
from apps.api.tests.test_auth_fail_closed import make_user as _user

pytestmark = pytest.mark.django_db

UNAUTH_BODY = {
    "error": {
        "code": "authentication_required",
        "message": "Authentication required.",
        "fields": {},
    }
}


def _authenticate(header):
    request = RequestFactory().get("/api/v1/inboxes", HTTP_AUTHORIZATION=header)
    return BearerAuthentication().authenticate(request)


def _recently_used(key):
    # inside the 5-minute window: no last_used_at UPDATE, so only the lookup runs
    ApiKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now() - timedelta(seconds=10))


# --- wrong prefix / short token: refused before any DB query ---------------------

SHORT_EXACT = "ms_live_" + "a" * API_KEY_PREFIX_LENGTH  # == the boundary: still too short


@pytest.mark.parametrize(
    "token",
    [
        "sk_live_" + "a" * 40,  # wrong prefix, long enough
        "ms_test_" + "a" * 40,
        "MS_LIVE_" + "a" * 40,
        SHORT_EXACT,
        "ms_live_abc",
        "ms_live_",
    ],
    ids=["sk-prefix", "ms-test", "upper", "len-boundary", "short", "bare-prefix"],
)
def test_malformed_token_refused_with_zero_queries(token, django_assert_num_queries):
    # kills authenticate mutmut_27 (`or` -> `and`: a wrong-prefix token reached
    # the key lookup), _32 (`<=` -> `<`: the exact-boundary token reached it),
    # _33 (`+` -> `-`: every short ms_live_ token reached it)
    with django_assert_num_queries(0), pytest.raises(AuthenticationFailed) as exc:
        _authenticate(f"Bearer {token}")
    assert exc.value.status_code == 401


def test_first_token_past_the_boundary_does_the_lookup(django_assert_num_queries):
    # control for the boundary above: one char longer -> a real lookup, still 401
    with django_assert_num_queries(1), pytest.raises(AuthenticationFailed):
        _authenticate(f"Bearer {SHORT_EXACT}b")


# --- hot path: exactly one query, suspension checks included ---------------------


def test_owner_key_hot_path_is_one_query(django_assert_num_queries):
    # kills authenticate mutmut_59 (user__account), _60 (user__account_plan),
    # _62 (owned_by_link: a plain tenant's missing link must not cost a query)
    owner = _user("am_owner")
    key, token = create_api_key(owner, "k")
    _recently_used(key)
    with django_assert_num_queries(1):
        user, matched = _authenticate(f"Bearer {token}")
    assert (user.pk, matched.pk) == (owner.pk, key.pk)


@override_settings(ENABLE_TEAMS=True)
def test_member_minted_key_hot_path_is_one_query(django_assert_num_queries):
    # kills authenticate mutmut_61 (created_by__account dropped -> the creator's
    # suspension check costs extra queries on every request)
    owner = _user("am_team_owner")
    member = _user("am_team_member", plan=AccountPlan.Plan.FREE)
    TeamMembership.objects.create(account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER)
    key, token = create_api_key(owner, "member key", created_by=member)
    _recently_used(key)
    with django_assert_num_queries(1):
        user, matched = _authenticate(f"Bearer {token}")
    assert (user.pk, matched.pk) == (owner.pk, key.pk)


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_workspace_key_hot_path_is_one_query(django_assert_num_queries):
    # kills authenticate mutmut_62 (owned_by_link__owner_user__account dropped)
    owner = _user("am_ws_owner")
    link = create_owned_account(owner, "WS")
    key, token = create_api_key(link.account_user, "ws key")
    _recently_used(key)
    with django_assert_num_queries(1):
        user, matched = _authenticate(f"Bearer {token}")
    assert (user.pk, matched.pk) == (link.account_user.pk, key.pk)


# --- timing-safe compare on every "no such usable key" path ----------------------


def _revoked(key):
    key.revoked_at = timezone.now()
    key.save(update_fields=["revoked_at"])


def _expired(key):
    key.expires_at = timezone.now() - timedelta(seconds=1)
    key.save(update_fields=["expires_at"])


@pytest.mark.parametrize("make_unusable", [None, _revoked, _expired], ids=["unknown", "revoked", "expired"])
def test_refused_key_still_does_an_equal_length_constant_time_compare(make_unusable):
    # kills authenticate mutmut_92 ("XX0XX" * 64) and _93 ("0" * 65): the dummy
    # compare must be against a string as long as a real key hash, else the
    # refusal is faster/slower than a hash mismatch (timing side channel)
    owner = _user("am_timing")
    key, token = create_api_key(owner, "k")
    if make_unusable is None:
        token = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")  # same prefix, wrong secret
    else:
        make_unusable(key)
    presented = hash_api_key(token)
    with (
        mock.patch("apps.api.authentication.secrets.compare_digest", wraps=secrets.compare_digest) as spy,
        pytest.raises(AuthenticationFailed),
    ):
        _authenticate(f"Bearer {token}")
    calls = [c.args for c in spy.call_args_list]
    assert calls, "no constant-time compare on the refusal path"
    assert all(a == presented for a, _ in calls)
    assert all(len(b) == len(presented) == 64 for _, b in calls), [len(b) for _, b in calls]
    dummy = calls[-1][1]
    assert dummy != presented and set(dummy) <= set("0123456789abcdef")


# --- last_used_at throttle: once per 5 minutes, boundary inclusive ---------------


def _stamp_case(age):
    owner = _user(f"am_stamp_{int(age.total_seconds())}")
    key, token = create_api_key(owner, "k")
    frozen = timezone.now()
    ApiKey.objects.filter(pk=key.pk).update(last_used_at=frozen - age)
    with mock.patch("django.utils.timezone.now", return_value=frozen):
        _authenticate(f"Bearer {token}")
    return ApiKey.objects.get(pk=key.pk).last_used_at, frozen


def test_last_used_stamped_exactly_at_five_minutes():
    # kills authenticate mutmut_127 (`<=` -> `<`)
    used, frozen = _stamp_case(timedelta(minutes=5))
    assert used == frozen


def test_last_used_stamped_between_five_and_six_minutes():
    # kills authenticate mutmut_124 (window widened to 6 minutes)
    used, frozen = _stamp_case(timedelta(minutes=5, seconds=30))
    assert used == frozen


def test_last_used_not_stamped_inside_the_window():
    used, frozen = _stamp_case(timedelta(minutes=4, seconds=59))
    assert used == frozen - timedelta(minutes=4, seconds=59)


# --- every 401 branch: one canonical body (no oracle, detail never leaks) --------


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Token ms_live_" + "a" * 40,
        "Bearer",
        "Bearer a b",
        "bearer ms_live_" + "a" * 40,
        "Bearer sk_live_" + "a" * 40,
        f"Bearer {SHORT_EXACT}",
        "Bearer ms_live_" + "a" * 40,
    ],
    ids=[
        "none",
        "empty",
        "token-scheme",
        "no-token",
        "three-parts",
        "lower-scheme",
        "wrong-prefix",
        "short",
        "unknown",
    ],
)
def test_every_401_branch_returns_the_canonical_body(header):
    client = APIClient()
    if header is not None:
        client.credentials(HTTP_AUTHORIZATION=header)
    response = client.get("/api/v1/inboxes")
    assert response.status_code == 401
    assert body_without_request_id(response) == UNAUTH_BODY
    assert response.headers["WWW-Authenticate"] == "Bearer"
