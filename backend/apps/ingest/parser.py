import logging
import re
import uuid
from array import array
from dataclasses import dataclass
from datetime import timedelta
from email import policy
from email.feedparser import BytesFeedParser
from email.message import EmailMessage
from email.utils import formataddr, getaddresses
from html.parser import HTMLParser
from itertools import accumulate
from typing import cast
from urllib.parse import parse_qsl, urlsplit

import nh3
from django.conf import settings
from django.db import DatabaseError
from django.db.models import F
from django.utils import timezone

from apps.inboxes.models import Message

logger = logging.getLogger(__name__)
OTP_RE = re.compile(r"(?<!\d)(?:\d{3}[ -]\d{3}|\d{4,8})(?!\d)", re.ASCII)
OTP_KEYWORD_RE = re.compile(
    r"(?:security\s+code|one[- ]time|verification|passcode|verify|code|otp|pin|"
    r"mã\s+xác\s+nh\u1eadn|m\u1eadt\s+kh\u1ea9u|mã)",
    re.IGNORECASE,
)
OTP_REJECT_RE = re.compile(
    r"(?:order|invoice|zip|postal|phone|tel(?:ephone)?)"
    r"(?:\s+(?:number|no\.?))?\s*#?\s*$",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
WRAPPED_URL_RE = re.compile(
    r"<\s*(https?://[^<>\s]*(?:\r?\n[ \t]+[^<>\s]+)*)\s*>",
    re.IGNORECASE,
)
MAGIC_KEY_RE = re.compile(r"^(?:token|code|verify|confirm|magic|auth|login|activate|t|k)$", re.IGNORECASE)
LONG_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Fa-f0-9]{20,}(?![A-Za-z0-9_-])|[A-Za-z0-9_-]{20,}")
MAGIC_TEXT_RE = re.compile(r"(?:verify|confirm|sign\s*in|log\s*in|activate|reset|xác\s+nh\u1eadn|\u0111ăng\s+nh\u1eadp)", re.IGNORECASE)
EXCLUDED_LINK_RE = re.compile(
    r"(?:^|[/&?=_-])(?:unsubscribe|preferences?|privacy|terms)(?:$|[/&?=_-])",
    re.IGNORECASE,
)
EXCLUDED_HOST_RE = re.compile(
    r"(?:^|\.)(?:facebook\.com|instagram\.com|linkedin\.com|twitter\.com|x\.com|tiktok\.com)$",
    re.IGNORECASE,
)
# Spans whose digits must never become OTP candidates (qafix2 #1): URLs
# (scheme or www.), bare domain paths / multi-label hostnames (incl. Unicode
# IDN labels), e-mail addresses and long mixed letter+digit tokens. Every
# pattern is a single forward pass with a no-overlap lookbehind and
# delimiter-disjoint repeats, so masking stays linear on pathological input.
#
# A URL/domain span ends at prose delimiters (qafix2 r2): whitespace, comma,
# semicolon, brackets, quotes and angle brackets. Its own path, query and
# fragment digits stay covered ("?id=771234&u=99887766"), but a code after
# the link survives ("https://example.com/login,OTP:482193"). A ":"/"." that
# is followed by more non-delimiter text stays in the URL (port, "?c=1:9").
_URL_STOP = "\\s<>\"'`,;\\[\\]{}\u201c\u201d\u2018\u2019\u00ab\u00bb"
# Possessive URL body: one run of non-delimiters, so there is exactly one way
# to match and nothing to backtrack into. "(" and ")" are URL characters here
# (any nesting, balanced or not: "/wiki/Foo_(bar)", "/a(123456"); a ")" that
# closes a "(" opened in the prose BEFORE the link ends the span instead —
# see ``_paren_trimmed_spans`` (qafix2 r3).
_URL_BODY = rf"[^{_URL_STOP}]*+"
_MASK_URL_RE = re.compile(rf"(?:https?://|www\.){_URL_BODY}", re.IGNORECASE)
_MASK_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]++@[\w-]++(?:\.[\w-]++)+")
# Host labels are Unicode-aware ([\w-], so IDN labels like "例子.公司" match);
# hosts containing "_" are rejected in ``mask_non_otp_spans``.
_MASK_DOMAIN_RE = re.compile(
    r"(?<![\w.@-])(?P<host>[\w-]++(?:\.[\w-]++)*"
    r"\.(?:[^\W\d_]{2,24}|xn--[A-Za-z0-9-]{1,59}))"
    rf"(?P<tail>[/?#]{_URL_BODY})?(?![\w-])"
)
_MASK_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{8,}(?![A-Za-z0-9])", re.ASCII)
_HAS_ASCII_DIGIT_RE = re.compile(r"[0-9]")
_ANY_DIGIT_RE = re.compile(r"\d")
_ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
_LETTER_RE = re.compile(r"[^\W\d_]")
_DIGIT_BLANK = str.maketrans("0123456789", "_" * 10)
_NON_PAREN_RE = re.compile(r"[^()]++")
# "(" -> +1, ")" -> -1 as signed bytes (array "b").
_PAREN_STEP = bytes.maketrans(b"()", b"\x01\xff")
_SHORT_GAP = 256
MAX_URL_LENGTH = 2_048
OTP_CONTEXT_CHARS = 32
HTML_TAGS = {
    "a", "b", "blockquote", "br", "code", "div", "em", "h1", "h2", "h3",
    "h4", "h5", "h6", "hr", "i", "li", "ol", "p", "pre", "span", "strong",
    "table", "tbody", "td", "th", "thead", "tr", "u", "ul",
}
HTML_ATTRIBUTES = {"a": {"href", "title"}, "td": {"colspan", "rowspan"}, "th": {"colspan", "rowspan"}}


class ParserLimitError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedFields:
    provider_message_id: str | None
    header_from: str
    header_to: list[str]
    header_cc: list[str]
    subject: str
    text_body: str
    html_body: str
    otp: str | None
    otp_confidence: float
    magic_link: str | None
    links: list[str]
    detected_otp: str | None
    warnings: tuple[str, ...]


class _HTMLSignals(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.anchors: list[tuple[str, str, bool]] = []
        self._href: str | None = None
        self._anchor_text: list[str] = []
        self._anchor_has_image = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag.lower() == "a":
            self._href = attributes.get("href")
            self._anchor_text = []
            self._anchor_has_image = False
        elif tag.lower() == "img" and self._href is not None:
            self._anchor_has_image = True

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.anchors.append((self._href, " ".join(self._anchor_text), self._anchor_has_image))
            self._href = None
            self._anchor_text = []
            self._anchor_has_image = False

    def handle_data(self, data):
        self.text.append(data)
        if self._href is not None:
            self._anchor_text.append(data)


def _html_signals(html_body: str) -> _HTMLSignals:
    parser = _HTMLSignals()
    try:
        parser.feed(html_body)
        parser.close()
    except (ValueError, AssertionError):
        # Extraction is best-effort; malformed HTML must never fail ingest.
        pass
    return parser


def _otp_rejected(text: str, match: re.Match, digits: str) -> bool:
    start, end = match.span()
    before = text[max(0, start - OTP_CONTEXT_CHARS):start]
    after = text[end:end + OTP_CONTEXT_CHARS]
    if len(digits) == 4 and 1900 <= int(digits) <= 2099:
        # A 4-digit value in the year range is usually a copyright/date ("© 2024",
        # "in 2025"), NOT a code — reject it UNLESS an OTP keyword sits adjacent,
        # in which case a real 4-digit code like "your code is 2025" must survive.
        keyword_nearby = OTP_KEYWORD_RE.search(
            text[max(0, start - 40):min(len(text), end + 40)]
        )
        if not keyword_nearby:
            return True
    if OTP_REJECT_RE.search(before) or re.search(r"#\s*$", before):
        return True
    if re.search(r"(?:[$€£₫]\s*|\bVND\s*)$", before, re.IGNORECASE):
        return True
    if re.match(r"\s*(?:[$€£₫]|VND\b)", after, re.IGNORECASE):
        return True
    if re.search(r"[.,]\d{1,2}\s*$", before) or re.match(r"^[.,]\d{1,2}\b", after):
        return True
    # Reject candidates embedded in phone-like runs, including separated groups.
    phone_start = start
    phone_end = end
    lower_bound = max(0, start - OTP_CONTEXT_CHARS)
    upper_bound = min(len(text), end + OTP_CONTEXT_CHARS)
    while phone_start > lower_bound and text[phone_start - 1] in "0123456789 ()+.-":
        phone_start -= 1
    while phone_end < upper_bound and text[phone_end] in "0123456789 ()+.-":
        phone_end += 1
    phone_window = text[phone_start:phone_end]
    if len(re.sub(r"\D", "", phone_window)) >= 10:
        return True
    return False


def html_to_text(html_body: str) -> str:
    """Render (sanitized) HTML to its visible text, for context snippets."""
    return " ".join(_html_signals(html_body or "").text)


def _prose_paren_depth(text: str, start: int, end: int, depth: int) -> int:
    """Open prose "(" depth after reading ``text[start:end]`` from ``depth``.

    Order-aware: a ")" with no "(" open is ignored (depth never goes below
    0), so ") (" leaves one "(" open. For a gap with paren sequence ``s``
    (+1/-1), the clamped walk gives ``max(depth, -min_prefix(s)) + sum(s)``.
    Every path is linear in the gap: short gaps hop between parens with
    ``str.find``; long ones get the prefix minimum in C (regex strip,
    translate to signed bytes, ``accumulate``) instead of a Python loop.
    """
    closes = text.count(")", start, end)
    if closes <= depth:
        # Depth can never reach 0 inside the gap: plain counting is exact.
        return depth + text.count("(", start, end) - closes
    if end - start <= _SHORT_GAP:
        find = text.find
        pos = start
        while True:
            close = find(")", pos, end)
            if close < 0:
                return depth + text.count("(", pos, end)
            depth += text.count("(", pos, close)
            if depth:
                depth -= 1
            pos = close + 1
    opens = text.count("(", start, end)
    if not opens:
        return 0
    steps = array("b", _NON_PAREN_RE.sub("", text[start:end]).encode("ascii").translate(_PAREN_STEP))
    lowest = min(0, min(accumulate(steps)))
    return max(depth, -lowest) + opens - closes


def _paren_trimmed_spans(text: str, matches, keep=None) -> list[tuple[int, int]]:
    """Spans of ``matches`` (in order, non-overlapping) with prose ")" trimmed.

    Inside a URL "(" and ")" are ordinary characters, nested or unbalanced
    ("/wiki/Foo_(bar)", "/a((123456))", "/a(123456"). A span is cut only at a
    ")" that has no "(" of its own inside the span AND closes a "(" opened in
    the prose before it: "(https://x.io/a?b=1) 123456", "[x](https://y/1)".

    Linear: the prose depth is carried across matches and each gap is visited
    once by ``_prose_paren_depth``; the in-span walk visits each ")" once (only
    when some prose "(" is open). ``keep`` filters matches without affecting
    depth.
    """
    spans: list[tuple[int, int]] = []
    depth = 0
    cursor = 0
    for match in matches:
        start, end = match.span()
        depth = _prose_paren_depth(text, cursor, start, depth)
        if depth:
            inner = 0
            pos = start
            while True:
                close = text.find(")", pos, end)
                if close < 0:
                    break
                inner += text.count("(", pos, close)
                if not inner:
                    end = close
                    break
                inner -= 1
                pos = close + 1
        # The trimmed tail (from the closing ")") is prose for the next gap.
        cursor = end
        if keep is None or keep(match):
            spans.append((start, end))
    return spans


def _is_masked_domain(match: re.Match) -> bool:
    host = match.group("host")
    if "_" in host:
        return False
    first = host[:host.find(".")]
    if host.isascii():
        # "482193.Thanks" (a code glued to the next sentence) is not a
        # host: only treat it as a domain when the first label has a letter.
        return _ASCII_LETTER_RE.search(first) is not None
    # Unicode (IDN) host, e.g. "例子.公司/login/123456". Prose with no
    # spaces ("验证码为123456.请勿泄露") also looks like a host, so an
    # IDN host must not carry digits itself; its path digits are masked.
    return _LETTER_RE.search(first) is not None and _ANY_DIGIT_RE.search(host) is None


def mask_non_otp_spans(text: str) -> str:
    """Return ``text`` with every digit inside a URL/e-mail/domain/token blanked.

    The result has exactly the same length as ``text`` (each masked digit
    becomes ``_``), so match offsets map 1:1 back onto the original string.
    Only the candidate *scan* uses the masked copy; rejection heuristics and
    keyword confidence still read the original text, so a code that sits
    outside every masked span ranks exactly as before.
    """
    if not text or not _HAS_ASCII_DIGIT_RE.search(text):
        return text
    spans = _paren_trimmed_spans(text, _MASK_URL_RE.finditer(text))
    spans.extend([match.span() for match in _MASK_EMAIL_RE.finditer(text)])
    spans.extend(_paren_trimmed_spans(text, _MASK_DOMAIN_RE.finditer(text), _is_masked_domain))
    for match in _MASK_TOKEN_RE.finditer(text):
        token = match.group(0)
        if _ASCII_LETTER_RE.search(token) and _HAS_ASCII_DIGIT_RE.search(token):
            spans.append(match.span())
    if not spans:
        return text
    # Merge overlapping spans, then blank ASCII digits inside each one; every
    # character is copied once, so this step is linear in len(text). Sentence
    # punctuation ending a span ("…/login. 482193", "…/a.\n482193") needs no
    # trimming: it holds no digits and the span already stops at whitespace.
    spans.sort()
    pieces: list[str] = []
    append = pieces.append
    cursor = 0
    for start, end in spans:
        if end <= cursor:
            continue
        if start < cursor:
            start = cursor
        append(text[cursor:start])
        append(text[start:end].translate(_DIGIT_BLANK))
        cursor = end
    append(text[cursor:])
    return "".join(pieces)


def _rank_otp_candidates(subject: str, text_body: str, html_body: str) -> list[tuple[float, int, int, str]]:
    """Score every non-rejected OTP-shaped token with the extractor's ranking.

    Returns ``(confidence, -source_priority, -match_start, digits)`` tuples,
    best first. This is the single scoring implementation shared by
    ``extract_otp`` and the additive ``extract_otp_candidates`` helper, so the
    two can never disagree about what counts as a plausible code.

    Digits inside URLs, e-mail addresses, bare domain paths and long
    alphanumeric tokens are never candidates (see ``mask_non_otp_spans``).
    """
    max_extract = _setting("MAX_EXTRACT_BYTES", 100_000)
    subject = _bounded_extraction(subject or "", max_extract)
    text_body = _bounded_extraction(text_body or "", max_extract)
    html_body = _bounded_extraction(html_body or "", max_extract)
    html_text = html_to_text(html_body)
    ranked: list[tuple[float, int, int, str]] = []
    for source_priority, text in enumerate((subject, text_body, html_text)):
        for match in OTP_RE.finditer(mask_non_otp_spans(text or "")):
            digits = re.sub(r"\D", "", match.group(0))
            if _otp_rejected(text, match, digits):
                continue
            nearby = text[max(0, match.start() - 40):min(len(text), match.end() + 40)]
            confidence = 0.95 if OTP_KEYWORD_RE.search(nearby) else 0.35
            ranked.append((confidence, -source_priority, -match.start(), digits))
    ranked.sort(reverse=True)
    return ranked


def extract_otp(subject: str, text_body: str, html_body: str) -> tuple[str | None, float]:
    ranked = _rank_otp_candidates(subject, text_body, html_body)
    return (ranked[0][3], ranked[0][0]) if ranked else (None, 0.0)


def extract_otp_candidates(subject: str, text_body: str, html_body: str) -> list[dict[str, float | str]]:
    """Ranked plausible codes (best first), each ``{"code", "confidence"}``.

    Additive, computed-on-top view of the exact same scoring ``extract_otp``
    uses (see ``_rank_otp_candidates``). It exposes the also-rans an agent can
    use to disambiguate. Codes are deduplicated, keeping the best-ranked
    occurrence. Returns an empty list when there is nothing plausible.
    """
    seen: set[str] = set()
    candidates: list[dict[str, float | str]] = []
    for confidence, _priority, _start, code in _rank_otp_candidates(subject, text_body, html_body):
        if code in seen:
            continue
        seen.add(code)
        candidates.append({"code": code, "confidence": confidence})
    return candidates


def _clean_url(url: str, *, wrapped: bool = False, allow_mailto: bool = False) -> str | None:
    value = re.sub(r"\s+", "", url) if wrapped else url.strip()
    value = value.rstrip(".,;:!?)]}")
    if len(value) > MAX_URL_LENGTH:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if allow_mailto and parsed.scheme.lower() == "mailto" and parsed.path:
        return value
    return value if parsed.scheme.lower() in {"http", "https"} and parsed.netloc else None


def extract_magic_link(text_body: str, html_body: str) -> tuple[str | None, list[str]]:
    max_extract = _setting("MAX_EXTRACT_BYTES", 100_000)
    text_body = _bounded_extraction(text_body or "", max_extract)
    html_body = _bounded_extraction(html_body or "", max_extract)
    signals = _html_signals(html_body)
    candidates: list[tuple[str, str, bool]] = []
    consumed: list[tuple[int, int]] = []
    for match in WRAPPED_URL_RE.finditer(text_body or ""):
        url = _clean_url(match.group(1), wrapped=True)
        if url:
            candidates.append((url, "", False))
            consumed.append(match.span())
    consumed_index = 0
    for match in URL_RE.finditer(text_body or ""):
        while consumed_index < len(consumed) and consumed[consumed_index][1] <= match.start():
            consumed_index += 1
        if consumed_index < len(consumed) and consumed[consumed_index][0] <= match.start():
            continue
        url = _clean_url(match.group(0))
        if url:
            candidates.append((url, "", False))
    for href, anchor_text, has_image in signals.anchors:
        url = _clean_url(href, allow_mailto=True)
        if url:
            candidates.append((url, anchor_text, has_image))

    links: list[str] = []
    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for url, anchor_text, has_image in candidates:
        if url not in seen and len(links) < 100:
            seen.add(url)
            links.append(url)
        if url not in seen or url not in links:
            continue
        parsed = urlsplit(url)
        excluded = (
            has_image
            or parsed.scheme.lower() == "mailto"
            or EXCLUDED_HOST_RE.search(parsed.hostname or "")
            or EXCLUDED_LINK_RE.search(f"{parsed.path}?{parsed.query}")
        )
        if excluded:
            continue
        query = parse_qsl(parsed.query, keep_blank_values=True)
        key_score = 3 if any(MAGIC_KEY_RE.match(key) for key, _ in query) else 0
        token_score = 4 if LONG_TOKEN_RE.search(parsed.path) or any(LONG_TOKEN_RE.search(value) for _, value in query) else 0
        text_score = 2 if MAGIC_TEXT_RE.search(anchor_text) else 0
        score = key_score + token_score + text_score
        if score:
            ranked.append((score, -links.index(url), url))
    magic_link = max(ranked)[2] if ranked else None
    return magic_link, links


def _setting(name: str, default: int) -> int:
    return int(getattr(settings, name, default))


def _header_bytes(raw_bytes: bytes) -> bytes:
    positions = [position for marker in (b"\r\n\r\n", b"\n\n") if (position := raw_bytes.find(marker)) >= 0]
    end = min(positions) if positions else len(raw_bytes)
    if end > _setting("MAX_PARSE_HEADER_BYTES", 65_536):
        raise ParserLimitError("header bytes limit exceeded")
    return raw_bytes[:end]


class _GuardedBytesFeedParser(BytesFeedParser):
    """Apply MIME limits before each part is allocated or its headers parsed."""

    def __init__(self):
        super().__init__(policy=policy.default)
        self._part_count = 0
        self._header_count = 0

    def _new_message(self):
        if self._part_count >= _setting("MAX_PARSE_MIME_PARTS", 200):
            raise ParserLimitError("MIME parts limit exceeded")
        # The stack contains the ancestors, so its length is the new part's depth.
        if len(self._msgstack) > _setting("MAX_PARSE_MIME_DEPTH", 20):
            raise ParserLimitError("MIME depth limit exceeded")
        self._part_count += 1
        super()._new_message()

    def _parse_headers(self, lines):
        header_bytes = sum(len(line.encode("ascii", errors="surrogateescape")) for line in lines)
        if header_bytes > _setting("MAX_PARSE_HEADER_BYTES", 65_536):
            raise ParserLimitError("header bytes limit exceeded")
        self._header_count += sum(1 for line in lines if line[:1] not in (" ", "\t"))
        if self._header_count > _setting("MAX_PARSE_HEADERS", 200):
            raise ParserLimitError("header count limit exceeded")
        super()._parse_headers(lines)


def _parse_with_limits(raw_bytes: bytes) -> EmailMessage:
    parser = _GuardedBytesFeedParser()
    parser.feed(raw_bytes)
    return cast(EmailMessage, parser.close())  # policy.default: message_factory is EmailMessage


def _validate_structure(message: EmailMessage) -> tuple[str, ...]:
    max_headers = _setting("MAX_PARSE_HEADERS", 200)
    max_depth = _setting("MAX_PARSE_MIME_DEPTH", 20)
    max_parts = _setting("MAX_PARSE_MIME_PARTS", 200)
    part_count = 0
    header_count = 0
    warnings: list[str] = []
    stack = [(message, 0)]

    while stack:
        part, depth = stack.pop()
        part_count += 1
        header_count += sum(1 for _ in part.raw_items())
        if part_count > max_parts:
            raise ParserLimitError("MIME parts limit exceeded")
        if header_count > max_headers:
            raise ParserLimitError("header count limit exceeded")
        if depth > max_depth:
            raise ParserLimitError("MIME depth limit exceeded")
        if part.defects:
            warnings.append("MIME defect")
        if part.is_multipart():  # is_multipart() is isinstance(_payload, list); EmailMessage children
            children = cast(list[EmailMessage], part.get_payload())
            stack.extend((child, depth + 1) for child in reversed(children))

    return tuple(warnings[:10])


def _bounded(value: str, limit: int) -> str:
    return value.replace("\x00", "\ufffd")[:limit]


def _bounded_utf8(value: str, byte_limit: int) -> str:
    """Bound stored text by UTF-8 bytes, as MAX_PARSE_TEXT_BYTES promises."""
    clean = value.replace("\x00", "\ufffd")
    encoded = clean.encode("utf-8")
    if len(encoded) <= byte_limit:
        return clean
    return encoded[:byte_limit].decode("utf-8", errors="ignore")


def _bounded_extraction(value: str, byte_limit: int) -> str:
    """Bound scanner input without creating a token at the truncation edge."""
    bounded = _bounded_utf8(value, byte_limit)
    if len(bounded.encode("utf-8")) < len(value.encode("utf-8")):
        # Strip the trailing partial token with a provably-linear backward scan
        # (no regex): the `\S*$` substitution is O(k^2) on some Python/regex
        # builds for a long no-space run at the cut edge.
        cut = len(bounded)
        while cut > 0 and not bounded[cut - 1].isspace():
            cut -= 1
        return bounded[:cut]
    return bounded


def _addresses(message: EmailMessage, header: str) -> list[str]:
    values = [str(value) for value in message.get_all(header, [])]
    addresses = []
    for display_name, address in getaddresses(values):
        rendered = formataddr((display_name, address)) if address else display_name
        if rendered:
            addresses.append(_bounded(rendered, 998))
    return addresses


def _decode_text_part(part: EmailMessage, warnings: list[str]) -> str:
    try:
        content = part.get_content()
        if isinstance(content, str):
            return _bounded(content, len(content))
    except (LookupError, UnicodeDecodeError, ValueError, TypeError):
        warnings.append("text decoding used replacement characters")

    payload = part.get_payload(decode=True)  # non-multipart text part: bytes, or None
    if not isinstance(payload, bytes):
        undecoded = part.get_payload()
        return _bounded(undecoded, len(undecoded)) if isinstance(undecoded, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        decoded = payload.decode(charset, errors="replace")
        return _bounded(decoded, len(decoded))
    except LookupError:
        warnings.append("unknown charset used UTF-8 replacement decoding")
        decoded = payload.decode("utf-8", errors="replace")
        return _bounded(decoded, len(decoded))


def parse_raw_message(raw_bytes: bytes) -> ParsedFields:
    if len(raw_bytes) > _setting("MAX_INBOUND_BYTES", 10_485_760):
        raise ParserLimitError("message size limit exceeded")
    _header_bytes(raw_bytes)
    message = _parse_with_limits(raw_bytes)
    warnings = list(_validate_structure(message))

    message_ids = message.get_all("Message-ID", [])
    provider_message_id = None
    if len(message_ids) == 1:
        provider_message_id = _bounded(str(message_ids[0]).strip(), 998) or None

    header_from = _bounded(str(message.get("From", "")), 998)
    subject = _bounded(str(message.get("Subject", "")), 998)
    max_text = _setting("MAX_PARSE_TEXT_BYTES", 100_000)
    max_html = _setting("MAX_PARSE_HTML_BYTES", 200_000)
    max_extract = _setting("MAX_EXTRACT_BYTES", 100_000)
    text_body = ""
    extraction_text_body = ""
    html_body = ""
    raw_html_body = ""
    stack = [message]
    while stack:
        part = stack.pop()
        # Attachment-disposition parts are skipped at ANY level, including root
        # (a single-part message flagged as an attachment must not become body).
        # message/rfc822 is skipped only below the root, so descending into an
        # attached email is prevented without discarding a bare root message.
        if part.get_content_disposition() == "attachment" or (
            part is not message and part.get_content_type() == "message/rfc822"
        ):
            # Never inspect the part or its descendants (notably attached emails).
            continue
        if part.is_multipart():
            payload = part.get_payload()
            if isinstance(payload, list):  # see _validate_structure: EmailMessage children
                stack.extend(reversed(cast(list[EmailMessage], payload)))
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        candidate = _decode_text_part(part, warnings)
        if content_type == "text/html" and not html_body and candidate.strip():
            bounded_html = _bounded_utf8(candidate, max_html)
            raw_html_body = bounded_html
            if len(candidate.encode("utf-8")) > max_html:
                warnings.append("HTML body truncated")
            html_body = nh3.clean(
                bounded_html,
                tags=HTML_TAGS,
                attributes=HTML_ATTRIBUTES,
                url_schemes={"http", "https", "mailto"},
                link_rel="noopener noreferrer nofollow",
                strip_comments=True,
            )
            continue
        if content_type == "text/plain" and not text_body and candidate.strip():
            text_body = _bounded_utf8(candidate, max_text)
            extraction_text_body = _bounded_extraction(candidate, max_extract)
            if len(candidate.encode("utf-8")) > max_text:
                warnings.append("text body truncated")
    otp, otp_confidence = extract_otp(subject, extraction_text_body, raw_html_body)
    magic_link, links = extract_magic_link(extraction_text_body, raw_html_body)

    return ParsedFields(
        provider_message_id=provider_message_id,
        header_from=header_from,
        header_to=_addresses(message, "To"),
        header_cc=_addresses(message, "Cc"),
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        otp=otp,
        otp_confidence=otp_confidence,
        magic_link=magic_link,
        links=links,
        detected_otp=otp,
        warnings=tuple(dict.fromkeys(warnings))[:10],
    )


def claim_message(message_id: int) -> uuid.UUID | None:
    now = timezone.now()
    token = uuid.uuid4()
    max_attempts = _setting("MAX_PARSE_ATTEMPTS", 5)
    Message.objects.filter(
        pk=message_id,
        ingest_status=Message.IngestStatus.RECEIVED,
        parse_attempts__gte=max_attempts,
    ).update(
        ingest_status=Message.IngestStatus.FAILED,
        parse_error="Parser resource limit: parse attempt limit exceeded",
        parse_lease_token=None,
        parse_lease_expires_at=None,
        next_parse_at=None,
    )
    claimed = Message.objects.filter(
        pk=message_id,
        ingest_status=Message.IngestStatus.RECEIVED,
        parse_attempts__lt=max_attempts,
    ).update(
        ingest_status=Message.IngestStatus.PARSING,
        parse_started_at=now,
        parse_attempts=F("parse_attempts") + 1,
        parse_error="",
        parse_lease_token=token,
        parse_lease_expires_at=now + timedelta(seconds=_setting("PARSE_LEASE_SECONDS", 300)),
        next_parse_at=None,
    )
    return token if claimed else None


def finalize_success(message_id: int, token: uuid.UUID, fields: ParsedFields) -> bool:
    updated = Message.objects.filter(
        pk=message_id,
        ingest_status=Message.IngestStatus.PARSING,
        parse_lease_token=token,
    ).update(
        provider_message_id=fields.provider_message_id,
        header_from=fields.header_from,
        header_to=fields.header_to,
        header_cc=fields.header_cc,
        subject=fields.subject,
        text_body=fields.text_body,
        html_body=fields.html_body,
        otp=fields.otp,
        otp_confidence=fields.otp_confidence,
        magic_link=fields.magic_link,
        links=fields.links,
        detected_otp=fields.detected_otp,
        ingest_status=Message.IngestStatus.PARSED,
        parsed_at=timezone.now(),
        parse_error="",
        parse_lease_token=None,
        parse_lease_expires_at=None,
        next_parse_at=None,
    )
    if updated and fields.warnings:
        logger.warning(
            "Message parser completed with bounded warnings message_id=%s warnings=%s",
            message_id,
            ", ".join(fields.warnings)[:500],
        )
    if updated:
        # Deliberately after the fenced parse update and outside raw-store
        # transaction A. Enqueue failure never rolls back durable mail/parse.
        try:
            from apps.webhooks.services import enqueue_message_delivery

            enqueue_message_delivery(Message.objects.select_related("inbox").get(pk=message_id))
        except Exception:
            # Preserve ingest response semantics and parsed state even if the
            # additive queue path fails; the exception contains no mail data.
            logger.exception("Webhook enqueue failed after parse message_id=%s", message_id)
    return bool(updated)


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, ParserLimitError):
        return _bounded(f"Parser resource limit: {exc}", 1000)
    return _bounded(f"Parser failure ({type(exc).__name__}).", 1000)


def record_failure(message_id: int, token: uuid.UUID, exc: Exception) -> bool:
    message = Message.objects.filter(
        pk=message_id,
        ingest_status=Message.IngestStatus.PARSING,
        parse_lease_token=token,
    ).only("parse_attempts").first()
    if message is None:
        return False

    max_attempts = _setting("MAX_PARSE_ATTEMPTS", 5)
    terminal = message.parse_attempts >= max_attempts
    delay = min(
        _setting("PARSE_RETRY_MAX_SECONDS", 3600),
        _setting("PARSE_RETRY_BASE_SECONDS", 60) * (2 ** max(message.parse_attempts - 1, 0)),
    )
    updated = Message.objects.filter(
        pk=message_id,
        ingest_status=Message.IngestStatus.PARSING,
        parse_lease_token=token,
    ).update(
        ingest_status=(Message.IngestStatus.FAILED if terminal else Message.IngestStatus.RECEIVED),
        parse_error=_safe_error(exc),
        parse_lease_token=None,
        parse_lease_expires_at=None,
        next_parse_at=None if terminal else timezone.now() + timedelta(seconds=delay),
    )
    return bool(updated)


def process_claim(message_id: int, token: uuid.UUID) -> Message:
    try:
        from apps.accounts.entitlements import record_message_once

        record_message_once(
            Message.objects.select_related("inbox").only(
                "id", "received_at", "usage_counted", "inbox__user_id"
            ).get(pk=message_id)
        )
    except Exception:
        # Usage is best-effort and must never change durable mail/parser state.
        logger.exception("usage_meter_failed counter=messages_received message_id=%s", message_id)
    try:
        raw_bytes = bytes(Message.objects.only("raw_bytes").get(pk=message_id).raw_bytes)
        fields = parse_raw_message(raw_bytes)
        finalize_success(message_id, token, fields)
    except DatabaseError:
        raise
    except Exception as exc:
        record_failure(message_id, token, exc)
    return Message.objects.get(pk=message_id)


def parse_message(message_id: int) -> Message:
    token = claim_message(message_id)
    if token is None:
        return Message.objects.get(pk=message_id)
    return process_claim(message_id, token)
