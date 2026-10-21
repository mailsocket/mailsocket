"""Service layer for outbound send: the sending-domain invariant (checked
3x per design §2), suppression, DKIM key provisioning, and enqueue.

THE INVARIANT (design §2, pasted verbatim in the build report): from_address
= local@domain sends ONLY if a Domain row exists with name == domain (exact,
lowercased, label-boundary) AND user_id == tenant AND status == VERIFIED AND
dkim_status == VERIFIED. Belt-and-braces denylist: MAIL_DOMAIN +
RESERVED_APEX_SUFFIXES, checked even though a Domain row can never actually
be one of those (Domain._validate_not_reserved_apex already blocks it at
create time) — defense in depth against a bugged/monkeypatched Domain
lookup. ALL FOUR real causes (not-found/not-owned/unverified/dkim-unverified)
plus the denylist collapse to ONE uniform failure — no oracle.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.entitlements import can_send_outbound
from apps.accounts.services import increment_rate_limit, normalize_email

from .dkim_keys import encrypt_private_key, generate_keypair
from .models import ApprovedRecipient, OutboundEmail, SuppressionEntry
from .validation import (
    AddressFormatError,
    parse_from_address,
    validate_plain_text_field,
    validate_recipient_address,
    validate_threading_field,
)

logger = logging.getLogger(__name__)


class SendingDomainNotEligible(Exception):
    """Raised by check_sending_domain_invariant on ANY of the four causes
    (not-found / not-owned / unverified / dkim-unverified) OR the denylist —
    all mapped to the SAME 403 sending_domain_not_eligible by the view/drain,
    with no distinguishing detail (design §2 point 1 / §6 no-oracle)."""


class RecipientRejected(Exception):
    """Suppressed recipient(s) at enqueue (design §3 step 7) -> 422."""

    def __init__(self, count: int):
        self.count = count
        super().__init__(f"{count} suppressed recipient(s)")


class RecipientNotApproved(Exception):
    "Public API."


class OutboundQuotaExceeded(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class OutboundVelocityExceeded(Exception):
    def __init__(self, reset_at):
        self.reset_at = reset_at
        super().__init__("velocity exceeded")


def _is_denylisted_domain(domain: str) -> bool:
    """Belt-and-braces explicit denylist (design §2 point 1): MAIL_DOMAIN +
    RESERVED_APEX_SUFFIXES, at a label boundary — mirrors
    Domain._validate_not_reserved_apex exactly. A real Domain row can never
    be one of these (create-time refusal), so this branch is defensive-only,
    but it is checked FIRST and unconditionally so a bugged/monkeypatched
    Domain lookup downstream can never make the invariant pass for our own
    infra domains (design §7 explicitly tests this "even via the no-Domain-
    row AND the denylist with the Domain lookup monkeypatched to pass" case).
    """
    from apps.domains.models import RESERVED_APEX_SUFFIXES

    mail_domain = settings.MAIL_DOMAIN.lower().rstrip(".")
    if domain == mail_domain or domain.endswith("." + mail_domain):
        return True
    for apex in RESERVED_APEX_SUFFIXES:
        if domain == apex or domain.endswith("." + apex):
            return True
    return False


def check_sending_domain_invariant(user, from_address: str):
    """THE 3-POINT SENDING-DOMAIN INVARIANT — enforcement point #1 (enqueue
    serializer/view). Points #2 (DB assert) and #3 (drain re-check) are in
    OutboundEmail.save-adjacent enqueue code below and apps.outbound.engine
    respectively — see the build report for the pasted 3-point excerpt.

    Returns the eligible Domain row on success. Raises SendingDomainNotEligible
    (uniform, no detail) on ANY failure — never distinguishes cause.
    """
    try:
        localpart, domain = parse_from_address(from_address)
    except AddressFormatError as exc:
        raise exc  # 422, NOT the 403 invariant failure — malformed shape

    if _is_denylisted_domain(domain):
        raise SendingDomainNotEligible()

    from apps.domains.models import Domain

    domain_row = Domain.objects.filter(name=domain, user=user).first()
    if (
        domain_row is None
        or domain_row.status != Domain.Status.VERIFIED
        or domain_row.dkim_status != Domain.DkimStatus.VERIFIED
    ):
        raise SendingDomainNotEligible()
    return domain_row


def check_sending_domain_invariant_at_drain(outbound_email) -> tuple[bool, str]:
    "Public API."
    from apps.accounts.models import Account, AccountPlan
    from apps.domains.models import Domain

    if outbound_email.domain_id is None:
        return False, "domain_no_longer_eligible"

    try:
        localpart, domain_name = parse_from_address(outbound_email.from_address)
    except AddressFormatError:
        return False, "domain_no_longer_eligible"

    if _is_denylisted_domain(domain_name):
        return False, "domain_no_longer_eligible"

    domain_row = Domain.objects.filter(
        pk=outbound_email.domain_id, name=domain_name, user_id=outbound_email.user_id
    ).first()
    if (
        domain_row is None
        or domain_row.status != Domain.Status.VERIFIED
        or domain_row.dkim_status != Domain.DkimStatus.VERIFIED
    ):
        return False, "domain_no_longer_eligible"

    try:
        account = Account.objects.get(pk=outbound_email.user_id)
    except Account.DoesNotExist:
        return False, "domain_no_longer_eligible"
    if account.is_suspended or account.outbound_paused_at is not None:
        return False, "domain_no_longer_eligible"

    try:
        plan = AccountPlan.objects.get(user_id=outbound_email.user_id)
    except AccountPlan.DoesNotExist:
        return False, "domain_no_longer_eligible"
    if plan.status != AccountPlan.Status.ACTIVE:
        return False, "domain_no_longer_eligible"

    from django.contrib.auth import get_user_model

    user_row = get_user_model().objects.filter(pk=outbound_email.user_id).values_list("is_active", flat=True).first()
    if not user_row:
        return False, "domain_no_longer_eligible"

    return True, ""


# --- Suppression --------------------------------------------------------


def _suppression_keys(email_address: str) -> set[str]:
    "Public API."
    from email.utils import getaddresses

    keys = {_normalize_email_or_fallback(email_address)}
    for _name, addr in getaddresses([email_address]):
        if addr:
            keys.add(_normalize_email_or_fallback(addr))
    return keys


def is_suppressed(email_address: str) -> bool:
    from django.db.models import Q

    now = timezone.now()
    return SuppressionEntry.objects.filter(
        email_normalized__in=_suppression_keys(email_address)
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now)).exists()


def _normalize_email_or_fallback(email_address: str) -> str:
    "Public API."
    try:
        return normalize_email(email_address)
    except Exception:
        return email_address.strip().lower()


def suppress(email_address: str, *, reason: str, source_user=None, ttl_days: int | None = None):
    try:
        normalized = normalize_email(email_address)
    except Exception:
        normalized = email_address.strip().lower()
    expires_at = timezone.now() + timedelta(days=ttl_days) if ttl_days else None
    SuppressionEntry.objects.update_or_create(
        email_normalized=normalized,
        defaults={
            "reason": reason,
            "source_user": source_user,
            "expires_at": expires_at,
        },
    )


# --- Enqueue --------------------------------------------------------------


def enqueue_outbound_email(
    *,
    user,
    created_by,
    from_address: str,
    to: list[str],
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    reply_to: str = "",
    subject: str = "",
    body_text: str = "",
    body_html: str = "",
    in_reply_to: str = "",
    references: str = "",
    inbox=None,
) -> OutboundEmail:
    "Public API."
    cc = cc or []
    bcc = bcc or []
    all_recipients = [*to, *cc, *bcc]
    recipient_count = len(all_recipients)
    if recipient_count == 0 or recipient_count > settings.OUTBOUND_MAX_RECIPIENTS:
        raise AddressFormatError()
    for addr in all_recipients:
        validate_recipient_address(addr)
    if reply_to:
        validate_recipient_address(reply_to)
    validate_plain_text_field(subject, max_length=512)
    body_bytes = len(body_text.encode("utf-8")) + len(body_html.encode("utf-8"))
    if body_bytes > settings.OUTBOUND_MAX_BODY_BYTES:
        raise AddressFormatError()
    if has_control_chars_any(body_text) or has_control_chars_any(body_html):
        # Bodies themselves are not header-injection vectors (they are never
        # placed in a header), but reject non-printable control chars other
        # than \n as defense-in-depth consistent with "EVERY user field".
        raise AddressFormatError()
    
    # params INSIDE the gate — not merely at the view. Empty string means
    # "no threading", never checked.
    if in_reply_to:
        validate_threading_field(in_reply_to)
    if references:
        validate_threading_field(references, is_references=True)

    # Gate #6 (design §3 step 6): sending-domain invariant, enforcement
    # point #1.
    domain_row = check_sending_domain_invariant(user, from_address)

    # Gate #7: suppression re-check at enqueue.
    suppressed = [addr for addr in all_recipients if is_suppressed(addr)]
    if suppressed:
        raise RecipientRejected(len(suppressed))

    
    # fail-closed membership test, keyed to the ACCOUNT flag ONLY (never the
    # tier — a billing lapse must never silently reopen sending). Outside the
    # quota atomic block below (gate #9), holds no locks, consumes no
    # rate-limit tick on rejection — checked AFTER suppression (the global
    # safety signal keeps precedence: suppressed AND not-approved still
    # collapses to recipient_rejected) but BEFORE velocity/quota.
    from apps.accounts.models import Account

    if Account.objects.filter(pk=user.pk, outbound_allowlist_enabled=True).exists():
        wanted = {_normalize_email_or_fallback(addr) for addr in all_recipients}
        # PURE membership test — NO "empty allowlist -> allow all" branch.
        # An empty allowlist means approved_count is always 0, and
        # 0 < len(wanted) is True for every wanted set (recipient_count >= 1
        # is already enforced above), so mode ON + empty list rejects every
        # send (design §3 "Fail-closed proof" — load-bearing, test 4).
        approved_count = ApprovedRecipient.objects.filter(
            user=user, email_normalized__in=wanted
        ).count()
        if approved_count < len(wanted):
            raise RecipientNotApproved()

    # Gate #8: velocity pre-gate + queue-depth guard.
    from apps.accounts.services import RateLimitExceeded

    try:
        increment_rate_limit(
            "outbound:enqueue:account:hour",
            str(user.pk),
            limit=int(1_000_000),  # per-account VOLUME cap is the plan quota
            # below; this bucket exists purely as the "velocity pre-gate"
            # named in the design and is deliberately generous — the
            # load-bearing check is the atomic tier quota (gate #9).
            window_seconds=3600,
        )
    except RateLimitExceeded as exc:
        raise OutboundVelocityExceeded(exc.reset_at) from exc

    queue_depth = OutboundEmail.objects.filter(
        user=user, status__in=(OutboundEmail.Status.QUEUED, OutboundEmail.Status.SENDING)
    ).count()
    if queue_depth >= settings.OUTBOUND_MAX_QUEUE_DEPTH:
        raise OutboundVelocityExceeded(timezone.now() + timedelta(minutes=1))

    # Gate #9: THE ATOMIC QUOTA GATE — inside the SAME transaction as create.
    with transaction.atomic():
        ok, reason = can_send_outbound(user, recipient_count)
        if not ok:
            raise OutboundQuotaExceeded(reason)

        # Enforcement point #2 (DB assert, design §2): from_address MUST end
        # with "@" + domain_row.name before save — defense-in-depth even
        # though check_sending_domain_invariant already proved this.
        localpart, parsed_domain = parse_from_address(from_address)
        assert from_address.endswith("@" + domain_row.name), "invariant point #2 violated"
        assert parsed_domain == domain_row.name

        outbound_email = OutboundEmail.objects.create(
            user=user,
            created_by=created_by or user,
            domain=domain_row,
            dkim_domain=domain_row.name,
            from_address=from_address,
            to=to,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            in_reply_to=in_reply_to or None,
            references=references or None,
            inbox=inbox,
            status=OutboundEmail.Status.QUEUED,
            recipient_count=recipient_count,
            queued_at=timezone.now(),
            next_attempt_at=timezone.now(),
        )
    return outbound_email


def has_control_chars_any(value: str) -> bool:
    if not value:
        return False
    return any(ord(ch) < 32 and ch not in ("\n",) for ch in value.replace("\r\n", "\n"))


# --- DKIM key provisioning (design §2) ------------------------------------


class SendingSetupRefused(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def provision_sending_setup(domain):
    """POST /api/v1/domains/{id}/sending_setup (design §2/§3). Generates a
    fresh RSA-2048 keypair, encrypts the private key with
    OUTBOUND_DKIM_MASTER_KEY, stores it + the TXT value on the Domain row,
    and (re)sets dkim_status to pending_dns — verification (the TXT-presence
    check) happens on the next verify/recheck pass, NOT here.
    """
    from apps.domains.models import Domain

    if domain.status != Domain.Status.VERIFIED:
        raise SendingSetupRefused("domain_not_verified")

    private_pem, txt_value = generate_keypair()
    encrypted = encrypt_private_key(private_pem)
    domain.dkim_selector = settings.OUTBOUND_DKIM_SELECTOR
    domain.dkim_private_key_enc = encrypted
    domain.dkim_public_key_txt = txt_value
    domain.dkim_status = Domain.DkimStatus.PENDING_DNS
    domain.dkim_verified_at = None
    domain.dkim_consecutive_failures = 0
    domain.save(
        update_fields=[
            "dkim_selector",
            "dkim_private_key_enc",
            "dkim_public_key_txt",
            "dkim_status",
            "dkim_verified_at",
            "dkim_consecutive_failures",
            "updated_at",
        ]
    )
    return domain





def maybe_auto_pause(user_id) -> bool:
    """Recompute ``user_id``'s rolling bounce ratio over its last
    OUTBOUND_AUTO_PAUSE_WINDOW SENT-or-later-status rows (sent, bounced —
    a row currently queued/sending/failed/rejected never sent, so it is
    excluded from the ratio entirely) and set Account.outbound_paused_at
    when the ratio exceeds OUTBOUND_AUTO_PAUSE_BOUNCE_RATIO. Idempotent;
    returns True iff the account is (now, or already was) paused.

    Called from the bounce reconciler right after a hard-bounce upsert
    (design §5.5 "cheap to build — it's a query over rows we already have").
    """
    from apps.accounts.models import Account

    window = OutboundEmail.objects.filter(
        user_id=user_id,
        status__in=(OutboundEmail.Status.SENT, OutboundEmail.Status.BOUNCED),
    ).order_by("-created_at", "-id")[: settings.OUTBOUND_AUTO_PAUSE_WINDOW]
    rows = list(window)
    if not rows:
        return False
    bounced = sum(1 for row in rows if row.status == OutboundEmail.Status.BOUNCED)
    ratio = bounced / len(rows)
    if ratio > settings.OUTBOUND_AUTO_PAUSE_BOUNCE_RATIO:
        Account.objects.filter(user_id=user_id, outbound_paused_at__isnull=True).update(
            outbound_paused_at=timezone.now()
        )
        return True
    return Account.objects.filter(user_id=user_id, outbound_paused_at__isnull=False).exists()







# (InboxMessageReplyView/InboxMessageForwardView/InboxComposeView,
# apps/api/views.py) were BearerAPIView bodies with the send orchestration
# inlined. A session-authed dashboard form cannot POST to those (401 + CSP).
# These three functions are that orchestration, extracted verbatim (same
# quote/subject/threading construction, same enqueue_outbound_email call,
# same exception set) so the API views become serializer->service->response
# adapters and the dashboard views become gate->form-parse->service->PRG
# adapters — ONE choke point, two thin doors. from_address=inbox.address is
# ALWAYS set here, never accepted from a caller-supplied parameter.


def reply_to_message(
    user,
    created_by,
    inbox,
    message,
    *,
    body_text: str = "",
    body_html: str = "",
    reply_all: bool = False,
    cc: list[str] | None = None,
) -> OutboundEmail:
    "Public API."
    from apps.outbound.reply_forward import (
        build_reply_quote,
        build_subject,
        reply_all_extra_recipients,
        resolve_reply_target,
        resolve_threading,
        sanitize_body,
    )

    reply_target = resolve_reply_target(message)  # raises ReplyTargetInvalid

    caller_cc = list(cc or [])
    if reply_all:
        to = [reply_target] + reply_all_extra_recipients(
            message, inbox_address=inbox.address, reply_target=reply_target
        ) + caller_cc
        cc_final: list[str] = []
    else:
        to = [reply_target]
        cc_final = caller_cc

    subject = build_subject(message.subject, prefix="Re")
    in_reply_to, references = resolve_threading(message)

    body_text = sanitize_body(body_text or "")
    body_html = sanitize_body(body_html or "")
    joiner_bytes = 2 if body_text else 0
    user_bytes = len(body_text.encode("utf-8")) + len(body_html.encode("utf-8")) + joiner_bytes
    budget = max(0, settings.OUTBOUND_MAX_BODY_BYTES - user_bytes)
    quote_text, quote_html = build_reply_quote(message, budget_bytes=budget)
    if quote_text:
        body_text = f"{body_text}\n\n{quote_text}" if body_text else quote_text
    if quote_html:
        body_html = f"{body_html}{quote_html}" if body_html else quote_html

    return enqueue_outbound_email(
        user=user,
        created_by=created_by,
        from_address=inbox.address,
        to=to,
        cc=cc_final,
        bcc=[],
        reply_to="",
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        in_reply_to=in_reply_to,
        references=references,
        inbox=inbox,
    )


def forward_message(
    user,
    created_by,
    inbox,
    message,
    *,
    to: list[str],
    body_text: str = "",
    body_html: str = "",
) -> OutboundEmail:
    "Public API."
    from apps.outbound.reply_forward import build_forward_quote, build_subject, sanitize_body
    from apps.outbound.validation import MSGID_RE

    subject = build_subject(message.subject, prefix="Fwd")
    orig_msgid = (message.provider_message_id or "").strip()
    references = orig_msgid if MSGID_RE.fullmatch(orig_msgid) else ""

    body_text = sanitize_body(body_text or "")
    body_html = sanitize_body(body_html or "")
    joiner_bytes = 2 if body_text else 0
    user_bytes = len(body_text.encode("utf-8")) + len(body_html.encode("utf-8")) + joiner_bytes
    budget = max(0, settings.OUTBOUND_MAX_BODY_BYTES - user_bytes)
    quote_text, quote_html = build_forward_quote(message, budget_bytes=budget)
    body_text = f"{body_text}\n\n{quote_text}" if body_text else quote_text
    if quote_html:
        body_html = f"{body_html}{quote_html}" if body_html else quote_html

    return enqueue_outbound_email(
        user=user,
        created_by=created_by,
        from_address=inbox.address,
        to=list(to),
        cc=[],
        bcc=[],
        reply_to="",
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        in_reply_to="",
        references=references,
        inbox=inbox,
    )


def compose_from_inbox(
    user,
    created_by,
    inbox,
    *,
    to: list[str],
    subject: str = "",
    body_text: str = "",
    body_html: str = "",
    cc: list[str] | None = None,
) -> OutboundEmail:
    "Public API."
    return enqueue_outbound_email(
        user=user,
        created_by=created_by,
        from_address=inbox.address,
        to=list(to),
        cc=list(cc or []),
        bcc=[],
        reply_to="",
        subject=subject or "",
        body_text=body_text or "",
        body_html=body_html or "",
        in_reply_to="",
        references="",
        inbox=inbox,
    )


def record_hard_bounce(outbound_public_id: str, *, final_recipients: list[str] | None = None) -> None:
    """Called by the ingest bounce reconciler (obe_ localpart on
    OUTBOUND_BOUNCE_DOMAIN, MAC-verified, DSN-parsed hard bounce, 5.x.x).
    Transitions sent -> bounced always, then re-evaluates the auto-pause
    circuit breaker for that tenant.

    D2 Part B (): with a single shared envelope
    sender, a MAC can't be bound to a specific recipient — any To/Cc/Bcc
    recipient sees the same valid Return-Path. So GLOBAL suppression (which
    affects every tenant) is only applied when it can be scoped to "who
    could plausibly have forged this":
      - the row has exactly one recipient (to+cc+bcc combined), AND
      - if the DSN carries a Final-Recipient, it normalizes to that same
        address (an absent Final-Recipient does not block suppression —
        many real DSNs omit it).
    A multi-recipient row is still marked BOUNCED (tenant-local effect via
    maybe_auto_pause) but is NEVER globally suppressed — logged for ops via
    bounce_multi_rcpt_unsuppressed.
    """
    row = OutboundEmail.objects.filter(
        public_id=outbound_public_id, status=OutboundEmail.Status.SENT
    ).first()
    if row is None:
        return
    OutboundEmail.objects.filter(pk=row.pk, status=OutboundEmail.Status.SENT).update(
        status=OutboundEmail.Status.BOUNCED, bounced_at=timezone.now()
    )
    recipients = [*row.to, *row.cc, *row.bcc]
    if len(recipients) == 1:
        target = recipients[0]
        final_recipients = final_recipients or []
        mismatch = False
        if final_recipients:
            try:
                target_normalized = normalize_email(target)
            except Exception:
                target_normalized = target.strip().lower()
            normalized_finals = set()
            for addr in final_recipients:
                try:
                    normalized_finals.add(normalize_email(addr))
                except Exception:
                    normalized_finals.add(addr.strip().lower())
            mismatch = target_normalized not in normalized_finals
        if not mismatch:
            suppress(target, reason=SuppressionEntry.Reason.HARD_BOUNCE, ttl_days=90)
    elif recipients:
        logger.info(
            "bounce_multi_rcpt_unsuppressed outbound_id=%s recipient_count=%s",
            row.public_id, len(recipients),
        )
    maybe_auto_pause(row.user_id)
