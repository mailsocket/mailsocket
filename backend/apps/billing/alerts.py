"Public API."
import logging

from django.conf import settings
from django.db import transaction
from django.urls import reverse

from apps.ops import notify as ops_notify

logger = logging.getLogger(__name__)


ALERT_EVENTS = frozenset(
    {
        "subscription_created",
        "subscription_payment_success",
        "subscription_payment_failed",
        "subscription_cancelled",
        "subscription_expired",
    }
)
# Refund events (order_refunded / subscription_payment_refunded) never change
# the plan, so they are NOT in ALERT_EVENTS: they alert through
# queue_refund_alert (money level, amount + full/partial + LS link).

# BL-17: webhook events _apply_ls_event acts on, and documented LS events
# whose state reaches us through another event (LS docs "Event types":
# subscription_updated is the catch-all for resume/unpause, payment_success
# always accompanies payment_recovered, subscription_created accompanies
# order_created). Any OTHER event type is ignored safely (never mapped to a

HANDLED_EVENTS = frozenset(
    {
        "subscription_created",
        "subscription_updated",
        "subscription_cancelled",
        "subscription_expired",
        "subscription_paused",
        "subscription_payment_success",
        "subscription_payment_failed",
        "subscription_payment_refunded",
        "order_refunded",
    }
)
KNOWN_IGNORED_EVENTS = frozenset(
    {
        "order_created",
        "subscription_resumed",
        "subscription_unpaused",
        "subscription_payment_recovered",
        "customer_updated",
    }
)
UNKNOWN_EVENT_FOOTER = (
    "",
    "Unknown Lemon Squeezy event type: ignored, NO plan change was applied. If LS",
    "added a new event we should act on, handle it in apps.billing.views._apply_ls_event.",
)

# The SMTP bound now lives in apps.ops.notify (same 10 s).
SMTP_TIMEOUT_SECONDS = ops_notify.SMTP_TIMEOUT_SECONDS


def _clean(value):
    """One-line, header-safe string (subjects must not contain newlines)."""
    return " ".join(str(value if value is not None else "").split())


def _plan_name(key):
    from apps.accounts.models import AccountPlan

    if not key:
        return "-"
    return f"{AccountPlan.DISPLAY_NAMES.get(key, key)} ({key})"


def _admin_url(plan_pk):
    path = reverse("admin:accounts_accountplan_change", args=[plan_pk])
    host = getattr(settings, "DASHBOARD_HOST", "") or "dash.mailsocket.app"
    return f"https://{host}{path}"


def _account_email(user_pk):
    "Public API."
    from django.contrib.auth import get_user_model

    from apps.accounts.models import Account, AccountOwnership

    user = get_user_model().objects.filter(pk=user_pk).first()
    if user is None:
        return "(unknown)"
    account = Account.objects.filter(user_id=user_pk).first()
    email = (account.email_normalized if account else "") or user.email or user.get_username()
    link = AccountOwnership.objects.filter(account_user_id=user_pk).select_related("owner_user").first()
    if link is not None:
        email = f"{email} (workspace of {link.owner_user.email})"
    return email


def _event_alert(event_name, user_pk, plan_before, plan_after, status_after, subscription_id, amount=None):
    try:
        email = _account_email(user_pk)
        if event_name == "subscription_created":
            title = f"New subscription: {_plan_name(plan_after)}"
        else:
            title = f"Billing event {event_name}: {_plan_name(plan_after)}"
        if amount:
            title = f"{title} — {_clean(amount)}"
        admin = _admin_url(user_pk)
        amount_line = [f"Amount: {_clean(amount)}"] if amount else []
        body = "\n".join(
            [
                f"Event: {event_name}",
                f"Account: {email}",
                f"User pk: {user_pk}",
                f"Plan: {_plan_name(plan_before)} -> {_plan_name(plan_after)}",
            ]
            + amount_line
            + [
                f"Status after: {status_after}",
                f"LS subscription id: {subscription_id or '-'}",
                f"Admin: {admin}",
                "",
                "Check the account got the expected plan.",
            ]
        )
        # Telegram: no account email, no customer data — plan change,
        # amount (billing-confirm item 6) + link.
        tg_body = "\n".join(
            [
                f"Event: {event_name}",
                f"Plan: {_plan_name(plan_before)} -> {_plan_name(plan_after)}",
            ]
            + amount_line
            + [
                f"Status after: {status_after}",
            ]
        )
        ops_notify.notify(
            "money",
            title,
            tg_body,
            link=admin,
            email_title=f"{title} — {email}",
            email_body=body,
        )
    except Exception:
        logger.exception("billing_alert_send_failed event=%s user_pk=%s", event_name, user_pk)


_DEFAULT_PROBLEM_FOOTER = (
    "",
    "The webhook returned 200 and recorded the BillingEvent, but NO plan change",
    "was applied. A customer may have paid and NOT got their plan — check Lemon",
    "Squeezy and fix the AccountPlan manually in admin if needed.",
)


def _problem_alert(reason, event_name, details, footer=None):
    try:
        lines = [
            f"Problem: {reason}",
            f"Event: {event_name}",
        ]
        tg_lines = list(lines)
        for label, value in details:
            line = f"{label}: {value if value not in (None, '') else '-'}"
            lines.append(line)
            if "email" not in label.lower():  # customer emails: EMAIL only
                tg_lines.append(line)
        footer = list(footer if footer is not None else _DEFAULT_PROBLEM_FOOTER)
        ops_notify.notify(
            "warn",
            f"Billing webhook problem: {reason}",
            "\n".join(tg_lines + footer),
            email_body="\n".join(lines + footer),
        )
    except Exception:
        logger.exception("billing_alert_send_failed problem=%s event=%s", reason, event_name)


def queue_event_alert(event_name, *, user_pk, plan_before, plan_after, status_after, subscription_id, amount=None):
    "Public API."
    try:
        if event_name not in ALERT_EVENTS or not ops_notify.any_channel_enabled():
            return
        transaction.on_commit(
            lambda: _event_alert(
                event_name, user_pk, plan_before, plan_after, status_after, subscription_id, amount
            ),
            robust=True,
        )
    except Exception:
        logger.exception("billing_alert_queue_failed event=%s", event_name)


def queue_problem_alert(reason, event_name, *, details=(), footer=None):
    "Public API."
    try:
        if not ops_notify.any_channel_enabled():
            return
        details = [(str(label), _clean(value)) for label, value in details]
        transaction.on_commit(
            lambda: _problem_alert(reason, event_name, details, footer),
            robust=True,
        )
    except Exception:
        logger.exception("billing_alert_queue_failed problem=%s event=%s", reason, event_name)


def _refund_alert(title, event_name, user_pk, details, footer):
    try:
        email = _account_email(user_pk)
        admin = _admin_url(user_pk)
        lines = [f"Event: {event_name}", f"Account: {email}"]
        tg_lines = [f"Event: {event_name}"]
        for label, value in details:
            line = f"{label}: {value if value not in (None, '') else '-'}"
            lines.append(line)
            if "email" not in label.lower():  # customer emails: EMAIL only
                tg_lines.append(line)
        lines += [f"Admin: {admin}"] + list(footer)
        ops_notify.notify(
            "money",
            title,
            "\n".join(tg_lines + list(footer)),
            link=admin,
            email_title=f"{title} — {email}",
            email_body="\n".join(lines),
        )
    except Exception:
        logger.exception("billing_alert_send_failed event=%s user_pk=%s", event_name, user_pk)


def queue_refund_alert(*, title, details, footer, event_name, user_pk):
    "Public API."
    try:
        if not ops_notify.any_channel_enabled():
            return
        title = _clean(title)
        details = [(str(label), _clean(value)) for label, value in details]
        footer = tuple(footer)
        transaction.on_commit(
            lambda: _refund_alert(title, event_name, user_pk, details, footer),
            robust=True,
        )
    except Exception:
        logger.exception("billing_alert_queue_failed event=%s", event_name)


def note_unhandled_event(event_name, user_pk):
    "Public API."
    logger.info("billing_webhook_unhandled_event event=%s", event_name)
    if event_name in HANDLED_EVENTS or event_name in KNOWN_IGNORED_EVENTS:
        return
    queue_problem_alert(
        "unknown event type", event_name, details=[("User pk", user_pk)], footer=UNKNOWN_EVENT_FOOTER
    )
