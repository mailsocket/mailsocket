"Public API."

from django.core.management.base import BaseCommand

from apps.billing.backfill import run


class Command(BaseCommand):
    help = (
        "List Lemon Squeezy subscription invoices with no BillingPayment row (dry-run), or "
        "create exactly those rows with --apply. Never updates existing rows, never sends mail, "
        "never changes a plan (operations notes 'Payment backfill')."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="create the missing rows (default: dry-run)")
        parser.add_argument(
            "--expect-count",
            type=int,
            default=None,
            help="with --apply: refuse unless exactly N rows are missing (the dry-run count)",
        )

    def handle(self, *_args, **options):
        code = run(
            write=self.stdout.write,
            write_err=self.stderr.write,
            apply=options["apply"],
            expect_count=options["expect_count"],
        )
        if code:
            raise SystemExit(code)
