import secrets

from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models.functions import Now

from apps.inboxes.models import Inbox, Message


class Webhook(models.Model):
    id = models.BigAutoField(primary_key=True)
    public_id = models.CharField(max_length=32, unique=True, editable=False)
    inbox = models.OneToOneField(
        Inbox,
        on_delete=models.PROTECT,
        related_name="webhook",
    )
    url = models.URLField(max_length=2048)
    is_enabled = models.BooleanField(default=True)
    secret_salt = models.CharField(max_length=32, editable=False)
    created_at = models.DateTimeField(db_default=Now(), editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["inbox", "is_enabled"], name="webhook_inbox_enabled_idx"),
        ]

    def save(self, *args, **kwargs):
        url_validated = kwargs.pop("_url_validated", False)
        original = None
        if not self._state.adding:
            original = type(self).objects.only("public_id", "inbox_id", "url", "is_enabled").get(pk=self.pk)
        needs_validation = self._state.adding or self.url != original.url or (
            self.is_enabled and not original.is_enabled
        )
        if not url_validated and needs_validation:
            from .security import validate_webhook_target

            target = validate_webhook_target(self.url)
            self.url = target.url
        if self._state.adding:
            self.secret_salt = secrets.token_urlsafe(16)
            for _ in range(10):
                self.public_id = f"wh_{secrets.token_urlsafe(18)}"
                try:
                    with transaction.atomic():
                        return super().save(*args, **kwargs)
                except IntegrityError as exc:
                    constraint = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "")
                    if constraint != "webhooks_webhook_public_id_key":
                        raise
                    self.pk = None
            raise RuntimeError("Unable to save a unique Webhook public_id")
        if (self.public_id, self.inbox_id) != (original.public_id, original.inbox_id):
            raise ValidationError("Webhook identity fields are immutable.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.public_id


class WebhookDelivery(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DELIVERING = "delivering", "Delivering"
        SUCCEEDED = "succeeded", "Succeeded"
        EXHAUSTED = "exhausted", "Exhausted"
        CANCELLED = "cancelled", "Cancelled"

    id = models.BigAutoField(primary_key=True)
    public_id = models.CharField(max_length=32, unique=True, editable=False)
    webhook = models.ForeignKey(Webhook, on_delete=models.PROTECT, related_name="deliveries")
    message = models.ForeignKey(Message, on_delete=models.PROTECT, related_name="webhook_deliveries")
    event_id = models.CharField(max_length=32, unique=True, editable=False)
    payload = models.JSONField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempt_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    last_response_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(db_default=Now(), editable=False)
    first_attempt_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["webhook", "message"],
                name="webhook_delivery_message_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(attempt_count__gte=0),
                name="webhook_delivery_attempts_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=("pending", "delivering", "succeeded", "exhausted", "cancelled")),
                name="webhook_delivery_status_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=["status", "next_attempt_at", "created_at", "id"],
                name="webhook_delivery_drain_idx",
            ),
            models.Index(
                fields=["webhook", "-created_at", "-id"],
                name="webhook_delivery_log_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding:
            if not self.event_id:
                self.event_id = f"evt_{secrets.token_urlsafe(18)}"
            for _ in range(10):
                self.public_id = f"whd_{secrets.token_urlsafe(18)}"
                try:
                    with transaction.atomic():
                        return super().save(*args, **kwargs)
                except IntegrityError as exc:
                    constraint = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "")
                    if constraint not in {
                        "webhooks_webhookdelivery_public_id_key",
                        "webhooks_webhookdelivery_event_id_key",
                    }:
                        raise
                    self.pk = None
                    self.event_id = f"evt_{secrets.token_urlsafe(18)}"
            raise RuntimeError("Unable to save a unique WebhookDelivery identity")
        original = type(self).objects.only(
            "public_id", "event_id", "webhook_id", "message_id", "payload"
        ).get(pk=self.pk)
        if (
            self.public_id,
            self.event_id,
            self.webhook_id,
            self.message_id,
            self.payload,
        ) != (
            original.public_id,
            original.event_id,
            original.webhook_id,
            original.message_id,
            original.payload,
        ):
            raise ValidationError("Webhook delivery event fields are immutable.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.public_id
