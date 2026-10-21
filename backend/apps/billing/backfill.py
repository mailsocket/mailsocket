"Public API."

from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction

from . import reconcile
from . import views as bviews
from .models import BillingPayment

EXIT_OK, EXIT_ERROR = 0, 2
NO_HOLDER = "no AccountPlan holds this subscription"
MALFORMED = "malformed total/refunded_amount/created_at"


@dataclass(frozen=True)
class Candidate:
    invoice_id: str
    user_pk: int
    row: dict

    def line(self):
        row = self.row
        refund = f" refunded={row['refunded_amount_cents']}" if row["refunded_amount_cents"] else ""
        return (
            f"invoice={self.invoice_id} date={row['paid_at'].isoformat()} amount={row['amount_cents']}"
            f" {row['currency']} status={row['status']}{refund} sub={row['ls_subscription_id'] or '-'}"
            f" user_pk={self.user_pk}"
        )


def _row_for(invoice_id, attrs, user_pk):
    """BillingPayment column values (the webhook's own mapping + LS refund state)."""
    row = bviews._invoice_row_defaults(user_pk, attrs)
    _total, _currency, refunded, fully = reconcile._ls_money(invoice_id, attrs)
    row["refunded_amount_cents"] = refunded
    row["refunded"] = fully
    row["refunded_at"] = bviews._refund_time(attrs) if refunded else None
    row["status"] = attrs.get("status")
    return row


def plan(invoices, warn):
    """Candidates (invoices to create, sorted by id) for LS ``invoices``; warns on skips."""
    existing = set(
        BillingPayment.objects.filter(ls_invoice_id__in=list(invoices)).values_list(
            "ls_invoice_id", flat=True
        )
    )
    holders = {sub: p.user_id for sub, p in reconcile._local_plans().items()}
    out = []
    for invoice_id in sorted(invoices):
        attrs = invoices[invoice_id]
        if invoice_id in existing or attrs.get("status") not in reconcile.INVOICE_MONEY_STATUSES:
            continue
        sub_id = str(attrs.get("subscription_id") or "")
        user_pk = holders.get(sub_id)
        if user_pk is None:
            warn(f"WARNING skipped invoice={invoice_id} sub={sub_id or '-'}: {NO_HOLDER}")
            continue
        try:
            row = _row_for(invoice_id, attrs, user_pk)
        except (ValueError, reconcile.ReconcileError):
            warn(f"WARNING skipped invoice={invoice_id} sub={sub_id}: {MALFORMED}")
            continue
        out.append(Candidate(invoice_id, user_pk, row))
    return out


def _already_recorded(invoice_id):
    """True when a BillingPayment for this LS invoice exists (checked after the
    savepoint rolled back, i.e. committed by someone else — the webhook)."""
    return BillingPayment.objects.filter(ls_invoice_id=invoice_id).exists()


def create_missing(candidates):
    """Create each candidate row unless it exists by now. Returns the number created."""
    created = 0
    for cand in candidates:
        fields = {k: v for k, v in cand.row.items() if k != "status"}
        try:
            with transaction.atomic():
                if BillingPayment.objects.filter(ls_invoice_id=cand.invoice_id).exists():
                    continue
                BillingPayment.objects.create(ls_invoice_id=cand.invoice_id, **fields)
        except IntegrityError:
            # Only the unique ls_invoice_id race (a webhook created the row
            # meanwhile) is benign: skip it, never overwrite. Any other
            # constraint failure would leave the payment missing -> fail loudly
            
            if _already_recorded(cand.invoice_id):
                continue
            raise
        created += 1
    return created


def run(*, write, write_err, apply=False, expect_count=None):
    """One backfill run; returns the exit code (0 done, 2 error)."""
    if not settings.ENABLE_BILLING or not settings.LS_API_KEY:
        write_err("billing_backfill_payments ERROR: ENABLE_BILLING off or LS_API_KEY not set — nothing read")
        return EXIT_ERROR
    try:
        invoices = reconcile.fetch_ls_invoices()
    except reconcile.ReconcileError as exc:
        write_err(f"billing_backfill_payments ERROR: {exc} — nothing written")
        return EXIT_ERROR
    candidates = plan(invoices, write_err)
    mode = "APPLY" if apply else "DRY-RUN"
    write(f"billing_backfill_payments {mode} ls_invoices={len(invoices)} missing={len(candidates)}")
    for cand in candidates:
        write(cand.line())
    if not apply:
        write("dry-run: nothing written (re-run with --apply --expect-count N to create these rows)")
        return EXIT_OK
    if expect_count is not None and expect_count != len(candidates):
        write_err(
            f"billing_backfill_payments ERROR: --expect-count {expect_count} but {len(candidates)}"
            " rows are missing — nothing written"
        )
        return EXIT_ERROR
    write(f"created={create_missing(candidates)}")
    return EXIT_OK
