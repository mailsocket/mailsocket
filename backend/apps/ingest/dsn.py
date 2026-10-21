"""Best-effort DSN (RFC 3464) status extraction for the bounce reconciler.

Deliberately minimal — a byte-level scan for the machine-readable
``message/delivery-status`` part's ``Status:``/``Action:`` lines, not a full
MIME parse. This mirrors the store-raw-first discipline elsewhere in ingest:
we never need to understand a bounce perfectly, we only need to classify it
as hard/soft/unparseable without ever raising (a malformed DSN must never
500 the ingest endpoint — see apps.ingest.services._handle_bounce).
"""
import re

# The class digit (4 or 5) is the authoritative signal per RFC 3463 — prefer
# it over the human-readable Action: line, which some senders omit or spell
# inconsistently.
_STATUS_RE = re.compile(rb"^Status:\s*([45])\.\d{1,3}\.\d{1,3}", re.IGNORECASE | re.MULTILINE)
_ACTION_RE = re.compile(rb"^Action:\s*(\S+)", re.IGNORECASE | re.MULTILINE)
# D2 Part B (): best-effort extraction of
# Final-Recipient addresses from a DSN's machine-readable part, used to
# scope global suppression to "who could plausibly have forged this" (see
# apps.outbound.services.record_hard_bounce). Deliberately permissive/best-
# effort — never raises, caps matches so a hostile body can't cause
# unbounded work.
_FINAL_RECIPIENT_RE = re.compile(
    rb"^Final-Recipient:\s*rfc822;\s*<?([^\s<>;]+)>?", re.IGNORECASE | re.MULTILINE
)
MAX_FINAL_RECIPIENTS = 10


def parse_dsn_status(raw_bytes: bytes) -> str | None:
    """Return "hard" (5.x.x / failed), "soft" (4.x.x / delayed), or None if
    no recognizable DSN status could be extracted at all (caller treats
    None as unparseable and leaves the raw row for replay)."""
    status_match = _STATUS_RE.search(raw_bytes)
    if status_match:
        return "hard" if status_match.group(1) == b"5" else "soft"

    action_match = _ACTION_RE.search(raw_bytes)
    if action_match:
        action = action_match.group(1).lower()
        if action == b"failed":
            return "hard"
        if action == b"delayed":
            return "soft"
    return None


def parse_dsn_final_recipients(raw_bytes: bytes) -> list[str]:
    """Best-effort extraction of every Final-Recipient rfc822 address in
    ``raw_bytes``, capped at MAX_FINAL_RECIPIENTS. Never raises."""
    try:
        matches = _FINAL_RECIPIENT_RE.findall(raw_bytes)
    except Exception:
        return []
    recipients = []
    for match in matches[:MAX_FINAL_RECIPIENTS]:
        try:
            recipients.append(match.decode("utf-8", errors="replace"))
        except Exception:
            continue
    return recipients
