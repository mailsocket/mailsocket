"Public API."

import logging
import re
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.urls import resolve, reverse
from django.utils import timezone

from apps.ops import notify as ops_notify

from .models import FeedbackAlertBreaker, FeedbackReport

logger = logging.getLogger(__name__)

HOURLY_CAP = 10
WINDOW = timedelta(hours=1)
DIGEST_THROTTLE = timedelta(hours=1)
BREAKER_KEY = "feedback"
PAGE_PATH_ALERT_MAX = 100

REDACTED = "[redacted]"
_OTP_WORDS = r"(?:code|otp|passcode|pin)"
_SCRUB_PATTERNS = (
    # Our API key format (ms_live_ + token_urlsafe), a possible test-key
    # variant, and webhook signing secrets.
    re.compile(r"\b(?:ms_live|ms_test|whsec)_[A-Za-z0-9_-]{6,}"),
    # Authorization header values.
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    
    # (magic links, verify/reset links: 16+ token chars, may contain ':' as
    # in Django's signed values). Only the token is replaced, the URL stays.
    
    # (~0.07% of 32-byte tokens), so "has a digit" alone leaked those. A
    # 20+ char run that mixes case or carries '_', '-' or ':' is a token too;
    # plain word paths ("quickstart-guide", "notifications") stay readable.
    re.compile(
        r"(?<=[/=])(?:"
        r"[A-Za-z0-9_:.~-]*(?=[A-Za-z0-9_:.~-]*\d)[A-Za-z0-9_:.~-]{16,}"
        r"|(?=[A-Za-z0-9_:.~-]*[A-Z])(?=[A-Za-z0-9_:.~-]*[a-z])[A-Za-z0-9_:.~-]{20,}"
        
        # real word slugs on our site stay far below that.
        r"|[A-Za-z0-9_:.~-]{32,}"
        r")"
    ),
)
# "code: 123456", "OTP is 482913", "my pin was 1234"
_OTP_AFTER_WORD = re.compile(rf"(?i)(\b{_OTP_WORDS}\b(?:\s*(?:is|was|:|=|-|#)\s*|\s+){{1,3}})\d{{4,8}}\b")
# "123456 is your code", "482913 your verification code"
_OTP_BEFORE_WORD = re.compile(
    rf"(?i)\b\d{{4,8}}\b(?=\s+(?:is\s+)?(?:your\s+|the\s+|my\s+)?(?:verification\s+|login\s+|one-time\s+)?{_OTP_WORDS}\b)"
)


def scrub(text):
    """Replace token-shaped strings (API keys, Bearer values, OTPs next to
    code/OTP words) with ``[redacted]``. Used for the ops EMAIL only."""
    text = str(text or "")
    for pattern in _SCRUB_PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _OTP_AFTER_WORD.sub(lambda m: m.group(1) + REDACTED, text)
    return _OTP_BEFORE_WORD.sub(REDACTED, text)


def alert_page(page_path):
    """Telegram-safe page label. The stored path is shown only when it
    resolves to one of our routes WITHOUT parameters; a parameterised route
    is shown as its pattern (``/verify-email/<str:token>/``), so tokens, ids
    or addresses embedded in a path never reach Telegram. Anything else
    (typed text, a 404 path) gets a fixed placeholder; the admin page has
    the full path."""
    if not page_path:
        return "-"
    try:
        match = resolve(page_path)
    except Exception:  # noqa: BLE001 — Resolver404 and anything odd
        return "(unmatched path)"
    if match.args or match.kwargs:
        label = "/" + str(match.route or "").lstrip("^").rstrip("$")
    else:
        label = page_path
    label = scrub(label)
    if len(label) > PAGE_PATH_ALERT_MAX:
        label = label[: PAGE_PATH_ALERT_MAX - 1] + "\u2026"
    return label


def admin_url(report_pk):
    path = reverse("admin:feedback_feedbackreport_change", args=[report_pk])
    host = getattr(settings, "DASHBOARD_HOST", "") or "dash.mailsocket.app"
    return f"https://{host}{path}"


def telegram_title(report):
    plan = report.plan or "anonymous"
    return f"New feedback #{report.pk} \u00b7 plan {plan} \u00b7 page {alert_page(report.page_path)}"


def _reply_to(report):
    "Public API."
    if report.email:
        return report.email
    if report.user_id:
        from apps.accounts.models import Account

        account_email = (
            Account.objects.filter(user_id=report.user_id).values_list("email_normalized", flat=True).first()
        )
        if account_email:
            return f"{account_email} (account email)"
    return "-"


def email_body(report):
    lines = [
        f"Feedback #{report.pk}",
        f"Plan: {report.plan or 'anonymous'}",
        
        # (/verify-email/<token>/); show the route pattern, full path in admin.
        f"Page: {alert_page(report.page_path)}",
        f"Request id: {report.request_id or '-'}",
        f"Reply to: {_reply_to(report)}",
        f"Admin: {admin_url(report.pk)}",
        "",
        "Message (token-shaped strings redacted; full text in admin only):",
        "",
        scrub(report.message),
    ]
    return "\n".join(lines)


def _breaker_row():
    try:
        FeedbackAlertBreaker.objects.get_or_create(key=BREAKER_KEY)
    except IntegrityError:
        pass  # created concurrently; it exists now


def _claim_slot(now):
    """True when this report may notify individually; otherwise the report
    is counted in ``suppressed`` for the digest."""
    _breaker_row()
    with transaction.atomic():
        row = FeedbackAlertBreaker.objects.select_for_update().get(key=BREAKER_KEY)
        if row.window_started_at is None or row.window_started_at <= now - WINDOW:
            row.window_started_at = now
            row.sent = 0
        if row.sent < HOURLY_CAP:
            row.sent += 1
            won = True
        else:
            row.suppressed += 1
            won = False
        row.save(update_fields=["window_started_at", "sent", "suppressed"])
    return won


def _count_suppressed():
    FeedbackAlertBreaker.objects.filter(key=BREAKER_KEY).update(suppressed=F("suppressed") + 1)


def notify_new_report(report, now=None):
    "Public API."
    slot_claimed = counted = False
    try:
        if not ops_notify.any_channel_enabled():
            return "disabled"
        now = now or timezone.now()
        if not _claim_slot(now):
            return "suppressed"
        slot_claimed = True
        title = telegram_title(report)
        result = ops_notify.notify(
            "info",
            title,
            "",
            link=admin_url(report.pk),
            email_title=f"New feedback #{report.pk} (plan {report.plan or 'anonymous'})",
            email_body=email_body(report),
        )
        if ops_notify.failed(result):
            # Not lost: the digest will mention it.
            counted = True
            _count_suppressed()
        return result
    except Exception:  # noqa: BLE001 — never fail the submit
        logger.exception("feedback_alert_failed report=%s", getattr(report, "pk", None))
        if slot_claimed and not counted:
            # The slot was consumed but nothing was delivered: same atomic
            # row update as a failed result, so the digest still reports it.
            try:
                _count_suppressed()
            except Exception:  # noqa: BLE001
                logger.exception("feedback_alert_count_failed report=%s", getattr(report, "pk", None))
        return "error"


def send_due_feedback_digest(now=None):
    """ONE digest when reports went un-alerted (breaker tripped or a send
    failed), at most once per hour. Returns 1 when delivered, else 0. No-op
    without a configured channel. Never raises."""
    try:
        if not ops_notify.any_channel_enabled():
            return 0
        now = now or timezone.now()
        if not FeedbackAlertBreaker.objects.filter(key=BREAKER_KEY, suppressed__gt=0).exists():
            return 0
        with transaction.atomic():
            row = FeedbackAlertBreaker.objects.select_for_update().filter(key=BREAKER_KEY).first()
            if row is None or row.suppressed <= 0:
                return 0
            if row.last_digest_at is not None and row.last_digest_at > now - DIGEST_THROTTLE:
                return 0
            previous_digest_at, suppressed = row.last_digest_at, row.suppressed
            row.last_digest_at = now
            row.suppressed = 0
            row.save(update_fields=["last_digest_at", "suppressed"])
        recent = FeedbackReport.objects.filter(created_at__gte=now - timedelta(hours=1)).count()
        fleet = f"https://{getattr(settings, 'DASHBOARD_HOST', '') or 'dash.mailsocket.app'}{reverse('dashboard:fleet')}"
        title = f"{recent} new feedback reports in the last hour"
        body = (
            f"{suppressed} report(s) were not alerted individually "
            f"(alert cap {HOURLY_CAP}/hour or a failed send)."
        )
        try:
            result = ops_notify.notify("info", title, body, link=fleet)
        except Exception:
            result = {"email": "error:Exception"}
        if ops_notify.failed(result):
            FeedbackAlertBreaker.objects.filter(key=BREAKER_KEY, last_digest_at=now).update(
                last_digest_at=previous_digest_at, suppressed=F("suppressed") + suppressed
            )
            return 0
        return 1
    except Exception:  # noqa: BLE001
        logger.exception("feedback_digest_failed")
        return 0
