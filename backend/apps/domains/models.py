"Public API."
import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models.functions import Lower, Now

from apps.inboxes.models import PublicIdModel

# FQDN validation (): labels 1-63 chars, 1+ labels, total
# length is separately capped by max_length=253 on the field. No leading or
# trailing dot, no bare single-label TLD (must have at least one dot, i.e.
# at least two labels) — a customer domain is always "sub.tld" shaped at
# minimum.
DOMAIN_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
DOMAIN_NAME_PATTERN = rf"^{DOMAIN_LABEL}(\.{DOMAIN_LABEL})+$"
DOMAIN_NAME_RE_VALIDATOR = RegexValidator(
    DOMAIN_NAME_PATTERN,
    message="Enter a valid fully-qualified domain name (e.g. otp.example.com).",
)

# Customers may not BYOD a subdomain of our own shared infra — it would
# collide with the shared routing apex and (for mailsocket.app) our own
# work-mail. Checked case-insensitively against the lowercase-normalized name.

# ".inboxpipe.net" / ".mailsocket.app" suffixes and matched with raw
# `str.endswith(suffix)`, which is a substring check, not a label-boundary
# check — it wrongly rejected any lookalike domain that merely ends with
# those characters (e.g. "notinboxpipe.net", "evilmailsocket.app"), which a
# Business customer could genuinely own. The label-boundary comparison in
# _validate_not_reserved_apex below (exact-match OR ".<apex>" suffix) is what
# correctly restricts this to the apex itself and its real subdomains.
RESERVED_APEX_SUFFIXES = (
    "inboxpipe.net",
    "mailsocket.app",
)


def _new_verification_token() -> str:
    return secrets.token_urlsafe(32)


class Domain(PublicIdModel):
    """A customer-owned FQDN pending/verified for BYOD inbound mail.

    STAGE A: purely additive. Reaching status=VERIFIED here does not cause
    any mail to route to it — Postfix keeps accepting only settings.MAIL_DOMAIN
    (see deploy/postfix/, untouched by this app) until Stage B flips
    ENABLE_BYOD's mail-path wiring.
    """

    public_id_prefix = "dom_"

    class Status(models.TextChoices):
        PENDING_DNS = "pending_dns", "Pending DNS"
        VERIFIED = "verified", "Verified"
        FAILED = "failed", "Failed"
        DISABLED = "disabled", "Disabled"

    id = models.BigAutoField(primary_key=True)
    public_id = models.CharField(max_length=32, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="domains",
    )
    # FQDN, stored lowercase. Global-unique: a domain is owned by exactly one
    # account at a time () — the tenant-binding invariant.
    # IMMUTABLE after create (see save() below); rename = delete + recreate.
    name = models.CharField(
        max_length=253,
        unique=True,
        validators=[DOMAIN_NAME_RE_VALIDATOR],
    )
    # status transitions are SERVICE-layer only (apps.domains.services), never
    # a raw form/serializer field — see verification.py for the state machine.
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING_DNS,
    )
    verification_token = models.CharField(
        max_length=64, editable=False, default=_new_verification_token
    )
    # Cached display value ("_mailsocket-verify.<name>"); derived, kept in
    # sync with `name` at create time by save() below (name is immutable, so
    # this can never drift after creation).
    verification_txt_host = models.CharField(max_length=253, editable=False, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True)
    
    # VERIFIED-domain recheck (apps.domains.services.recheck_domain). Additive
    # field, default 0. Reset to 0 on any successful recheck; incremented on
    # failure; a previously-VERIFIED domain is demoted to failed only once
    # this reaches VERIFY_DEMOTE_STRIKE_COUNT (3) CONSECUTIVE failures. Lives
    # on the row (not an in-process dict) so a strike survives a worker
    # restart and is deterministically testable.
    consecutive_failures = models.IntegerField(default=0)
    verified_at = models.DateTimeField(null=True, blank=True)
    disabled_at = models.DateTimeField(null=True, blank=True)
    
    
    # default False/NULL — no backfill needed. catch_all_inbox is PROTECT
    # (never silently orphan a live routing target) and must point at a
    # sentinel Inbox owned by the SAME user, bound to THIS domain, live
    # (is_enabled, deleted_at NULL) — enforced in apps.domains.services, not
    # here (mirrors the "status transitions are service-layer only" pattern
    # above). The CheckConstraint below is the DB-level half of that
    # invariant: catch_all=True can never be saved without a target.
    catch_all = models.BooleanField(default=False)
    catch_all_inbox = models.ForeignKey(
        "inboxes.Inbox",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="catch_all_for",
    )
    created_at = models.DateTimeField(db_default=Now(), editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    
    
    # backfill needed — Stage-A-style inert until settings.ENABLE_OUTBOUND
    # flips (apps.api.views gate stack). Per-domain DKIM signing keypair for
    # customer outbound send, entirely separate from the static opendkim
    # milter (deploy/milter/*, untouched by this feature).
    class DkimStatus(models.TextChoices):
        NONE = "none", "None"
        PENDING_DNS = "pending_dns", "Pending DNS"
        VERIFIED = "verified", "Verified"
        FAILED = "failed", "Failed"

    dkim_selector = models.CharField(max_length=63, default="msout1")
    # Private key, RSA-2048, encrypted at rest with OUTBOUND_DKIM_MASTER_KEY
    # (mirrors WEBHOOK_SIGNING_MASTER_KEY required-env discipline). NEVER
    # stored or logged in plaintext; see apps.outbound.dkim_keys.
    dkim_private_key_enc = models.BinaryField(null=True, blank=True)
    # Exact TXT value the customer must publish at
    # <dkim_selector>._domainkey.<name> — msout1._domainkey.<domain>.
    dkim_public_key_txt = models.CharField(max_length=1024, blank=True)
    dkim_status = models.CharField(
        max_length=16, choices=DkimStatus.choices, default=DkimStatus.NONE
    )
    dkim_verified_at = models.DateTimeField(null=True, blank=True)
    # Reuses the 3-strike demote pattern (consecutive_failures / services.py
    # recheck_domain) — a dedicated counter so a DKIM-TXT blip and an
    # MX/verify-TXT blip demote independently.
    dkim_consecutive_failures = models.IntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(name=Lower("name")),
                name="domain_name_lower",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=("pending_dns", "verified", "failed", "disabled")
                ),
                name="domain_status_valid",
            ),
            
            # Defense-in-depth alongside the recipient-map SQL's own
            # `catch_all IS TRUE` + sentinel-join gate (deploy/postfix/
            # recipient-map.cf.template) — this makes the invalid state
            # unrepresentable at the DB layer, not just unreachable via the
            # service layer.
            models.CheckConstraint(
                condition=models.Q(catch_all=False) | models.Q(catch_all_inbox__isnull=False),
                name="domain_catch_all_requires_inbox",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    dkim_status__in=("none", "pending_dns", "verified", "failed")
                ),
                name="domain_dkim_status_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["user"], name="domain_owner_idx"),
            # Hot path for the Stage-B Postfix relay_domains query.
            models.Index(
                fields=["name"],
                condition=models.Q(status="verified"),
                name="domain_verified_name_idx",
            ),
        ]

    def _validate_not_reserved_apex(self) -> None:
        lowered = self.name.lower().rstrip(".")
        for apex in RESERVED_APEX_SUFFIXES:
            if lowered == apex or lowered.endswith("." + apex):
                raise ValidationError(
                    {"name": f"Domains under {apex} cannot be added as a custom domain."}
                )

    def save(self, *args, **kwargs):
        if self._state.adding:
            self.name = self.name.lower()
            self._validate_not_reserved_apex()
            self.verification_txt_host = f"_mailsocket-verify.{self.name}"
        else:
            original = type(self).objects.only("name").get(pk=self.pk)
            if self.name != original.name:
                raise ValidationError({"name": "Domain name is immutable."})
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name
