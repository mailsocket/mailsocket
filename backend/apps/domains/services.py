"Public API."
import logging
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Domain

logger = logging.getLogger(__name__)

#  / D-1: the single published MX target for every BYOD
# customer. Stage A only DOCUMENTS/CHECKS this value; Stage B is what makes
# Postfix actually accept mail for a domain pointing here.
MX_TARGET_HOST = "mx.mailsocket.app"

# N-strike (): don't demote a live verified domain to `failed` on a
# single transient DNS blip — require this many CONSECUTIVE failed re-checks.
VERIFY_DEMOTE_STRIKE_COUNT = 3

# Safety caps (): bound resolver work per lookup.
DNS_TIMEOUT_SECONDS = 5.0
DNS_RESOLVER_IPS = ("1.1.1.1", "8.8.8.8")
MAX_TXT_STRINGS_SCANNED = 20


class DomainCreateRefused(Exception):
    "Public API."

    def __init__(self, message: str, code: str = "domain_taken"):
        self.message = message
        self.code = code
        super().__init__(message)




DOMAIN_PENDING_CLAIM_DAYS = 7


def _expired_pending_claims():
    """pending_dns rows older than DOMAIN_PENDING_CLAIM_DAYS with nothing
    attached (no bound inbox, no catch-all). Never verified/failed/disabled."""
    from django.db.models import Exists, OuterRef

    from apps.inboxes.models import Inbox

    cutoff = timezone.now() - timedelta(days=DOMAIN_PENDING_CLAIM_DAYS)
    return Domain.objects.filter(
        ~Exists(Inbox.objects.filter(domain_id=OuterRef("pk"))),
        status=Domain.Status.PENDING_DNS,
        verified_at__isnull=True,
        created_at__lt=cutoff,
        catch_all=False,
    )


def release_expired_pending_claims() -> int:
    """Delete expired pending claims (run by ``verify_domains``). Returns the
    number released. Logged by id only."""
    released = 0
    for domain in _expired_pending_claims().iterator():
        with transaction.atomic():
            if _expired_pending_claims().filter(pk=domain.pk).select_for_update().exists():
                logger.info("domain_pending_claim_released domain_id=%s", domain.public_id)
                Domain.objects.filter(pk=domain.pk).delete()
                released += 1
    return released


def create_domain(user, name: str) -> Domain:
    "Public API."
    normalized = (name or "").strip().lower().rstrip(".")
    
    # account, give it one DNS check (outside the transaction: no DB lock held
    
    # longer an expired pending claim, so the delete below does not touch it.
    # An inconclusive check (DNS timeout/error) keeps the claim: the name stays
    
    # Only claims that were DNS-checked just now and came back unverified may
    
    checked_ids = []
    for stale in _expired_pending_claims().filter(name=normalized):
        try:
            verify_domain(stale)
        except Exception:  # noqa: BLE001 — inconclusive: never release on a failed lookup
            logger.warning("domain_stale_claim_verify_failed domain_id=%s", stale.public_id)
            continue
        checked_ids.append(stale.pk)
    with transaction.atomic():
        _expired_pending_claims().filter(name=normalized, pk__in=checked_ids).delete()  
        if Domain.objects.filter(name=normalized).exists():
            raise DomainCreateRefused(
                "That domain is already registered.", code="domain_taken"
            )
        domain = Domain(user=user, name=normalized)
        try:
            domain.full_clean(exclude=["public_id", "verification_txt_host"])
            domain.save()
        except ValidationError as exc:
            message = "; ".join(
                str(item) for values in exc.message_dict.values() for item in values
            ) if hasattr(exc, "message_dict") else str(exc)
            raise DomainCreateRefused(message, code="validation_error") from exc
    return domain


def owned_domains(user):
    return Domain.objects.filter(user=user)


def delete_domain(domain: Domain) -> None:
    """Release a domain's name back to the pool.

    PROTECT: refuse when inboxes are still bound ("decide +
    test"). Stage A never has bound inboxes in practice (ENABLE_BYOD gates
    inbox creation on a domain), but this guard makes the invariant explicit
    and future-proof for Stage B.
    """
    if domain.inboxes.exists():
        raise DomainDeleteRefused(
            "Domain has bound inboxes; remove them first.", code="domain_has_inboxes"
        )
    domain.delete()


class DomainDeleteRefused(Exception):
    def __init__(self, message: str, code: str = "domain_has_inboxes"):
        self.message = message
        self.code = code
        super().__init__(message)



#

# of Domain.catch_all / catch_all_inbox goes through this function — never a
# raw serializer/model field write — mirroring the "status transitions are
# service-layer only" pattern documented on Domain above.


class CatchAllToggleRefused(Exception):
    """Raised by set_catch_all on any refusal. ``code`` maps 1:1 to the view's
    error response (see apps.api.views.DomainCatchAllView)."""

    def __init__(self, message: str, code: str):
        self.message = message
        self.code = code
        super().__init__(message)


def assert_not_active_catch_all_target(inbox) -> None:
    "Public API."
    active = Domain.objects.filter(
        catch_all=True, catch_all_inbox_id=inbox.pk
    ).exists()
    if active:
        raise CatchAllToggleRefused(
            "This inbox is the active catch-all target for its domain; "
            "disable catch-all on the domain first.",
            code="catch_all_target",
        )


def _sentinel_routing_key(domain: Domain) -> str:
    """Generate a fresh, non-colliding routing_key for an auto-created
    sentinel inbox. Base "catchall", suffixed on collision (P2-1, cosmetic;
    a Business account owns at most 10 domains so this loop is short)."""
    from apps.inboxes.models import Inbox

    candidate = "catchall"
    if not Inbox.objects.filter(domain=domain, routing_key=candidate).exists():
        return candidate
    for suffix in range(2, 100):
        candidate = f"catchall-{suffix}"
        if not Inbox.objects.filter(domain=domain, routing_key=candidate).exists():
            return candidate
    raise CatchAllToggleRefused(
        "Unable to allocate a sentinel routing key.", code="inbox_limit_reached"
    )


def set_catch_all(domain: Domain, user, *, enabled: bool, inbox_id: str | None = None) -> Domain:
    "Public API."
    from apps.accounts.entitlements import can_create_inbox
    from apps.inboxes.models import Inbox

    if not enabled:
        domain.catch_all = False
        domain.save(update_fields=["catch_all", "updated_at"])
        return domain

    if domain.status != Domain.Status.VERIFIED:
        raise CatchAllToggleRefused(
            "Domain must be verified before enabling catch-all.",
            code="domain_not_verified",
        )

    with transaction.atomic():
        if inbox_id:
            try:
                inbox = Inbox.objects.select_for_update().get(
                    public_id=inbox_id, user=user
                )
            except Inbox.DoesNotExist as exc:
                raise CatchAllToggleRefused(
                    "Inbox not found.", code="domain_not_found_or_unverified"
                ) from exc
            if (
                inbox.domain_id != domain.id
                or inbox.deleted_at is not None
                or not inbox.is_enabled
            ):
                raise CatchAllToggleRefused(
                    "Inbox must be a live inbox bound to this domain.",
                    code="catch_all_target_invalid",
                )
        else:
            if not can_create_inbox(user):
                raise CatchAllToggleRefused(
                    "Inbox limit reached for this plan.", code="inbox_limit_reached"
                )
            # Deterministic "catchall"(-N) routing_key (design §G) rather
            # than create_random_inbox's opaque random key — a sentinel is a
            # human-facing, permanent fixture of the domain, not a disposable
            # slug. can_create_inbox was just re-checked under the plan's
            # row lock (see _locked_plan), so this create is guarded by the
            # same atomic block.
            inbox = Inbox.objects.create(
                user=user,
                label="Catch-all",
                routing_key=_sentinel_routing_key(domain),
                domain=domain,
            )

        domain.catch_all = True
        domain.catch_all_inbox = inbox
        domain.save(update_fields=["catch_all", "catch_all_inbox", "updated_at"])
    return domain


# --- DNS verification --------------------------------------------------------


class _StubResolver:
    """Never used in production; exists only so verify_domain has a resolver
    to call when the caller doesn't inject one. Tests always inject a mock —
    see apps/domains/tests/test_verification.py — this class deliberately
    raises if ever reached, so a missing mock in a real DNS test fails loudly
    instead of hitting the real network."""

    def resolve(self, qname, rdtype):  # pragma: no cover - guarded by callers
        raise RuntimeError(
            "verify_domain() was called without a resolver in a context that "
            "would hit the real network. Inject `resolver=` explicitly."
        )


def _default_resolver():
    """Build a dnspython Resolver pinned to public DNS (),
    5s timeout. Imported lazily so importing this module never requires
    dnspython to be installed unless DNS verification actually runs."""
    import dns.resolver

    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = list(DNS_RESOLVER_IPS)
    resolver.timeout = DNS_TIMEOUT_SECONDS
    resolver.lifetime = DNS_TIMEOUT_SECONDS
    return resolver


def _resolve_txt_strings(resolver, host: str) -> list[str]:
    import dns.exception

    try:
        answer = resolver.resolve(host, "TXT")
    except dns.exception.DNSException:
        return []
    strings: list[str] = []
    for record in answer:
        # dnspython TXT records expose `.strings` (list[bytes]); each TXT
        # record may itself carry multiple character-strings.
        for chunk in getattr(record, "strings", []):
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8", errors="replace")
            strings.append(chunk)
            if len(strings) >= MAX_TXT_STRINGS_SCANNED:
                return strings
    return strings



# DKIM TXT presence check, exposed here (not apps.outbound) so it can share
# _resolve_txt_strings/_default_resolver/DNS_TIMEOUT_SECONDS with the
# existing verify/recheck resolver pass — same presence-only, pinned-
# resolver, never-dials-any-IP discipline as _check_dns above. Extends (does
# not replace) the existing verify_domain/recheck_domain flow: called
# separately by apps.outbound callers so a domain that has never run
# sending_setup (dkim_public_key_txt == "") is never queried at all.


def check_dkim_dns(resolver, domain: Domain) -> bool:
    """True iff the TXT record at <selector>._domainkey.<name> exactly
    matches domain.dkim_public_key_txt. Presence-only; never resolves or
    dials any address found in the record (there are none to dial — a DKIM
    TXT record is pure text)."""
    if not domain.dkim_public_key_txt or not domain.dkim_selector:
        return False
    host = f"{domain.dkim_selector}._domainkey.{domain.name}"
    txt_strings = _resolve_txt_strings(resolver, host)
    # A DKIM TXT value can be split across multiple quoted strings by some
    # DNS providers; join before matching in case any single chunk is
    # shorter than the whole value.
    joined = "".join(txt_strings)
    return domain.dkim_public_key_txt in txt_strings or domain.dkim_public_key_txt == joined


def _apply_dkim_check(domain: Domain, resolver, fields_to_update: list[str], *, demote: bool) -> list[str]:
    """Shared by verify_domain (manual, demote=False — mirrors the "never
    demote on a manual check" doctrine) and recheck_domain (periodic,
    demote=True — 3-strike demote). No-op (does not touch dkim_* at all,
    fields_to_update unchanged) when sending_setup was never run
    (dkim_public_key_txt == ""), so a domain with no DKIM key provisioned
    never gets spuriously touched by the plain domain verify/recheck pass.
    """
    if not domain.dkim_public_key_txt:
        return fields_to_update
    dkim_ok = check_dkim_dns(resolver, domain)
    if dkim_ok:
        was_verified = domain.dkim_status == Domain.DkimStatus.VERIFIED
        domain.dkim_status = Domain.DkimStatus.VERIFIED
        domain.dkim_consecutive_failures = 0
        fields_to_update += ["dkim_status", "dkim_consecutive_failures"]
        if not was_verified:
            domain.dkim_verified_at = timezone.now()
            fields_to_update.append("dkim_verified_at")
        return fields_to_update
    if not demote:
        return fields_to_update
    domain.dkim_consecutive_failures += 1
    fields_to_update.append("dkim_consecutive_failures")
    if domain.dkim_consecutive_failures >= VERIFY_DEMOTE_STRIKE_COUNT:
        domain.dkim_status = Domain.DkimStatus.FAILED
        fields_to_update.append("dkim_status")
    return fields_to_update


def _resolve_mx_hosts(resolver, name: str) -> list[str]:
    import dns.exception

    try:
        answer = resolver.resolve(name, "MX")
    except dns.exception.DNSException:
        return []
    hosts = []
    for record in answer:
        exchange = str(getattr(record, "exchange", "")).rstrip(".").lower()
        if exchange:
            hosts.append(exchange)
    return hosts


def _check_dns(resolver, domain: Domain) -> tuple[bool, bool]:
    "Public API."
    txt_strings = _resolve_txt_strings(resolver, domain.verification_txt_host)
    txt_ok = domain.verification_token in txt_strings
    if not txt_ok:
        return False, False
    mx_hosts = _resolve_mx_hosts(resolver, domain.name)
    mx_ok = MX_TARGET_HOST in mx_hosts
    return True, mx_ok


def verify_domain(domain: Domain, *, resolver=None) -> Domain:
    "Public API."
    resolver = resolver if resolver is not None else _default_resolver()
    now = timezone.now()

    if domain.status == Domain.Status.DISABLED:
        # status=disabled is a manual/billing action, never DNS-driven — a
        # verify call on a disabled domain does nothing.
        domain.last_checked_at = now
        domain.save(update_fields=["last_checked_at", "updated_at"])
        return domain

    txt_ok, mx_ok = _check_dns(resolver, domain)
    fields_to_update = ["last_checked_at", "updated_at"]
    domain.last_checked_at = now

    if txt_ok and mx_ok:
        was_verified = domain.status == Domain.Status.VERIFIED
        domain.status = Domain.Status.VERIFIED
        domain.last_error = ""
        domain.consecutive_failures = 0
        fields_to_update += ["status", "last_error", "consecutive_failures"]
        if not was_verified:
            domain.verified_at = now
            fields_to_update.append("verified_at")
        fields_to_update = _apply_dkim_check(domain, resolver, fields_to_update, demote=False)
        domain.save(update_fields=fields_to_update)
        return domain

    # Failure: never demote here (see docstring) — just record why, leaving
    # status exactly where it was (pending_dns stays pending_dns; an
    # already-VERIFIED domain stays verified on a manual re-check failure).
    domain.last_error = (
        "TXT record missing or does not match."
        if not txt_ok
        else f"TXT verified; MX does not include {MX_TARGET_HOST} yet."
    )
    fields_to_update.append("last_error")
    domain.save(update_fields=fields_to_update)
    return domain


def recheck_domain(domain: Domain, *, resolver=None) -> Domain:
    "Public API."
    if domain.status != Domain.Status.VERIFIED:
        return verify_domain(domain, resolver=resolver)

    resolver = resolver if resolver is not None else _default_resolver()
    now = timezone.now()
    txt_ok, mx_ok = _check_dns(resolver, domain)
    domain.last_checked_at = now
    fields_to_update = ["last_checked_at", "updated_at"]

    if txt_ok and mx_ok:
        domain.consecutive_failures = 0
        domain.last_error = ""
        fields_to_update += ["consecutive_failures", "last_error"]
        fields_to_update = _apply_dkim_check(domain, resolver, fields_to_update, demote=True)
        domain.save(update_fields=fields_to_update)
        return domain

    domain.consecutive_failures += 1
    domain.last_error = (
        "TXT record missing or does not match."
        if not txt_ok
        else f"TXT verified; MX does not include {MX_TARGET_HOST} yet."
    )
    fields_to_update += ["consecutive_failures", "last_error"]
    if domain.consecutive_failures >= VERIFY_DEMOTE_STRIKE_COUNT:
        domain.status = Domain.Status.FAILED
        fields_to_update.append("status")
    
    # strike counter — a domain whose primary verify DNS is currently
    # failing does not get its DKIM demoted by the SAME failure (they check
    # different hosts); still run the DKIM check on this pass so an
    # independent DKIM-TXT removal is caught even while the primary DNS is
    # fine, and vice versa is naturally excluded since this whole branch is
    # the "primary DNS failed" branch — DKIM re-check here is a best-effort
    # extra probe, matching design §2's "extend ... with a third check".
    fields_to_update = _apply_dkim_check(domain, resolver, fields_to_update, demote=True)
    domain.save(update_fields=fields_to_update)
    return domain
