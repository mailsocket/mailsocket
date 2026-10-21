"Public API."

import logging

from django.db import transaction

from apps.accounts.services import RateLimitExceeded, increment_rate_limit
from apps.ops import notify as ops_notify

logger = logging.getLogger(__name__)

ALERT_RATE_NAME = "ops:webhook-internal-error:hour"
ALERT_WINDOW = 3600


def queue_internal_error_alert(*, delivery_public_id, event_id, error_type):
    try:
        if not ops_notify.any_channel_enabled():
            return
        try:
            with transaction.atomic():
                increment_rate_limit(ALERT_RATE_NAME, error_type, limit=1, window_seconds=ALERT_WINDOW)
        except RateLimitExceeded:
            return
        body = "\n".join(
            [
                "Problem: webhook delivery internal_error",
                f"Error type: {error_type}",
                f"Delivery: {delivery_public_id}",
                f"Event: {event_id}",
                "",
                "One row failed with an unexpected exception; the drain went on and",
                "the row retries on the normal schedule (exhausted at the attempt cap).",
                "Logs: docker compose logs web | grep webhook_delivery_internal_error",
                "At most one alert per error type per hour.",
            ]
        )
        ops_notify.notify("warn", f"Webhook delivery internal_error: {error_type}", body)
    except Exception:  # noqa: BLE001 — alerting must never break delivery
        logger.exception("webhook_internal_error_alert_failed error=%s", error_type)
