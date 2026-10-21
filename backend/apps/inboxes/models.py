import re
import secrets
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import IntegrityError, models, transaction
from django.db.models.functions import Lower, Now

ROUTING_KEY_PATTERN = r"^[a-z0-9][a-z0-9_-]{2,63}$"
ROUTING_KEY_RE = re.compile(ROUTING_KEY_PATTERN)
LOWER_HEX_64_PATTERN = r"^[0-9a-f]{64}$"


def _new_public_id(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(18)


def _concrete_manager(instance: models.Model) -> "models.Manager[Any]":
    # PublicIdModel is abstract: the stubs know neither its `objects` nor the
    # `public_id` field each concrete subclass declares. At runtime this is the
    # subclass's default manager (`objects`; none of them defines another).
    return type(instance)._default_manager


class PublicIdModel(models.Model):
    public_id_prefix = ""

    class Meta:
        abstract = True

    def _set_available_public_id(self) -> None:
        for _ in range(10):
            candidate = _new_public_id(self.public_id_prefix)
            if not _concrete_manager(self).filter(public_id=candidate).exists():
                self.public_id = candidate
                return
        raise RuntimeError(f"Unable to generate a unique {type(self).__name__} public_id")

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
            raise RuntimeError(f"Unable to save a unique {type(self).__name__} public_id")

        original = type(self).objects.only("public_id").get(pk=self.pk)
        if self.public_id != original.public_id:
            raise ValidationError({"public_id": "Public ID is immutable."})
        return super().save(*args, **kwargs)


class Inbox(PublicIdModel):
    public_id_prefix = "inbox_"

    id = models.BigAutoField(primary_key=True)
    public_id = models.CharField(max_length=32, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
    )
    label = models.CharField(max_length=120, blank=True)
    routing_key = models.CharField(
        max_length=64,
        validators=[RegexValidator(ROUTING_KEY_PATTERN)],
    )
    #  (BYOD): NULL = shared in.inboxpipe.net (every inbox that
    # exists before/without this feature). Set = bound to a verified custom
    
    # §2 "THE HARD PART" for the full collision analysis: this FK is nullable
    # and additive, all existing rows are NULL, and the two partial unique
    # constraints below (NOT a single composite) are what keep shared-domain
    # uniqueness intact — a single UniqueConstraint(["domain","routing_key"])
    # would silently lose it via Postgres NULL-distinct semantics.
    #
    # STAGE A NEVER WRITES THIS FIELD (confirmed, QA r2 "also answer"): no
    # Stage-A CRUD path (apps.inboxes views/services, apps.api.views
    # InboxListCreateView, create_random_inbox, provision_first_inbox) ever
    # sets `domain` on create or update — every Stage-A-created inbox is
    # NULL-domain (shared). Binding an inbox to a custom domain is deferred
    # to Stage B's bind service, which does not exist yet. When it IS built,
    # it MUST enforce: (1) the target Domain is owned by the SAME user as the
    # Inbox being bound, and (2) the target Domain.status == VERIFIED — never
    # let an inbox bind to another tenant's domain or to a pending/failed one
    # (that would silently route someone else's custom-domain mail, or mail
    # for a domain that doesn't actually point at us, to this inbox).
    domain = models.ForeignKey(
        "domains.Domain",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="inboxes",
    )
    is_enabled = models.BooleanField(default=True)
    
    # flip every shared inbox readable — every pre-existing shared-domain
    # inbox holds OTP mail (live credentials). Read path
    # (apps.api.views.PublicInboxMessagesView) re-guards at READ time with
    # domain__isnull=True + is_public=True + MAIL_DOMAIN parse, so even a
    # DB-forced is_public=True on a BYOD inbox can never be read publicly.
    # Any FUTURE inbox-update surface (none exists today; create is the only
    # door — apps.api.views.InboxListCreateView.post) MUST re-apply the same
    # "public=True + custom domain -> 422 public_not_allowed_on_custom_domain"
    
    is_public = models.BooleanField(default=False, db_default=False)
    
    # Hard-capped at 4KB compact-UTF8 / 50 top-level keys by
    # InboxWriteSerializer.validate_metadata — the only write path. NEVER
    
    
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(db_default=Now(), editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(routing_key=Lower("routing_key")),
                name="inbox_routing_key_lower",
            ),
            models.CheckConstraint(
                condition=models.Q(routing_key__regex=ROUTING_KEY_PATTERN),
                name="inbox_routing_key_format",
            ),
            # THE HARD PART (risk #1, tenant collision): two partial uniques,
            # NOT one composite. Preserves today's exact global-unique
            # behaviour on the shared domain (#1) while allowing the same
            # routing_key to exist once per distinct custom domain (#2).
            models.UniqueConstraint(
                fields=["routing_key"],
                condition=models.Q(domain__isnull=True),
                name="inbox_shared_routing_key_uniq",
            ),
            models.UniqueConstraint(
                fields=["domain", "routing_key"],
                condition=models.Q(domain__isnull=False),
                name="inbox_custom_routing_key_uniq",
            ),
        ]
        indexes = [
            # Rebuilt composite (Stage A): covers the Stage-B recipient-map
            # join (domain, routing_key) instead of routing_key alone. Same
            # is_enabled/not-deleted condition as before.
            models.Index(
                fields=["domain", "routing_key"],
                condition=models.Q(is_enabled=True, deleted_at__isnull=True),
                name="inbox_active_route_idx",
            ),
            models.Index(
                fields=["user", "deleted_at", "-created_at", "-id"],
                name="inbox_owner_timeline_idx",
            ),
            # inbox_burst_per_account: last hour grouped by user (index-only).
            models.Index(fields=["created_at", "user"], name="inbox_created_user_idx"),
        ]

    @property
    def address(self) -> str:
        # : the ONLY place the inbox's domain is resolved to a
        # string — every downstream consumer (welcome mail, X-Envelope-To,
        # InboxSerializer.address) inherits this for free. STAGE A note: no
        # inbox can have domain_id set yet (ENABLE_BYOD-gated create path is
        # Stage B's concern; Stage A's CRUD is domain-only, not inbox-binding),
        # so in practice this always takes the MAIL_DOMAIN branch today — kept
        # generic so Stage B needs zero changes here.
        if self.domain_id:
            # domain_id set => the FK row exists (DB constraint, on_delete=PROTECT).
            assert self.domain is not None
            return f"{self.routing_key}@{self.domain.name}"
        return f"{self.routing_key}@{settings.MAIL_DOMAIN}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original = type(self).objects.only("routing_key").get(pk=self.pk)
            if self.routing_key != original.routing_key:
                raise ValidationError({"routing_key": "Routing key is immutable."})
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.address


class Message(PublicIdModel):
    public_id_prefix = "msg_"

    class IngestStatus(models.TextChoices):
        RECEIVED = "received", "Received"
        PARSING = "parsing", "Parsing"
        PARSED = "parsed", "Parsed"
        FAILED = "failed", "Failed"

    id = models.BigAutoField(primary_key=True)
    public_id = models.CharField(max_length=32, unique=True, editable=False)
    inbox = models.ForeignKey(Inbox, on_delete=models.PROTECT, related_name="messages")
    dedupe_key = models.CharField(max_length=64)
    provider_message_id = models.CharField(max_length=998, null=True, blank=True, db_index=True)
    postfix_queue_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    intake_id = models.CharField(max_length=128, null=True, blank=True)
    envelope_from = models.CharField(max_length=320, blank=True)
    envelope_recipient = models.CharField(max_length=320)
    header_from = models.CharField(max_length=998, blank=True)
    header_to = models.JSONField(default=list)
    header_cc = models.JSONField(default=list)
    subject = models.CharField(max_length=998, blank=True)
    text_body = models.TextField(blank=True)
    html_body = models.TextField(blank=True)
    otp = models.CharField(max_length=8, null=True, blank=True)
    otp_confidence = models.FloatField(null=True, blank=True)
    magic_link = models.TextField(null=True, blank=True)
    links = models.JSONField(default=list, null=True, blank=True)
    detected_otp = models.CharField(max_length=8, null=True, blank=True)
    raw_bytes = models.BinaryField()
    raw_sha256 = models.CharField(max_length=64, db_index=True)
    size_bytes = models.BigIntegerField()
    ingest_status = models.CharField(
        max_length=16,
        choices=IngestStatus.choices,
        default=IngestStatus.RECEIVED,
    )
    parse_attempts = models.IntegerField(default=0)
    parse_error = models.CharField(max_length=1000, blank=True)
    parse_started_at = models.DateTimeField(null=True, blank=True)
    parse_lease_token = models.UUIDField(null=True, blank=True)
    parse_lease_expires_at = models.DateTimeField(null=True, blank=True)
    next_parse_at = models.DateTimeField(null=True, blank=True)
    usage_counted = models.BooleanField(default=False)
    # Feature #1 (mark-read parity): NULL = unread, timestamp = read-at. No
    # backfill on migration — every pre-existing message is unread. Not
    # indexed in v1 (all queries already scope by inbox via
    # message_inbox_timeline_idx; read is a secondary filter). See
    
    read_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(db_default=Now(), editable=False)
    parsed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now(), editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["inbox", "dedupe_key"],
                name="message_inbox_dedupe_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(size_bytes__gte=0),
                name="message_size_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(parse_attempts__gte=0),
                name="message_attempts_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(raw_sha256__regex=LOWER_HEX_64_PATTERN),
                name="message_raw_sha256_format",
            ),
            models.CheckConstraint(
                condition=models.Q(dedupe_key__regex=LOWER_HEX_64_PATTERN),
                name="message_dedupe_key_format",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    ingest_status__in=("received", "parsing", "parsed", "failed")
                ),
                name="message_ingest_status_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=["inbox", "-received_at", "-id"],
                name="message_inbox_timeline_idx",
            ),
            models.Index(
                fields=["ingest_status", "next_parse_at", "received_at", "id"],
                name="message_reconcile_idx",
            ),
        ]

    def __str__(self) -> str:
        return self.public_id
