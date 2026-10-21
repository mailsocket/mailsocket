"Public API."
import logging
from datetime import datetime, time, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.db import IntegrityError
from django.db.models import Q
from django.utils import timezone

from apps.ops import notify as ops_notify

from . import abuse_launch
from .models import AbuseAlert, AccountPlan, RateLimitBucket

logger = logging.getLogger(__name__)

AGENT_REGISTER_GLOBAL = "agent_register_global"
FREE_DAILY_MESSAGES = "free_daily_messages"
KINDS = (AGENT_REGISTER_GLOBAL, FREE_DAILY_MESSAGES, *abuse_launch.KINDS)
LOOKBACK = timedelta(hours=1)


def _utc_day_start(now):
    return datetime.combine(now.astimezone(dt_timezone.utc).date(), time.min, tzinfo=dt_timezone.utc)


def agent_register_global_hits(now):
    """Refused global agent-register requests in a window that is live or
    ended within the last hour, else 0. One primary-key lookup."""
    from . import services

    bucket = (
        RateLimitBucket.objects.filter(
            bucket_hash=services._bucket_hash(services.AGENT_REGISTER_GLOBAL_RATE_NAME, "global"),
            expires_at__gt=now - LOOKBACK,
        )
        .values("count")
        .first()
    )
    if bucket is None:
        return 0
    return max(0, bucket["count"] - services.AGENT_REGISTER_GLOBAL_RATE_LIMIT)


def free_messages_today(now):
    "Public API."
    from apps.inboxes.models import Message

    return Message.objects.filter(
        received_at__gte=_utc_day_start(now),
        inbox__user__account_plan__plan=AccountPlan.Plan.FREE,
    ).count()


def _already_sent_today(kind, now):
    last = AbuseAlert.objects.filter(kind=kind).values_list("last_alert_at", flat=True).first()
    return last is not None and last >= _utc_day_start(now)


def _claim(kind, now):
    """Return the previous last_alert_at (or True for "was NULL") when THIS
    caller won today's slot, else None."""
    try:
        AbuseAlert.objects.get_or_create(kind=kind)
    except IntegrityError:
        pass  # created concurrently; the row exists now
    row = AbuseAlert.objects.filter(kind=kind).values("last_alert_at").first()
    if row is None:
        return None
    previous = row["last_alert_at"]
    won = (
        AbuseAlert.objects.filter(kind=kind)
        .filter(Q(last_alert_at__isnull=True) | Q(last_alert_at__lt=_utc_day_start(now)))
        .update(last_alert_at=now)
    )
    if won != 1:
        return None
    return previous or True


def _unclaim(kind, now, previous):
    AbuseAlert.objects.filter(kind=kind, last_alert_at=now).update(
        last_alert_at=None if previous is True else previous
    )


def _message(kind, value):
    from . import services

    if kind in abuse_launch.KINDS:
        return abuse_launch.message(kind, value)
    if kind == AGENT_REGISTER_GLOBAL:
        title = "Agent register: global hourly cap hit"
        lines = [
            "POST /api/v1/agents/register hit its GLOBAL hourly cap "
            f"({services.AGENT_REGISTER_GLOBAL_RATE_LIMIT} new agent accounts / hour).",
            f"Refused in the current or last window: {value}.",
            "",
            "What it means: someone may be farming Free accounts (each agent account",
            "is a Free account with its own inbox and message quota). New agents get",
            "429 until the window resets; existing accounts are unaffected.",
        ]
    else:
        title = "Free plan: daily message volume high"
        lines = [
            f"Free-plan accounts received {value:,} messages today (UTC), over the",
            f"FREE_DAILY_MESSAGE_ALERT threshold of {settings.FREE_DAILY_MESSAGE_ALERT:,}.",
            "",
            "What it means: unusual Free traffic (possible account farming or a",
            "mail flood). Free retention is 1 day, so disk use clears itself.",
        ]
    lines += [
        "",
        "Action: check /app/fleet/ and recent agent-register signups. Thresholds:",
        "AGENT_REGISTER_GLOBAL_RATE_LIMIT (apps/accounts/services.py),",
        "FREE_DAILY_MESSAGE_ALERT (env).",
        "",
        "At most one alert of this kind per UTC day.",
    ]
    return title, "\n".join(lines)


def send_due_abuse_alerts(now=None):
    """Check both signals and send what is due. Returns the number of alerts
    delivered by at least one channel. No-op (no queries) when no alert
    channel is configured. Never raises."""
    sent = 0
    try:
        if not ops_notify.any_channel_enabled():
            return 0
        now = now or timezone.now()
        checks = (
            (AGENT_REGISTER_GLOBAL, lambda: agent_register_global_hits(now), 0),
            (FREE_DAILY_MESSAGES, lambda: free_messages_today(now), settings.FREE_DAILY_MESSAGE_ALERT),
            *abuse_launch.checks(now),
        )
        for kind, measure, threshold in checks:
            try:
                if _already_sent_today(kind, now):
                    continue  # skip the measurement query too
                value = measure()
                if value <= threshold:
                    continue
                previous = _claim(kind, now)
                if previous is None:
                    continue  # another sender won today's slot
                title, body = _message(kind, value)
                try:
                    result = ops_notify.notify("warn", title, body)
                except Exception:
                    _unclaim(kind, now, previous)
                    raise
                if ops_notify.failed(result):
                    _unclaim(kind, now, previous)
                    continue
                sent += 1
            except Exception:
                logger.exception("abuse_alert_failed kind=%s", kind)
    except Exception:
        logger.exception("abuse_alerts_failed")
    return sent
