"""Fail-closed startup invariants for the web runtime sizing (cap r2).

Gunicorn workers/threads, the DB connection mode, the global wait pool and the
per-plan wait caps are all env-driven (config.settings.base). A partial edit,
e.g. ``DB_POOL_MAX_SIZE=0`` while gunicorn still runs 3 x 32 threads, can push
PostgreSQL past ``max_connections`` (each thread pins a persistent backend)
or let waits starve every request slot. These checks refuse to start such a
combination: the container entrypoint runs ``manage.py check`` before
gunicorn, and every management command (cron one-shots, migrate) runs them
too.

IDs:
  mailsocket.E001  persistent connections: workers*threads + reserved > max_connections
  mailsocket.E002  pooled connections: workers*pool_max + reserved > max_connections
  mailsocket.E003  WAIT_CAPACITY_LIMIT leaves too few non-wait request slots
  mailsocket.E004  a plan's wait_concurrency exceeds WAIT_CAPACITY_LIMIT
  mailsocket.E005  free sub-pool not strictly below WAIT_CAPACITY_LIMIT
  mailsocket.E006  canonical hosts missing from ALLOWED_HOSTS / CSRF origins
  mailsocket.E007  invalid MTA-STS policy settings (mode / max_age / mx)
  mailsocket.E008  BILLING_CHANGES_ENABLED is not exactly true / false / 1 / 0
"""

from django.conf import settings as django_settings
from django.core.checks import Error, register

# Below this a wait burst can take nearly every thread; the lowest value ever
# run in production (3x8 slots, 16 waits) is exactly this.
MIN_NON_WAIT_HEADROOM = 8

# psycopg_pool's default max_size when DATABASES OPTIONS "pool" is just True.
_PSYCOPG_POOL_DEFAULT_MAX = 4


def _pool_max_size(database: dict) -> int | None:
    """max_size of Django's native psycopg pool for this alias, or None if
    the alias uses one (persistent or per-request) connection per thread."""
    pool = (database.get("OPTIONS") or {}).get("pool")
    if not pool:
        return None
    if isinstance(pool, dict):
        return int(pool.get("max_size") or pool.get("min_size") or _PSYCOPG_POOL_DEFAULT_MAX)
    return _PSYCOPG_POOL_DEFAULT_MAX


def capacity_errors(conf=None) -> list[Error]:
    """Evaluate every invariant (pure; no DB). ``conf`` is any object with the
    settings attributes — django.conf.settings by default; tests pass a
    freshly loaded settings module for a given env."""
    settings = conf if conf is not None else django_settings
    errors: list[Error] = []
    workers = settings.GUNICORN_WORKERS
    threads = settings.GUNICORN_THREADS
    slots = workers * threads
    reserved = settings.RESERVED_NON_WEB_DB_CONNECTIONS
    max_connections = settings.DB_MAX_CONNECTIONS
    wait_limit = settings.WAIT_CAPACITY_LIMIT
    free_limit = settings.WAIT_CAPACITY_FREE_LIMIT
    headroom = settings.WAIT_NON_WAIT_HEADROOM_MIN

    if getattr(settings, "DB_CONNECTION_BUDGET_CHECK", True):
        pool_max = _pool_max_size(settings.DATABASES["default"])
        if pool_max is None:
            needed = slots + reserved
            if needed > max_connections:
                errors.append(
                    Error(
                        f"Persistent DB connections: GUNICORN_WORKERS*GUNICORN_THREADS "
                        f"({workers}*{threads}={slots}) + RESERVED_NON_WEB_DB_CONNECTIONS "
                        f"({reserved}) = {needed} > DB_MAX_CONNECTIONS ({max_connections}).",
                        hint="Enable the pool (DB_POOL_MAX_SIZE>0) or lower GUNICORN_THREADS "
                        "(scripts/rollback-wait-capacity.sh sets the full safe combination).",
                        id="mailsocket.E001",
                    )
                )
        else:
            needed = workers * pool_max + reserved
            if needed > max_connections:
                errors.append(
                    Error(
                        f"Pooled DB connections: GUNICORN_WORKERS*DB_POOL_MAX_SIZE "
                        f"({workers}*{pool_max}) + RESERVED_NON_WEB_DB_CONNECTIONS "
                        f"({reserved}) = {needed} > DB_MAX_CONNECTIONS ({max_connections}).",
                        hint="Lower DB_POOL_MAX_SIZE or GUNICORN_WORKERS.",
                        id="mailsocket.E002",
                    )
                )

    if headroom < MIN_NON_WAIT_HEADROOM or wait_limit > slots - headroom:
        errors.append(
            Error(
                f"WAIT_CAPACITY_LIMIT ({wait_limit}) must be <= GUNICORN_WORKERS*GUNICORN_THREADS "
                f"({slots}) - WAIT_NON_WAIT_HEADROOM_MIN ({headroom}), and the headroom must be "
                f">= {MIN_NON_WAIT_HEADROOM}, so waits cannot take every request slot.",
                hint="Lower WAIT_CAPACITY_LIMIT or raise GUNICORN_THREADS.",
                id="mailsocket.E003",
            )
        )

    for plan, limits in settings.BILLING_PLAN_LIMITS.items():
        cap = limits.get("wait_concurrency")
        if cap is not None and cap > wait_limit:
            errors.append(
                Error(
                    f"Plan {plan!r} wait_concurrency ({cap}) > WAIT_CAPACITY_LIMIT ({wait_limit}).",
                    hint="One account must never need more than the whole global wait pool.",
                    id="mailsocket.E004",
                )
            )

    if not 0 < free_limit < wait_limit:
        errors.append(
            Error(
                f"WAIT_CAPACITY_FREE_LIMIT ({free_limit}) must be > 0 and < WAIT_CAPACITY_LIMIT "
                f"({wait_limit}) so paid waits always keep reserved slots.",
                id="mailsocket.E005",
            )
        )
    return errors


@register()
def check_wait_capacity(app_configs=None, **kwargs):
    return capacity_errors()


def canonical_host_errors(settings_obj=None):
    "Public API."
    s = settings_obj or django_settings
    if not getattr(s, "CANONICAL_HOST_REDIRECTS", False):
        return []
    errors = []
    allowed = {h.lower() for h in getattr(s, "ALLOWED_HOSTS", [])}
    trusted = {o.lower().rstrip("/") for o in getattr(s, "CSRF_TRUSTED_ORIGINS", [])}
    for host in (getattr(s, "APEX_HOST", ""), getattr(s, "DASHBOARD_HOST", "")):
        host = (host or "").lower()
        if not host:
            continue
        if host not in allowed and "*" not in allowed and f".{host.split('.', 1)[-1]}" not in allowed:
            errors.append(Error(f"CANONICAL_HOST_REDIRECTS is on but {host!r} is not in ALLOWED_HOSTS.",
                                hint="Add both APEX_HOST and DASHBOARD_HOST to ALLOWED_HOSTS.", id="mailsocket.E006"))
        if f"https://{host}" not in trusted:
            errors.append(Error(f"CANONICAL_HOST_REDIRECTS is on but https://{host} is not in CSRF_TRUSTED_ORIGINS.",
                                hint="Add both https origins to CSRF_TRUSTED_ORIGINS.", id="mailsocket.E006"))
    return errors


@register()
def check_canonical_hosts(app_configs=None, **kwargs):
    return canonical_host_errors()


def mta_sts_errors(settings_obj=None):
    """mailsocket.E007: a malformed MTA-STS policy is worse than none (a
    receiving MTA that cannot parse it may defer mail), so refuse to start."""
    from apps.accounts.mta_sts import MAX_AGE_LIMIT, VALID_MODES

    s = settings_obj or django_settings
    if not getattr(s, "MTA_STS_HOST", ""):
        return []
    errors = []
    if getattr(s, "MTA_STS_MODE", "") not in VALID_MODES:
        errors.append(Error(f"MTA_STS_MODE must be one of {VALID_MODES}.", id="mailsocket.E007"))
    max_age = getattr(s, "MTA_STS_MAX_AGE", None)
    if not isinstance(max_age, int) or not 0 <= max_age <= MAX_AGE_LIMIT:
        errors.append(Error(f"MTA_STS_MAX_AGE must be 0..{MAX_AGE_LIMIT}.", id="mailsocket.E007"))
    mx = getattr(s, "MTA_STS_MX", [])
    if getattr(s, "MTA_STS_MODE", "") != "none" and not mx:
        errors.append(Error("MTA_STS_MX must list at least one MX host.", id="mailsocket.E007"))
    if any(not h or any(c.isspace() for c in h) for h in mx):
        errors.append(Error("MTA_STS_MX entries must be bare host names.", id="mailsocket.E007"))
    return errors


@register()
def check_mta_sts(app_configs=None, **kwargs):
    return mta_sts_errors()


def billing_kill_switch_errors(settings_obj=None):
    """mailsocket.E008: the billing kill switch is a money control, so a typo
    (``BILLING_CHANGES_ENABLED=flase``) must not be guessed either way —
    refuse to start (config.settings.base.strict_bool_env)."""
    s = settings_obj or django_settings
    if isinstance(getattr(s, "BILLING_CHANGES_ENABLED", True), bool):
        return []
    raw = getattr(s, "BILLING_CHANGES_ENABLED_RAW", None)
    return [
        Error(
            f"BILLING_CHANGES_ENABLED={raw!r} is not a valid value.",
            hint="Use exactly true, false, 1 or 0 (lowercase, no spaces); unset means true.",
            id="mailsocket.E008",
        )
    ]


@register()
def check_billing_kill_switch(app_configs=None, **kwargs):
    return billing_kill_switch_errors()
