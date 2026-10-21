"Public API."

import logging
import secrets

from django.db import IntegrityError, transaction
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_GET

from apps.accounts.client_ip import client_ip as _client_ip
from apps.accounts.funnel_signals import record_demo_start
from apps.accounts.services import (
    DEMO_INBOX_RATE_LIMIT,
    DEMO_INBOX_RATE_NAME,
    DEMO_INBOX_RATE_WINDOW,
    DEMO_SAMPLE_PER_INBOX_LIMIT,
    DEMO_SAMPLE_RATE_LIMIT,
    DEMO_SAMPLE_RATE_NAME,
    DEMO_SAMPLE_RATE_WINDOW,
    RateLimitExceeded,
    get_demo_user,
    increment_rate_limit,
)
from apps.api.serializers import MessageSummarySerializer
from apps.api.views import _parse_wait_since, _parse_wait_timeout, perform_wait
from apps.inboxes.models import Inbox, Message
from apps.inboxes.services import random_routing_key
from apps.ingest.parser import parse_message
from apps.ingest.services import store_message

from .decorators import post_only

logger = logging.getLogger(__name__)

# try-page.md step 1: the demo should feel like a real long-poll (one call,
# re-issued immediately on 204) instead of visibly re-polling every few
# seconds — the product pitch is "no polling". The clamp is raised to match
# the paid API's own ceiling (apps.api.views.WAIT_TIMEOUT_MAX = 25) because
# gunicorn capacity allows it: a blocked wait occupies one gthread thread,
# not a whole worker (3 workers x 32 threads = 96 slots), and demo traffic is
# the DEMO USER, who is on the Free plan — so every demo wait routes through
# ``wait_slot`` -> ``is_free_plan`` -> the WAIT_CAPACITY_FREE_LIMIT=12 sub-pool
# (apps.accounts.services.acquire_wait_capacity), never the shared headroom.
# A longer per-call timeout does not change how many demo waits can be
# in-flight at once (still capped at 12), it only changes how long each of
# those <=12 slots is held before the caller re-issues — so paid traffic
# keeps its reserved (WAIT_CAPACITY_LIMIT - WAIT_CAPACITY_FREE_LIMIT) = 52
# slots regardless. See test_wait_proxy_uses_free_subpool_at_full_clamp.
DEMO_WAIT_TIMEOUT_MAX = 25.0
DEMO_WAIT_TIMEOUT_DEFAULT = 25.0


def _demo_user():
    return get_demo_user()


def _demo_inbox(public_id) -> Inbox:
    """Resolve a demo inbox ONLY by the demo user + its public id.

    A foreign (real customer) or unknown public_id never matches and surfaces as
    a plain 404 — identical to "not owned", so the demo surface is unusable as
    an IDOR oracle against real inboxes.
    """
    demo_user = _demo_user()
    try:
        return Inbox.objects.filter(
            user=demo_user, public_id=public_id, deleted_at__isnull=True
        ).get()
    except Inbox.DoesNotExist:
        raise Http404("Demo inbox not found.") from None


def _demo_inbox_json(inbox) -> dict:
    return {"id": inbox.public_id, "address": inbox.address}


@csrf_exempt
@post_only("dashboard:try-it")
def try_create_inbox(request):
    """POST /docs/try/inbox — mint a demo inbox under the shared demo user.

    CSRF-exempt by design: this is an anonymous, session-less JSON endpoint
    (the page fetches it with no cookie), so there are no ambient credentials a
    cross-site request could ride. The IP rate limit is the abuse control.
    """
    client_ip = _client_ip(request)
    try:
        increment_rate_limit(
            DEMO_INBOX_RATE_NAME,
            client_ip,
            limit=DEMO_INBOX_RATE_LIMIT,
            window_seconds=DEMO_INBOX_RATE_WINDOW,
        )
    except RateLimitExceeded as exc:
        response = JsonResponse(
            {"error": "Too many demo inboxes from this address. Try again later."},
            status=429,
        )
        response.headers["Cache-Control"] = "no-store"
        if exc.reset_at is not None:
            response.headers["Retry-After"] = str(
                max(1, int((exc.reset_at - timezone.now()).total_seconds()))
            )
        return response

    demo_user = _demo_user()

    inbox = None
    for attempt in range(10):
        try:
            with transaction.atomic():
                inbox = Inbox.objects.create(
                    user=demo_user,
                    label="Demo inbox",
                    routing_key=random_routing_key(),
                )
            break
        except IntegrityError:
            # routing_key collision — retry with a fresh key. Log so that a
            # SYSTEMATIC collision (e.g. a broken key generator) becomes
            # visible instead of silently degrading to 503s.
            logger.warning(
                "demo inbox routing_key collision on attempt %d/10", attempt + 1
            )
            continue
    if inbox is None:
        logger.error("demo inbox allocation exhausted 10 retries; returning 503")
        response = JsonResponse(
            {"error": "Could not allocate a demo address. Try again."}, status=503
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    # activation item 2a: count the demo start (per UTC day + the page's ?ref=,
    # which try.js forwards on this POST). Best-effort, counts only.
    record_demo_start(request.GET.get("ref"))
    response = JsonResponse(_demo_inbox_json(inbox), status=201)
    response.headers["Cache-Control"] = "no-store"
    return response


@require_GET
def try_wait(request, public_id):
    """GET /docs/try/inbox/<public_id>/wait — long-poll a demo inbox for an OTP.

    Scoped to the demo user's inboxes only; clamps ``timeout`` low; reuses the
    real wait path (per-account + global caps, same OTP result shape) via a
    summary serializer so no raw/HTML body ever reaches the page.
    """
    inbox = _demo_inbox(public_id)

    # Reuse the API's battle-tested timeout parser (handles NaN / negative /
    # non-numeric robustly, unit-tested in test_wait.py) instead of a hand-rolled
    # copy, then re-clamp to the demo's deliberately LOW ceiling so anon traffic
    # can never hold a worker thread long.
    raw_timeout = request.GET.get("timeout")
    if raw_timeout is None:
        requested = DEMO_WAIT_TIMEOUT_DEFAULT
    else:
        requested = _parse_wait_timeout(raw_timeout)
    timeout = max(0.25, min(DEMO_WAIT_TIMEOUT_MAX, requested))

    since = _parse_wait_since(request.GET.get("since"), inbox=inbox)
    if since is None:
        since = timezone.now()

    response = perform_wait(
        user=inbox.user,
        inbox=inbox,
        require="otp",
        min_confidence=0.0,
        timeout=timeout,
        since=since,
        serializer_class=MessageSummarySerializer,
    )

    # perform_wait returns a DRF Response; translate to a plain JSON body so the
    # demo surface carries no DRF baggage and always emits a no-store header.
    status_code = response.status_code
    if status_code == 204:
        response = JsonResponse({"data": None}, status=204)
        response.headers["Cache-Control"] = "no-store"
        return response
    if status_code == 429:
        drf_response = response
        response = JsonResponse(drf_response.data, status=429)
        response.headers["Cache-Control"] = "no-store"
        retry_after = drf_response.get("Retry-After")
        if retry_after is not None:
            response.headers["Retry-After"] = retry_after
        return response
    data = response.data.get("data") if isinstance(response.data, dict) else None
    response = JsonResponse({"data": data}, status=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


DEMO_SAMPLE_SENDER = "demo@mailsocket.app"
DEMO_SAMPLE_FROM_HEADER = "mailsocket demo <demo@mailsocket.app>"


def _sample_raw_message(
    inbox, code: str, *, from_header: str = DEMO_SAMPLE_FROM_HEADER, sender: str = DEMO_SAMPLE_SENDER
) -> bytes:
    """Build the synthetic RFC822 bytes for a demo "sample code" request.

    Deliberately shaped like a real OTP mail (From/Subject/body a real
    provider would send) so the production parser (apps.ingest.parser) is
    exercised exactly as it would be for a genuine inbound message — this is
    NOT a shortcut that writes otp/otp_confidence directly onto the Message
    row. No SMTP, no outbound traffic: the bytes never leave this process:
    they're fed straight into apps.ingest.services.store_message, the same
    entry point Postfix's /internal/ingest view calls.
    """
    body = f"Your code is {code}\r\n"
    local = sender.split("@", 1)[0]
    raw = (
        f"From: {from_header}\r\n"
        f"To: {inbox.address}\r\n"
        f"Message-ID: <{local}-sample-{secrets.token_urlsafe(8)}@mailsocket.app>\r\n"
        "Subject: Your verification code\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
    ).encode("utf-8") + body.encode("utf-8")
    return raw


def deliver_sample_code(inbox, *, from_header: str = DEMO_SAMPLE_FROM_HEADER, sender: str = DEMO_SAMPLE_SENDER):
    """Store + parse one synthetic OTP mail for ``inbox`` through the SAME
    store_message + parse_message pipeline real inbound mail uses (so usage
    metering, parsing and webhook enqueue behave exactly as for a real
    message). Returns the StoreResult. Raises IngestError when the inbox
    cannot receive mail. Callers own authorization and rate limiting.
    """
    code = f"{secrets.randbelow(1_000_000):06d}"
    raw = _sample_raw_message(inbox, code, from_header=from_header, sender=sender)
    result = store_message(
        raw_bytes=raw,
        headers={"X-Envelope-To": inbox.address, "X-Envelope-From": sender},
    )
    _parse_stored_sample(result, inbox)
    return result


def _parse_stored_sample(result, inbox) -> None:
    message = result.message
    if message is None or result.deduplicated:
        return
    try:
        parse_message(message.pk)
    except Exception:
        logger.exception("sample_parse_failed message_id=%s inbox_id=%s", message.pk, inbox.pk)


@csrf_protect
@post_only("dashboard:try-it")
def try_send_sample(request, public_id):
    """POST /docs/try/inbox/<public_id>/sample — synthesize one inbound code.

    CSRF-protected (unlike the other two demo endpoints): this one actually
    creates data server-side on the caller's behalf, so the page's fetch
    carries the csrftoken cookie's value in X-CSRFToken (see try.js).

    Guards, in order, fail-closed to a uniform 404 for anything that is not
    "a live demo inbox owned by the shared demo user":
      - the public_id must resolve via the SAME ``_demo_inbox`` lookup the
        wait proxy uses (demo user + public_id + not deleted) — a foreign or
        unknown id 404s identically, so this can never be an IDOR oracle
        against a real customer's inbox, nor create mail in one.
      - the inbox must be enabled (``is_enabled``); a disabled demo inbox
        404s rather than quietly reviving it.
    Then the abuse controls:
      - per-IP rate limit (DEMO_SAMPLE_RATE_LIMIT/hour, its own bucket,
        separate from inbox creation).
      - per-inbox cap (DEMO_SAMPLE_PER_INBOX_LIMIT) so a single IP cannot
        keep minting mail into the same inbox forever even while under the
        hourly rate limit.
    On success, builds a raw RFC822 message server-side (random 6-digit
    code baked into the body) and feeds it through the EXACT same
    store_message + parse_message pipeline real inbound mail uses, so the
    OTP the page shows came from the production parser, not a shortcut.
    """
    inbox = _demo_inbox(public_id)
    if not inbox.is_enabled:
        raise Http404("Demo inbox not found.")

    client_ip = _client_ip(request)
    try:
        increment_rate_limit(
            DEMO_SAMPLE_RATE_NAME,
            client_ip,
            limit=DEMO_SAMPLE_RATE_LIMIT,
            window_seconds=DEMO_SAMPLE_RATE_WINDOW,
        )
    except RateLimitExceeded as exc:
        response = JsonResponse(
            {"error": "Too many sample codes requested. Try again later."},
            status=429,
        )
        response.headers["Cache-Control"] = "no-store"
        if exc.reset_at is not None:
            response.headers["Retry-After"] = str(
                max(1, int((exc.reset_at - timezone.now()).total_seconds()))
            )
        return response

    
    # (SELECT ... FOR UPDATE) so concurrent sample POSTs for the SAME inbox
    # serialize on count+store — without it, N parallel requests all see the
    # same count and the inbox ends up with 2+N messages. Parsing runs only
    # AFTER this block commits (store-raw-first, and the lock is not held
    # across the parser).
    code = f"{secrets.randbelow(1_000_000):06d}"
    raw = _sample_raw_message(inbox, code)
    with transaction.atomic():
        Inbox.objects.select_for_update().get(pk=inbox.pk)
        sample_count = Message.objects.filter(
            inbox=inbox, envelope_from="demo@mailsocket.app"
        ).count()
        if sample_count >= DEMO_SAMPLE_PER_INBOX_LIMIT:
            result = None
        else:
            result = store_message(
                raw_bytes=raw,
                headers={
                    "X-Envelope-To": inbox.address,
                    "X-Envelope-From": "demo@mailsocket.app",
                },
            )
    if result is None:
        response = JsonResponse(
            {"error": "This demo inbox has already received its sample codes."},
            status=429,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    message = result.message
    if message is None:
        # Cannot happen for this envelope recipient (inbox.address is never
        # the OUTBOUND_BOUNCE_DOMAIN), but fail closed rather than crash if
        # it ever did.
        logger.error("demo_sample_store_returned_no_message inbox_id=%s", inbox.pk)
        response = JsonResponse({"error": "Could not create the sample message."}, status=503)
        response.headers["Cache-Control"] = "no-store"
        return response
    if not result.deduplicated:
        try:
            message = parse_message(message.pk)
        except Exception:
            logger.exception("demo_sample_parse_failed message_id=%s", message.pk)

    response = JsonResponse({"ok": True}, status=201)
    response.headers["Cache-Control"] = "no-store"
    return response
