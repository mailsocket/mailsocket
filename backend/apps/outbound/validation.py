"Public API."

import re

# Dot-atom-subset localpart (design §2 point 1): lowercase letters/digits and
# a conservative punctuation set, no leading/trailing/double dot. Deliberately
# stricter than full RFC 5321 (which allows quoted strings) — outbound V1
# only needs to validate addresses WE construct/accept, not parse arbitrary
# third-party mail.
LOCALPART_RE = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}$")


# single References token must fullmatch or it is rejected inside the gate.
# Printable US-ASCII (no space, no control chars), angle-bracketed.
MSGID_RE = re.compile(r"^<[!-9;-~]{1,995}>$")


class AddressFormatError(ValueError):
    def __init__(self, code: str = "validation_error"):
        self.code = code
        super().__init__(code)


def has_control_chars(value: str) -> bool:
    "Public API."
    if not isinstance(value, str):
        return True
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def parse_from_address(value: str) -> tuple[str, str]:
    """Split+validate a from_address into (localpart, domain), both already
    lowercased on the domain side (localpart case is preserved — RFC 5321
    localparts are technically case-sensitive; we only lowercase the domain
    for the invariant lookup, matching Domain.name's own lowercase-only
    storage).

    Raises AddressFormatError on: not exactly one '@', empty local/domain
    parts, a malformed localpart (regex above — also rejects '..' via the
    length-1-char-class match, since a bare '.' can appear consecutively
    only if two dots are adjacent, which the class permits per-char but the
    the additional explicit check below forbids), or any control char
    anywhere in the string.
    """
    if not isinstance(value, str) or has_control_chars(value):
        raise AddressFormatError()
    if value.count("@") != 1:
        raise AddressFormatError()
    localpart, domain = value.split("@", 1)
    if not localpart or not domain:
        raise AddressFormatError()
    if not LOCALPART_RE.fullmatch(localpart):
        raise AddressFormatError()
    if localpart.startswith(".") or localpart.endswith(".") or ".." in localpart:
        raise AddressFormatError()
    domain = domain.lower().rstrip(".")
    if not domain:
        raise AddressFormatError()
    return localpart, domain



# angle-addr / comment / address-list form, never in the bare addr-spec we
# accept. "Bob <x@y>" used to pass, dodge the suppression lookup (which
# normalised the whole string) and still be delivered to <x@y> by smtplib.
_NON_ADDR_SPEC_CHARS = frozenset('<>()",;:[]\\ ')


def _is_single_bare_addr_spec(value) -> bool:
    
    # so a non-ASCII address used to pass here and then crash the drain.
    if not isinstance(value, str) or not value.isascii() or has_control_chars(value) or value.count("@") != 1:
        return False
    return not any(ch in _NON_ADDR_SPEC_CHARS or ch.isspace() for ch in value)


def validate_recipient_address(value: str) -> str:
    """Looser structural check for To/Cc/Bcc/Reply-To addresses (they are not
    subject to the sending-domain invariant — only from_address is). Rejects
    control chars, requires exactly one '@', and accepts ONLY a bare
    addr-spec: no display name, no <angle-addr>, no comment, no second
    address — the stored value is exactly what the suppression check, the
    allowlist and RCPT TO all see."""
    if not _is_single_bare_addr_spec(value):
        raise AddressFormatError()
    localpart, domain = value.split("@", 1)
    if not localpart or not domain or domain.startswith(".") or domain.endswith("."):
        raise AddressFormatError()
    if len(value) > 320:
        raise AddressFormatError()
    return value


def validate_threading_field(value: str, *, is_references: bool = False) -> str:
    "Public API."
    if not isinstance(value, str) or has_control_chars(value):
        raise AddressFormatError()
    if is_references:
        if len(value.encode("utf-8")) > 2048:
            raise AddressFormatError()
        tokens = value.split()
    else:
        tokens = [value]
    for token in tokens:
        if not MSGID_RE.fullmatch(token):
            raise AddressFormatError()
    return value


def validate_plain_text_field(value: str, *, max_length: int) -> str:
    """Subject / display-name style field: control chars → reject, length cap."""
    if not isinstance(value, str) or has_control_chars(value):
        raise AddressFormatError()
    if len(value) > max_length:
        raise AddressFormatError()
    return value
