"""Email address verification — SAFE MODE (no SMTP probe).

Product decision D7: we deliberately do NOT open an SMTP connection to the
target mail server ("does this mailbox exist?") because live SMTP probing from
our IPs burns sending reputation and is what abuse filters flag. Instead we
answer the *useful* questions that carry zero reputation risk:

  * Is the address syntactically valid (RFC-ish, one @, sane local/domain)?
  * Does the domain actually accept mail (has MX, or a fallback A/AAAA record)?
  * Is it a disposable / throwaway domain?
  * Is it a role address (admin@, support@, no-reply@ …)?

From those we derive a coarse ``deliverable`` verdict:
  * "undeliverable"  — bad syntax or no mail-accepting DNS records.
  * "risky"          — resolves but disposable or role-based.
  * "deliverable"    — valid syntax + domain accepts mail, not disposable/role.
  * "unknown"        — DNS lookup failed transiently (caller may retry).

This is intentionally conservative: we never claim a specific mailbox exists.
"""

from __future__ import annotations

import re
import time

import dns.exception
import dns.resolver

# Conservative RFC 5321-ish local part + domain. We are validating, not parsing
# every legal edge case; addresses this rejects are not worth delivering to.
_LOCAL_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}$")
_LABEL_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")

# Common role / non-personal mailboxes (RFC 2142 + usual suspects).
ROLE_LOCALPARTS = {
    "admin", "administrator", "abuse", "billing", "executive", "contact", "help",
    "hostmaster", "info", "mail", "marketing", "noc", "no-reply", "noreply",
    "postmaster", "root", "sales", "security", "support", "sysadmin", "team",
    "webmaster", "www",
}

# A compact, high-signal disposable-domain list. Not exhaustive by design —
# it catches the throwaway providers that matter for signup/OTP abuse without
# pretending to be a full blocklist service.
DISPOSABLE_DOMAINS = {
    "10minutemail.com", "20minutemail.com", "guerrillamail.com", "guerrillamail.net",
    "sharklasers.com", "mailinator.com", "mailinator.net", "yopmail.com", "yopmail.net",
    "temp-mail.org", "tempmail.com", "tempmailo.com", "throwawaymail.com", "trashmail.com",
    "getnada.com", "nada.email", "dispostable.com", "fakeinbox.com", "maildrop.cc",
    "mohmal.com", "spam4.me", "mailnesia.com", "moakt.com", "tmpmail.org", "tmpmail.net",
    "emailondeck.com", "mintemail.com", "spamgourmet.com", "mailcatch.com",
    "inboxbear.com", "burnermail.io", "33mail.com", "einrot.com", "harakirimail.com",
}


def _resolver(lifetime: float):
    r = dns.resolver.Resolver(configure=True)
    r.lifetime = lifetime
    r.timeout = min(2.0, lifetime)
    return r



# every lookup (MX, then A/AAAA fallback) rather than a fresh ~4s allowance
# per record. Worst case before this fix was MX + A + AAAA each up to 4s =
# ~12s pinning a gthread worker; now the whole call is bounded to this budget.
DNS_TOTAL_BUDGET_SECONDS = 4.0


def _has_mail_dns(domain: str) -> tuple[bool, str]:
    """Return (accepts_mail, reason). Uses MX, falling back to A/AAAA per RFC 5321.

    Shares one wall-clock deadline (DNS_TOTAL_BUDGET_SECONDS) across every
    lookup this call makes, and short-circuits the A/AAAA fallback entirely
    when MX already failed with a transient dns_error — there is no point
    burning more of the budget on lookups likely to hit the same broken
    resolver chain.
    """
    deadline = time.monotonic() + DNS_TOTAL_BUDGET_SECONDS

    def _remaining() -> float:
        return max(0.05, deadline - time.monotonic())

    resolver = _resolver(_remaining())
    try:
        answers = resolver.resolve(domain, "MX")
        if len(answers) > 0:
            return True, "mx"
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
        pass
    except (dns.resolver.NoNameservers, dns.exception.Timeout):
        
        # A/AAAA against a resolver chain that just failed transiently.
        return False, "dns_error"
    # No MX → implicit MX rule: the domain's A/AAAA record may still accept mail.
    for rr in ("A", "AAAA"):
        if time.monotonic() >= deadline:
            return False, "dns_error"
        resolver.lifetime = _remaining()
        resolver.timeout = min(2.0, resolver.lifetime)
        try:
            if len(resolver.resolve(domain, rr)) > 0:
                return True, "a_fallback"
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            continue
        except (dns.resolver.NoNameservers, dns.exception.Timeout):
            return False, "dns_error"
    return False, "no_mx"


def verify_email(address: str) -> dict:
    """Safe-mode verification. Never opens an SMTP connection."""
    raw = (address or "").strip()
    result = {
        "email": raw,
        "deliverable": "undeliverable",
        "syntax_valid": False,
        "domain": None,
        "has_mx": False,
        "is_disposable": False,
        "is_role": False,
        "reason": "",
        "checked": "syntax+dns (no smtp probe)",
    }

    if raw.count("@") != 1:
        result["reason"] = "address must contain exactly one @"
        return result
    local, domain = raw.rsplit("@", 1)
    domain = domain.lower().rstrip(".")
    result["domain"] = domain or None

    if not local or not _LOCAL_RE.match(local) or ".." in local \
            or local.startswith(".") or local.endswith("."):
        result["reason"] = "invalid local part"
        return result
    labels = domain.split(".")
    if len(labels) < 2 or not all(_LABEL_RE.match(lbl) for lbl in labels):
        result["reason"] = "invalid domain"
        return result

    result["syntax_valid"] = True
    result["is_role"] = local.lower() in ROLE_LOCALPARTS
    result["is_disposable"] = domain in DISPOSABLE_DOMAINS

    accepts, reason = _has_mail_dns(domain)
    result["has_mx"] = accepts
    if reason == "dns_error":
        result["deliverable"] = "unknown"
        result["reason"] = "dns lookup failed; try again"
        return result
    if not accepts:
        result["deliverable"] = "undeliverable"
        result["reason"] = "domain has no mail-accepting DNS records"
        return result

    if result["is_disposable"]:
        result["deliverable"] = "risky"
        result["reason"] = "disposable/throwaway domain"
    elif result["is_role"]:
        result["deliverable"] = "risky"
        result["reason"] = "role-based (non-personal) address"
    else:
        result["deliverable"] = "deliverable"
        result["reason"] = "valid syntax and domain accepts mail"
    return result
