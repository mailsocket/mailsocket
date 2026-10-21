from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from apps.ingest.reconciler import MAX_BATCH_SIZE, reconcile


class Command(BaseCommand):
    help = (
        "Reconcile due or stale inbound messages in one finite run. Schedule every minute: "
        "docker compose exec -T web python manage.py reconcile_ingest --once"
    )

    def add_arguments(self, parser):
        parser.add_argument("--stale-seconds", type=int, default=300)
        parser.add_argument("--batch-size", type=int, default=100)
        parser.add_argument(
            "--once",
            action="store_true",
            help="Compatibility flag; every invocation is already a single finite run.",
        )

    def handle(self, *args, **options):
        stale_seconds = options["stale_seconds"]
        batch_size = options["batch_size"]
        if stale_seconds < 0:
            raise CommandError("--stale-seconds must be nonnegative")
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise CommandError(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}")

        try:
            result = reconcile(stale_seconds=stale_seconds, batch_size=batch_size)
        except DatabaseError as exc:
            raise CommandError(f"Ingest reconciliation database failure: {type(exc).__name__}") from exc

        self.stdout.write(
            "reconcile_ingest "
            f"claimed={result.claimed} parsed={result.parsed} "
            f"retryable={result.retryable} failed={result.failed}"
        )
