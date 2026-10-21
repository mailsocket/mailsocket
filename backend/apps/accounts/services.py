import hashlib
import hmac
import logging
import secrets
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.utils import timezone

from .attribution import AGENT_REGISTER_REF
from .models import (
    Account,
    AccountPlan,
    ApiKey,
    RateLimitBucket,
    WaitCapacity,
    WaitCapacityDaily,
    WaitLease,
)

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    def __init__(self, *, limit: int | None = None, reset_at=None):
        self.limit = limit
        self.reset_at = reset_at


class ApiKeyLimitExceeded(Exception):
    """Raised when minting another key would exceed the per-user active-key cap."""

    def __init__(self, *, active_count: int, limit: int):
        self.active_count = active_count
        self.limit = limit
        super().__init__(f"{active_count} active API keys (limit {limit})")


def normalize_email(value: str) -> str:
    email = value.strip(" \t\r\n\f\v")
    validate_email(email)
    local, domain = email.rsplit("@", 1)
    normalized = f"{local.lower()}@{domain.lower()}"
    if len(normalized) > 254:
        raise ValidationError("Enter a valid email address.")
    return normalized


def create_product_user(email: str, password: str | None, *, is_active: bool = True):
    normalized = normalize_email(email)
    User = get_user_model()
    with transaction.atomic():
        user = User(username=f"ms_{secrets.token_urlsafe(18)}", email=normalized, is_active=is_active)
        if password is None:
            user.set_unusable_password()
        else:
            user.set_password(password)
        user.save()
        Account.objects.create(user=user, email_normalized=normalized)
        AccountPlan.objects.create(user=user)
    return user


def register_pending_signup(email: str):
    """Create/refresh a pending signup with an UNUSABLE password.

    Register takes no password: only the email controller may choose the
    active credential, on the token-bound verification form."""
    normalized = normalize_email(email)
    nonce = secrets.token_urlsafe(32)
    User = get_user_model()

    try:
        with transaction.atomic():
            user = User(username=f"ms_{secrets.token_urlsafe(18)}", email=normalized)
            user.set_unusable_password()
            user.save()
            Account.objects.create(
                user=user,
                email_normalized=normalized,
                verification_nonce=nonce,
            )
            AccountPlan.objects.create(user=user)
            return user
    except IntegrityError:
        pass

    with transaction.atomic():
        account = Account.objects.select_for_update().select_related("user").get(
            email_normalized=normalized
        )
        if account.email_verified_at is not None:
            return None
        user = account.user
        user.set_unusable_password()
        user.save(update_fields=["password"])
        account.verification_nonce = nonce
        account.save(update_fields=["verification_nonce", "updated_at"])
        return user


def refresh_pending_verification(user):
    """Rotate a resend token and ensure no pre-fix credential can become active."""
    with transaction.atomic():
        account = Account.objects.select_for_update().select_related("user").get(user=user)
        if account.email_verified_at is not None:
            return None
        locked_user = account.user
        locked_user.set_unusable_password()
        locked_user.save(update_fields=["password"])
        account.verification_nonce = secrets.token_urlsafe(32)
        account.save(update_fields=["verification_nonce", "updated_at"])
        return locked_user


def ensure_account(user) -> Account:
    normalized = normalize_email(user.email)
    with transaction.atomic():
        account, _ = Account.objects.get_or_create(
            user=user,
            defaults={"email_normalized": normalized},
        )
        AccountPlan.objects.get_or_create(user=user)
    return account


def _bucket_hash(limit_name: str, subject: str) -> str:
    secret = settings.AUTH_RATE_LIMIT_SECRET.encode()
    material = f"v1\0{limit_name}\0{subject}".encode()
    return hmac.new(secret, material, hashlib.sha256).hexdigest()


def increment_rate_limit(limit_name: str, subject: str, *, limit: int, window_seconds: int):
    now = timezone.now()
    expires_at = now + timedelta(seconds=window_seconds)
    bucket_hash = _bucket_hash(limit_name, subject)
    table = connection.ops.quote_name("accounts_ratelimitbucket")
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO {table} (bucket_hash, window_started_at, count, expires_at)
            VALUES (%s, %s, 1, %s)
            ON CONFLICT (bucket_hash) DO UPDATE SET
                window_started_at = CASE
                    WHEN {table}.expires_at <= %s THEN EXCLUDED.window_started_at
                    ELSE {table}.window_started_at END,
                count = CASE
                    WHEN {table}.expires_at <= %s THEN 1
                    ELSE {table}.count + 1 END,
                expires_at = CASE
                    WHEN {table}.expires_at <= %s THEN EXCLUDED.expires_at
                    ELSE {table}.expires_at END
            RETURNING count, expires_at
            """,
            [bucket_hash, now, expires_at, now, now, now],
        )
        count, reset_at = cursor.fetchone()
    if count > limit:
        raise RateLimitExceeded(limit=limit, reset_at=reset_at)
    return limit - count, reset_at


def charge_rate_limit(limit_name: str, subject: str, *, amount: int, limit: int, window_seconds: int):
    "Public API."
    now = timezone.now()
    expires_at = now + timedelta(seconds=window_seconds)
    bucket_hash = _bucket_hash(limit_name, subject)
    table = connection.ops.quote_name("accounts_ratelimitbucket")
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT count, expires_at FROM {table} WHERE bucket_hash = %s FOR UPDATE",
                [bucket_hash],
            )
            row = cursor.fetchone()
            if row is None or row[1] <= now:
                new_count = amount
                new_expires = expires_at
                charged = True
                if row is None:
                    cursor.execute(
                        f"""
                        INSERT INTO {table} (bucket_hash, window_started_at, count, expires_at)
                        VALUES (%s, %s, %s, %s)
                        """,
                        [bucket_hash, now, new_count, new_expires],
                    )
                else:
                    cursor.execute(
                        f"""
                        UPDATE {table}
                        SET window_started_at = %s, count = %s, expires_at = %s
                        WHERE bucket_hash = %s
                        """,
                        [now, new_count, new_expires, bucket_hash],
                    )
            else:
                old_count, old_expires = row
                new_expires = old_expires
                if old_count + amount <= limit:
                    new_count = old_count + amount
                    charged = True
                    cursor.execute(
                        f"UPDATE {table} SET count = %s WHERE bucket_hash = %s",
                        [new_count, bucket_hash],
                    )
                else:
                    new_count = old_count
                    charged = False
    if not charged:
        raise RateLimitExceeded(limit=limit, reset_at=new_expires)
    return limit - new_count, new_expires


# D1 (P3-3, ): email-only login ceiling, on top
# of the existing per-(email+IP) buckets above. A single IP is stopped by the
# email+IP limit (5/15m) long before it can add much to this shared ceiling,
# so an attacker needs multiple IPs just to trip it. Applies to "login" and,

# buckets so the two never share state. Password-reset uses a different
# action/bucket-name and is unaffected. See clear_login_email_ceiling below
# for the reset escape hatch (login only; admin-login has no reset flow).
LOGIN_EMAIL_CEILING_ACTIONS = {"login", "admin-login"}


def _login_email_ceiling_names(action: str) -> tuple[str, str]:
    return (f"auth:{action}:email:1h", f"auth:{action}:email:1d")


LOGIN_EMAIL_CEILING_NAMES = _login_email_ceiling_names("login")


def enforce_auth_rate_limits(action: str, email: str, ip_address: str) -> None:
    try:
        normalized = normalize_email(email)
    except ValidationError:
        normalized = email.strip().lower()[:254]
    if action in {"register", "resend"}:
        increment_rate_limit("auth:signup:ip:1h", ip_address, limit=10, window_seconds=3600)
    increment_rate_limit(f"auth:{action}:ip:15m", ip_address, limit=20, window_seconds=900)
    increment_rate_limit(f"auth:{action}:ip:1d", ip_address, limit=100, window_seconds=86400)
    email_subject = f"{normalized}\0{ip_address}"
    increment_rate_limit(f"auth:{action}:15m", email_subject, limit=5, window_seconds=900)
    increment_rate_limit(f"auth:{action}:1d", email_subject, limit=20, window_seconds=86400)
    if action in LOGIN_EMAIL_CEILING_ACTIONS:
        hourly_name, daily_name = _login_email_ceiling_names(action)
        increment_rate_limit(hourly_name, normalized, limit=50, window_seconds=3600)
        increment_rate_limit(daily_name, normalized, limit=200, window_seconds=86400)


def clear_login_email_ceiling(email_normalized: str) -> None:
    """Delete the two login-email-ceiling RateLimitBucket rows for this
    address (design §D1): called after a successful password reset, so
    only someone who controls the mailbox can lift the ceiling. Never
    raises — best-effort, mirrors the other release_* helpers' posture.
    Login only — admin-login has no self-service reset flow.
    """
    try:
        hashes = [
            _bucket_hash(name, email_normalized) for name in LOGIN_EMAIL_CEILING_NAMES
        ]
        RateLimitBucket.objects.filter(bucket_hash__in=hashes).delete()
    except Exception:
        logger.exception(
            "clear_login_email_ceiling_failed email=%s", email_normalized
        )


API_KEY_PREFIX_LENGTH = 12

# Per-user cap on simultaneously live (un-revoked) API keys. Every key is

# hardening, not a privilege boundary.
MAX_ACTIVE_API_KEYS_PER_USER = 20

# Per-account concurrent-wait caps by plan. Values are looked up from
# BILLING_PLAN_LIMITS[plan]["wait_concurrency"] (the single source of truth
# also feeding /pricing/); WAIT_LEASE_LIMIT_FREE/_PAID are kept as named
# constants purely because existing call sites and tests monkeypatch/import
# them directly (e.g. apps/api/tests/test_wait.py), NOT as a second binary
# plan check — wait_lease_limit_for below never branches on Plan.PAID.
WAIT_LEASE_LIMIT_FREE = settings.BILLING_PLAN_LIMITS["free"]["wait_concurrency"]
WAIT_LEASE_LIMIT_PAID = settings.BILLING_PLAN_LIMITS["paid"]["wait_concurrency"]
WAIT_LEASE_GRACE_SECONDS = 60

# Global cap on ALL in-flight wait requests across every account.

# prod-like 2 vCPU box): gunicorn runs GUNICORN_WORKERS x GUNICORN_THREADS =
# 3 x 32 = 96 request slots with --worker-connections == --threads, so a
# worker whose threads are all busy stops accepting and the kernel hands new
# requests to a worker with a free thread. 64 waits leave >= 32 slots (at
# least one whole worker) for dashboard, login, ingest and normal API calls.
# cap r2: all of these are env-driven from ONE place (config.settings.base —
# the same GUNICORN_* env vars build the gunicorn command line), and
# apps.accounts.checks refuses to start when they disagree (mailsocket.E00x).
GUNICORN_WORKERS = settings.GUNICORN_WORKERS
GUNICORN_THREADS = settings.GUNICORN_THREADS
WAIT_NON_WAIT_HEADROOM_MIN = settings.WAIT_NON_WAIT_HEADROOM_MIN
WAIT_CAPACITY_LIMIT = settings.WAIT_CAPACITY_LIMIT
WAIT_CAPACITY_KEY = "global"


# pool. Free/agent-plan traffic (the only plan an unauthenticated
# /agents/register key can ever hold) is capped at a sub-limit strictly below
# the global limit, so it can never occupy every slot and starve paying
# customers. Paid accounts are still bounded by WAIT_CAPACITY_LIMIT overall,
# but the free sub-cap guarantees at least (WAIT_CAPACITY_LIMIT -
# WAIT_CAPACITY_FREE_LIMIT) slots stay reachable for paid waits.
# wait-cap: raised 4 -> 12 with the global cap. Deliberately below the old
# 1/4 ratio (16): the free pool is the cheap-signup abuse surface, and 12
# keeps 52 of 64 slots reserved for paid waits. The demo user's own cap
# stays DEMO_WAIT_LEASE_LIMIT = WAIT_CAPACITY_FREE_LIMIT - 1 (= 11).
WAIT_CAPACITY_FREE_LIMIT = settings.WAIT_CAPACITY_FREE_LIMIT
WAIT_CAPACITY_FREE_KEY = "free"


# pool, so unbounded free keys performing slow DNS lookups cannot tie up
# every gthread worker. Deliberately separate from WAIT_CAPACITY_* — verify
# and wait are different resources and should not compete for the same slots.
VERIFY_CAPACITY_LIMIT = 8
VERIFY_CAPACITY_KEY = "verify-global"
VERIFY_CAPACITY_GRACE_SECONDS = 30


# hold up to MAX_ACTIVE_API_KEYS_PER_USER keys, so a per-key-only ceiling is
# trivially multiplied by minting more keys.
VERIFY_KEY_RATE_LIMIT = 500
VERIFY_KEY_RATE_WINDOW = 3600
VERIFY_ACCOUNT_RATE_LIMIT = 1500
VERIFY_ACCOUNT_RATE_WINDOW = 3600


def _acquire_counter_row_count(model, *, lookup, limit, grace_seconds) -> tuple[bool, int | None]:
    """Atomically grant one slot from a single guarded counter row.

    Shared by the per-account WaitLease and the global WaitCapacity. Locks the
    row with SELECT ... FOR UPDATE, resets it if its window has fully elapsed
    (the longest permitted wait + grace) so a worker that died mid-wait never
    leaks a permanent slot, then bumps ``count`` only if below ``limit``.

    Returns ``(granted, count)`` where ``count`` is the in-flight value right
    after a successful acquire (None when refused) — walert peak tracking.
    """
    now = timezone.now()
    for _ in range(2):
        with transaction.atomic():
            row = model.objects.select_for_update().filter(**lookup).first()
            if row is None:
                try:
                    model.objects.create(
                        **lookup,
                        count=1,
                        expires_at=now + timedelta(seconds=grace_seconds),
                    )
                except IntegrityError:
                    # Lost a first-row race with another worker: the row now
                    # exists; retry the select-for-update path.
                    continue
                return True, 1
            reset_cutoff = now - timedelta(seconds=grace_seconds)
            if row.expires_at is not None and row.expires_at <= reset_cutoff:
                row.count = 1
                row.expires_at = now + timedelta(seconds=grace_seconds)
                row.save(update_fields=["count", "expires_at", "updated_at"])
                return True, 1
            if row.count >= limit:
                return False, None
            row.count += 1
            row.expires_at = now + timedelta(seconds=grace_seconds)
            row.save(update_fields=["count", "expires_at", "updated_at"])
            return True, row.count
    return False, None


def _acquire_counter_row(model, *, lookup, limit, grace_seconds) -> bool:
    """Boolean form of :func:`_acquire_counter_row_count` (unchanged contract)."""
    granted, _count = _acquire_counter_row_count(
        model, lookup=lookup, limit=limit, grace_seconds=grace_seconds
    )
    return granted


# walert: a pool counts as "near full" (WARNING alert due) at >= 80% of its
# limit, rounded UP: global 52 of 64, free 10 of 12.
WAIT_CAPACITY_WARN_PERCENT = 80


def wait_capacity_warn_threshold(limit: int) -> int:
    return -(-limit * WAIT_CAPACITY_WARN_PERCENT // 100)


def _upsert_wait_capacity_daily(*, pool: str, peak: int, refused: bool, warned: bool, now) -> None:
    """ONE statement: INSERT ... ON CONFLICT (date, pool) DO UPDATE with
    GREATEST(peak) / refusals + n / latest warned_at|saturated_at."""
    table = WaitCapacityDaily._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO {table} AS t (date, pool, peak, refusals, warned_at, saturated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (date, pool) DO UPDATE SET
                peak = GREATEST(t.peak, EXCLUDED.peak),
                refusals = t.refusals + EXCLUDED.refusals,
                warned_at = COALESCE(EXCLUDED.warned_at, t.warned_at),
                saturated_at = COALESCE(EXCLUDED.saturated_at, t.saturated_at)
            """,
            [
                now.date(),
                pool,
                peak,
                1 if refused else 0,
                now if warned else None,
                now if refused else None,
            ],
        )


def _record_wait_capacity_stats(pool: str, *, count: int | None = None, refused: bool = False) -> None:
    """walert: best-effort per-day stats for one pool. Called only AFTER the
    counter-row transaction has finished (never inside the FOR UPDATE lock).
    Runs in its own savepoint so a failed statement cannot poison a caller's
    outer transaction; ANY failure is logged and swallowed — a stats write
    must never fail or slow a wait beyond one upsert."""
    try:
        limit = WAIT_CAPACITY_FREE_LIMIT if pool == WAIT_CAPACITY_FREE_KEY else WAIT_CAPACITY_LIMIT
        warned = count is not None and count >= wait_capacity_warn_threshold(limit)
        with transaction.atomic():
            _upsert_wait_capacity_daily(
                pool=pool, peak=count or 0, refused=refused, warned=warned, now=timezone.now()
            )
    except Exception:
        logger.exception("wait_capacity_stats_failed pool=%s", pool)


def wait_lease_limit_for(user_id: int) -> int:
    """Return the per-account concurrent-wait cap for the given user.

    Free and Paid keep dedicated module constants (WAIT_LEASE_LIMIT_FREE /
    WAIT_LEASE_LIMIT_PAID) purely so existing call sites/tests that
    monkeypatch them directly keep working; every OTHER known tier
    (including Scale) is looked up from BILLING_PLAN_LIMITS[plan] — the
    single source of truth — so a paying Scale account is never silently
    downgraded to the Free cap. An account with no plan row, or an unknown
    plan value, falls back to WAIT_LEASE_LIMIT_FREE. Never raises.

    r2 (try-page  P2): the single shared try-it-live demo user
    (DEMO_USER_EMAIL) is the one exception — every anonymous /docs/try/
    visitor waits on that ONE account, so the Free per-account cap (2) would
    mean only 2 visitors site-wide could watch a wait at once. It gets
    DEMO_WAIT_LEASE_LIMIT (= WAIT_CAPACITY_FREE_LIMIT - 1) instead. It is still a
    Free-plan account, so ``is_free_plan`` keeps routing it through the
    WAIT_CAPACITY_FREE_LIMIT sub-pool, which remains the global bound on
    demo waits; no real plan's limit changes.
    """
    try:
        row = (
            AccountPlan.objects.filter(user_id=user_id)
            .values_list("plan", "user__email")
            .first()
        )
    except Exception:
        logger.exception("wait_lease_limit_plan_lookup_failed user_id=%s", user_id)
        row = None
    plan, email = row if row is not None else (None, None)
    if email == DEMO_USER_EMAIL:
        return DEMO_WAIT_LEASE_LIMIT
    if plan == AccountPlan.Plan.FREE:
        return WAIT_LEASE_LIMIT_FREE
    if plan == AccountPlan.Plan.PAID:
        return WAIT_LEASE_LIMIT_PAID
    limits = settings.BILLING_PLAN_LIMITS.get(plan or "")  # no row -> None -> "": never a key
    if limits is None:
        return WAIT_LEASE_LIMIT_FREE
    return limits.get("wait_concurrency", WAIT_LEASE_LIMIT_FREE)


def acquire_wait_lease(user_id: int) -> bool:
    """Cap concurrent long-poll waits per account (plan-aware)."""
    return _acquire_counter_row(
        WaitLease,
        lookup={"user_id": user_id},
        limit=wait_lease_limit_for(user_id),
        grace_seconds=WAIT_LEASE_GRACE_SECONDS,
    )


def _release_counter_row(model, *, lookup) -> None:
    """Best-effort decrement of a single guarded counter row (shared helper)."""
    try:
        with transaction.atomic():
            model.objects.select_for_update().filter(count__gt=0, **lookup).update(
                count=F("count") - 1
            )
    except Exception:
        logger.exception("counter_release_failed model=%s lookup=%s", model.__name__, lookup)


def is_free_plan(user_id: int) -> bool:
    "Public API."
    try:
        plan = AccountPlan.objects.filter(user_id=user_id).values_list("plan", flat=True).first()
    except Exception:
        logger.exception("is_free_plan_lookup_failed user_id=%s", user_id)
        return True
    limits = settings.BILLING_PLAN_LIMITS.get(plan or "")  # no row -> None -> "": never a key
    if limits is None:
        return True
    return not limits.get("is_paid_tier", False)


def acquire_wait_capacity(*, free: bool = False) -> bool:
    "Public API."
    if free:
        granted_free, free_count = _acquire_counter_row_count(
            WaitCapacity,
            lookup={"key": WAIT_CAPACITY_FREE_KEY},
            limit=WAIT_CAPACITY_FREE_LIMIT,
            grace_seconds=WAIT_LEASE_GRACE_SECONDS,
        )
        if not granted_free:
            _record_wait_capacity_stats(WAIT_CAPACITY_FREE_KEY, refused=True)
            return False
        granted_global, global_count = _acquire_counter_row_count(
            WaitCapacity,
            lookup={"key": WAIT_CAPACITY_KEY},
            limit=WAIT_CAPACITY_LIMIT,
            grace_seconds=WAIT_LEASE_GRACE_SECONDS,
        )
        if not granted_global:
            _release_counter_row(WaitCapacity, lookup={"key": WAIT_CAPACITY_FREE_KEY})
            _record_wait_capacity_stats(WAIT_CAPACITY_KEY, refused=True)
            return False
        _record_wait_capacity_stats(WAIT_CAPACITY_FREE_KEY, count=free_count)
        _record_wait_capacity_stats(WAIT_CAPACITY_KEY, count=global_count)
        return True
    granted, count = _acquire_counter_row_count(
        WaitCapacity,
        lookup={"key": WAIT_CAPACITY_KEY},
        limit=WAIT_CAPACITY_LIMIT,
        grace_seconds=WAIT_LEASE_GRACE_SECONDS,
    )
    if granted:
        _record_wait_capacity_stats(WAIT_CAPACITY_KEY, count=count)
    else:
        _record_wait_capacity_stats(WAIT_CAPACITY_KEY, refused=True)
    return granted


def release_wait_lease(user_id: int) -> None:
    """Best-effort decrement of the account's in-flight wait counter."""
    try:
        with transaction.atomic():
            WaitLease.objects.select_for_update().filter(user_id=user_id, count__gt=0).update(
                count=F("count") - 1
            )
    except Exception:
        logger.exception("wait_lease_release_failed user_id=%s", user_id)


def release_wait_capacity(*, free: bool = False) -> None:
    """Best-effort decrement of the global (and, for free plans, the reserved
    free sub-pool) in-flight wait counter(s)."""
    _release_counter_row(WaitCapacity, lookup={"key": WAIT_CAPACITY_KEY})
    if free:
        _release_counter_row(WaitCapacity, lookup={"key": WAIT_CAPACITY_FREE_KEY})


class WaitSlotError(Exception):
    """Raised by ``wait_slot`` when no slot could be acquired.

    ``reason`` is either "too_many_wait_requests" (per-account cap) or
    "wait_capacity" (global/free-pool cap) — the same two API error codes the
    view layer already returns, so callers can map this straight to a
    response without re-deriving which cap tripped.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@contextmanager
def wait_slot(user_pk: int):
    "Public API."
    if not acquire_wait_lease(user_pk):
        raise WaitSlotError("too_many_wait_requests")
    free = is_free_plan(user_pk)
    try:
        if not acquire_wait_capacity(free=free):
            release_wait_lease(user_pk)
            raise WaitSlotError("wait_capacity")
    except WaitSlotError:
        raise
    except Exception:
        release_wait_lease(user_pk)
        raise
    try:
        yield
    finally:
        release_wait_lease(user_pk)
        release_wait_capacity(free=free)


def acquire_verify_capacity() -> bool:
    "Public API."
    return _acquire_counter_row(
        WaitCapacity,
        lookup={"key": VERIFY_CAPACITY_KEY},
        limit=VERIFY_CAPACITY_LIMIT,
        grace_seconds=VERIFY_CAPACITY_GRACE_SECONDS,
    )


def release_verify_capacity() -> None:
    """Best-effort decrement of the global /verify in-flight counter."""
    _release_counter_row(WaitCapacity, lookup={"key": VERIFY_CAPACITY_KEY})




# VERIFY_CAPACITY_* — a full-mode probe holds a worker up to
# SMTP_PROBE_TOTAL_BUDGET_SECONDS (~12s vs the DNS-only path's 4s), so it


VERIFY_FULL_CAPACITY_KEY = "verify-full-global"
VERIFY_FULL_CAPACITY_GRACE_SECONDS = 30


def acquire_verify_full_capacity() -> bool:
    """Global in-flight cap on full-mode /verify probes (design §4c),
    settings.VERIFY_FULL_CAPACITY_LIMIT — separate WaitCapacity row from
    both the safe verify pool and the wait-endpoint pool."""
    return _acquire_counter_row(
        WaitCapacity,
        lookup={"key": VERIFY_FULL_CAPACITY_KEY},
        limit=settings.VERIFY_FULL_CAPACITY_LIMIT,
        grace_seconds=VERIFY_FULL_CAPACITY_GRACE_SECONDS,
    )


def release_verify_full_capacity() -> None:
    """Best-effort decrement of the global full-mode /verify in-flight counter."""
    _release_counter_row(WaitCapacity, lookup={"key": VERIFY_FULL_CAPACITY_KEY})


def check_verify_full_domain_cooldown(domain: str) -> bool:
    """Design §4d: GLOBAL per-target-domain cooldown, ACROSS ALL ACCOUNTS —
    the remote MX sees one probing IP, so reputation is shared and the
    bucket subject is the domain, not the tenant. Returns True if a probe to
    ``domain`` may proceed (and charges one slot of the window); False when
    the window (settings.VERIFY_FULL_PER_DOMAIN_LIMIT within
    VERIFY_FULL_PER_DOMAIN_WINDOW seconds) is already exhausted — the caller
    must degrade (smtp.reason="domain_cooldown"), never probe.

    Deliberately does NOT raise RateLimitExceeded (unlike increment_rate_limit)
    so the caller can degrade to a 200 safe verdict instead of a 429 — a
    global, cross-tenant cooldown is not something an individual caller can
    "fix" by backing off, so it should never surface as their rate limit.
    """
    try:
        increment_rate_limit(
            "verify-full:domain:window",
            domain,
            limit=settings.VERIFY_FULL_PER_DOMAIN_LIMIT,
            window_seconds=settings.VERIFY_FULL_PER_DOMAIN_WINDOW,
        )
    except RateLimitExceeded:
        return False
    return True


def check_verify_full_global_fleet_cap() -> bool:
    "Public API."
    try:
        increment_rate_limit(
            "verify-full:global:hour",
            "global",
            limit=settings.VERIFY_FULL_GLOBAL_HOURLY_CAP,
            window_seconds=3600,
        )
    except RateLimitExceeded:
        return False
    return True


def hash_api_key(token: str) -> str:
    return hmac.new(settings.API_KEY_PEPPER.encode(), token.encode(), hashlib.sha256).hexdigest()


def create_api_key(
    user, name: str, *, created_by=None, expires_at=None
) -> tuple[ApiKey, str]:
    "Public API."
    if created_by is None:
        created_by = user
    with transaction.atomic():
        # Serialize concurrent mints for the same user on their plan row (the
        # same lock inbox provisioning uses), so two simultaneous creates
        # cannot both slip past the active-key cap. A user without a plan row
        # (never the case on the dashboard path) simply skips the lock.
        AccountPlan.objects.select_for_update().filter(user=user).first()
        active_count = ApiKey.objects.filter(user=user, revoked_at__isnull=True).count()
        if active_count >= MAX_ACTIVE_API_KEYS_PER_USER:
            raise ApiKeyLimitExceeded(
                active_count=active_count, limit=MAX_ACTIVE_API_KEYS_PER_USER
            )
        token = f"ms_live_{secrets.token_urlsafe(32)}"
        api_key = ApiKey.objects.create(
            user=user,
            created_by=created_by,
            name=name,
            prefix=token.removeprefix("ms_live_")[:API_KEY_PREFIX_LENGTH],
            key_hash=hash_api_key(token),
            expires_at=expires_at,
        )
    return api_key, token


DEMO_USER_EMAIL = "demo@mailsocket.internal"
DEMO_INBOX_RATE_NAME = "demo:inbox-create:hour"
DEMO_INBOX_RATE_LIMIT = 5
DEMO_INBOX_RATE_WINDOW = 3600

# try-page.md step 2 ("send me a sample code"): a SEPARATE bucket from inbox
# creation — minting an inbox and asking for a synthetic code are different
# abuse surfaces (the sample path creates an inbound Message through the
# real ingest/parse pipeline, so it is the more expensive of the two).
DEMO_SAMPLE_RATE_NAME = "demo:sample:hour"
DEMO_SAMPLE_RATE_LIMIT = 5
DEMO_SAMPLE_RATE_WINDOW = 3600
# Per-inbox cap (independent of the per-IP bucket above): even a single IP
# that stays under the hourly rate limit cannot keep minting mail into the
# SAME demo inbox forever.
DEMO_SAMPLE_PER_INBOX_LIMIT = 3

# r2 (try-page  P2): per-account concurrent-wait cap for the shared
# demo user only (see wait_lease_limit_for). Equal to the Free sub-pool size,
# which still bounds total in-flight demo waits globally.
DEMO_WAIT_LEASE_LIMIT = WAIT_CAPACITY_FREE_LIMIT - 1  # always leave one free-pool slot for real Free/agent users

# --- Zero-signup agent self-registration -------------------------------------
# An AI agent (or a dev in a hurry) can POST /api/v1/agents/register and get a
# working API key back in one call, no email and no human confirmation. Each
# agent gets its OWN throwaway Free-plan account (unlike the shared demo user),
# so keys are isolated and independently revocable/rate-limited. Accounts live
# on the non-deliverable ``agent.mailsocket.internal`` domain so they can never
# be a real signed-in customer and their inboxes fall under the Free 1-day
# retention purge. Registration is IP-rate-limited to curb abuse.
AGENT_ACCOUNT_DOMAIN = "agent.mailsocket.internal"
AGENT_REGISTER_RATE_NAME = "api:agent-register:ip:hour"
AGENT_REGISTER_RATE_LIMIT = 10
AGENT_REGISTER_RATE_WINDOW = 3600


# per-IP limit above. Per-IP alone is defeated by a routine /64 IPv6
# allocation (or a botnet), which can mint effectively unlimited accounts
# while never repeating a source IP.
AGENT_REGISTER_GLOBAL_RATE_NAME = "api:agent-register:global:hour"
AGENT_REGISTER_GLOBAL_RATE_LIMIT = 200
AGENT_REGISTER_GLOBAL_RATE_WINDOW = 3600


# the window) are eligible for GC by the gc_agent_accounts management command.
AGENT_ACCOUNT_GC_IDLE_DAYS = 7


def register_agent(name: str = "") -> tuple[object, ApiKey, str]:
    """Create a fresh anonymous agent account + first API key in one shot.

    Returns ``(user, api_key, token)``. The account is pre-verified and on the
    Free plan so the returned key immediately passes API auth. The caller is
    responsible for IP rate limiting before invoking this.
    """
    User = get_user_model()
    label = (name or "agent").strip()[:60] or "agent"
    for _ in range(10):
        handle = secrets.token_urlsafe(12).lower().replace("-", "").replace("_", "")[:16]
        email = f"agent_{handle}@{AGENT_ACCOUNT_DOMAIN}"
        try:
            with transaction.atomic():
                user = User(username=f"ms_agent_{handle}", email=email)
                user.set_unusable_password()
                user.save()
                Account.objects.create(
                    user=user,
                    email_normalized=normalize_email(email),
                    email_verified_at=timezone.now(),
                    verification_nonce="",
                    # Signup source attribution: every self-registered
                    # agent is tagged with the fixed channel ref.
                    src_ref=AGENT_REGISTER_REF,
                    src_first_seen=timezone.now(),
                )
                AccountPlan.objects.create(user=user, plan=AccountPlan.Plan.FREE)
                token = f"ms_live_{secrets.token_urlsafe(32)}"
                api_key = ApiKey.objects.create(
                    user=user,
                    name=f"{label} (self-registered)",
                    prefix=token.removeprefix("ms_live_")[:API_KEY_PREFIX_LENGTH],
                    key_hash=hash_api_key(token),
                )
            return user, api_key, token
        except IntegrityError:
            # Extremely rare handle/username collision — retry with a fresh one.
            continue
    raise RuntimeError("could not allocate a unique agent account")


def get_demo_user():
    """Idempotently provision (or fetch) the single shared demo user.

    The try-it-live playground is served by ONE dedicated Free-plan account,
    ``demo@mailsocket.internal``, that owns every demo inbox. It is created
    with an unusable password and a pre-verified account so it can never be
    signed into, and it can never be a real customer: its domain
    (``mailsocket.internal``) is not a deliverable public mailbox. All demo
    inboxes therefore fall under the existing Free retention purge (1 day),
    which is what deletes their inbound mail — no new purge path.
    """
    User = get_user_model()
    normalized = normalize_email(DEMO_USER_EMAIL)
    try:
        with transaction.atomic():
            user = (
                User.objects.select_related("account")
                .filter(email=DEMO_USER_EMAIL)
                .first()
            )
            if user is not None:
                return user
            user = User(
                username=f"ms_demo_{secrets.token_urlsafe(12)}", email=DEMO_USER_EMAIL
            )
            user.set_unusable_password()
            user.save()
            Account.objects.create(
                user=user,
                email_normalized=normalized,
                email_verified_at=timezone.now(),
                verification_nonce="",
            )
            AccountPlan.objects.create(user=user, plan=AccountPlan.Plan.FREE)
        return user
    except IntegrityError:
        # First-boot race: a concurrent request already created the demo user
        # (Account.email_normalized is UNIQUE). The losing thread's atomic block
        # rolled back cleanly; re-fetch the winner instead of surfacing a 500.
        existing = (
            User.objects.select_related("account")
            .filter(email=DEMO_USER_EMAIL)
            .first()
        )
        if existing is not None:
            return existing
        raise



INSTANT_USER_EMAIL = "instant@mailsocket.internal"
INSTANT_INBOX_MINT_RATE_NAME = "instant:inbox-create:hour"
INSTANT_INBOX_MINT_RATE_LIMIT = 5
INSTANT_INBOX_MINT_RATE_WINDOW = 3600
INSTANT_MESSAGES_RATE_NAME = "instant:messages:minute"
INSTANT_MESSAGES_RATE_LIMIT = 30
INSTANT_MESSAGES_RATE_WINDOW = 60
INSTANT_CLAIM_SALT = "instant-inbox-v1"
INSTANT_CLAIM_MAX_AGE = 86400
INSTANT_CLAIM_COOKIE_NAME = "ms_instant"


def get_instant_user():
    "Public API."
    User = get_user_model()
    normalized = normalize_email(INSTANT_USER_EMAIL)
    try:
        with transaction.atomic():
            user = (
                User.objects.select_related("account")
                .filter(email=INSTANT_USER_EMAIL)
                .first()
            )
            if user is not None:
                return user
            user = User(
                username=f"ms_instant_{secrets.token_urlsafe(12)}", email=INSTANT_USER_EMAIL
            )
            user.set_unusable_password()
            user.save()
            Account.objects.create(
                user=user,
                email_normalized=normalized,
                email_verified_at=timezone.now(),
                verification_nonce="",
            )
            AccountPlan.objects.create(user=user, plan=AccountPlan.Plan.FREE)
        return user
    except IntegrityError:
        # First-boot race: a concurrent request already created the instant
        # user (Account.email_normalized is UNIQUE). Re-fetch the winner
        # instead of surfacing a 500 — same pattern as get_demo_user.
        existing = (
            User.objects.select_related("account")
            .filter(email=INSTANT_USER_EMAIL)
            .first()
        )
        if existing is not None:
            return existing
        raise
