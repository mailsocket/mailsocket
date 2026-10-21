"Public API."
import logging

from django.conf import settings
from django.core.mail import get_connection, send_mail
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

SMTP_TIMEOUT_SECONDS = 10
SUPPORT_LINE = "Questions or a mistake? Reply or email sales@mailsocket.app — 14-day money-back."
NOT_DOUBLE_LINE = (
    "This is your mailsocket receipt (Lemon Squeezy also emails one — you were charged once)."
)


def _clean(value):
    return " ".join(str(value if value is not None else "").split())


def customer_email(user_pk):
    "Public API."
    from django.contrib.auth import get_user_model

    from apps.accounts.models import Account, AccountOwnership

    user = get_user_model().objects.filter(pk=user_pk).first()
    if user is None:
        return ""
    link = AccountOwnership.objects.filter(account_user_id=user_pk).select_related("owner_user").first()
    if link is not None and link.owner_user.email:
        return link.owner_user.email
    account = Account.objects.filter(user_id=user_pk).first()
    return (account.email_normalized if account else "") or user.email or ""


def _send(subject, body, to):
    connection = get_connection(timeout=SMTP_TIMEOUT_SECONDS)
    send_mail(
        _clean(subject), body, settings.DEFAULT_FROM_EMAIL, [to],
        fail_silently=False, connection=connection,
    )


def _send_payment_receipt(payment_pk, description):
    from .models import BillingPayment

    try:
        claimed = BillingPayment.objects.filter(pk=payment_pk, receipt_sent_at__isnull=True).update(
            receipt_sent_at=timezone.now()
        )
        if not claimed:
            return  # already sent for this invoice id
        payment = BillingPayment.objects.get(pk=payment_pk)
        to = customer_email(payment.user_id) if payment.user_id else ""
        if not to:
            logger.error("billing_receipt_no_recipient invoice=%s", payment.ls_invoice_id)
            return
        amount = payment.amount_display
        paid = payment.paid_at or payment.created_at or timezone.now()
        paid_utc = paid.astimezone(timezone.get_fixed_timezone(0))
        lines = [
            "Thanks for your payment.",
            "",
            f"Plan: {description}",
            f"Amount: {amount}",
            f"Date: {paid_utc:%Y-%m-%d %H:%M} UTC",
            f"Reason: {payment.get_reason_display()}",
        ]
        if payment.card_last_four:
            brand = (payment.card_brand or "card").title()
            lines.append(f"Paid with: {brand} ••{payment.card_last_four}")
        lines.append(f"Invoice: {payment.invoice_url or 'available in the billing portal'}")
        lines += [
            "",
            NOT_DOUBLE_LINE,
            "",
            SUPPORT_LINE,
            "",
            f"Billing: https://{getattr(settings, 'DASHBOARD_HOST', '') or 'dash.mailsocket.app'}/app/billing/",
        ]
        _send(f"mailsocket receipt — {amount} ({description})", "\n".join(lines), to)
    except Exception:
        logger.exception("billing_receipt_send_failed payment_pk=%s", payment_pk)


def queue_payment_receipt(payment_pk, description):
    """Queue our customer receipt for a NEWLY recorded BillingPayment.
    Never raises."""
    try:
        transaction.on_commit(lambda: _send_payment_receipt(payment_pk, description), robust=True)
    except Exception:
        logger.exception("billing_receipt_queue_failed payment_pk=%s", payment_pk)


def send_downgrade_notice(user, *, target_name, when, from_name=None):
    "Public API."
    try:
        to = customer_email(user.pk)
        if not to:
            return
        limits = f"{from_name} limits ended immediately. " if from_name else ""
        body = "\n".join([
            f"Your mailsocket plan changed to {target_name} now. {limits}".rstrip(),
            "",
            f"No charge now, and nothing is refunded for the rest of this period. "
            f"Your new {target_name} price applies from {when}.",
            "",
            SUPPORT_LINE,
        ])
        _send(f"mailsocket: your plan changed to {target_name}", body, to)
    except Exception:
        logger.exception("billing_downgrade_notice_failed user_pk=%s", getattr(user, "pk", None))
