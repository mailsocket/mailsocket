"Public API."

from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.db.models.functions import Now

from apps.inboxes.models import PublicIdModel


class OutboundEmail(PublicIdModel):
    """A single customer-originated outbound send (design §1).

    Status lifecycle is SERVICE-layer only (mirrors Domain.status doctrine,
    apps/domains/models.py:89-90): the API view creates ``queued`` (the ONLY
    state a serializer can ever produce); the drain claim/finalize functions
    in apps.outbound.engine own every other transition. Nothing ever moves
    backwards.
    """

    public_id_prefix = "obe_"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        REJECTED = "rejected", "Rejected"
        BOUNCED = "bounced", "Bounced"
        CANCELLED = "cancelled", "Cancelled"

    id = models.BigAutoField(primary_key=True)
    public_id = models.CharField(max_length=32, unique=True, editable=False)
    # Tenant. PROTECT: an OutboundEmail audit row must never be silently
    # orphaned by a user delete (mirrors Domain.user / Inbox.user).
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="outbound_emails",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="outbound_emails_created",
    )
    # SET_NULL on domain delete: the audit row (and dkim_domain snapshot)
    # must survive the Domain row being deleted. The drain re-check (design
    
    # eligible at claim time — a NULL FK here (post-delete) is exactly the
    # "no longer eligible" case and rejects the send, never sends unsigned.
    domain = models.ForeignKey(
        "domains.Domain",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="outbound_emails",
    )
    # Snapshot of domain.name at enqueue time (audit survives domain
    # deletion/rename-is-impossible-but-delete-is-possible).
    dkim_domain = models.CharField(max_length=253, blank=True)

    from_address = models.CharField(max_length=320)
    to = models.JSONField()
    cc = models.JSONField(default=list, blank=True)
    bcc = models.JSONField(default=list, blank=True)
    reply_to = models.CharField(max_length=320, blank=True)

    subject = models.CharField(max_length=512, blank=True)
    body_text = models.TextField(blank=True)
    body_html = models.TextField(blank=True)

    
    
    
    # build_raw_message/dkim_sign only emit/sign these headers when set, so
    
    # enqueue_outbound_email's gate (has_control_chars + msg-id shape +
    # references <=2KB) — the view-layer sanitizer is convenience, this gate
    # re-validation is the guarantee (design §3).
    in_reply_to = models.CharField(max_length=998, null=True, blank=True)
    references = models.TextField(null=True, blank=True)

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.QUEUED
    )

    # WebhookDelivery shape (webhooks/models.py) — drain claim/lease fencing.
    attempt_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)

    provider_message_id = models.CharField(max_length=255, blank=True)
    smtp_message_id = models.CharField(max_length=255, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_detail = models.CharField(max_length=255, blank=True)

    # Denormalized len(to+cc+bcc) at enqueue — cheap quota SUMs (design §1/§3.9)
    # without re-parsing the JSONFields for every windowed count.
    recipient_count = models.PositiveSmallIntegerField(default=0)

    # Reply-threading only (no behavior in V1) — nullable, SET_NULL.
    inbox = models.ForeignKey(
        "inboxes.Inbox",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="outbound_emails",
    )

    created_at = models.DateTimeField(db_default=Now(), editable=False)
    queued_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    bounced_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    status__in=(
                        "queued",
                        "sending",
                        "sent",
                        "failed",
                        "rejected",
                        "bounced",
                        "cancelled",
                    )
                ),
                name="outbound_email_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(attempt_count__gte=0),
                name="outbound_email_attempts_nonnegative",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "-created_at", "-id"], name="obe_owner_created_idx"),
            # Hot path for the drain claim (design §4): only queued/sending
            # rows with a due next_attempt_at are ever scanned.
            models.Index(
                fields=["next_attempt_at"],
                condition=models.Q(status__in=("queued", "sending")),
                name="obe_drain_claim_idx",
            ),
            # Quota window counts (design §3.9): SUM(recipient_count) over
            # rows created today/this hour for a given user.
            models.Index(fields=["user", "created_at"], name="obe_owner_created_at_idx"),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding:
            self.public_id = ""
            for _ in range(10):
                self._set_available_public_id()
                try:
                    with transaction.atomic():
                        return super().save(*args, **kwargs)
                except IntegrityError as exc:
                    constraint = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "")
                    expected = f"{self._meta.db_table}_public_id_key"
                    if constraint != expected:
                        raise
                    self.public_id = ""
                    self.pk = None
            raise RuntimeError("Unable to save a unique OutboundEmail public_id")
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.public_id


class SuppressionEntry(models.Model):
    """Global suppression list (design §1/§4/§6): a nonexistent or
    complaint-flagged mailbox is nonexistent for every tenant — protecting
    the shared sending IP wins over per-tenant purity. Checked at enqueue
    AND at drain-time re-check (design §2 point 3 / §3 step 7)."""

    class Reason(models.TextChoices):
        HARD_BOUNCE = "hard_bounce", "Hard bounce"
        COMPLAINT = "complaint", "Complaint"
        MANUAL = "manual", "Manual"

    id = models.BigAutoField(primary_key=True)
    email_normalized = models.CharField(max_length=254, unique=True)
    reason = models.CharField(max_length=16, choices=Reason.choices)
    source_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="suppression_entries_caused",
    )
    created_at = models.DateTimeField(db_default=Now(), editable=False)
    # hard_bounce: 90d TTL; manual/complaint: NULL (permanent) per design §1.
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["email_normalized"], name="suppression_email_idx"),
        ]

    def __str__(self) -> str:
        return self.email_normalized


class ApprovedRecipient(PublicIdModel):
    "Public API."

    public_id_prefix = "apr_"

    id = models.BigAutoField(primary_key=True)
    public_id = models.CharField(max_length=32, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="approved_recipients",
    )
    email_normalized = models.CharField(max_length=254)
    created_at = models.DateTimeField(db_default=Now(), editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approved_recipients_created",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "email_normalized"],
                name="approved_recipient_user_email_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "-created_at", "-id"], name="apr_owner_created_idx"),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding:
            self.public_id = ""
            for _ in range(10):
                self._set_available_public_id()
                try:
                    with transaction.atomic():
                        return super().save(*args, **kwargs)
                except IntegrityError as exc:
                    constraint = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "")
                    expected = f"{self._meta.db_table}_public_id_key"
                    if constraint != expected:
                        raise
                    self.public_id = ""
                    self.pk = None
            raise RuntimeError("Unable to save a unique ApprovedRecipient public_id")
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.email_normalized} (user={self.user_id})"


class BounceEvent(models.Model):
    "Public API."

    class ParseStatus(models.TextChoices):
        HARD = "hard", "Hard bounce (5.x.x)"
        SOFT = "soft", "Soft/transient bounce (4.x.x)"
        UNPARSED = "unparsed", "Unparseable / not a recognized DSN"

    id = models.BigAutoField(primary_key=True)
    envelope_recipient = models.CharField(max_length=320)
    # The obe_<public_id> localpart, VERBATIM (never lowercased — public_id
    # is generated by secrets.token_urlsafe and can be mixed-case). Blank is
    # never expected in practice (recognition requires the obe_ prefix) but
    # is not DB-constrained away, matching the other blank=True audit fields
    # on OutboundEmail.
    outbound_public_id = models.CharField(max_length=32, blank=True)
    parse_status = models.CharField(max_length=16, choices=ParseStatus.choices)
    raw_bytes = models.BinaryField()
    raw_sha256 = models.CharField(max_length=64)
    # Set the moment we act on this row (hard -> record_hard_bounce called;
    # soft -> the row itself is "the record"). NULL means "not yet acted on"
    # — the unparsed/foreign replay queue.
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now(), editable=False)

    class Meta:
        indexes = [
            models.Index(fields=["outbound_public_id"], name="bounce_event_obe_idx"),
            models.Index(
                fields=["processed_at"],
                condition=models.Q(processed_at__isnull=True),
                name="bounce_event_unprocessed_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.envelope_recipient} ({self.parse_status})"
