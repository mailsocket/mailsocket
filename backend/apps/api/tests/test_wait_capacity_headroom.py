"Public API."
import re
from pathlib import Path

import pytest
from django.conf import settings

from apps.accounts import services
from apps.api import views

REPO_ROOT = Path(__file__).resolve().parents[4]


def _gunicorn_line(text):
    return next(ln for ln in text.splitlines() if "gunicorn" in ln and "config.wsgi:application" in ln)


def _flag_default(line, name):
    """Default of an env-driven gunicorn flag: `--name ${VAR:-N}` (Dockerfile
    shell form) or `"--name", "${VAR:-N}"` (compose JSON array)."""
    m = re.search(rf'--{name}"?,?\s*"?\$\{{(\w+):-(\d+)\}}', line)
    return (m.group(1), int(m.group(2))) if m else None


@pytest.mark.parametrize("path", ["docker-compose.yml", "Dockerfile"])
def test_gunicorn_flags_are_env_driven_with_settings_defaults(path, monkeypatch):
    """cap r2: ONE source — the gunicorn command reads the same GUNICORN_*
    env vars as config.settings.base, with the same defaults."""
    line = _gunicorn_line((REPO_ROOT / path).read_text())
    assert _flag_default(line, "workers") == ("GUNICORN_WORKERS", 3)
    assert _flag_default(line, "threads") == ("GUNICORN_THREADS", 32)
    # worker_connections == threads: a worker whose threads are all held by
    # waits stops accepting, so new requests go to a worker with a free thread
    # instead of queueing behind long-polls (measured: default 1000 queued
    # normal requests for many seconds).
    assert _flag_default(line, "worker-connections") == ("GUNICORN_THREADS", 32)
    # ...and settings derive the same defaults from the same env.
    for name in ("GUNICORN_WORKERS", "GUNICORN_THREADS", "WAIT_CAPACITY_LIMIT", "WAIT_CAPACITY_FREE_LIMIT"):
        monkeypatch.delenv(name, raising=False)
    base = _load_settings_module("base")
    assert (base.GUNICORN_WORKERS, base.GUNICORN_THREADS) == (3, 32)
    assert (base.WAIT_CAPACITY_LIMIT, base.WAIT_CAPACITY_FREE_LIMIT) == (64, 12)


def test_dockerfile_cmd_uses_shell_so_env_expands():
    line = _gunicorn_line((REPO_ROOT / "Dockerfile").read_text())
    assert line.startswith('CMD ["sh", "-c", "exec gunicorn ')


def test_services_constants_come_from_settings():
    assert services.GUNICORN_WORKERS == settings.GUNICORN_WORKERS
    assert services.GUNICORN_THREADS == settings.GUNICORN_THREADS
    assert services.WAIT_CAPACITY_LIMIT == settings.WAIT_CAPACITY_LIMIT
    assert services.WAIT_CAPACITY_FREE_LIMIT == settings.WAIT_CAPACITY_FREE_LIMIT
    assert services.WAIT_NON_WAIT_HEADROOM_MIN == settings.WAIT_NON_WAIT_HEADROOM_MIN


def test_wait_capacity_leaves_non_wait_headroom():
    slots = services.GUNICORN_WORKERS * services.GUNICORN_THREADS
    assert slots - services.WAIT_CAPACITY_LIMIT >= services.WAIT_NON_WAIT_HEADROOM_MIN >= 24


def test_raised_global_and_free_pool_values():
    assert services.WAIT_CAPACITY_LIMIT == 64
    assert services.WAIT_CAPACITY_FREE_LIMIT == 12
    # Free sub-pool strictly below the global pool so paid always has room.
    assert services.WAIT_CAPACITY_FREE_LIMIT < services.WAIT_CAPACITY_LIMIT
    # Demo keeps one free-pool slot for real Free/agent users.
    assert services.DEMO_WAIT_LEASE_LIMIT == services.WAIT_CAPACITY_FREE_LIMIT - 1


def test_plan_wait_concurrency_tiers():
    limits = settings.BILLING_PLAN_LIMITS
    caps = {k: limits[k]["wait_concurrency"] for k in ("free", "paid", "scale", "business")}
    assert caps == {"free": 2, "paid": 10, "scale": 20, "business": 40}
    assert caps["free"] <= caps["paid"] <= caps["scale"] <= caps["business"]
    # One account can never need more than the whole global pool.
    assert max(caps.values()) <= services.WAIT_CAPACITY_LIMIT
    # A Free account always fits inside the free sub-pool.
    assert caps["free"] <= services.WAIT_CAPACITY_FREE_LIMIT
    assert services.WAIT_LEASE_LIMIT_PAID == caps["paid"]


def test_adaptive_poll_interval():
    fast, window, slow = (
        views.WAIT_POLL_SECONDS,
        views.WAIT_POLL_FAST_WINDOW_SECONDS,
        views.WAIT_POLL_SLOW_SECONDS,
    )
    assert (fast, window, slow) == (0.5, 5.0, 1.0)
    assert views._wait_poll_interval(0) == fast
    assert views._wait_poll_interval(window - 0.01) == fast
    assert views._wait_poll_interval(window) == slow
    assert views._wait_poll_interval(24) == slow


def test_adaptive_poll_never_faster_than_base(monkeypatch):
    # Tests shrink WAIT_POLL_SECONDS; a slow interval must never be FASTER
    # than the configured base cadence, and shrinking the base must not be
    # undone after the fast window by a larger slow value in a way that
    # breaks the min() against `remaining` in _wait.
    monkeypatch.setattr(views, "WAIT_POLL_SECONDS", 2.0)
    assert views._wait_poll_interval(100) == 2.0


class _FakeConn:
    def __init__(self, pool, in_atomic_block):
        self.pool = pool
        self.in_atomic_block = in_atomic_block
        self.closed = 0

    def close(self):
        self.closed += 1


@pytest.mark.parametrize(
    "pool,atomic,expect_close",
    [
        (object(), False, 1),  # pooled, autocommit: hand the conn back
        (object(), True, 0),  # pooled but inside a transaction: never close
        (None, False, 0),  # no pool (dev/tests): keep persistent conn, as before
    ],
)
def test_release_db_connection_while_idle(monkeypatch, pool, atomic, expect_close):
    fake = _FakeConn(pool, atomic)
    monkeypatch.setattr(views, "connection", fake)
    views._release_db_connection_while_idle()
    assert fake.closed == expect_close


_PROD_ENV = (
    "SECRET_KEY", "ALLOWED_HOSTS", "INGEST_SHARED_SECRET", "AUTH_RATE_LIMIT_SECRET",
    "API_KEY_PEPPER", "WEBHOOK_SIGNING_MASTER_KEY", "DB_NAME", "DB_USERNAME",
    "DB_PASS", "DB_HOST", "DB_PORT",
)


def _load_settings_module(name):
    """Execute config/settings/<name>.py in isolation (fresh module, current
    os.environ) without touching the active test settings."""
    import importlib.util

    path = Path(settings.BASE_DIR) / "config" / "settings" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"config.settings._probe_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "config.settings"
    spec.loader.exec_module(module)
    return module


def _load_production(monkeypatch, **env):
    """production.py with dummy secrets + ``env`` overrides, on top of a
    freshly executed base (so env-driven base values are re-read)."""
    import sys

    for name in _PROD_ENV:
        monkeypatch.setenv(name, "x" if name != "ALLOWED_HOSTS" else "localhost")
    for name in ("DB_POOL_MAX_SIZE", "GUNICORN_WORKERS", "GUNICORN_THREADS",
                 "WAIT_CAPACITY_LIMIT", "WAIT_CAPACITY_FREE_LIMIT", "WAIT_NON_WAIT_HEADROOM_MIN",
                 "PAID_WAIT_CONCURRENCY", "SCALE_WAIT_CONCURRENCY", "BUSINESS_WAIT_CONCURRENCY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENABLE_OUTBOUND", "false")
    monkeypatch.setenv("ENABLE_BILLING", "false")
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    # production.py does `from .base import *` and mutates base.DATABASES in
    # place: hand it a freshly executed base so nothing leaks into the
    # real config.settings.base used by the running tests.
    monkeypatch.setitem(sys.modules, "config.settings.base", _load_settings_module("base"))
    return _load_settings_module("production")


def test_production_settings_enable_bounded_db_pool(monkeypatch):
    """The production module turns on Django's psycopg pool (max 16/worker,
    so 3 workers <= 48 backends < 60) and drops CONN_MAX_AGE, which Django
    requires when pooling."""
    module = _load_production(monkeypatch)
    db = module.DATABASES["default"]
    assert db["CONN_MAX_AGE"] == 0
    pool = db["OPTIONS"]["pool"]
    assert pool["max_size"] == 16
    assert pool["max_size"] * module.GUNICORN_WORKERS < 60


# --- the pooled release path, end to end against the real test database ----
# Tests normally run WITHOUT a connection pool (so the release helper is a
# no-op, see test_release_db_connection_while_idle). This one switches the
# default alias onto Django's real psycopg pool for its duration — exactly
# the production mode — and proves a wait that hands its connection back
# between polls still sees a message committed mid-wait, returns it, and
# leaves the lease/capacity counters released.


@pytest.fixture
def pooled_default_db():
    from django.db import connection
    from django.db.backends.postgresql.psycopg_any import is_psycopg3

    if not is_psycopg3:
        pytest.skip("Django's native pool needs psycopg 3")
    connection.close()
    saved = (connection.settings_dict.get("CONN_MAX_AGE"), dict(connection.settings_dict.get("OPTIONS", {})))
    connection.settings_dict["CONN_MAX_AGE"] = 0
    connection.settings_dict.setdefault("OPTIONS", {})["pool"] = {"min_size": 1, "max_size": 4, "timeout": 5}
    try:
        yield connection
    finally:
        connection.close()
        connection.close_pool()
        connection.settings_dict["CONN_MAX_AGE"] = saved[0]
        connection.settings_dict["OPTIONS"] = saved[1]


@pytest.mark.django_db(transaction=True)
def test_pooled_wait_releases_connection_and_still_returns_otp(monkeypatch, pooled_default_db):
    import threading
    import time

    from django.db import close_old_connections

    from apps.accounts.models import AccountPlan, WaitCapacity, WaitLease
    from apps.api.tests.test_wait import auth_client, make_message, make_user, wait_url
    from apps.inboxes.models import Inbox

    connection = pooled_default_db
    assert connection.pool is not None

    closes = []
    real_close = connection.close

    def counting_close():
        closes.append(1)
        real_close()

    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    monkeypatch.setattr(connection, "close", counting_close)

    owner = make_user("pooled_wait", plan=AccountPlan.Plan.PAID)
    inbox = Inbox.objects.create(user=owner, routing_key="pooled_wait_box")
    client = auth_client(owner)

    def deliver_mid_wait():
        close_old_connections()
        time.sleep(0.4)
        make_message(inbox, "pooled-otp", otp="654321", otp_confidence=0.9)
        close_old_connections()

    thread = threading.Thread(target=deliver_mid_wait)
    thread.start()
    response = client.get(f"{wait_url(inbox)}?timeout=5")
    thread.join(timeout=5)

    assert response.status_code == 200
    assert response.json()["data"]["otp"] == "654321"
    # The wait handed its connection back to the pool between polls.
    assert len(closes) >= 2
    # ...and the slot bookkeeping (which reconnects lazily) still released.
    assert WaitLease.objects.get(user_id=owner.pk).count == 0
    assert all(row.count == 0 for row in WaitCapacity.objects.all())
