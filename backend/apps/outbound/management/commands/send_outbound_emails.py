from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from apps.outbound.engine import MAX_BATCH_SIZE, drain


class Command(BaseCommand):
    help = "Send one finite leased batch of due OutboundEmail rows (design §4)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one finite pass (default).")
        parser.add_argument("--batch-size", type=int, default=20)

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise CommandError(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}")
        try:
            result = drain(batch_size=batch_size)
        except DatabaseError as exc:
            raise CommandError(f"Outbound send database failure: {type(exc).__name__}") from exc
        self.stdout.write(
            "send_outbound_emails "
            f"claimed={result.claimed} sent={result.sent} "
            f"retrying={result.retrying} failed={result.failed} "
            f"rejected={result.rejected} fenced={result.fenced} "
            f"global_cap_exhausted={result.global_cap_exhausted}"
        )
