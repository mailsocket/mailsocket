import hashlib
import logging
import re
from dataclasses import dataclass
from email import policy
from email.parser import BytesHeaderParser
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.inboxes.models import ROUTING_KEY_RE, Inbox, Message

from .dsn import parse_dsn_status

if TYPE_CHECKING:
    from apps.outbound.models import BounceEvent

logger = logging.getLogger(__name__)
MESSAGE_ID_RE = re.compile(r"^<[^<>\s@]+@[^<>\s@]+>$")
MAX_HEADER_BYTES = 65536

# recognition. public_id is secrets.token_urlsafe (base64 urlsafe alphabet),
# so the localpart charset intentionally allows mixed case + '-'/'_' — this
# is NOT the shared-apex ROUTING_KEY_RE or the custom-domain
# CATCHALL_LOCALPART_RE, both of which are lowercase-only and would silently
# reject a real bounce localpart before it ever reached this check.
# D2 Part A (): the VERP bounce localpart now
# carries a keyed MAC after the public_id — obe_<public_id>.<20-hex-mac>.
# The legacy MAC-less obe_xxx shape is NOT accepted: prod has 0
# OutboundEmail rows and outbound was never on, so no legacy envelopes are
# in flight, and accepting an unauthenticated shape would defeat the point
# of the MAC.
BOUNCE_LOCALPART_RE = re.compile(r"^(obe_[A-Za-z0-9_-]{1,28})\.([0-9A-Fa-f]{20})$")


# permissive localpart charset for non-MAIL_DOMAIN recipients. MUST mirror
# the regex guard in the recipient-map.cf.template catch-all SQL leg
# (`'%u' ~ '^[a-z0-9][a-z0-9._+=-]{0,63}$'`) byte-for-byte — this is the

# other reopens the 5-day deferral-loop class described there. Only ever
# used for domain != settings.MAIL_DOMAIN; the shared domain keeps
# ROUTING_KEY_RE (strict) always.
CATCHALL_LOCALPART_PATTERN = r"^[a-z0-9][a-z0-9._+=-]{0,63}$"
CATCHALL_LOCALPART_RE = re.compile(CATCHALL_LOCALPART_PATTERN)


class IngestError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class StoreResult:
    message: Message | None
    deduplicated: bool
    # CODE FIX 0 (gate R2): set instead of ``message`` when the envelope
    # recipient was a recognized bounce (obe_<public_id>@OUTBOUND_BOUNCE_
    # DOMAIN). Never both — a bounce never creates a customer Message.
    bounce_event: "BounceEvent | None" = None


def _reject_control_or_non_ascii(value: str, field_name: str) -> None:
    if "\r" in value or "\n" in value:
        raise IngestError(400, f"Malformed {field_name}.")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise IngestError(400, f"Malformed {field_name}.") from exc


def parse_envelope_recipient(value: str | None) -> tuple[str, str, str]:
    
    # function no longer hard-rejects a domain != settings.MAIL_DOMAIN. Domain
    # acceptance already happened at Postfix RCPT time (relay-domains.cf +
    # recipient-map.cf, both proxy:pgsql, verified-domain-only) — by the time
    # a message reaches here, Postfix has already refused any domain that
    # isn't the shared apex or a verified custom domain. This function keeps
    # being the single source of truth for the STRUCTURAL checks (ascii,
    # control chars, single @, lowercase, routing_key format) and now RETURNS
    # the domain as data instead of gating on it; resolve_inbox is the single
    # place domain->inbox binding is enforced (fail-closed there too).
    if not value or len(value) > 320:
        raise IngestError(400, "Malformed envelope recipient.")
    _reject_control_or_non_ascii(value, "envelope recipient")
    if value.count("@") != 1:
        raise IngestError(400, "Malformed envelope recipient.")
    localpart, domain = value.split("@", 1)
    domain_lower = domain.lower()
    # CODE FIX 0 (gate R2): the VERP bounce localpart is
    # row.public_id VERBATIM (engine.py: f"{row.public_id}@{OUTBOUND_BOUNCE_
    # DOMAIN}") and public_id is secrets.token_urlsafe — a MIXED-CASE
    # alphabet by construction. Every other domain requires the WHOLE
    # envelope recipient lowercase (mirrors Postfix's own case-folding for
    # customer-facing addresses); the bounce domain is the one deliberate
    # exception, so it must be checked BEFORE the blanket lowercase rule
    # would reject a perfectly legitimate bounce. Domain itself is still
    # case-folded (domain names are never case-sensitive).
    if domain_lower == settings.OUTBOUND_BOUNCE_DOMAIN.lower():
        if not domain_lower or domain_lower.startswith(".") or domain_lower.endswith("."):
            raise IngestError(400, "Malformed envelope recipient.")
        return f"{localpart}@{domain_lower}", localpart, domain_lower
    
    # pgsql lookup keys %u/%d are lowercased), but pipe(8) with empty flags=
    # hands us the ORIGINAL case. Rejecting it here 4xx-deferred the mail for
    # 5 days and it was never stored. Every non-bounce recipient is therefore
    # case-FOLDED (never rejected for case) and only the folded form is
    # validated/routed. The returned/stored envelope recipient is the
    # lowercase canonical form. Do NOT fix this with master.cf `flags=u`: it
    # would also fold the VERP bounce localpart handled verbatim above.
    value = value.lower()
    localpart = localpart.lower()
    domain = domain_lower
    if not domain or domain.startswith(".") or domain.endswith("."):
        raise IngestError(400, "Malformed envelope recipient.")
    
    # (it never has a catch-all concept); any other domain (custom, verified
    # or not — Postfix RCPT already gated on verified+catch_all before this
    # runs) is checked against the permissive catch-all-compatible charset so
    # real-world catch-all localparts (john.doe, info+tag, a) can be PARSED.
    # This never widens what Postfix ACCEPTS (see CATCHALL_LOCALPART_RE
    # docstring) — non-catch-all custom domains still require an exact,
    # strictly-formatted routing_key Inbox row to ever reach ingest at all.
    localpart_re = (
        ROUTING_KEY_RE if domain == settings.MAIL_DOMAIN.lower() else CATCHALL_LOCALPART_RE
    )
    if not localpart_re.fullmatch(localpart):
        raise IngestError(400, "Malformed envelope recipient.")
    return value, localpart, domain


def validate_envelope_sender(value: str | None) -> str:
    if value is None or len(value) > 320:
        raise IngestError(400, "Malformed envelope sender.")
    if value == "":
        return value
    _reject_control_or_non_ascii(value, "envelope sender")
    if value.count("@") != 1 or any(char in value for char in "<>(),;:\\[] "):
        raise IngestError(400, "Malformed envelope sender.")
    localpart, domain = value.rsplit("@", 1)
    if not localpart or not domain or domain.startswith(".") or domain.endswith("."):
        raise IngestError(400, "Malformed envelope sender.")
    return value


def validate_transport_value(value: str | None, name: str, max_length: int) -> str | None:
    if value in (None, ""):
        return None
    if len(value) > max_length:
        raise IngestError(400, f"Malformed {name}.")
    _reject_control_or_non_ascii(value, name)
    return value


def extract_message_id(raw_bytes: bytes) -> tuple[str | None, str]:
    header_end = raw_bytes.find(b"\r\n\r\n")
    separator_length = 4
    if header_end < 0:
        header_end = raw_bytes.find(b"\n\n")
        separator_length = 2
    if header_end < 0 or header_end + separator_length > MAX_HEADER_BYTES:
        return None, "-"
    header_bytes = raw_bytes[: header_end + separator_length]
    try:
        parsed = BytesHeaderParser(policy=policy.default).parsebytes(header_bytes)
        values = parsed.get_all("Message-ID", [])
    except (ValueError, IndexError):
        return None, "-"
    if len(values) != 1:
        return None, "-"
    original = str(values[0]).strip()
    if not original or len(original) > 998 or "\r" in original or "\n" in original:
        return None, "-"
    try:
        original.encode("ascii")
    except UnicodeEncodeError:
        return original, "-"
    normalized = original.lower()
    if not MESSAGE_ID_RE.fullmatch(normalized):
        return original, "-"
    return original, normalized


def build_dedupe_key(normalized_message_id: str, raw_sha256: str) -> str:
    material = (
        b"mailsocket-dedupe-v1\0"
        + normalized_message_id.encode("utf-8")
        + b"\0"
        + raw_sha256.encode("ascii")
    )
    return hashlib.sha256(material).hexdigest()


def resolve_inbox(routing_key: str, domain: str) -> Inbox:
    
    # composite (domain, routing_key) filter, mirroring the two partial
    # unique constraints on Inbox (inbox_shared_routing_key_uniq /
    # inbox_custom_routing_key_uniq). Each branch below filters on at most
    # one partial-unique's condition, so this can NEVER raise
    # MultipleObjectsReturned even though routing_key is no longer globally
    # unique. Unknown/unverified/disabled custom domain -> fail-closed 404,
    # never a 500 and never a silent cross-tenant match.
    #
    
    # branch below is BYTE-FOR-BYTE UNTOUCHED. All catch-all logic lives in
    # the custom-domain else-branch, and its precedence MUST match the
    
    # ingest-can-store bi-implication): exact LIVE inbox wins; an exact-but-
    # dead match or no match at all falls through to the live sentinel (if
    # catch_all is on); a dead/missing sentinel is a 422/404 reject, never a
    # silent misroute.
    domain = domain.lower()
    if domain == settings.MAIL_DOMAIN.lower():
        try:
            inbox = Inbox.objects.get(routing_key__iexact=routing_key, domain__isnull=True)
        except Inbox.DoesNotExist as exc:
            raise IngestError(404, "Inbox not found.") from exc
        if inbox.deleted_at is not None or not inbox.is_enabled:
            raise IngestError(422, "Inbox is disabled.")
        return inbox

    from apps.domains.models import Domain

    try:
        domain_row = Domain.objects.get(name=domain, status=Domain.Status.VERIFIED)
    except Domain.DoesNotExist as exc:
        raise IngestError(404, "Inbox not found.") from exc

    try:
        inbox = Inbox.objects.get(routing_key__iexact=routing_key, domain=domain_row)
    except Inbox.DoesNotExist:
        inbox = _catch_all_fallback(domain_row)
    else:
        if inbox.deleted_at is not None or not inbox.is_enabled:
            if domain_row.catch_all:
                # Exact match exists but is dead AND catch-all is on: fall
                # through to the sentinel (matches the SQL — leg 1's
                # exact-match branch and leg 2's catch-all branch are
                # independent UNION ALL legs, so a dead exact match does not
                # block the catch-all leg from matching). If the dead inbox
                # IS the sentinel itself, _catch_all_fallback hands back the
                # identical dead row and the liveness check below rejects it
                # — this only happens on a race, since Postfix RCPT would
                # already have refused a dead sentinel.
                inbox = _catch_all_fallback(domain_row)
            # else: no catch-all configured for this domain — nothing to
            # fall through to. Fall out of this branch with the dead exact
            # match still assigned; the liveness check below raises the
            # same 422 this domain always raised pre-Stage-C (regression
            # guard: catch_all=False must be a complete no-op for every
            # existing custom-domain behavior).

    if inbox.deleted_at is not None or not inbox.is_enabled:
        raise IngestError(422, "Inbox is disabled.")
    return inbox


def _catch_all_fallback(domain_row) -> Inbox:
    if not (domain_row.catch_all and domain_row.catch_all_inbox_id):
        raise IngestError(404, "Inbox not found.")
    return domain_row.catch_all_inbox


def _is_bounce_recipient(routing_key: str, domain: str) -> bool:
    "Public API."
    if not settings.ENABLE_OUTBOUND:
        return False
    return domain == settings.OUTBOUND_BOUNCE_DOMAIN.lower()


def _store_bounce(*, raw_bytes: bytes, envelope_recipient: str, routing_key: str) -> "BounceEvent":
    """Store-raw-first (mirrors store_message): the DSN bytes are persisted
    UNCONDITIONALLY before any classification/dispatch decision, so a
    malformed or foreign bounce is never lost and never 500s. Never falls
    through to resolve_inbox — a bounce recipient can never create (or
    match) a customer Inbox/Message row.

    D2 Part A (): the MAC check happens BEFORE
    parse_dsn_status and before any mutation. ``outbound_public_id`` is left
    blank on an invalid/missing MAC on purpose — so a future replay
    reconciler that picks up UNPARSED rows by id can never bypass the MAC.
    The claimed address is kept verbatim in ``envelope_recipient`` for
    forensics; the localpart itself (which may embed a bogus MAC) is never
    logged.
    """
    from apps.outbound.models import BounceEvent
    from apps.outbound.services import record_hard_bounce
    from apps.outbound.verp import verify_verp_localpart

    from .dsn import parse_dsn_final_recipients

    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()

    match = BOUNCE_LOCALPART_RE.fullmatch(routing_key)
    if not match:
        # Unparseable localpart shape — route to reconciler, leave the raw
        # row unprocessed for replay. Never a 500, never resolve_inbox.
        event = BounceEvent.objects.create(
            envelope_recipient=envelope_recipient,
            outbound_public_id="",
            parse_status=BounceEvent.ParseStatus.UNPARSED,
            raw_bytes=raw_bytes,
            raw_sha256=raw_sha256,
        )
        return event

    verified_public_id = verify_verp_localpart(routing_key)
    if verified_public_id is None:
        # Recognized obe_<id>.<mac> shape but the MAC does not verify (or is
        # from a different key) — never trust the claimed public_id.
        logger.warning("bounce_mac_invalid")
        event = BounceEvent.objects.create(
            envelope_recipient=envelope_recipient,
            outbound_public_id="",
            parse_status=BounceEvent.ParseStatus.UNPARSED,
            raw_bytes=raw_bytes,
            raw_sha256=raw_sha256,
        )
        return event

    dsn_status = parse_dsn_status(raw_bytes)
    public_id = verified_public_id

    if dsn_status is None:
        # Recognized obe_ shape with a valid MAC but the body isn't a
        # parseable DSN — leave unprocessed for replay, never a customer
        # inbox, never a 500.
        event = BounceEvent.objects.create(
            envelope_recipient=envelope_recipient,
            outbound_public_id=public_id,
            parse_status=BounceEvent.ParseStatus.UNPARSED,
            raw_bytes=raw_bytes,
            raw_sha256=raw_sha256,
        )
        return event

    parse_status = (
        BounceEvent.ParseStatus.HARD if dsn_status == "hard" else BounceEvent.ParseStatus.SOFT
    )
    with transaction.atomic():
        event = BounceEvent.objects.create(
            envelope_recipient=envelope_recipient,
            outbound_public_id=public_id,
            parse_status=parse_status,
            raw_bytes=raw_bytes,
            raw_sha256=raw_sha256,
            processed_at=timezone.now(),
        )
        if dsn_status == "hard":
            # A foreign (belongs to nobody we know / already-terminal) or
            # unknown public_id is a documented safe no-op inside
            # record_hard_bounce (OutboundEmail.objects.filter(...).first()
            # returns None) — never raises, never creates a customer inbox.
            final_recipients = parse_dsn_final_recipients(raw_bytes)
            record_hard_bounce(public_id, final_recipients=final_recipients)
        # Soft (4.x.x) bounces: record only, per design — no suppression.
    return event


def store_message(*, raw_bytes: bytes, headers) -> StoreResult:
    envelope_recipient, routing_key, domain = parse_envelope_recipient(
        headers.get("X-Envelope-To")
    )

    # CODE FIX 0 (gate R2): bounce recognition happens BEFORE resolve_inbox
    # so a VERP bounce recipient can never be treated as an unknown
    # customer recipient (404) or matched against a real Inbox/catch-all.
    if _is_bounce_recipient(routing_key, domain):
        event = _store_bounce(
            raw_bytes=raw_bytes, envelope_recipient=envelope_recipient, routing_key=routing_key
        )
        return StoreResult(message=None, deduplicated=False, bounce_event=event)

    envelope_from = validate_envelope_sender(headers.get("X-Envelope-From"))
    postfix_queue_id = validate_transport_value(
        headers.get("X-Postfix-Queue-Id"), "Postfix queue ID", 64
    )
    intake_id = validate_transport_value(headers.get("X-Intake-Id"), "intake ID", 128)
    inbox = resolve_inbox(routing_key, domain)

    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    provider_message_id, normalized_message_id = extract_message_id(raw_bytes)
    dedupe_key = build_dedupe_key(normalized_message_id, raw_sha256)
    create_values = {
        "inbox": inbox,
        "dedupe_key": dedupe_key,
        "provider_message_id": provider_message_id,
        "postfix_queue_id": postfix_queue_id,
        "intake_id": intake_id,
        "envelope_from": envelope_from,
        "envelope_recipient": envelope_recipient,
        "raw_bytes": raw_bytes,
        "raw_sha256": raw_sha256,
        "size_bytes": len(raw_bytes),
        "ingest_status": Message.IngestStatus.RECEIVED,
    }
    try:
        with transaction.atomic():
            message = Message.objects.create(**create_values)
        return StoreResult(message=message, deduplicated=False)
    except IntegrityError as exc:
        constraint_name = getattr(getattr(exc, "__cause__", None), "diag", None)
        constraint_name = getattr(constraint_name, "constraint_name", None)
        if constraint_name != "message_inbox_dedupe_uniq":
            raise

    existing = Message.objects.get(inbox=inbox, dedupe_key=dedupe_key)
    if existing.raw_sha256 != raw_sha256 or existing.inbox_id != inbox.id:
        logger.critical(
            "Dedupe integrity mismatch for inbox_id=%s message_id=%s",
            inbox.id,
            existing.id,
        )
        raise IngestError(409, "Dedupe integrity conflict.")
    return StoreResult(message=existing, deduplicated=True)


def parse_after_commit(message_id: int) -> Message:
    """Parse only after the raw-store transaction has committed."""
    from .parser import parse_message

    return parse_message(message_id)
