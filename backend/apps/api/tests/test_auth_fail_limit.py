"Public API."

import logging
from unittest import mock

import pytest
from django.db import DatabaseError
from django.test import override_settings
from rest_framework.test import APIClient

from apps.accounts.models import RateLimitBucket
from apps.accounts.services import _bucket_hash, create_api_key
from apps.api.auth_failures import (
    AUTH_FAIL_IP_RATE_LIMIT,
    AUTH_FAIL_IP_RATE_NAME,
    AUTH_FAIL_PREFIX_RATE_LIMIT,
)
from apps.api.tests.support import body_without_request_id
from apps.api.tests.test_auth_fail_closed import make_user

pytestmark = pytest.mark.django_db(transaction=True)

PROXY = "172.29.0.249"
ATTACKER = "203.0.113.66"
BYSTANDER = "203.0.113.77"
BAD_KEY = "ms_live_" + "A" * 43
RATE_LIMITED_BODY = {"error": {"code": "rate_limited", "message": "Rate limit exceeded.", "fields": {}}}


def call(token, ip, *, remote=PROXY):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client.get("/api/v1/inboxes", REMOTE_ADDR=remote, HTTP_CF_CONNECTING_IP=ip)


def burn(ip, n, *, token=BAD_KEY, remote=PROXY):
    for i in range(n):
        # distinct tokens of a valid shape: the per-prefix bucket must not be
        # what trips the per-IP assertions
        tok = token if token != BAD_KEY else f"ms_live_{i:012d}" + "B" * 31
        assert call(tok, ip, remote=remote).status_code == 401


@pytest.fixture(autouse=True)
def _trusted_proxy(settings):
    settings.TRUSTED_PROXY_CIDRS = f"{PROXY}/32"


@pytest.fixture(autouse=True)
def _fresh_limited_memo():
    from apps.api import auth_failures

    auth_failures._reset_memo()
    yield
    auth_failures._reset_memo()


def test_failures_over_threshold_from_one_ip_get_429_with_retry_after():
    burn(ATTACKER, AUTH_FAIL_IP_RATE_LIMIT)
    response = call("ms_live_zzzzzzzzzzzz" + "C" * 31, ATTACKER)
    assert response.status_code == 429
    assert body_without_request_id(response) == RATE_LIMITED_BODY
    assert 1 <= int(response["Retry-After"]) <= 60
    assert "WWW-Authenticate" not in response.headers


def test_other_ip_is_unaffected():
    burn(ATTACKER, AUTH_FAIL_IP_RATE_LIMIT)
    assert call("ms_live_vvvvvvvvvvvv" + "D" * 31, ATTACKER).status_code == 429
    response = call("ms_live_yyyyyyyyyyyy" + "D" * 31, BYSTANDER)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_valid_key_from_the_limited_ip_is_never_slowed():
    user = make_user("victim")
    _, token = create_api_key(user, "CI")
    burn(ATTACKER, AUTH_FAIL_IP_RATE_LIMIT)
    assert call("ms_live_xxxxxxxxxxxx" + "E" * 31, ATTACKER).status_code == 429
    ok = call(token, ATTACKER)
    assert ok.status_code == 200
    ok_elsewhere = call(token, BYSTANDER)
    assert ok_elsewhere.status_code == 200


def test_success_writes_no_failure_bucket():
    user = make_user("happy")
    _, token = create_api_key(user, "CI")
    before = set(RateLimitBucket.objects.values_list("bucket_hash", flat=True))
    assert call(token, BYSTANDER).status_code == 200
    after = set(RateLimitBucket.objects.values_list("bucket_hash", flat=True))
    assert _bucket_hash(AUTH_FAIL_IP_RATE_NAME, BYSTANDER) not in after - before


def test_spoofed_header_from_untrusted_hop_is_ignored():
    """Direct-to-origin attacker rotating CF-Connecting-IP still lands in ONE
    bucket (its real peer address)."""
    for i in range(AUTH_FAIL_IP_RATE_LIMIT):
        assert call(f"ms_live_{i:012d}" + "F" * 31, f"198.51.100.{i + 1}", remote=ATTACKER).status_code == 401
    assert call("ms_live_wwwwwwwwwwww" + "G" * 31, "198.51.100.250", remote=ATTACKER).status_code == 429
    assert not RateLimitBucket.objects.filter(
        bucket_hash=_bucket_hash(AUTH_FAIL_IP_RATE_NAME, "198.51.100.1")
    ).exists()


def test_same_prefix_from_many_ips_is_limited_by_prefix():
    token = "ms_live_samesameprfx" + "H" * 31
    for i in range(AUTH_FAIL_PREFIX_RATE_LIMIT):
        assert call(token, f"198.51.100.{i + 1}").status_code == 401
    assert call(token, "198.51.100.251").status_code == 429


def test_garbage_shape_token_is_counted_per_ip():
    for _ in range(AUTH_FAIL_IP_RATE_LIMIT):
        assert call("garbage", ATTACKER).status_code == 401
    assert call("garbage", ATTACKER).status_code == 429


def test_limiter_storage_error_fails_open(caplog):
    with mock.patch("apps.api.auth_failures.increment_rate_limit", side_effect=DatabaseError("boom")):
        with caplog.at_level(logging.ERROR, logger="apps.api.auth_failures"):
            for _ in range(AUTH_FAIL_IP_RATE_LIMIT + 5):
                response = call(BAD_KEY, ATTACKER)
                assert response.status_code == 401
    assert any("api_auth_fail_limiter_error" in r.getMessage() for r in caplog.records)


def test_storage_error_does_not_break_a_valid_key_afterwards():
    user = make_user("after_err")
    _, token = create_api_key(user, "CI")
    with mock.patch("apps.api.auth_failures.increment_rate_limit", side_effect=DatabaseError("boom")):
        assert call(BAD_KEY, ATTACKER).status_code == 401
    assert call(token, ATTACKER).status_code == 200


def test_failure_is_logged_with_client_ip_never_the_key(caplog):
    with caplog.at_level(logging.INFO, logger="apps.api.auth_failures"):
        call(BAD_KEY, ATTACKER)
    lines = [r.getMessage() for r in caplog.records if r.name == "apps.api.auth_failures"]
    assert lines and any(f"ip={ATTACKER}" in line for line in lines)
    assert all(BAD_KEY not in line and BAD_KEY[8:20] not in line for line in lines)


@override_settings(TRUSTED_PROXY_CIDRS="")
def test_missing_header_and_garbage_auth_is_also_counted():
    client = APIClient()
    for _ in range(AUTH_FAIL_IP_RATE_LIMIT):
        assert client.get("/api/v1/inboxes", REMOTE_ADDR=ATTACKER).status_code == 401
    assert client.get("/api/v1/inboxes", REMOTE_ADDR=ATTACKER).status_code == 429


def test_over_limit_ip_is_answered_from_memory_without_db_writes(django_assert_max_num_queries):
    "Public API."
    burn(ATTACKER, AUTH_FAIL_IP_RATE_LIMIT)
    assert call(BAD_KEY, ATTACKER).status_code == 429
    rows = RateLimitBucket.objects.count()
    from apps.api.auth_failures import record_failure

    class _Req:
        headers = {"Authorization": "Bearer ms_live_" + "Z" * 43}
        META = {"REMOTE_ADDR": PROXY, "HTTP_CF_CONNECTING_IP": ATTACKER}
        path = "/api/v1/inboxes"

    with django_assert_max_num_queries(0):
        for i in range(20):
            _Req.headers = {"Authorization": f"Bearer ms_live_{i:012d}" + "Q" * 31}
            assert record_failure(_Req()) >= 1
    assert RateLimitBucket.objects.count() == rows
    assert call(BAD_KEY, BYSTANDER).status_code == 401


def test_prefix_only_limit_does_not_429_the_ips_other_prefixes():
    """GPT round 2 P2: a prefix-bucket hit must not memoise the whole IP."""
    shared = "ms_live_" + "P" * 43
    for i in range(AUTH_FAIL_PREFIX_RATE_LIMIT):
        assert call(shared, f"198.51.100.{i + 1}").status_code == 401
    assert call(shared, ATTACKER).status_code == 429  # prefix bucket over
    assert call("ms_live_" + "R" * 43, ATTACKER).status_code == 401  # other prefix, same IP


def test_full_memo_still_answers_429_without_db_writes(django_assert_max_num_queries):
    "Public API."
    from apps.api import auth_failures

    with mock.patch.object(auth_failures, "_LIMITED_IPS_MAX", 0):
        burn(ATTACKER, AUTH_FAIL_IP_RATE_LIMIT)
        assert call(BAD_KEY, ATTACKER).status_code == 429  # worker now saturated
        rows = RateLimitBucket.objects.count()

        class _Req:
            headers = {"Authorization": "Bearer ms_live_" + "Y" * 43}
            META = {"REMOTE_ADDR": PROXY, "HTTP_CF_CONNECTING_IP": "203.0.113.200"}
            path = "/api/v1/inboxes"

        with django_assert_max_num_queries(0):
            assert auth_failures.record_failure(_Req()) >= 1
        assert RateLimitBucket.objects.count() == rows


def test_memo_is_thread_safe_under_concurrent_access():
    """GPT round 2 P2: gunicorn gthread — concurrent remember/lookup must not raise."""
    import threading
    from datetime import timedelta

    from django.utils import timezone

    from apps.api import auth_failures

    errors = []
    soon = timezone.now() + timedelta(seconds=30)

    def worker(n):
        try:
            for i in range(500):
                auth_failures._memo_remember(f"10.{n}.{i // 256}.{i % 256}", soon)
                auth_failures._memo_reset_at(f"10.{n}.0.1", timezone.now())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []


def test_full_log_memo_does_no_linear_scan():
    """GPT round 3 P2: at capacity, each limited event must not scan the memo."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.api import auth_failures

    later = timezone.now() + timedelta(seconds=60)
    with mock.patch.object(auth_failures, "_LAST_LIMITED_LOG_MAX", 50):
        for i in range(50):
            auth_failures._log_limited_once(f"10.9.0.{i}", "invalid", "/x", later)

        class _NoScan(dict):
            def items(self):
                raise AssertionError("linear scan of the log memo")

        auth_failures._last_limited_log = _NoScan(auth_failures._last_limited_log)
        try:
            for i in range(200):
                auth_failures._log_limited_once(f"10.8.{i // 256}.{i % 256}", "invalid", "/x", later)
        finally:
            auth_failures._last_limited_log = dict(auth_failures._last_limited_log)
        assert len(auth_failures._last_limited_log) == 50
