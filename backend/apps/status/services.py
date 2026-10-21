"""Status page data: current state per component, daily uptime, retention.

Everything here is computed from ComponentCheck rows; nothing is invented.
A day with no checks is shown as "no data", never as up.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import ComponentCheck, Incident

RETENTION_DAYS = 90
HISTORY_DAYS = 90
# The probe runs every 5 minutes. If the newest check is older than this the
# scheduler (or the probe itself) has stopped, so we say "no recent data"
# instead of repeating a stale "Operational".
STALE_AFTER = timedelta(minutes=15)

OPERATIONAL = "operational"
DEGRADED = "degraded"
DOWN = "down"
UNKNOWN = "unknown"

STATE_LABELS = {
    OPERATIONAL: "Operational",
    DEGRADED: "Degraded",
    DOWN: "Down",
    UNKNOWN: "No recent data",
}


def current_state(recent_ok: list[bool]) -> str:
    """State from the newest (up to) two checks, newest first.

    Two failures in a row -> Down; any single failure -> Degraded (one blip
    is not an outage); otherwise Operational. No checks -> Unknown.
    """
    if not recent_ok:
        return UNKNOWN
    last_two = recent_ok[:2]
    if len(last_two) == 2 and not any(last_two):
        return DOWN
    if not all(last_two):
        return DEGRADED
    return OPERATIONAL


def uptime_percent(ok: int, total: int):
    """ok/total as a percentage, or None when there were no checks."""
    if total <= 0:
        return None
    return ok * 100.0 / total


def format_percent(value) -> str:
    if value is None:
        return "No data"
    if value >= 100:
        return "100%"
    # Floor, never round up: 99.996% must not display as 100%.
    floored = int(value * 100) / 100
    return f"{floored:.2f}%"


def bar_level(value) -> str:
    if value is None:
        return "none"
    if value >= 100:
        return "up"
    if value >= 95:
        return "partial"
    return "down"


@dataclass
class Day:
    day: date
    ok: int
    total: int

    @property
    def percent(self):
        return uptime_percent(self.ok, self.total)

    @property
    def level(self):
        return bar_level(self.percent)

    @property
    def label(self):
        if not self.total:
            return f"{self.day.isoformat()}: no data"
        return (
            f"{self.day.isoformat()}: {format_percent(self.percent)} "
            f"({self.ok}/{self.total} checks ok)"
        )


@dataclass
class ComponentStatus:
    key: str
    name: str
    state: str
    last_checked_at: object = None
    last_latency_ms: object = None
    days: list = field(default_factory=list)

    @property
    def state_label(self):
        return STATE_LABELS[self.state]

    @property
    def ok(self):
        return sum(d.ok for d in self.days)

    @property
    def total(self):
        return sum(d.total for d in self.days)

    @property
    def uptime(self):
        return uptime_percent(self.ok, self.total)

    @property
    def uptime_display(self):
        return format_percent(self.uptime)

    @property
    def span_label(self):
        """Axis label for the oldest bar (the newest is always today)."""
        ago = len(self.days) - 1
        if ago <= 0:
            return "Today"
        return "1 day ago" if ago == 1 else f"{ago} days ago"


def _history_start(now) -> date:
    return now.date() - timedelta(days=HISTORY_DAYS - 1)


def build_components(now=None) -> list[ComponentStatus]:
    now = now or timezone.now()
    today = now.date()
    start = _history_start(now)
    rows = (
        ComponentCheck.objects.filter(checked_at__date__gte=start, checked_at__lte=now)
        .annotate(day=TruncDate("checked_at"))
        .values("component", "day")
        .annotate(total=Count("id"), ok=Count("id", filter=Q(ok=True)))
    )
    per_day: dict[str, dict[date, tuple[int, int]]] = {}
    first_day = None
    for row in rows:
        per_day.setdefault(row["component"], {})[row["day"]] = (row["ok"], row["total"])
        if first_day is None or row["day"] < first_day:
            first_day = row["day"]

    # Only the days we actually have: from the first recorded check (across
    # all components, so every row lines up) to today.
    span = []
    if first_day is not None:
        span = [first_day + timedelta(days=i) for i in range((today - first_day).days + 1)]

    components = []
    for key, name in ComponentCheck.Component.choices:
        recent = list(
            ComponentCheck.objects.filter(component=key, checked_at__lte=now)
            .order_by("-checked_at", "-id")
            .values("ok", "checked_at", "latency_ms")[:2]
        )
        if recent and now - recent[0]["checked_at"] > STALE_AFTER:
            state = UNKNOWN
        else:
            state = current_state([r["ok"] for r in recent])
        days = []
        counts = per_day.get(key, {})
        for d in span:
            ok, total = counts.get(d, (0, 0))
            days.append(Day(day=d, ok=ok, total=total))
        components.append(
            ComponentStatus(
                key=key,
                name=name,
                state=state,
                last_checked_at=recent[0]["checked_at"] if recent else None,
                last_latency_ms=recent[0]["latency_ms"] if recent else None,
                days=days,
            )
        )
    return components


def overall_state(components) -> str:
    states = {c.state for c in components}
    if not components or states == {UNKNOWN}:
        return UNKNOWN
    if DOWN in states:
        return DOWN
    if DEGRADED in states or UNKNOWN in states:
        return DEGRADED
    return OPERATIONAL


OVERALL_LABELS = {
    OPERATIONAL: "All systems operational",
    DEGRADED: "Some systems are degraded",
    DOWN: "Partial outage",
    UNKNOWN: "No recent checks",
}


def affected_names(components) -> list[str]:
    """Names of components not currently Operational, for the banner."""
    return [c.name for c in components if c.state != OPERATIONAL]


def recent_incidents(now=None):
    """Unresolved incidents plus anything started within the history window."""
    now = now or timezone.now()
    cutoff = now - timedelta(days=HISTORY_DAYS)
    return list(
        Incident.objects.filter(Q(resolved_at__isnull=True) | Q(started_at__gte=cutoff))
        .order_by("-started_at", "-id")
    )


def purge_old_component_checks(*, batch_size=500, now=None) -> int:
    """Delete probe rows older than RETENTION_DAYS in finite batches."""
    cutoff = (now or timezone.now()) - timedelta(days=RETENTION_DAYS)
    total = 0
    while True:
        ids = list(
            ComponentCheck.objects.filter(checked_at__lt=cutoff)
            .order_by("checked_at")
            .values_list("pk", flat=True)[:batch_size]
        )
        if not ids:
            return total
        deleted, _ = ComponentCheck.objects.filter(pk__in=ids).delete()
        total += deleted
