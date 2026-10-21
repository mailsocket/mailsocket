"Public API."

import logging
from contextlib import contextmanager
from datetime import timezone as dt_timezone

from django.db import connection, transaction
from django.utils import timezone

from apps.inboxes.models import Inbox, Message

from .activity import EXTERNAL_SQL, external_params
from .attribution import sanitize_ref
from .models import Account, AccountActiveDay, AccountIntent, DemoStartDaily

logger = logging.getLogger(__name__)

ACTIVE_DAY_WINDOW_DAYS = 7
VIEWED_PRICING = "viewed_pricing_at"
STARTED_CHECKOUT = "started_checkout_at"
INTENT_FIELDS = (VIEWED_PRICING, STARTED_CHECKOUT)
# The intent write runs on the response path (IntentMiddleware): a row lock
# or a slow DB may hold the response at most this long, then the write is
# cancelled and only logged (Postgres statement_timeout; other vendors: none).
INTENT_WRITE_TIMEOUT_MS = 200


def _quote(model) -> str:
    return connection.ops.quote_name(model._meta.db_table)


@contextmanager
def _statement_timeout(cursor, timeout_ms):
    """``SET LOCAL statement_timeout`` for the statements in the block, then
    back to the previous value (SET LOCAL would otherwise outlive the
    savepoint up to the end of an enclosing transaction). On an error the
    savepoint rollback restores it. No-op without a timeout or off Postgres."""
    if timeout_ms is None or connection.vendor != "postgresql":
        yield
        return
    cursor.execute("SELECT current_setting('statement_timeout')")
    previous = cursor.fetchone()[0]
    cursor.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
    yield
    cursor.execute("SELECT set_config('statement_timeout', %s, true)", [previous])


def _best_effort(name, build, *, timeout_ms=None) -> int:
    """Run the (sql, params) that ``build()`` returns in its own savepoint,
    optionally bounded by ``timeout_ms``. Building the statement is inside
    the guard too. Never raises."""
    try:
        sql, params = build()
        with transaction.atomic():
            with connection.cursor() as cursor, _statement_timeout(cursor, timeout_ms):
                cursor.execute(sql, params)
                return cursor.rowcount
    except Exception:
        logger.exception("funnel_signal_failed signal=%s", name)
        return 0


def _demo_start_sql(ref, now):
    day = (now or timezone.now()).astimezone(dt_timezone.utc).date()
    table = _quote(DemoStartDaily)
    sql = f"""
        INSERT INTO {table} (day, ref, count) VALUES (%s, %s, 1)
        ON CONFLICT (day, ref) DO UPDATE SET count = {table}.count + 1
    """
    return sql, [day, sanitize_ref(ref) or ""]


def record_demo_start(ref=None, *, now=None) -> int:
    """+1 on today's (UTC) demo counter for ``ref`` (validated, else '')."""
    return _best_effort("demo_start", lambda: _demo_start_sql(ref, now))


def _active_day_sql(where: str) -> str:
    return f"""
        INSERT INTO {_quote(AccountActiveDay)} (user_id, day)
        SELECT DISTINCT i.user_id, (m.received_at AT TIME ZONE 'UTC')::date
        FROM {_quote(Message)} m
        JOIN {_quote(Inbox)} i ON i.id = m.inbox_id
        JOIN {_quote(Account)} a ON a.user_id = i.user_id
        WHERE {where}
          AND m.received_at >= a.created_at
          AND m.received_at < a.created_at + make_interval(days => %s)
          AND {EXTERNAL_SQL}
        ON CONFLICT (user_id, day) DO NOTHING
    """


def record_active_day(message_id) -> int:
    """Record the UTC day of one stored message when it is external and
    inside the account's first ACTIVE_DAY_WINDOW_DAYS days (else no-op)."""
    return _best_effort(
        "active_day",
        lambda: (_active_day_sql("m.id = %s"), [message_id, ACTIVE_DAY_WINDOW_DAYS, *external_params()]),
    )


def backfill_active_days(since) -> int:
    """Same for every retained message received at/after ``since`` (idempotent)."""
    return _best_effort(
        "active_day_backfill",
        lambda: (_active_day_sql("m.received_at >= %s"), [since, ACTIVE_DAY_WINDOW_DAYS, *external_params()]),
    )


def record_intent(user_id, field, *, now=None) -> int:
    """Set ``field`` (one of INTENT_FIELDS) to now for ``user_id`` unless it
    is already set: first event wins, later events write nothing.

    Bounded and rare: a primary-key read first, so once the flag is set no
    write is ever attempted again; the INSERT itself is capped at
    INTENT_WRITE_TIMEOUT_MS (a timeout is logged, never raised)."""
    if field not in INTENT_FIELDS:
        raise ValueError(f"unknown intent field {field!r}")
    if _intent_already_set(user_id, field):
        return 0
    return _best_effort(
        "intent", lambda: _intent_sql(user_id, field, now), timeout_ms=INTENT_WRITE_TIMEOUT_MS
    )


def _intent_already_set(user_id, field) -> bool:
    try:
        return AccountIntent.objects.filter(pk=user_id, **{f"{field}__isnull": False}).exists()
    except Exception:
        logger.exception("funnel_signal_failed signal=intent_read")
        return False


def _intent_sql(user_id, field, now):
    table = _quote(AccountIntent)
    column = connection.ops.quote_name(field)
    sql = f"""
        INSERT INTO {table} (user_id, {column}) VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET {column} = EXCLUDED.{column}
        WHERE {table}.{column} IS NULL
    """
    return sql, [user_id, now or timezone.now()]


# Route -> intent field, keyed on the resolved URL name (no path parsing).
# GET of a page = "viewed"; a checkout start is the POST that hands the
# buyer on (303 to the hosted checkout, or to the confirm page for an
# existing subscriber). Refusals (4xx/5xx, kill switch) record nothing.
INTENT_ROUTES = {
    ("dashboard:pricing", "GET"): VIEWED_PRICING,
    ("dashboard:billing-page", "GET"): VIEWED_PRICING,
    ("dashboard:billing-checkout", "POST"): STARTED_CHECKOUT,
}


def intent_for(request, response):
    match = getattr(request, "resolver_match", None)
    if match is None or not getattr(request, "user", None) or not request.user.is_authenticated:
        return None
    field = INTENT_ROUTES.get((match.view_name, request.method))
    if field == VIEWED_PRICING and response.status_code != 200:
        return None
    if field == STARTED_CHECKOUT and response.status_code not in (302, 303):
        return None
    return field


class IntentMiddleware:
    """Records the commercial-intent flags after the view ran, only on the
    three routes above for a signed-in account; nothing on any other
    request. One primary-key read, plus one INSERT (<= 200 ms) only until
    the flag is first set. Never changes or breaks the response."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            field = intent_for(request, response)
            if field is not None:
                from .tenancy import tenant

                record_intent(tenant(request).pk, field)
        except Exception:
            logger.exception("intent_middleware_failed")
        return response
