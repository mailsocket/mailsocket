"Public API."
from __future__ import annotations

import ipaddress
import logging
import secrets
import smtplib
import time
from dataclasses import dataclass

import dns.exception
import dns.resolver
from django.conf import settings

from apps.domains.services import DNS_RESOLVER_IPS
from apps.webhooks.security import SSRFValidationError, _validate_public_address

logger = logging.getLogger(__name__)

# Design §4g / §5: bound resolver + connection work per request.
MAX_MX_HOSTS_ATTEMPTED = 2
MAX_IPS_TOTAL = 3


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one full-mode probe attempt.

    ``verdict`` is None when the caller should fall back to the SAFE-check
    verdict (skipped / failed / degraded); otherwise it is one of the
    existing deliverable enum values and REPLACES the safe verdict.
    """

    probe: str  # "completed" | "skipped" | "failed"
    verdict: str | None = None
    rcpt_code: int | None = None
    catch_all: bool | None = None
    reason: str = ""


class _MxResolutionError(Exception):
    """Internal signal carrying a skip/undeliverable reason out of resolution."""

    def __init__(self, reason: str, verdict: str | None = None):
        self.reason = reason
        self.verdict = verdict
        super().__init__(reason)


def _default_mx_resolver(lifetime: float):
    """Pinned public resolver (design §5) — same discipline as
    apps.domains.services._default_resolver, never the container's own
    resolv.conf (an attacker who influences local DNS can't hand us internal
    answers)."""
    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = list(DNS_RESOLVER_IPS)
    resolver.timeout = min(2.0, lifetime)
    resolver.lifetime = lifetime
    return resolver


def _validated_ip_or_none(candidate: str) -> str | None:
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    try:
        _validate_public_address(address)
    except SSRFValidationError:
        return None
    return str(address)


def _resolve_mx_hosts(resolver, domain: str) -> list[str]:
    """Returns the list of MX exchange hostnames sorted by preference, or
    raises _MxResolutionError("null_mx", verdict="undeliverable") on an RFC
    7505 null MX. An empty return means "no MX record" (implicit-MX fallback
    applies)."""
    try:
        answer = resolver.resolve(domain, "MX")
    except dns.resolver.NXDOMAIN:
        return []
    except dns.resolver.NoAnswer:
        return []
    except (dns.resolver.NoNameservers, dns.exception.Timeout):
        raise _MxResolutionError("dns_error") from None

    records = sorted(answer, key=lambda r: getattr(r, "preference", 0))
    hosts = []
    for record in records:
        exchange = str(getattr(record, "exchange", "")).rstrip(".").lower()
        if not exchange:
            # RFC 7505: MX "0 ." — the domain explicitly refuses mail.
            raise _MxResolutionError("null_mx", verdict="undeliverable")
        hosts.append(exchange)
    return hosts


def _resolve_host_ips(resolver, host: str) -> tuple[list[str], bool]:
    """Returns (validated_ip_literals, saw_non_public). A/AAAA fallback for
    an implicit-MX host gets the IDENTICAL validation as an explicit MX
    exchange (design §5, last bullet)."""
    literal = _validated_ip_or_none(host)
    if literal is not None:
        return [literal], False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # A syntactically-valid IP literal that failed validation.
        return [], True

    validated: list[str] = []
    saw_non_public = False
    for rdtype in ("A", "AAAA"):
        try:
            answer = resolver.resolve(host, rdtype)
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            continue
        except (dns.resolver.NoNameservers, dns.exception.Timeout):
            continue
        for rr in answer:
            raw = str(rr)
            good = _validated_ip_or_none(raw)
            if good is None:
                saw_non_public = True
                continue
            validated.append(good)
    return validated, saw_non_public


def resolve_probe_targets(domain: str, *, resolver=None) -> tuple[list[str], str]:
    """Resolve+SSRF-validate up to MAX_IPS_TOTAL connectable IP literals for
    ``domain``. Returns (validated_ips, reason) where reason is "ok" when
    validated_ips is non-empty, else one of "null_mx" / "mx_non_public" /
    "no_mx" / "dns_error".

    ``resolver`` is injectable (tests MUST inject one — same `.resolve(name,
    rdtype)` contract as apps.domains.services.verify_domain); production
    pins to DNS_RESOLVER_IPS via _default_mx_resolver.
    """
    resolver = resolver if resolver is not None else _default_mx_resolver(4.0)
    try:
        mx_hosts = _resolve_mx_hosts(resolver, domain)
    except _MxResolutionError as exc:
        return [], exc.reason

    if not mx_hosts:
        # RFC 5321 implicit-MX rule: fall back to the domain's own A/AAAA.
        mx_hosts = [domain]

    validated: list[str] = []
    saw_non_public = False
    for host in mx_hosts[:MAX_MX_HOSTS_ATTEMPTED]:
        ips, non_public = _resolve_host_ips(resolver, host)
        saw_non_public = saw_non_public or non_public
        for ip in ips:
            if ip not in validated:
                validated.append(ip)
            if len(validated) >= MAX_IPS_TOTAL:
                return validated, "ok"
    if validated:
        return validated, "ok"
    if saw_non_public:
        return [], "mx_non_public"
    return [], "no_mx"


def _default_connect(ip: str, port: int, timeout: float):
    """Real transport: smtplib.SMTP dialed against the validated IP LITERAL
    (``host=str(ip)``) — never the hostname, never re-resolved (anti-rebind,
    design §5). Port is always 25 (hardcoded by the caller)."""
    return smtplib.SMTP(host=ip, port=port, timeout=timeout)


def _random_localpart() -> str:
    # 20 hex chars — unguessable, cannot be allow-listed by the remote MX.
    return f"mx-{secrets.token_hex(10)}"


def _classify_target_code(code: int) -> tuple[str, str] | None:
    """Maps an RCPT-on-target response code to (bucket, reason) where
    bucket is "success" (needs the random-RCPT follow-up), "undeliverable",
    "risky_full", or "risky_greylist". Returns None for anything else
    (unexpected code -> caller treats as unknown/failed)."""
    if 200 <= code < 300:
        return "success", ""
    if code in (550, 551, 553, 554):
        return "undeliverable", ""
    if code == 552:
        return "risky_full", "mailbox full or over quota"
    if 400 <= code < 500:
        return "risky_greylist", "greylisted_or_tempfail"
    return None


def _run_session(connect_factory, ip: str, port: int, connect_timeout: float,
                  helo_host: str, mail_from: str, target: str, domain: str) -> ProbeResult:
    """One SMTP session against one validated IP. NEVER calls `.data(` on the
    transport — DATA has no code path here (T18, structural)."""
    smtp = connect_factory(ip, port, connect_timeout)
    try:
        code, _ = smtp.ehlo(helo_host)
        if code >= 400:
            code, _ = smtp.helo(helo_host)
        if code >= 400:
            return ProbeResult(probe="failed", reason="ehlo_helo_refused")

        if smtp.has_extn("starttls"):
            try:
                smtp.starttls()
                smtp.ehlo(helo_host)
            except Exception:
                # Tolerate STARTTLS failure and continue plain — we never
                # send content, so there is nothing confidential to protect.
                pass

        code, _ = smtp.mail(mail_from)
        if code >= 400:
            return ProbeResult(probe="failed", reason="mail_from_refused")

        target_code, _ = smtp.rcpt(target)
        bucket = _classify_target_code(target_code)
        if bucket is None:
            return ProbeResult(probe="failed", reason="unexpected_rcpt_code", rcpt_code=target_code)
        kind, reason = bucket
        if kind == "undeliverable":
            return ProbeResult(probe="completed", verdict="undeliverable", rcpt_code=target_code, reason=reason)
        if kind == "risky_full":
            return ProbeResult(probe="completed", verdict="risky", rcpt_code=target_code, reason=reason)
        if kind == "risky_greylist":
            return ProbeResult(probe="completed", verdict="risky", rcpt_code=target_code, reason=reason)

        # kind == "success": only NOW fire the random RCPT (halves RCPT
        # volume on invalid addresses — design §3 reputation note).
        random_addr = f"{_random_localpart()}@{domain}"
        random_code, _ = smtp.rcpt(random_addr)
        if 200 <= random_code < 300:
            return ProbeResult(
                probe="completed", verdict="risky", rcpt_code=target_code,
                catch_all=True, reason="domain accepts any recipient (catch-all)",
            )
        if 400 <= random_code < 600:
            return ProbeResult(
                probe="completed", verdict="deliverable", rcpt_code=target_code,
                catch_all=False, reason="valid syntax, domain accepts mail, mailbox confirmed",
            )
        return ProbeResult(probe="failed", reason="unexpected_random_rcpt_code", rcpt_code=target_code)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


def run_full_probe(
    target_email: str,
    domain: str,
    *,
    resolver=None,
    connect=None,
    budget_seconds: float | None = None,
) -> ProbeResult:
    """Full-mode SMTP RCPT probe entrypoint (design §3). Graceful degrade:
    ANY exception here is caught by the caller (VerifyEmailView) and turned
    into a 200 + safe verdict — this function itself also catches every
    expected failure mode and returns ProbeResult(probe="failed"/"skipped")
    rather than raising, so a caller that forgets to wrap it still gets a
    safe, non-500 result.

    Flag re-checked HERE too (defense in depth, mirrors
    apps.outbound.engine.drain's ENABLE_OUTBOUND check at the point of any
    real I/O) — a stray future call path can never dial out with the flag
    off.
    """
    if not settings.ENABLE_SMTP_VERIFY:
        return ProbeResult(probe="skipped", reason="smtp_verify_disabled")

    budget = budget_seconds if budget_seconds is not None else float(settings.SMTP_PROBE_TOTAL_BUDGET_SECONDS)
    connect_timeout = min(float(settings.SMTP_PROBE_CONNECT_TIMEOUT_SECONDS), budget)
    connect_factory = connect if connect is not None else _default_connect
    helo_host = settings.SMTP_PROBE_HELO_HOST
    mail_from = f"verify-probe@{settings.OUTBOUND_BOUNCE_DOMAIN}"

    try:
        ips, reason = resolve_probe_targets(domain, resolver=resolver)
    except Exception:
        logger.exception("smtp_probe_mx_resolution_failed domain=%s", domain)
        return ProbeResult(probe="skipped", reason="mx_resolution_error")

    if reason == "null_mx":
        return ProbeResult(probe="skipped", verdict="undeliverable", reason="null_mx")
    if not ips:
        # mx_non_public / no_mx / dns_error: never claim "invalid" — fall
        # back to the safe verdict, no which-range oracle.
        return ProbeResult(probe="skipped", reason=reason)

    deadline = time.monotonic() + budget
    last_reason = "probe_failed"
    for ip in ips:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ProbeResult(probe="failed", reason="timeout")
        try:
            result = _run_session(
                connect_factory, ip, 25, min(connect_timeout, max(0.1, remaining)),
                helo_host, mail_from, target_email, domain,
            )
        except (TimeoutError, OSError, smtplib.SMTPException) as exc:
            logger.info("smtp_probe_connect_failed domain=%s ip=%s error=%s", domain, ip, exc)
            last_reason = "connect_failed"
            continue
        except Exception:
            logger.exception("smtp_probe_unexpected_error domain=%s ip=%s", domain, ip)
            last_reason = "unexpected_error"
            continue
        if result.probe == "completed":
            return result
        # This IP's session ended without a usable verdict (e.g. EHLO/MAIL
        # refused) — try the next candidate IP, if any.
        last_reason = result.reason or last_reason
    return ProbeResult(probe="failed", reason=last_reason)
