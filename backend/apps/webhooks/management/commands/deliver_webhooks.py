from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from apps.webhooks.engine import MAX_BATCH_SIZE, drain


class Command(BaseCommand):
    help = "Deliver one finite leased batch of due webhooks."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one finite pass (default).")
        parser.add_argument("--batch-size", type=int, default=50)

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise CommandError(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}")
        try:
            result = drain(batch_size=batch_size)
        except DatabaseError as exc:
            raise CommandError(f"Webhook delivery database failure: {type(exc).__name__}") from exc
        self.stdout.write(
            "deliver_webhooks "
            f"claimed={result.claimed} succeeded={result.succeeded} "
            f"retrying={result.retrying} exhausted={result.exhausted} "
            f"cancelled={result.cancelled} fenced={result.fenced}"
        )
