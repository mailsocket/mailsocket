"""Durable per-week activity signal for the funnel report and the North Star.

``AccountWeeklyActivity`` keeps one row per (account, ISO week, UTC) in which
the account received at least one EXTERNAL message, with the time of the
first such message. It is written append-only (``ON CONFLICT DO NOTHING``):

* from ``entitlements.record_message_once`` when a message is first
  usage-counted (the one place every parsed message passes exactly once), and
* from ``reconcile_monthly_usage``, which marks messages counted in bulk
  without going through ``record_message_once`` (this is also the backfill
  for messages still retained when the table first ships).

Why not MonthlyUsage alone: it is monthly (cannot tell a message on day 6
from one on day 20 of a month) and it counts the self-generated welcome mail
every verified account gets, so every verified signup would look
"activated". Message rows cannot be used either: Free retention purges them
after 1 day.

Self-generated mail never counts:

* welcome / dashboard "send a test code" / demo sample: ``SYSTEM_SENDERS``
  lists their exact envelope senders;
* mailsocket OUTBOUND mail (apps.outbound.engine): its envelope sender is
  the VERP address ``<public_id>.<mac>@settings.OUTBOUND_BOUNCE_DOMAIN``, so
  a customer mailing their own mailsocket inbox through mailsocket could
  otherwise "activate" themselves. ``system_sender_domains()`` lists the
  domains (and their subdomains) whose senders never count, matched
  case-insensitively.

No content and no addresses are stored; only the user id, the week bucket
and a timestamp.
"""

from django.conf import settings
from django.db import connection

from apps.inboxes.models import Inbox, Message

from .models import AccountWeeklyActivity

# Envelope senders of mail mailsocket generates itself. Kept in sync with
# apps.inboxes.services (welcome), apps.dashboard.views.TEST_CODE_SENDER and
# apps.dashboard.demo.DEMO_SAMPLE_SENDER by a test (accounts may not import
# dashboard: backend/.importlinter core-no-ui-no-money).
SYSTEM_SENDERS = (
    "welcome@mailsocket.app",
    "test-code@mailsocket.app",
    "demo@mailsocket.app",
)


def system_sender_domains() -> tuple[str, ...]:
    """Envelope-sender domains mailsocket itself generates. apps.outbound
    builds exactly one: the VERP bounce domain (engine.py; message_builder's
    MAIL_DOMAIN is only used for Message-ID, never as a sender)."""
    domain = (settings.OUTBOUND_BOUNCE_DOMAIN or "").strip().lower().rstrip(".")
    return (domain,) if domain else ()


# SQL predicate "m is external" shared by the hook and the backfill
# (params: SYSTEM_SENDERS list, system_sender_domains() list).
EXTERNAL_SQL = """
    NOT (lower(m.envelope_from) = ANY(%s))
    AND NOT EXISTS (
        SELECT 1 FROM unnest(%s::text[]) AS d(domain)
        WHERE right(lower(m.envelope_from), length(d.domain) + 1)
              IN ('@' || d.domain, '.' || d.domain)
    )
"""


def external_params() -> list:
    return [list(SYSTEM_SENDERS), list(system_sender_domains())]


def _insert(where: str, params: list) -> int:
    quote = connection.ops.quote_name
    sql = f"""
        INSERT INTO {quote(AccountWeeklyActivity._meta.db_table)} (user_id, week_start, first_message_at)
        SELECT i.user_id,
               (date_trunc('week', m.received_at AT TIME ZONE 'UTC'))::date,
               min(m.received_at)
        FROM {quote(Message._meta.db_table)} m
        JOIN {quote(Inbox._meta.db_table)} i ON i.id = m.inbox_id
        WHERE {where} AND {EXTERNAL_SQL}
        GROUP BY 1, 2
        ON CONFLICT (user_id, week_start) DO NOTHING
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [*params, *external_params()])
        return cursor.rowcount


def record_weekly_activity(message_id: int) -> int:
    """Record the week of one stored message (no-op for system mail or an
    already-recorded week). Returns the number of new rows (0 or 1)."""
    return _insert("m.id = %s", [message_id])


def backfill_weekly_activity(since) -> int:
    """Record every retained message received at/after ``since`` (idempotent)."""
    return _insert("m.received_at >= %s", [since])
