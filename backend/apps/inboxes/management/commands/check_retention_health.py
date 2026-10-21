from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from apps.accounts.models import AccountPlan
from apps.inboxes.retention import check_retention_health
from apps.ops.notify import notify


def _format_hours(age):
    if age is None:
        return "n/a"
    return f"{age.total_seconds() / 3600:.1f}h"


class Command(BaseCommand):
    help = (
        "Read-only liveness check: exit non-zero if any message is overdue for "
        "retention purge beyond a grace margin, and email an ops address."
    )

    def add_arguments(self, parser):
        parser.add_argument("--grace-hours", type=float, default=2.0)

    def handle(self, *args, **options):
        grace_hours = options["grace_hours"]
        if grace_hours < 0:
            raise CommandError("--grace-hours must be >= 0")
        # Keys off the live Plan enum, same guard shape as purge_expired_messages,
        # so a newly-added tier can never crash this command either.
        expected_plans = set(AccountPlan.Plan.values)
        if set(settings.RETENTION_DAYS) != expected_plans or any(
            not isinstance(days, int) or days < 1
            for days in settings.RETENTION_DAYS.values()
        ):
            raise CommandError(
                "RETENTION_DAYS must define a positive integer window for every plan "
                f"({sorted(expected_plans)})"
            )

        try:
            health = check_retention_health(grace_hours=grace_hours)
        except DatabaseError as exc:
            raise CommandError(
                f"Retention health check database failure: {type(exc).__name__}"
            ) from exc

        if health.ok:
            self.stdout.write(
                "retention OK "
                f"oldest_free={_format_hours(health.oldest_free)} "
                f"oldest_paid={_format_hours(health.oldest_paid)} "
                f"oldest_scale={_format_hours(health.oldest_scale)}"
            )
            return

        summary = (
            "retention purge overdue "
            f"overdue_free={health.overdue_free} "
            f"overdue_paid={health.overdue_paid} "
            f"overdue_scale={health.overdue_scale} "
            f"oldest_free={_format_hours(health.oldest_free)} "
            f"oldest_paid={_format_hours(health.oldest_paid)} "
            f"oldest_scale={_format_hours(health.oldest_scale)}"
        )
        self.stdout.write(summary)

        # tg-notify: email + Telegram via the one notifier (never raises).
        # The summary holds counts/ages only — no message contents.
        result = notify("warn", "retention purge overdue", summary)
        if result["email"] == "disabled":
            self.stdout.write("alert email skipped (OPS_ALERT_EMAIL unset)")
        self.stdout.write(f"alert email={result['email']} telegram={result['telegram']}")

        raise CommandError("retention purge overdue")
