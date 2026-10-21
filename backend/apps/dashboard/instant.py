"Public API."

import unicodedata

from django.core import signing
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.accounts.services import (
    INSTANT_CLAIM_COOKIE_NAME,
    INSTANT_CLAIM_MAX_AGE,
    INSTANT_CLAIM_SALT,
    INSTANT_INBOX_MINT_RATE_LIMIT,
    INSTANT_INBOX_MINT_RATE_NAME,
    INSTANT_INBOX_MINT_RATE_WINDOW,
    INSTANT_MESSAGES_RATE_LIMIT,
    INSTANT_MESSAGES_RATE_NAME,
    INSTANT_MESSAGES_RATE_WINDOW,
    RateLimitExceeded,
    get_instant_user,
    increment_rate_limit,
)
from apps.api.views import _client_ip, parse_shared_address
from apps.inboxes.models import Inbox
from apps.inboxes.services import random_routing_key

INSTANT_MAX_MESSAGES = 20


def _md_response(body: str, status: int = 200) -> HttpResponse:
    response = HttpResponse(body, content_type="text/markdown; charset=utf-8", status=status)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Robots-Tag"] = "noindex"
    response.headers["Cache-Control"] = "no-store"
    return response


def _not_found() -> HttpResponse:
    # Uniform across EVERY failure mode (missing claim, forged claim, expired
    # claim, wrong-user inbox, custom-domain address, unknown address) — see
    # module docstring "not an existence oracle".
    return _md_response("# Not found\n\nThis instant inbox does not exist or has expired.\n", status=404)


def _rate_limited(exc: RateLimitExceeded) -> HttpResponse:
    response = _md_response(
        "# Rate limited\n\nToo many requests. Try again shortly.\n", status=429
    )
    if exc.reset_at is not None:
        reset_seconds = max(1, int((exc.reset_at - timezone.now()).total_seconds()))
        response.headers["Retry-After"] = str(reset_seconds)
    return response


def _mint_claim(inbox) -> str:
    return signing.dumps({"i": inbox.public_id}, salt=INSTANT_CLAIM_SALT)


def _load_claim(value):
    """Return the claimed public_id, or None on ANY failure (never raises)."""
    if not value:
        return None
    try:
        payload = signing.loads(value, salt=INSTANT_CLAIM_SALT, max_age=INSTANT_CLAIM_MAX_AGE)
    except signing.BadSignature:
        return None
    if not isinstance(payload, dict):
        return None
    inbox_id = payload.get("i")
    if not isinstance(inbox_id, str) or not inbox_id:
        return None
    return inbox_id


def _resolve_instant_inbox_by_public_id(public_id: str):
    """Resolve ONLY within the instant user's shared-domain, non-deleted rows.

    Mirrors apps.dashboard.demo._demo_inbox's no-IDOR-oracle argument: a
    forged claim naming a real customer's inbox public_id can never match
    here because the ``user=instant_user`` filter excludes every other
    account's rows, regardless of what public_id string is presented.
    """
    instant_user = get_instant_user()
    try:
        return Inbox.objects.filter(
            user=instant_user,
            public_id=public_id,
            domain__isnull=True,
            deleted_at__isnull=True,
        ).get()
    except Inbox.DoesNotExist:
        return None


@require_GET
def instant_email(request):
    """GET /instant/email.md — mint or (idempotently) refresh an instant inbox."""
    claimed_id = _load_claim(request.COOKIES.get(INSTANT_CLAIM_COOKIE_NAME)) or _load_claim(
        request.GET.get("claim")
    )
    if claimed_id is not None:
        inbox = _resolve_instant_inbox_by_public_id(claimed_id)
        if inbox is not None:
            return _render_instant_email(inbox, request, minted=False)
        # Claim decoded fine but the inbox is gone (e.g. purged) — fall
        # through to minting a fresh one exactly as if there were no claim.

    ip = _client_ip(request)
    try:
        increment_rate_limit(
            INSTANT_INBOX_MINT_RATE_NAME,
            ip,
            limit=INSTANT_INBOX_MINT_RATE_LIMIT,
            window_seconds=INSTANT_INBOX_MINT_RATE_WINDOW,
        )
    except RateLimitExceeded as exc:
        return _rate_limited(exc)

    instant_user = get_instant_user()
    inbox = None
    for _ in range(10):
        try:
            with transaction.atomic():
                inbox = Inbox.objects.create(
                    user=instant_user,
                    label="Instant inbox",
                    routing_key=random_routing_key(),
                )
            break
        except IntegrityError:
            continue
    if inbox is None:
        return _md_response("# Unavailable\n\nCould not allocate an instant address. Try again.\n", status=503)

    return _render_instant_email(inbox, request, minted=True)


def _render_instant_email(inbox, request, *, minted: bool) -> HttpResponse:
    claim = _mint_claim(inbox)
    scheme = "https" if request.is_secure() else "http"
    host = request.get_host()
    messages_url = f"{scheme}://{host}/instant/email/{inbox.address}/messages.md"
    lines = [
        "# Instant inbox" + (" (new)" if minted else ""),
        "",
        f"Address: `{inbox.address}`",
        "Expires: 24 hours from mint",
        "",
        "## Read messages",
        "",
        f"With cookies (recommended): `GET {messages_url}`",
        "",
        f"Without cookies (capability URL, works for 24h): `GET {messages_url}?claim={claim}`",
        "",
        "## curl example",
        "",
        "```",
        f"curl --cookie-jar cookies.txt --cookie cookies.txt {scheme}://{host}/instant/email.md",
        f"curl --cookie cookies.txt {messages_url}",
        "```",
        "",
    ]
    response = _md_response("\n".join(lines))
    response.set_cookie(
        INSTANT_CLAIM_COOKIE_NAME,
        claim,
        max_age=INSTANT_CLAIM_MAX_AGE,
        path="/instant",
        httponly=True,
        secure=True,
        samesite="Lax",
    )
    return response


def _is_unsafe_char(ch: str) -> bool:
    """Line breaks of every kind (CR, LF, VT, FF, NEL, U+2028/2029), other
    C0/C1 controls, DEL, and invisible format chars (bidi overrides/isolates,
    zero-width) — anything that can end a line or disguise one."""
    return unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp")


def _md_escape(value) -> str:
    "Public API."
    if value is None:
        return ""
    text = str(value).replace("`", "'")
    out: list[str] = []
    for ch in text:
        if _is_unsafe_char(ch):
            if out and out[-1] == " ":
                continue
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


@require_GET
def instant_messages(request, email):
    """GET /instant/email/<email>/messages.md — list an instant inbox's mail.

    metadata is NEVER emitted here (R4) — the fields below are drawn
    explicitly from MessageSummarySerializer's public fields, and metadata
    is not one of them.
    """
    ip = _client_ip(request)
    try:
        increment_rate_limit(
            INSTANT_MESSAGES_RATE_NAME,
            ip,
            limit=INSTANT_MESSAGES_RATE_LIMIT,
            window_seconds=INSTANT_MESSAGES_RATE_WINDOW,
        )
    except RateLimitExceeded as exc:
        return _rate_limited(exc)

    routing_key = parse_shared_address(email)
    if routing_key is None:
        return _not_found()

    instant_user = get_instant_user()
    try:
        inbox = Inbox.objects.filter(
            user=instant_user,
            routing_key=routing_key,
            domain__isnull=True,
            deleted_at__isnull=True,
        ).get()
    except Inbox.DoesNotExist:
        return _not_found()

    claimed_id = _load_claim(request.COOKIES.get(INSTANT_CLAIM_COOKIE_NAME)) or _load_claim(
        request.GET.get("claim")
    )
    if claimed_id is None or claimed_id != inbox.public_id:
        return _not_found()

    rows = list(
        inbox.messages.order_by("-received_at", "-id")[:INSTANT_MAX_MESSAGES]
    )
    if not rows:
        body = f"# Messages for `{inbox.address}`\n\nNo messages yet.\n"
        return _md_response(body)

    lines = [f"# Messages for `{inbox.address}`", ""]
    for msg in rows:
        lines.append(f"## `{_md_escape(msg.subject)}`")
        lines.append("")
        lines.append(f"- From: `{_md_escape(msg.header_from)}`")
        lines.append(f"- Received: `{_md_escape(msg.received_at.isoformat())}`")
        otp = msg.otp or msg.detected_otp
        if otp:
            lines.append(f"- OTP: `{_md_escape(otp)}`")
        if msg.magic_link:
            lines.append(f"- Magic link: `{_md_escape(msg.magic_link)}`")
        lines.append("")
    return _md_response("\n".join(lines))
