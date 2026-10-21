"Public API."

import heapq
import logging
import math
import threading
from datetime import datetime

from django.db import DatabaseError, transaction
from django.utils import timezone

from apps.accounts.client_ip import client_ip
from apps.accounts.services import (
    API_KEY_PREFIX_LENGTH,
    RateLimitExceeded,
    increment_rate_limit,
)

logger = logging.getLogger(__name__)

KEY_SCHEME = "ms_live_"
AUTH_FAIL_WINDOW = 60
AUTH_FAIL_IP_RATE_NAME = "api:auth-fail:ip:minute"
AUTH_FAIL_IP_RATE_LIMIT = 30
AUTH_FAIL_PREFIX_RATE_NAME = "api:auth-fail:prefix:minute"
AUTH_FAIL_PREFIX_RATE_LIMIT = 30


def key_prefix(token):
    """The lookup prefix of a well-shaped ``ms_live_`` token, else None."""
    if not isinstance(token, str) or not token.startswith(KEY_SCHEME):
        return None
    if len(token) <= len(KEY_SCHEME) + API_KEY_PREFIX_LENGTH:
        return None
    return token.removeprefix(KEY_SCHEME)[:API_KEY_PREFIX_LENGTH]



# its limit, answer further failures from a per-worker memo until the window
# resets: no DB upserts (hot-row contention) and no new per-prefix rows.
# gunicorn runs gthread (32 threads/worker), so every access holds a lock;
# expiry is popped from a heap (O(log n), no full scans). Bounded: when the
# memo is full, an IP that just went over the limit is still answered 429 and
# the WHOLE worker enters a short "saturated" mode in which every failed auth
# is 429 without touching the DB. Valid keys never reach this module.
_lock = threading.Lock()
_limited_ips: dict[str, datetime] = {}
_expiry_heap: list[tuple[datetime, str]] = []
_LIMITED_IPS_MAX = 10_000
_saturated_until = None


def _prune(now):
    while _expiry_heap and _expiry_heap[0][0] <= now:
        reset_at, ip = heapq.heappop(_expiry_heap)
        if _limited_ips.get(ip) == reset_at:
            del _limited_ips[ip]


def _memo_reset_at(ip, now):
    """Reset time if this IP (or the whole saturated worker) is limited."""
    global _saturated_until
    with _lock:
        _prune(now)
        if _saturated_until is not None:
            if _saturated_until > now:
                return _saturated_until
            _saturated_until = None
        return _limited_ips.get(ip)


def _memo_remember(ip, reset_at):
    global _saturated_until
    with _lock:
        if ip in _limited_ips or len(_limited_ips) < _LIMITED_IPS_MAX:
            _limited_ips[ip] = reset_at
            heapq.heappush(_expiry_heap, (reset_at, ip))
        elif _saturated_until is None or reset_at > _saturated_until:
            _saturated_until = reset_at


def _reset_memo():
    """Tests only: forget every memoised limit."""
    global _saturated_until
    with _lock:
        _limited_ips.clear()
        _expiry_heap.clear()
        _saturated_until = None
        _last_limited_log.clear()
        _log_expiry_heap.clear()


def _charge(ip, prefix):
    """Count one failure in every applicable bucket. Returns
    (ip_limited_at, any_limited_at): the IP bucket's reset time if the IP
    itself is over its limit, and the latest reset time among all buckets
    over their limit (None when not limited). Raises DatabaseError on
    storage trouble."""
    buckets = [(AUTH_FAIL_IP_RATE_NAME, ip, AUTH_FAIL_IP_RATE_LIMIT)]
    if prefix is not None:
        buckets.append((AUTH_FAIL_PREFIX_RATE_NAME, prefix, AUTH_FAIL_PREFIX_RATE_LIMIT))
    ip_limited_at = limited_at = None
    for name, subject, limit in buckets:
        try:
            # Savepoint: a storage error must not poison an outer transaction.
            with transaction.atomic():
                increment_rate_limit(name, subject, limit=limit, window_seconds=AUTH_FAIL_WINDOW)
        except RateLimitExceeded as exc:
            if name == AUTH_FAIL_IP_RATE_NAME:
                ip_limited_at = exc.reset_at
            if limited_at is None or exc.reset_at > limited_at:
                limited_at = exc.reset_at
    return ip_limited_at, limited_at


def _reason_and_token(request):
    value = request.headers.get("Authorization", "") if hasattr(request, "headers") else ""
    parts = value.split()
    if len(parts) != 2 or parts[0] != "Bearer":
        return "missing", None
    token = parts[1]
    if key_prefix(token) is None:
        return "malformed", None
    return "invalid", token


def record_failure(request):
    """Called from the API exception handler for every 401 (after
    BearerAuthentication refused the request — the authenticator itself stays
    query-free on garbage tokens). Returns None -> answer the normal 401, or
    the Retry-After seconds -> answer 429 instead. Never raises: limiter
    storage trouble is logged and fails open (None)."""
    reason, token = _reason_and_token(request)
    ip = client_ip(request)
    path = (getattr(request, "path", "") or "")[:100]
    now = timezone.now()
    memo = _memo_reset_at(ip, now)
    if memo is not None:
        return max(1, math.ceil((memo - now).total_seconds()))
    try:
        ip_limited_at, limited_at = _charge(ip, key_prefix(token))
    except DatabaseError:
        logger.exception("api_auth_fail_limiter_error ip=%s (fail-open)", ip)
        logger.warning("api_auth_failed ip=%s reason=%s path=%s", ip, reason, path)
        return None
    if limited_at is None:
        logger.warning("api_auth_failed ip=%s reason=%s path=%s", ip, reason, path)
        return None
    if ip_limited_at is not None:
        # Only the IP's own bucket memoises the IP; a prefix-only limit must
        # not 429 that IP's unrelated prefixes.
        _memo_remember(ip, ip_limited_at)
    _log_limited_once(ip, reason, path, limited_at)
    return max(1, math.ceil((limited_at - timezone.now()).total_seconds()))


_last_limited_log: dict[str, datetime] = {}
_log_expiry_heap: list[tuple[datetime, str]] = []
_LAST_LIMITED_LOG_MAX = 10_000


def _log_limited_once(ip, reason, path, reset_at):
    """One WARNING per (ip, window) — bounded log volume under a flood. The
    memo is per worker process, locked (gthread), expired in O(log n) from a
    heap and capped: when full, further limited events are not logged (the
    429s themselves still happen)."""
    now = timezone.now()
    with _lock:
        while _log_expiry_heap and _log_expiry_heap[0][0] <= now:
            expired_at, key = heapq.heappop(_log_expiry_heap)
            if _last_limited_log.get(key) == expired_at:
                del _last_limited_log[key]
        if ip in _last_limited_log or len(_last_limited_log) >= _LAST_LIMITED_LOG_MAX:
            return
        _last_limited_log[ip] = reset_at
        heapq.heappush(_log_expiry_heap, (reset_at, ip))
    logger.warning(
        "api_auth_fail_limited ip=%s reason=%s path=%s window_s=%s",
        ip,
        reason,
        path,
        AUTH_FAIL_WINDOW,
    )
