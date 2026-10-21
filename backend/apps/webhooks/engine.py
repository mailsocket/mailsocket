import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.entitlements import record_webhook_attempt

from .alerts import queue_internal_error_alert
from .models import Webhook, WebhookDelivery
from .security import SSRFValidationError
from .services import EVENT_NAME, SIGNATURE_VERSION, derive_signing_secret, event_body_bytes, signature_header
from .transport import DeliveryTransportError, post_json_pinned

logger = logging.getLogger(__name__)
MAX_BATCH_SIZE = 1000


@dataclass(frozen=True)
class DrainResult:
    claimed: int
    succeeded: int
    retrying: int
    exhausted: int
    cancelled: int
    fenced: int


def claim_batch(*, batch_size):
    now = timezone.now()
    max_attempts = int(settings.WEBHOOK_MAX_ATTEMPTS)
    eligible = Q(status=WebhookDelivery.Status.PENDING, next_attempt_at__lte=now) | Q(
        status=WebhookDelivery.Status.PENDING,
        next_attempt_at__isnull=True,
    ) | Q(
        status=WebhookDelivery.Status.DELIVERING,
        lease_expires_at__lte=now,
    )
    claims = []
    with transaction.atomic():
        rows = list(
            WebhookDelivery.objects.select_for_update(skip_locked=True)
            .filter(eligible)
            .select_related("webhook")
            .order_by("created_at", "id")[:batch_size]
        )
        for row in rows:
            if row.attempt_count >= max_attempts:
                WebhookDelivery.objects.filter(pk=row.pk).update(
                    status=WebhookDelivery.Status.EXHAUSTED,
                    next_attempt_at=None,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error_code="attempt_limit",
                )
                continue
            if not row.webhook.is_enabled:
                WebhookDelivery.objects.filter(pk=row.pk).update(
                    status=WebhookDelivery.Status.CANCELLED,
                    next_attempt_at=None,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error_code="webhook_disabled",
                )
                continue
            token = uuid.uuid4()
            claimed = WebhookDelivery.objects.filter(
                pk=row.pk,
                status=row.status,
                lease_token=row.lease_token,
            ).update(
                status=WebhookDelivery.Status.DELIVERING,
                attempt_count=row.attempt_count + 1,
                first_attempt_at=row.first_attempt_at or now,
                last_attempt_at=now,
                next_attempt_at=None,
                lease_token=token,
                lease_expires_at=now + timedelta(seconds=int(settings.WEBHOOK_LEASE_SECONDS)),
                last_response_code=None,
                last_error_code="",
            )
            if claimed:
                claims.append((row.pk, token))
    return claims


def _headers(delivery, body, timestamp):
    return {
        "Content-Type": "application/json",
        "User-Agent": "mailsocket-webhooks/1",
        "X-Mailsocket-Event-Id": delivery.event_id,
        "X-Mailsocket-Event": EVENT_NAME,
        "X-Mailsocket-Signature": signature_header(
            derive_signing_secret(delivery.webhook), timestamp, body
        ),
        "X-Mailsocket-Signature-Version": SIGNATURE_VERSION,
    }


def _retry_delay(attempt_count, retry_after):
    schedule = tuple(int(value) for value in settings.WEBHOOK_RETRY_SCHEDULE_SECONDS)
    ceiling = schedule[min(attempt_count, len(schedule) - 1)]
    base = ceiling
    if retry_after is not None and retry_after < ceiling:
        base = max(1, retry_after)
    jitter = int(settings.WEBHOOK_RETRY_JITTER_SECONDS)
    return max(1, min(ceiling, base + (secrets.randbelow(jitter * 2 + 1) - jitter if jitter else 0)))


def _classify(status_code):
    if 200 <= status_code <= 299:
        return "success"
    if status_code in {408, 425, 429} or 500 <= status_code <= 599:
        return "retry"
    return "permanent"


def _finalize(delivery_id, token, *, outcome, response_code=None, error_code="", retry_after=None):
    now = timezone.now()
    with transaction.atomic():
        delivery = WebhookDelivery.objects.select_for_update().select_related("webhook").filter(
            pk=delivery_id,
            status=WebhookDelivery.Status.DELIVERING,
            lease_token=token,
        ).first()
        if delivery is None:
            return "fenced"
        if outcome == "success":
            status = WebhookDelivery.Status.SUCCEEDED
            next_attempt_at = None
            delivered_at = now
        elif outcome == "permanent" or delivery.attempt_count >= int(settings.WEBHOOK_MAX_ATTEMPTS):
            status = WebhookDelivery.Status.EXHAUSTED
            next_attempt_at = None
            delivered_at = None
        else:
            status = WebhookDelivery.Status.PENDING
            next_attempt_at = now + timedelta(seconds=_retry_delay(delivery.attempt_count, retry_after))
            delivered_at = None
        WebhookDelivery.objects.filter(pk=delivery.pk, lease_token=token).update(
            status=status,
            next_attempt_at=next_attempt_at,
            lease_token=None,
            lease_expires_at=None,
            last_response_code=response_code,
            last_error_code=error_code[:64],
            delivered_at=delivered_at,
        )
        Webhook.objects.filter(pk=delivery.webhook_id).update(
            **({"last_success_at": now} if status == WebhookDelivery.Status.SUCCEEDED else {"last_failure_at": now})
        )
        if status == WebhookDelivery.Status.SUCCEEDED:
            return "succeeded"
        if status == WebhookDelivery.Status.PENDING:
            return "retrying"
        return "exhausted"


def deliver_claim(delivery_id, token, *, transport=post_json_pinned):
    delivery = WebhookDelivery.objects.select_related("webhook").filter(
        pk=delivery_id,
        status=WebhookDelivery.Status.DELIVERING,
        lease_token=token,
        lease_expires_at__gt=timezone.now(),
    ).first()
    if delivery is None:
        return "fenced"
    record_webhook_attempt(delivery.webhook.inbox.user_id)
    started = time.monotonic()
    try:
        result = _attempt(delivery, token, transport)
    except DatabaseError:
        # A database failure is not a per-row problem: let the pass (and the
        # deliver_webhooks command, which exits non-zero on it) stop.
        raise
    except Exception as exc:
        
        # the global drain pass (every tenant's deliveries queue behind it).
        # Record a failed attempt (normal retry schedule, exhausted at the
        # attempt cap). Ids + exception type only: never the URL, headers,
        # signature/secret, payload or the exception text (it may embed them).
        logger.error(
            "webhook_delivery_internal_error delivery_id=%s event_id=%s error=%s",
            delivery.public_id,
            delivery.event_id,
            type(exc).__name__,
        )
        result = _finalize(delivery_id, token, outcome="retry", error_code="internal_error")
        queue_internal_error_alert(  
            delivery_public_id=delivery.public_id,
            event_id=delivery.event_id,
            error_type=type(exc).__name__,
        )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "webhook_delivery delivery_id=%s event_id=%s attempt=%s elapsed_ms=%s outcome=%s",
        delivery.public_id,
        delivery.event_id,
        delivery.attempt_count,
        elapsed_ms,
        result,
    )
    return result


def _attempt(delivery, token, transport):
    delivery_id = delivery.pk
    body = event_body_bytes(delivery.payload)
    timestamp = int(time.time())
    try:
        response = transport(delivery.webhook.url, body, _headers(delivery, body, timestamp))
        classification = _classify(response.status_code)
        result = _finalize(
            delivery_id,
            token,
            outcome=classification,
            response_code=response.status_code,
            error_code="" if classification == "success" else f"http_{response.status_code}",
            retry_after=response.retry_after_seconds,
        )
    except SSRFValidationError as exc:
        result = _finalize(delivery_id, token, outcome="permanent", error_code=exc.code)
    except DeliveryTransportError as exc:
        result = _finalize(delivery_id, token, outcome="retry", error_code=exc.code)
    return result


def drain(*, batch_size=50, transport=post_json_pinned):
    counts = {"succeeded": 0, "retrying": 0, "exhausted": 0, "cancelled": 0, "fenced": 0}
    claimed = 0
    # Claim immediately before each network request. A whole batch claimed up
    # front could let later rows' leases expire while earlier 10-second I/O runs.
    for _ in range(batch_size):
        claims = claim_batch(batch_size=1)
        if not claims:
            break
        delivery_id, token = claims[0]
        claimed += 1
        result = deliver_claim(delivery_id, token, transport=transport)
        counts[result] += 1
    return DrainResult(claimed=claimed, **counts)
