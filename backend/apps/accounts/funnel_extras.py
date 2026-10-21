"Public API."

from collections import defaultdict
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from statistics import median

from .funnel_signals import ACTIVE_DAY_WINDOW_DAYS
from .models import AccountActiveDay, AccountIntent, AccountWeeklyActivity, DemoStartDaily

NO_REF = "(none)"


class Signals:
    """Per-user extras for one report run (loaded once, three queries + one)."""

    def __init__(self, user_ids, external_messages):
        self.days: dict[int, set] = defaultdict(set)
        for user_id, day in AccountActiveDay.objects.filter(user_id__in=user_ids).values_list(
            "user_id", "day"
        ):
            self.days[user_id].add(day)
        self.intent = {
            row[0]: row[1:]
            for row in AccountIntent.objects.filter(user_id__in=user_ids).values_list(
                "user_id", "viewed_pricing_at", "started_checkout_at"
            )
        }
        self.first_mail: dict[int, list] = defaultdict(list)
        for user_id, stamp in AccountWeeklyActivity.objects.filter(user_id__in=user_ids).values_list(
            "user_id", "first_message_at"
        ):
            self.first_mail[user_id].append(stamp)
        self.retained_messages: dict[int, list] = defaultdict(list)
        for user_id, stamp in external_messages.filter(inbox__user_id__in=user_ids).values_list(
            "inbox__user_id", "received_at"
        ):
            self.retained_messages[user_id].append(stamp)
            self.first_mail[user_id].append(stamp)

    def active_days(self, user_id, created_at) -> set:
        end = created_at + timedelta(days=ACTIVE_DAY_WINDOW_DAYS)
        days = set(self.days.get(user_id, ()))
        for stamp in self.retained_messages.get(user_id, ()):
            if created_at <= stamp < end:
                days.add(stamp.astimezone(dt_timezone.utc).date())
        return days

    def hours_to_first_mail(self, user_id, created_at):
        stamps = [stamp for stamp in self.first_mail.get(user_id, ()) if stamp >= created_at]
        if not stamps:
            return None
        return (min(stamps) - created_at).total_seconds() / 3600


def add_extras(row, account, activated, signals: Signals) -> float | None:
    """Fill the extra SourceRow counters for one account; returns its hours
    to first external mail (None when it has none) for the median."""
    user_id, created_at = account[0], account[1]
    row.retained += activated and len(signals.active_days(user_id, created_at)) >= 2
    viewed, checkout = signals.intent.get(user_id, (None, None))
    row.pricing_viewed += viewed is not None
    row.checkout_started += checkout is not None
    return signals.hours_to_first_mail(user_id, created_at)


def time_to_first_mail(hours) -> dict:
    values = [value for value in hours if value is not None]
    return {
        "accounts": len(values),
        "median_hours": round(median(values), 1) if values else None,
    }


def demo_started(start: datetime, end: datetime) -> dict:
    """Demo inboxes created per UTC day and per ref in [start, end)."""
    rows = DemoStartDaily.objects.filter(day__gte=start.date(), day__lt=end.date()).values_list(
        "day", "ref", "count"
    )
    by_day: dict = defaultdict(int)
    by_ref: dict = defaultdict(int)
    for day, ref, count in rows:
        by_day[day.isoformat()] += count
        by_ref[ref or NO_REF] += count
    return {
        "total": sum(by_day.values()),
        "by_day": [{"day": day, "count": by_day[day]} for day in sorted(by_day)],
        "by_ref": dict(sorted(by_ref.items(), key=lambda item: (-item[1], item[0]))),
    }


def render_extras(report) -> list[str]:
    demo = report.demo_started
    refs = ", ".join(f"{ref} {count}" for ref, count in demo["by_ref"].items()) or "-"
    ttfm = report.time_to_first_mail
    median_text = "n/a" if ttfm["median_hours"] is None else f"{ttfm['median_hours']} h"
    return [
        f"retained = activated + external mail on >= 2 distinct UTC days within "
        f"{ACTIVE_DAY_WINDOW_DAYS} days of signup",
        f"demo_started (zero-signup /docs/try) {demo['total']}; by ref: {refs}",
        f"median signup -> first external mail: {median_text} (over {ttfm['accounts']} accounts)",
    ]
