from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from apps.accounts.models import AccountPlan
from apps.accounts.wait_alerts import purge_old_wait_capacity_daily
from apps.api.idempotency import purge_expired_idempotency_records
from apps.inboxes.retention import MAX_BATCH_SIZE, purge_expired_messages
from apps.status.services import purge_old_component_checks


class Command(BaseCommand):
    help = (
        "Hard-delete expired messages in finite batches. Run only as a standalone job; "
        "it is intentionally not part of inbound ingest."
    )

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=500)

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise CommandError(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}")
        # Keys off the live Plan enum (not a hard-coded {"free", "paid"} set) so
        # adding a new tier to BILLING_PLAN_LIMITS/RETENTION_DAYS (e.g. "scale")
        # never trips this guard as long as every plan has a window defined.
        expected_plans = set(AccountPlan.Plan.values)
        if set(settings.RETENTION_DAYS) != expected_plans or any(
            not isinstance(days, int) or days < 1 for days in settings.RETENTION_DAYS.values()
        ):
            raise CommandError(
                f"RETENTION_DAYS must define a positive integer window for every plan "
                f"({sorted(expected_plans)})"
            )

        try:
            result = purge_expired_messages(batch_size=batch_size)
            # Idempotency-Key records live 24h; purged on the same hourly
            # retention job rather than a new cron.
            idempotency = purge_expired_idempotency_records(batch_size=batch_size)
            # /status probe history keeps 90 days; same hourly job.
            status_checks = purge_old_component_checks(batch_size=batch_size)
            # walert: wait-capacity daily stats keep 400 days; same hourly job.
            wait_capacity_days = purge_old_wait_capacity_daily(batch_size=batch_size)
        except DatabaseError as exc:
            raise CommandError(f"Message purge database failure: {type(exc).__name__}") from exc

        self.stdout.write(
            "purge_expired_messages "
            f"messages={result.messages} deliveries={result.deliveries} batches={result.batches} "
            f"idempotency_records={idempotency} status_checks={status_checks} "
            f"wait_capacity_days={wait_capacity_days}"
        )
