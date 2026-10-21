from django.conf import settings
from django.db import models
from django.db.models.functions import Now
from django.utils import timezone


class BillingEvent(models.Model):
    "Public API."

    id = models.BigAutoField(primary_key=True)
    event_key = models.CharField(max_length=80, unique=True)
    event_name = models.CharField(max_length=64)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="billing_events",
    )
    created_at = models.DateTimeField(db_default=Now(), editable=False)

    def __str__(self) -> str:
        return f"{self.event_name} ({self.event_key})"


class BillingPayment(models.Model):
    """One row per PAID Lemon Squeezy subscription invoice, written only by
    the webhook (subscription_payment_success).

    Minimal fields, all of which LS prints on its own receipt: amount,
    currency, reason, invoice link, card brand + last four. UNIQUE on the LS
    invoice id, so a re-delivered webhook never adds a second row — and never
    a second customer receipt (``receipt_sent_at`` is claimed atomically
    before the mail goes out)."""

    class Reason(models.TextChoices):
        INITIAL = "initial", "Initial payment"
        RENEWAL = "renewal", "Renewal"
        UPDATED = "updated", "Plan change"
        OTHER = "other", "Payment"

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="billing_payments",
    )
    ls_invoice_id = models.CharField(max_length=64, unique=True)
    ls_subscription_id = models.CharField(max_length=64, blank=True, default="")
    amount_cents = models.IntegerField()
    currency = models.CharField(max_length=8, default="USD")
    amount_formatted = models.CharField(max_length=32, blank=True, default="")
    reason = models.CharField(max_length=16, choices=Reason.choices, default=Reason.OTHER)
    invoice_url = models.URLField(max_length=1000, blank=True, default="")
    card_brand = models.CharField(max_length=24, blank=True, default="")
    card_last_four = models.CharField(max_length=4, blank=True, default="")
    paid_at = models.DateTimeField(null=True, blank=True)
    receipt_sent_at = models.DateTimeField(null=True, blank=True)
    # Refunds (subscription_payment_refunded). A refund is a MONEY fact only:
    # it never changes AccountPlan (see apps.billing.views._record_refund).
    # ``refunded_amount_cents`` is LS's CUMULATIVE ``refunded_amount`` for
    # this invoice, reduced with max() under a row lock, so duplicate /
    # out-of-order deliveries never sum twice. ``refunded`` = FULLY refunded
    # (refunded_amount_cents >= amount_cents), LS semantics.
    refunded = models.BooleanField(default=False, db_default=False)
    refunded_amount_cents = models.IntegerField(default=0, db_default=0)
    refunded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now(), editable=False)

    class Meta:
        ordering = ("-paid_at", "-id")

    def __str__(self) -> str:
        return f"{self.ls_invoice_id} {self.amount_cents} {self.currency} ({self.reason})"

    def _money(self, cents) -> str:
        if self.currency == "USD":
            return f"${cents / 100:,.2f}"
        return f"{cents / 100:,.2f} {self.currency}"

    @property
    def amount_display(self) -> str:
        if self.amount_formatted:
            return self.amount_formatted
        return self._money(self.amount_cents)

    @property
    def refund_display(self) -> str:
        """"Refunded $X" / "Partially refunded $X of $Y", "Refunded" (fully
        refunded, amount unknown/0), or "" (no refund)."""
        if self.refunded and self.refunded_amount_cents <= 0:
            return "Refunded"
        if self.refunded_amount_cents <= 0:
            return ""
        if self.refunded or self.refunded_amount_cents >= self.amount_cents:
            return f"Refunded {self._money(self.refunded_amount_cents)}"
        return f"Partially refunded {self._money(self.refunded_amount_cents)} of {self.amount_display}"


class PlanChangeRequest(models.Model):
    """Audit row + double-submit marker for a CONFIRMED plan change
    (apps.billing.views._change_plan). Created under the AccountPlan row lock
    right before the Lemon Squeezy PATCH. While a recent row is
    submitting/submitted/unknown and the webhook has not yet moved the plan
    to ``to_plan``, the billing page hides the upgrade buttons and another
    confirm is refused. Kept in the DB (not the per-process LocMem cache) so
    every gunicorn worker sees it. Never touches AccountPlan."""

    class Status(models.TextChoices):
        SUBMITTING = "submitting", "Submitting"
        SUBMITTED = "submitted", "Submitted"
        FAILED = "failed", "Failed"
        # PATCH timed out / 5xx: LS may or may not have applied (and charged).
        UNKNOWN = "unknown", "Unknown outcome"

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="plan_change_requests",
    )
    ls_subscription_id = models.CharField(max_length=64)
    from_plan = models.CharField(max_length=16)
    to_plan = models.CharField(max_length=16)
    from_variant = models.CharField(max_length=32)
    to_variant = models.CharField(max_length=32)
    estimate_cents = models.IntegerField(default=0)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SUBMITTING)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["user", "-created_at"], name="billing_pcr_user_created")]

    def __str__(self) -> str:
        return f"{self.user_id}: {self.from_plan} -> {self.to_plan} ({self.status})"
