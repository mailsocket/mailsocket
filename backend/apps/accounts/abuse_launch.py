"Public API."

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import Count, Q

SIGNUP_BURST = "signup_burst"
INBOX_BURST = "inbox_burst_per_account"
SENDER_CONCENTRATION = "sender_concentration"
KINDS = (SIGNUP_BURST, INBOX_BURST, SENDER_CONCENTRATION)
HOUR = timedelta(hours=1)


class Measure(int):
    """An int (compared with the threshold) that also carries how many
    accounts crossed it, for the alert text."""

    accounts: int = 0

    def __new__(cls, value, accounts=0):
        obj = super().__new__(cls, value)
        obj.accounts = accounts
        return obj


def _demo_and_workspace_excluded():
    from .services import DEMO_USER_EMAIL

    return DEMO_USER_EMAIL.lower(), "%@" + settings.WORKSPACE_ACCOUNT_DOMAIN.lower()


def signups_last_hour(now) -> Measure:
    from .models import Account
    from .services import AGENT_ACCOUNT_DOMAIN

    demo, workspace = _demo_and_workspace_excluded()
    recent = (
        Account.objects.filter(created_at__gte=now - HOUR)
        .exclude(email_normalized=demo)
        .exclude(email_normalized__endswith=workspace[1:])
    )
    counts = recent.aggregate(
        total=Count("pk"),
        agents=Count("pk", filter=Q(email_normalized__endswith="@" + AGENT_ACCOUNT_DOMAIN)),
    )
    return Measure(counts["total"], accounts=counts["agents"])


def _top_per_account(sql, params, threshold) -> Measure:
    """``sql`` returns one count per account (descending); Measure = the top
    count, ``accounts`` = how many accounts are over ``threshold``."""
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        counts = [row[0] for row in cursor.fetchall()]
    if not counts:
        return Measure(0)
    return Measure(counts[0], accounts=sum(1 for count in counts if count > threshold))


def inboxes_created_last_hour(now) -> Measure:
    from apps.inboxes.models import Inbox

    demo, _workspace = _demo_and_workspace_excluded()
    quote = connection.ops.quote_name
    sql = f"""
        SELECT count(*) AS n
        FROM {quote(Inbox._meta.db_table)} i
        JOIN {quote(get_user_model()._meta.db_table)} u ON u.id = i.user_id
        WHERE i.created_at >= %s AND lower(u.email) <> %s
        GROUP BY i.user_id
        ORDER BY n DESC
    """
    return _top_per_account(sql, [now - HOUR, demo], settings.INBOX_BURST_ALERT)


def free_sender_domains_today(now) -> Measure:
    from apps.inboxes.models import Inbox, Message

    from .abuse_alerts import _utc_day_start
    from .activity import EXTERNAL_SQL, external_params
    from .models import AccountPlan

    demo, _workspace = _demo_and_workspace_excluded()
    quote = connection.ops.quote_name
    sql = f"""
        SELECT count(DISTINCT lower(split_part(m.envelope_from, '@', 2))) AS n
        FROM {quote(Message._meta.db_table)} m
        JOIN {quote(Inbox._meta.db_table)} i ON i.id = m.inbox_id
        JOIN {quote(AccountPlan._meta.db_table)} p ON p.user_id = i.user_id
        JOIN {quote(get_user_model()._meta.db_table)} u ON u.id = i.user_id
        WHERE m.received_at >= %s AND p.plan = %s AND lower(u.email) <> %s
          AND position('@' in m.envelope_from) > 0
          AND {EXTERNAL_SQL}
        GROUP BY i.user_id
        ORDER BY n DESC
    """
    params = [_utc_day_start(now), AccountPlan.Plan.FREE, demo, *external_params()]
    return _top_per_account(sql, params, settings.SENDER_DOMAINS_ALERT)


def checks(now):
    """(kind, measure, threshold) triples, same shape as abuse_alerts uses."""
    return (
        (SIGNUP_BURST, lambda: signups_last_hour(now), settings.SIGNUP_BURST_ALERT),
        (INBOX_BURST, lambda: inboxes_created_last_hour(now), settings.INBOX_BURST_ALERT),
        (SENDER_CONCENTRATION, lambda: free_sender_domains_today(now), settings.SENDER_DOMAINS_ALERT),
    )


_TEXT = {
    SIGNUP_BURST: (
        "Signup burst",
        "{value:,} accounts were created in the last hour ({accounts:,} of them self-registered\n"
        "agents), over SIGNUP_BURST_ALERT={threshold:,}.\n\n"
        "What it means: a launch post landing, or scripted signups. Real launch traffic\n"
        "comes with a ?ref= source and verified emails; farming does not.",
    ),
    INBOX_BURST: (
        "Inbox burst on one account",
        "One account created {value:,} inboxes in the last hour ({accounts:,} account(s) over\n"
        "INBOX_BURST_ALERT={threshold:,}; deleted inboxes count). The plan cap still applies.\n\n"
        "What it means: a CI suite creating an inbox per run (normal on paid plans), or\n"
        "create/delete churn to get fresh addresses (farming).",
    ),
    SENDER_CONCENTRATION: (
        "Free account: mail from many sender domains",
        "A Free account received mail from {value:,} distinct sender domains today (UTC)\n"
        "({accounts:,} account(s) over SENDER_DOMAINS_ALERT={threshold:,}).\n\n"
        "What it means: one inbox used to verify accounts on many different sites, the\n"
        "pattern that gets a domain onto disposable-email blocklists.",
    ),
}


_SETTING = {
    SIGNUP_BURST: "SIGNUP_BURST_ALERT",
    INBOX_BURST: "INBOX_BURST_ALERT",
    SENDER_CONCENTRATION: "SENDER_DOMAINS_ALERT",
}


def message(kind, value):
    title, text = _TEXT[kind]
    threshold = getattr(settings, _SETTING[kind])
    body = text.format(value=int(value), accounts=getattr(value, "accounts", 0), threshold=threshold)
    return title, body + "\n\nAlert only: nothing was suspended. See operations notes."
