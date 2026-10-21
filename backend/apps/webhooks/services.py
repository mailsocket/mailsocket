import hashlib
import hmac
import json
import secrets
from datetime import timezone as datetime_timezone

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

# NOTE: part of an intentional import cycle (serializers → parser →
# webhooks.services → serializers). It is safe because parser.py imports THIS
# module lazily inside a function. Keep top-level; do not "fix" the cycle.
from apps.accounts.entitlements import can_enable_webhook
from apps.api.serializers import MessageSerializer

from .models import Webhook, WebhookDelivery
from .security import validate_webhook_target

EVENT_NAME = "email.received"
SIGNATURE_VERSION = "1"


def derive_signing_secret(webhook):
    material = f"mailsocket.webhook.secret.v1:{webhook.public_id}:{webhook.secret_salt}".encode("utf-8")
    derived = hmac.new(
        settings.WEBHOOK_SIGNING_MASTER_KEY.encode("utf-8"),
        material,
        hashlib.sha256,
    ).hexdigest()
    return "whsec_" + derived


def display_signing_secret(webhook):
    return derive_signing_secret(webhook)


def rotate_signing_secret(webhook):
    webhook.secret_salt = secrets.token_urlsafe(16)
    webhook.save(update_fields=["secret_salt", "updated_at"])
    return display_signing_secret(webhook)


def _utc_z(value):
    return value.astimezone(datetime_timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_event_payload(message, event_id, *, created_at=None):
    message_data = MessageSerializer(message).data
    return {
        "id": event_id,
        "event": EVENT_NAME,
        "created_at": _utc_z(created_at or timezone.now()),
        "data": {"message": message_data},
    }


def event_body_bytes(payload):
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def signature_header(secret, timestamp, body):
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    digest = hmac.new(secret, str(timestamp).encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_signature(secret, header, body, *, expected_version=SIGNATURE_VERSION):
    if expected_version != SIGNATURE_VERSION:
        return False
    try:
        fields = dict(part.split("=", 1) for part in header.split(","))
        timestamp = int(fields["t"])
        supplied = fields["v1"]
    except (KeyError, TypeError, ValueError):
        return False
    expected = signature_header(secret, timestamp, body).split("v1=", 1)[1]
    return hmac.compare_digest(supplied, expected)


def create_webhook(inbox, url, *, enabled=True, resolver=None):
    target = validate_webhook_target(url, resolver=resolver)
    with transaction.atomic():
        if not can_enable_webhook(inbox.user):
            raise PermissionError("webhook_entitlement_required")
        webhook = Webhook(inbox=inbox, url=target.url, is_enabled=enabled)
        webhook.save(_url_validated=True)
    return webhook, display_signing_secret(webhook)


def update_webhook(webhook, url, *, enabled, resolver=None):
    target = validate_webhook_target(url, resolver=resolver)
    with transaction.atomic():
        if enabled and not can_enable_webhook(webhook.inbox.user):
            raise PermissionError("webhook_entitlement_required")
        webhook.url = target.url
        webhook.is_enabled = enabled
        webhook.save(update_fields=["url", "is_enabled", "updated_at"], _url_validated=True)
    return webhook


def delete_webhook(webhook):
    """Delete ``webhook`` and its delivery history in one transaction.

    WebhookDelivery.webhook is PROTECT, so the deliveries go first (same
    order as the account-erase path in dashboard/settings_views.py). The
    webhook row is locked first so a concurrent enqueue cannot add a
    delivery between the two deletes.
    """
    with transaction.atomic():
        Webhook.objects.select_for_update().filter(pk=webhook.pk).first()
        WebhookDelivery.objects.filter(webhook_id=webhook.pk).delete()
        Webhook.objects.filter(pk=webhook.pk).delete()


def enqueue_message_delivery(message):
    if message.ingest_status != message.IngestStatus.PARSED:
        return None, False
    try:
        webhook = Webhook.objects.get(inbox_id=message.inbox_id, is_enabled=True)
    except Webhook.DoesNotExist:
        return None, False
    event_id = f"evt_{secrets.token_urlsafe(18)}"
    payload = build_event_payload(message, event_id)
    try:
        delivery, created = WebhookDelivery.objects.get_or_create(
            webhook=webhook,
            message=message,
            defaults={
                "event_id": event_id,
                "payload": payload,
                "next_attempt_at": timezone.now(),
            },
        )
    except IntegrityError:
        delivery = WebhookDelivery.objects.get(webhook=webhook, message=message)
        created = False
    return delivery, created
