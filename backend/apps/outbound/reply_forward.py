"Public API."
import re
from email.utils import getaddresses, parseaddr

from apps.outbound.validation import MSGID_RE, has_control_chars

# design §5: strip repeatedly, prefix once.
SUBJECT_PREFIX_RE = re.compile(r"^(?:\s*(?:re|fwd?|fw)\s*(?:\[\d+\])?\s*:\s*)+", re.IGNORECASE)

_WS_RE = re.compile(r"\s+")


class ReplyTargetInvalid(ValueError):
    """No usable reply-to address could be derived (design §3) -> 422
    reply_target_invalid."""


def sanitize_header_text(value: str) -> str:
    """Strip C0+DEL (has_control_chars predicate) and collapse whitespace.
    Does NOT truncate or dedup — callers apply those separately."""
    if not value:
        return ""
    cleaned = "".join(ch for ch in value if not (ord(ch) < 32 or ord(ch) == 127))
    return _WS_RE.sub(" ", cleaned).strip()


def dedup_subject_prefix(subject: str, *, prefix: str) -> str:
    """SUBJECT_PREFIX_RE dedup (design §5): strip every existing Re:/Fwd:
    run, then prepend exactly one ``prefix``. "Re: Re: Fwd: x" -> reply
    "Re: x" / forward "Fwd: x"; "FW: x" forward -> "Fwd: x"."""
    stripped = SUBJECT_PREFIX_RE.sub("", subject or "").strip()
    return f"{prefix}: {stripped}" if stripped else f"{prefix}:"


def build_subject(orig_subject: str, *, prefix: str) -> str:
    sanitized = sanitize_header_text(orig_subject or "")
    deduped = dedup_subject_prefix(sanitized, prefix=prefix)
    return deduped[:512]


def _addr_spec(raw: str) -> str | None:
    """parseaddr -> bare addr-spec, DISCARDING the display name entirely
    (design §3 "a hostile display name is the classic smuggling vector").
    Returns None if unparseable / no '@' present."""
    if not raw:
        return None
    display_name, address = parseaddr(raw)
    if not address or "@" not in address or has_control_chars(address):
        return None
    return address


def _header_only_reparse(raw_bytes: bytes):
    """Bounded header-only re-parse of Message.raw_bytes (design §4: reuse
    the ingest parser's size/header limits) — used to recover Reply-To and
    References, which apps.inboxes.models.Message does not store as
    columns."""
    from email import message_from_bytes, policy

    from apps.ingest.parser import _header_bytes

    try:
        header_bytes = _header_bytes(bytes(raw_bytes))
    except Exception:
        return message_from_bytes(b"", policy=policy.default)
    try:
        return message_from_bytes(header_bytes, policy=policy.compat32)
    except Exception:
        return message_from_bytes(b"", policy=policy.default)


def resolve_reply_target(message) -> str:
    "Public API."
    reparsed = _header_only_reparse(message.raw_bytes)
    reply_to_raw = str(reparsed.get("Reply-To", "") or "")
    candidate = _addr_spec(reply_to_raw)
    if candidate:
        return candidate
    candidate = _addr_spec(message.header_from or "")
    if candidate:
        return candidate
    candidate = _addr_spec(message.envelope_from or "")
    if candidate:
        return candidate
    raise ReplyTargetInvalid()


def resolve_threading(message) -> tuple[str, str]:
    """(in_reply_to, references) per design §4/§3: accept a msg-id token
    ONLY if it fullmatches MSGID_RE, else OMIT (never raise — an attacker
    must not be able to block replies by mangling their own Message-ID).
    References = validated original References tokens (re-parsed from
    raw_bytes, last 20 / <=2KB) + the original Message-ID token,
    space-joined."""
    orig_msgid = (message.provider_message_id or "").strip()
    in_reply_to = orig_msgid if MSGID_RE.fullmatch(orig_msgid) else ""

    reparsed = _header_only_reparse(message.raw_bytes)
    raw_references = str(reparsed.get("References", "") or "")
    tokens = [tok for tok in raw_references.split() if MSGID_RE.fullmatch(tok)]
    if in_reply_to:
        tokens.append(in_reply_to)
    tokens = tokens[-20:]
    references = ""
    joined = ""
    for tok in tokens:
        candidate = f"{joined} {tok}".strip() if joined else tok
        if len(candidate.encode("utf-8")) > 2048:
            break
        joined = candidate
    references = joined
    return in_reply_to, references


def _dedup_case_insensitive(addrs: list[str], *, exclude: set[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for addr in addrs:
        key = addr.strip().lower()
        if not key or key in seen or key in exclude:
            continue
        seen.add(key)
        result.append(addr)
    return result


def reply_all_extra_recipients(message, *, inbox_address: str, reply_target: str) -> list[str]:
    """header_to + header_cc addr-specs, deduped case-insensitively, MINUS
    the inbox's own address and the reply-target duplicate (design §4)."""
    exclude = {inbox_address.strip().lower(), reply_target.strip().lower()}
    raw_addrs = [*(message.header_to or []), *(message.header_cc or [])]
    addr_specs = []
    for _display_name, address in getaddresses(raw_addrs):
        if address and not has_control_chars(address):
            addr_specs.append(address)
    return _dedup_case_insensitive(addr_specs, exclude=exclude)


def sanitize_body(value: str) -> str:
    """Normalize \\r\\n,\\r -> \\n; strip C0 except \\n/\\t (-> space)."""
    if not value:
        return ""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        ch if (ch in ("\n", "\t") or ord(ch) >= 32) and ord(ch) != 127 else " "
        for ch in normalized
    )


def truncate_quote(text: str, html: str, *, budget_bytes: int) -> tuple[str, str]:
    """Truncate the QUOTED portion (text, html) so their combined UTF-8
    byte length fits within ``budget_bytes`` (design §3: OUTBOUND_MAX_BODY_
    BYTES minus the user's own body). Text is truncated before html."""
    if budget_bytes <= 0:
        return "", ""
    text_bytes = text.encode("utf-8")
    if len(text_bytes) > budget_bytes:
        text = text_bytes[:budget_bytes].decode("utf-8", errors="ignore")
        return text, ""
    remaining = budget_bytes - len(text_bytes)
    html_bytes = html.encode("utf-8")
    if len(html_bytes) > remaining:
        html = html_bytes[:remaining].decode("utf-8", errors="ignore")
    return text, html


def build_forward_quote(message, *, budget_bytes: int) -> tuple[str, str]:
    "Public API."
    from_line = sanitize_header_text(message.header_from or "")
    date_line = sanitize_header_text(str(message.received_at or ""))
    subject_line = sanitize_header_text(message.subject or "")
    to_line = sanitize_header_text(", ".join(message.header_to or []))

    separator = (
        "\n\n---------- Forwarded message ---------\n"
        f"From: {from_line}\n"
        f"Date: {date_line}\n"
        f"Subject: {subject_line}\n"
        f"To: {to_line}\n\n"
    )
    # Reserve the separator's own bytes out of the budget FIRST — the quoted
    # body is truncated to whatever remains, so
    # len(separator)+len(quoted_text)+len(quoted_html) never exceeds
    # budget_bytes (design §3: without this reservation, a quote truncated
    # to exactly budget_bytes would push the final combined body over
    # OUTBOUND_MAX_BODY_BYTES once the separator is prepended).
    remaining = max(0, budget_bytes - len(separator.encode("utf-8")))

    quoted_text = sanitize_body(message.text_body or "")
    quoted_html = sanitize_body(message.html_body or "")
    quoted_text, quoted_html = truncate_quote(quoted_text, quoted_html, budget_bytes=remaining)

    text_quote = separator + quoted_text if quoted_text or not quoted_html else separator
    html_quote = quoted_html
    return text_quote, html_quote


def build_reply_quote(message, *, budget_bytes: int) -> tuple[str, str]:
    """Bare sanitized/truncated original body for a reply quote — no
    Forwarded-message banner (that is forward-specific)."""
    quoted_text = sanitize_body(message.text_body or "")
    quoted_html = sanitize_body(message.html_body or "")
    return truncate_quote(quoted_text, quoted_html, budget_bytes=budget_bytes)
