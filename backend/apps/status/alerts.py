"""tg-notify: component down / recovery alerts from the /status probes.

Called by ``status_probe`` right after it writes a round of ComponentCheck
rows. For each component the state is computed with the page's own rule
(:func:`apps.status.services.current_state`: two failed checks in a row ->
Down, one -> Degraded) and compared with the last ALERTED state in
:class:`ComponentAlertState`:

- Operational -> Down (2 consecutive failures): one "DOWN since HH:MM UTC".
- Down -> Operational (2 consecutive successes): one "recovered after N min".
- Degraded (a single failure, or the first success after an outage) changes
  nothing: no alert, no flapping.

Exactly-once across concurrent cron runs: the transition is claimed with a
conditional UPDATE ``WHERE state = <previous>``; only the run whose UPDATE
matched a row sends. If no channel delivered and one errored, the claim is
rolled back so the next run retries. Never raises.
"""
import logging

from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone

from apps.ops import notify as ops_notify

from . import services
from .models import ComponentAlertState, ComponentCheck

logger = logging.getLogger(__name__)

UP = ComponentAlertState.OPERATIONAL
DOWN = ComponentAlertState.DOWN


def status_url():
    return (getattr(settings, "PUBLIC_BASE_URL", "") or "https://mailsocket.app").rstrip("/") + "/status"


def _recent_ok(component, now):
    return list(
        ComponentCheck.objects.filter(component=component, checked_at__lte=now)
        .order_by("-checked_at", "-id")
        .values_list("ok", flat=True)[:2]
    )


def _down_since(component, now):
    """Time of the first failed check of the current failure streak."""
    last_ok = (
        ComponentCheck.objects.filter(component=component, ok=True, checked_at__lte=now)
        .order_by("-checked_at")
        .values_list("checked_at", flat=True)
        .first()
    )
    fails = ComponentCheck.objects.filter(component=component, ok=False, checked_at__lte=now)
    if last_ok is not None:
        fails = fails.filter(checked_at__gt=last_ok)
    return fails.order_by("checked_at").values_list("checked_at", flat=True).first() or now


def _claim(component, previous, new, *, down_since, now):
    """Flip previous -> new atomically; True only for the winning caller."""
    try:
        ComponentAlertState.objects.get_or_create(component=component)
    except IntegrityError:
        pass  # created concurrently; the row exists now
    won = ComponentAlertState.objects.filter(component=component, state=previous).update(
        state=new, down_since=down_since, changed_at=now
    )
    return won == 1


def _minutes(delta):
    return max(0, int(delta.total_seconds() // 60))


def check_component_transitions(now=None):
    """Alert on every component state transition. Returns the number of
    transitions alerted (claimed). Never raises."""
    alerted = 0
    try:
        now = now or timezone.now()
        labels = dict(ComponentCheck.Component.choices)
        for component in ComponentCheck.Component.values:
            try:
                state = services.current_state(_recent_ok(component, now))
                if state not in (services.OPERATIONAL, services.DOWN):
                    continue  # degraded / unknown: never alert, never flip
                row = ComponentAlertState.objects.filter(component=component).values(
                    "state", "down_since", "changed_at"
                ).first()
                last = row["state"] if row else UP
                name = labels[component]
                if state == services.DOWN and last == UP:
                    since = _down_since(component, now)
                    if not _claim(component, UP, DOWN, down_since=since, now=now):
                        continue
                    result = ops_notify.notify(
                        "down",
                        f"{name} is DOWN since {since:%H:%M} UTC",
                        f"The /status probe failed {name} twice in a row "
                        f"(first failure {since:%Y-%m-%d %H:%M} UTC).",
                        link=status_url(),
                    )
                    if ops_notify.failed(result):
                        ComponentAlertState.objects.filter(
                            component=component, state=DOWN, changed_at=now
                        ).update(state=UP, down_since=None, changed_at=row["changed_at"] if row else None)
                        continue
                    alerted += 1
                elif state == services.OPERATIONAL and last == DOWN:
                    since = row["down_since"]
                    if not _claim(component, DOWN, UP, down_since=None, now=now):
                        continue
                    minutes = _minutes(now - since) if since else 0
                    result = ops_notify.notify(
                        "ok",
                        f"{name} recovered after {minutes} min",
                        f"{name} is operational again (down since "
                        f"{since:%Y-%m-%d %H:%M} UTC)." if since else f"{name} is operational again.",
                        link=status_url(),
                    )
                    if ops_notify.failed(result):
                        ComponentAlertState.objects.filter(
                            component=component, state=UP, changed_at=now
                        ).update(state=DOWN, down_since=since, changed_at=row["changed_at"])
                        continue
                    alerted += 1
            except Exception:
                logger.exception("status_transition_alert_failed component=%s", component)
    except Exception:
        logger.exception("status_transition_alerts_failed")
    return alerted
