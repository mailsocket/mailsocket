from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from django.utils import timezone

from apps.accounts.abuse_alerts import send_due_abuse_alerts
from apps.accounts.wait_alerts import send_due_wait_capacity_alerts
from apps.billing.kill_switch import billing_changes_enabled
from apps.feedback.alerts import send_due_feedback_digest
from apps.status.alerts import check_component_transitions
from apps.status.models import ComponentCheck
from apps.status.probes import run_probe


class Command(BaseCommand):
    help = (
        "Probe each public component once and record the result for /status. "
        "Run every 5 minutes from the host cron (see operations notes)."
    )

    def handle(self, *args, **options):
        checked_at = timezone.now()
        results = []
        for component in ComponentCheck.Component.values:
            ok, latency_ms = run_probe(component)
            results.append(
                ComponentCheck(
                    component=component, ok=ok, latency_ms=latency_ms, checked_at=checked_at
                )
            )
        try:
            ComponentCheck.objects.bulk_create(results)
        except DatabaseError as exc:
            # Nothing to record into: the web probe would have said so too.
            raise CommandError(f"status_probe database failure: {type(exc).__name__}") from exc
        summary = " ".join(
            f"{r.component}={'ok' if r.ok else 'FAIL'}({r.latency_ms}ms)" for r in results
        )
        # tg-notify: component down / recovery alerts on state transitions
        # (email + Telegram, once per transition; never raises).
        status_alerts = check_component_transitions(now=checked_at)
        # walert: the wait path only records saturation; this 5-minute cron is
        # where due wait-capacity alerts go out (never raises).
        wait_alerts = send_due_wait_capacity_alerts()
        
        # cap hit / Free daily message volume), once per kind per UTC day.
        abuse_alerts = send_due_abuse_alerts()
        # Report a problem: ONE digest when the feedback alert breaker held
        # reports back (cap 10/hour), at most one per hour (never raises).
        feedback_digest = send_due_feedback_digest()
        self.stdout.write(
            f"status_probe {summary} status_alerts={status_alerts} wait_alerts={wait_alerts}"
            f" abuse_alerts={abuse_alerts} feedback_digest={feedback_digest}"
        )
        # Billing kill switch: one extra line in the 5-minute cron log while
        # checkout / plan changes are paused (no alert; /app/fleet shows it).
        if not billing_changes_enabled():
            self.stdout.write("status_probe billing_changes=PAUSED (BILLING_CHANGES_ENABLED is off)")
