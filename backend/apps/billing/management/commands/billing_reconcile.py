"""manage.py billing_reconcile — BL-21 (see apps/billing/reconcile.py).

Exit 0 clean · 1 mismatches (ops alerted) · 2 error (nothing proven, no dead-man ping).
"""

from django.core.management.base import BaseCommand

from apps.billing.reconcile import INVOICE_DAYS_DEFAULT, run


class Command(BaseCommand):
    help = (
        "Read-only, alert-only reconciliation of Lemon Squeezy subscriptions vs local "
        "AccountPlan rows (status, variant, renews_at), LS prices vs the pricing page, and LS "
        "subscription invoices of the last --invoice-days days vs BillingPayment rows. "
        "Daily from the host cron (operations notes 'Billing reconciliation')."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--invoice-days",
            type=int,
            default=INVOICE_DAYS_DEFAULT,
            help=f"compare LS subscription invoices of the last N days (default {INVOICE_DAYS_DEFAULT})",
        )

    def handle(self, *_args, **options):
        code = run(write=self.stdout.write, write_err=self.stderr.write, invoice_days=options["invoice_days"])
        if code:
            raise SystemExit(code)
