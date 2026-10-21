"""Outbound drain engine (design §4): clones webhooks/engine.py claim_batch
(select_for_update(skip_locked) + lease token + claim-one-send-one — "leases
must not expire behind slow I/O", engine.py:205-218) and adds THE GLOBAL
FLEET CAP (design §4 "lives HERE, not at enqueue") plus enforcement point #3
of the sending-domain invariant, suppression re-check, auto-pause rejection,
and per-domain DKIM signing.
"""
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.services import RateLimitExceeded, increment_rate_limit

from .dkim_sign import DkimSigningError, sign_message
from .message_builder import build_raw_message
from .models import OutboundEmail
from .services import (
    _normalize_email_or_fallback,
    check_sending_domain_invariant_at_drain,
    is_suppressed,
)
from .transport import SmtpTransportError, send_via_postfix
from .verp import build_verp_localpart

logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = 1000

# THE GLOBAL DRAIN CAP: a single named RateLimitBucket, checked/consumed
# ONCE PER CLAIMED SEND (design §4 "budget = min(batch_size, remaining of
# outbound:global:hour limit=OUTBOUND_GLOBAL_HOURLY_CAP)"). Exhausted ->
# drain claims (and therefore sends) zero more rows this pass; unclaimed
# rows stay queued for the next pass.
GLOBAL_BUCKET_NAME = "outbound:global:hour"


@dataclass(frozen=True)
class DrainResult:
    claimed: int
    sent: int
    retrying: int
    failed: int
    rejected: int
    fenced: int
    global_cap_exhausted: bool


def _global_budget_available() -> bool:
    """One-shot check: does the global bucket have room for ONE more send
    this hour? Uses increment_rate_limit's own atomic UPSERT (the same
    primitive every other bucket in the codebase uses) — a successful call
    IS the reservation (consumes one slot); RateLimitExceeded means the
    fleet cap is exhausted for this window."""
    try:
        increment_rate_limit(
            GLOBAL_BUCKET_NAME,
            "global",
            limit=int(settings.OUTBOUND_GLOBAL_HOURLY_CAP),
            window_seconds=3600,
        )
    except RateLimitExceeded:
        return False
    return True


def claim_one():
    """Claim exactly one due OutboundEmail row (design §4, webhooks/engine.py
    claim_batch pattern). Returns (pk, lease_token) or None. Does NOT check
    the global cap — callers must call _global_budget_available() first,
    OUTSIDE this transaction, so an exhausted global bucket never claims
    (and therefore never sends) another row this pass (design §7 "Global
    cap" test: "fleet bucket exhausted -> drain sends 0, rows stay queued")."""
    now = timezone.now()
    max_attempts = int(settings.OUTBOUND_MAX_ATTEMPTS)
    eligible = Q(
        status=OutboundEmail.Status.QUEUED, next_attempt_at__lte=now
    ) | Q(
        status=OutboundEmail.Status.QUEUED, next_attempt_at__isnull=True
    ) | Q(
        status=OutboundEmail.Status.SENDING, lease_expires_at__lte=now
    )
    with transaction.atomic():
        row = (
            OutboundEmail.objects.select_for_update(skip_locked=True)
            .filter(eligible)
            .order_by("created_at", "id")
            .first()
        )
        if row is None:
            return None
        if row.attempt_count >= max_attempts:
            OutboundEmail.objects.filter(pk=row.pk).update(
                status=OutboundEmail.Status.FAILED,
                next_attempt_at=None,
                lease_token=None,
                lease_expires_at=None,
                error_code="attempt_limit",
            )
            return None
        token = uuid.uuid4()
        claimed = OutboundEmail.objects.filter(
            pk=row.pk, status=row.status, lease_token=row.lease_token
        ).update(
            status=OutboundEmail.Status.SENDING,
            attempt_count=row.attempt_count + 1,
            next_attempt_at=None,
            lease_token=token,
            lease_expires_at=now + timedelta(seconds=int(settings.OUTBOUND_LEASE_SECONDS)),
            error_code="",
        )
        if not claimed:
            return None
        return row.pk, token


def _retry_delay(attempt_count: int) -> int:
    schedule = tuple(int(v) for v in settings.OUTBOUND_RETRY_SCHEDULE_SECONDS)
    base = schedule[min(attempt_count, len(schedule) - 1)]
    jitter = int(settings.OUTBOUND_RETRY_JITTER_SECONDS)
    return max(0, base + (secrets.randbelow(jitter * 2 + 1) - jitter if jitter else 0))


def _finalize(pk, token, *, outcome: str, error_code: str = "", provider_message_id: str = ""):
    """Lease-fenced finalize (mirrors webhooks/engine.py _finalize exactly):
    only a row still SENDING with THIS token can be finalized — a stale/
    expired lease that has already been reclaimed by another worker is
    "fenced" (no-op), which is what makes claim-one-send-one safe under
    concurrent drain workers."""
    now = timezone.now()
    with transaction.atomic():
        row = OutboundEmail.objects.select_for_update().filter(
            pk=pk, status=OutboundEmail.Status.SENDING, lease_token=token
        ).first()
        if row is None:
            return "fenced"
        if outcome == "sent":
            OutboundEmail.objects.filter(pk=pk, lease_token=token).update(
                status=OutboundEmail.Status.SENT,
                next_attempt_at=None,
                lease_token=None,
                lease_expires_at=None,
                sent_at=now,
                provider_message_id=provider_message_id,
                error_code="",
            )
            return "sent"
        if outcome == "rejected":
            OutboundEmail.objects.filter(pk=pk, lease_token=token).update(
                status=OutboundEmail.Status.REJECTED,
                next_attempt_at=None,
                lease_token=None,
                lease_expires_at=None,
                error_code=error_code[:64],
            )
            return "rejected"
        if outcome == "permanent" or row.attempt_count >= int(settings.OUTBOUND_MAX_ATTEMPTS):
            OutboundEmail.objects.filter(pk=pk, lease_token=token).update(
                status=OutboundEmail.Status.FAILED,
                next_attempt_at=None,
                lease_token=None,
                lease_expires_at=None,
                error_code=error_code[:64],
            )
            return "failed"
        # tempfail -> retry
        OutboundEmail.objects.filter(pk=pk, lease_token=token).update(
            status=OutboundEmail.Status.QUEUED,
            next_attempt_at=now + timedelta(seconds=_retry_delay(row.attempt_count)),
            lease_token=None,
            lease_expires_at=None,
            error_code=error_code[:64],
        )
        return "retrying"


def deliver_claim(pk, token, *, transport=send_via_postfix):
    row = OutboundEmail.objects.select_related("domain").filter(
        pk=pk,
        status=OutboundEmail.Status.SENDING,
        lease_token=token,
        lease_expires_at__gt=timezone.now(),
    ).first()
    if row is None:
        return "fenced"

    # --- Enforcement point #3 of the sending-domain invariant (design §2,
    
    # deleted, or an account suspended/paused, mid-queue can never emit
    # mail — this is checked BEFORE any DKIM decrypt or SMTP I/O.
    ok, error_code = check_sending_domain_invariant_at_drain(row)
    if not ok:
        return _finalize(pk, token, outcome="rejected", error_code=error_code)

    # --- Suppression re-check (design §3 step 7 re-run at send time, design
    # §7 "drain-time re-check catches an entry created after enqueue").
    all_recipients = [*row.to, *row.cc, *row.bcc]
    if any(is_suppressed(addr) for addr in all_recipients):
        return _finalize(pk, token, outcome="rejected", error_code="recipient_rejected")

    
    # re-check, immediately after the suppression re-check, BEFORE DKIM
    
    # discovers key compromise, deletes entries / flips mode ON while
    # attacker rows sit QUEUED) — same PURE membership test as enqueue gate
    # #7.5, no "empty -> allow" branch. Mode flipped OFF mid-queue makes this
    # inert (permissive direction is the tenant's explicit choice).
    from apps.accounts.models import Account

    from .models import ApprovedRecipient

    if Account.objects.filter(pk=row.user_id, outbound_allowlist_enabled=True).exists():
        wanted = {_normalize_email_or_fallback(addr) for addr in all_recipients}
        approved_count = ApprovedRecipient.objects.filter(
            user_id=row.user_id, email_normalized__in=wanted
        ).count()
        if approved_count < len(wanted):
            return _finalize(pk, token, outcome="rejected", error_code="recipient_not_approved")

    domain = row.domain
    if domain is None or not domain.dkim_private_key_enc:
        return _finalize(pk, token, outcome="rejected", error_code="domain_no_longer_eligible")

    raw_message, message_id = build_raw_message(row)
    try:
        signature = sign_message(
            raw_message,
            domain_name=domain.name,
            selector=domain.dkim_selector,
            private_key_enc=bytes(domain.dkim_private_key_enc),
        )
    except DkimSigningError:
        logger.exception("outbound_dkim_sign_failed outbound_id=%s", row.public_id)
        # Fail-closed (design §2 P0-3): a signing failure is NEVER sent
        # unsigned. Permanent — a signing failure on a verified key is a
        # code/config bug, not a transient condition worth retrying.
        return _finalize(pk, token, outcome="permanent", error_code="dkim_sign_failed")

    signed_message = signature + raw_message
    envelope_from = f"{build_verp_localpart(row.public_id)}@{settings.OUTBOUND_BOUNCE_DOMAIN}"  # VERP

    try:
        transport(
            envelope_from=envelope_from,
            envelope_to=all_recipients,
            raw_message=signed_message,
        )
    except SmtpTransportError as exc:
        result = _finalize(pk, token, outcome=exc.classification, error_code=exc.detail[:64])
        logger.info(
            "outbound_delivery outbound_id=%s attempt=%s outcome=%s error=%s",
            row.public_id, row.attempt_count, result, exc.detail,
        )
        return result

    result = _finalize(pk, token, outcome="sent", provider_message_id=message_id)
    logger.info(
        "outbound_delivery outbound_id=%s attempt=%s outcome=%s", row.public_id, row.attempt_count, result
    )
    return result


def drain(*, batch_size=None, transport=send_via_postfix) -> DrainResult:
    """One finite pass (design §4): claim-one-send-one, gated at EVERY claim
    by the global fleet cap. ENABLE_OUTBOUND off -> no-op (design build spec
    "even a stray cron invocation sends nothing")."""
    if not settings.ENABLE_OUTBOUND:
        return DrainResult(0, 0, 0, 0, 0, 0, global_cap_exhausted=False)

    batch_size = batch_size if batch_size is not None else int(settings.OUTBOUND_DRAIN_BATCH_SIZE)
    counts = {"sent": 0, "retrying": 0, "failed": 0, "rejected": 0, "fenced": 0}
    claimed = 0
    global_cap_exhausted = False
    for _ in range(batch_size):
        if not _global_budget_available():
            global_cap_exhausted = True
            break
        claim = claim_one()
        if claim is None:
            break
        pk, token = claim
        claimed += 1
        result = deliver_claim(pk, token, transport=transport)
        counts[result] += 1
        if result == "sent":
            _maybe_auto_pause_after_send(pk)
    return DrainResult(claimed=claimed, global_cap_exhausted=global_cap_exhausted, **counts)


def _maybe_auto_pause_after_send(pk):
    """Best-effort hook point for the auto-pause circuit breaker (design
    §5.5) — the bounce-driven recompute lives in services.maybe_auto_pause,
    called from the bounce reconciler (a sent message cannot itself trigger
    a bounce-ratio breach). Kept as a no-op call site here so drain() has an
    obvious extension point without needing a second cron job for V1."""
    return None
