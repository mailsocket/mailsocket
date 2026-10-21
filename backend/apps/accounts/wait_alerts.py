"Public API."
import logging
from datetime import timedelta

from django.db import IntegrityError
from django.db.models import Max, Q
from django.utils import timezone

from apps.ops import notify as ops_notify

from .models import WaitCapacity, WaitCapacityAlert, WaitCapacityDaily

logger = logging.getLogger(__name__)

THROTTLE = timedelta(hours=1)
WARNING = "warning"
SATURATED = "saturated"
LEVELS = (SATURATED, WARNING)


def pool_limits():
    """{pool key: limit}. Read through the services module so a monkeypatched
    limit (tests) and the acquire path always agree."""
    from . import services

    return {
        services.WAIT_CAPACITY_KEY: services.WAIT_CAPACITY_LIMIT,
        services.WAIT_CAPACITY_FREE_KEY: services.WAIT_CAPACITY_FREE_LIMIT,
    }


def current_in_flight(pool):
    """Live in-flight count for a pool, treating an expired (self-healing)
    counter row as 0 — mirrors apps.status.probes.probe_wait. Read-only."""
    from .services import WAIT_LEASE_GRACE_SECONDS

    row = WaitCapacity.objects.filter(key=pool).values("count", "expires_at").first()
    if row is None:
        return 0
    cutoff = timezone.now() - timedelta(seconds=WAIT_LEASE_GRACE_SECONDS)
    if row["expires_at"] is not None and row["expires_at"] <= cutoff:
        return 0
    return row["count"]


def _claim(pool, level, now):
    """Return the previous last_alert_at sentinel (or True for "was NULL") when
    THIS caller won the hourly slot, else None."""
    try:
        WaitCapacityAlert.objects.get_or_create(pool=pool, level=level)
    except IntegrityError:
        pass  # created concurrently by another sender; the row now exists
    row = WaitCapacityAlert.objects.filter(pool=pool, level=level).values("last_alert_at").first()
    if row is None:
        return None
    previous = row["last_alert_at"]
    won = (
        WaitCapacityAlert.objects.filter(pool=pool, level=level)
        .filter(Q(last_alert_at__isnull=True) | Q(last_alert_at__lt=now - THROTTLE))
        .update(last_alert_at=now)
    )
    if won != 1:
        return None
    return previous or True


def _unclaim(pool, level, now, previous):
    """Undo a claim after a failed send (only if nobody re-claimed since)."""
    WaitCapacityAlert.objects.filter(pool=pool, level=level, last_alert_at=now).update(
        last_alert_at=None if previous is True else previous
    )


def _body(pool, level, *, current, limit, peak, refusals):
    from .services import WAIT_CAPACITY_KEY

    pool_label = "global" if pool == WAIT_CAPACITY_KEY else "free/agent/demo sub-pool"
    if level == SATURATED:
        headline = f"The {pool_label} wait pool is FULL: wait requests are being refused."
    else:
        headline = f"The {pool_label} wait pool is at >= 80% of its limit."
    return "\n".join(
        [
            headline,
            "",
            f"Pool: {pool}",
            f"Level: {level.upper()}",
            f"In flight now: {current} / {limit}",
            f"Today's peak (UTC): {peak} / {limit}",
            f"Today's refusals (UTC): {refusals}",
            "",
            "What it means: over the cap, callers get 429 wait_capacity + Retry-After 2s;",
            "the SDKs retry, so customers see delay, not failure.",
            "",
            "Action: consider a bigger server. See operations notes 'Long-poll wait endpoint",
            "and gunicorn workers'. Limits are env-driven: WAIT_CAPACITY_LIMIT,",
            "WAIT_CAPACITY_FREE_LIMIT, GUNICORN_THREADS (manage.py check enforces safe",
            "combinations). Owner view: /app/fleet/ (Wait capacity panel).",
            "",
            "At most one alert per pool and level per hour.",
        ]
    )


def send_due_wait_capacity_alerts(now=None):
    """Send every due (pool, level) alert. Returns the number of alerts
    delivered (by at least one channel). No-op when neither OPS_ALERT_EMAIL
    nor Telegram is configured. Never raises."""
    sent = 0
    try:
        if not ops_notify.any_channel_enabled():
            return 0

        now = now or timezone.now()
        # Today plus yesterday so an event just before UTC midnight still
        # alerts on the first run after it.
        since = now.date() - timedelta(days=1)
        for pool, limit in pool_limits().items():
            rows = WaitCapacityDaily.objects.filter(pool=pool, date__gte=since)
            events = rows.aggregate(warned=Max("warned_at"), saturated=Max("saturated_at"))
            today = rows.filter(date=now.date()).values("peak", "refusals").first() or {
                "peak": 0,
                "refusals": 0,
            }
            for level in LEVELS:
                event_at = events["saturated" if level == SATURATED else "warned"]
                if event_at is None:
                    continue
                try:
                    last = (
                        WaitCapacityAlert.objects.filter(pool=pool, level=level)
                        .values_list("last_alert_at", flat=True)
                        .first()
                    )
                    if last is not None and event_at <= last:
                        continue  # already alerted about this (or a later) event
                    previous = _claim(pool, level, now)
                    if previous is None:
                        continue  # throttled, or another sender won
                    try:
                        result = ops_notify.notify(
                            "down" if level == SATURATED else "warn",
                            f"Wait capacity {level.upper()}: {pool} pool",
                            _body(
                                pool,
                                level,
                                current=current_in_flight(pool),
                                limit=limit,
                                peak=today["peak"],
                                refusals=today["refusals"],
                            ),
                        )
                    except Exception:
                        _unclaim(pool, level, now, previous)
                        raise
                    if ops_notify.failed(result):
                        _unclaim(pool, level, now, previous)
                        continue  # retried on the next cron run
                    sent += 1
                except Exception:
                    logger.exception("wait_capacity_alert_failed pool=%s level=%s", pool, level)
    except Exception:
        logger.exception("wait_capacity_alerts_failed")
    return sent


def recent_days(days=14, now=None):
    """Last ``days`` UTC days, newest first: (date, global_peak, free_peak,
    global_refusals, free_refusals). Days without rows show zeros."""
    from .services import WAIT_CAPACITY_FREE_KEY, WAIT_CAPACITY_KEY

    now = now or timezone.now()
    start = now.date() - timedelta(days=days - 1)
    by_key = {
        (r["date"], r["pool"]): r
        for r in WaitCapacityDaily.objects.filter(date__gte=start).values(
            "date", "pool", "peak", "refusals"
        )
    }
    empty = {"peak": 0, "refusals": 0}
    out = []
    for i in range(days):
        day = now.date() - timedelta(days=i)
        g = by_key.get((day, WAIT_CAPACITY_KEY), empty)
        f = by_key.get((day, WAIT_CAPACITY_FREE_KEY), empty)
        out.append(
            {
                "date": day,
                "global_peak": g["peak"],
                "free_peak": f["peak"],
                "global_refusals": g["refusals"],
                "free_refusals": f["refusals"],
            }
        )
    return out


RETENTION_DAYS = 400


def purge_old_wait_capacity_daily(*, batch_size=500, now=None):
    """Delete WaitCapacityDaily rows older than RETENTION_DAYS in finite batches."""
    cutoff = (now or timezone.now()).date() - timedelta(days=RETENTION_DAYS)
    total = 0
    while True:
        ids = list(
            WaitCapacityDaily.objects.filter(date__lt=cutoff)
            .order_by("date")
            .values_list("pk", flat=True)[:batch_size]
        )
        if not ids:
            return total
        deleted, _ = WaitCapacityDaily.objects.filter(pk__in=ids).delete()
        total += deleted
