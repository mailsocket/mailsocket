import logging
import secrets

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .services import IngestError, parse_after_commit, store_message

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def ingest(request):
    supplied_secret = request.headers.get("X-Mailsocket-Ingest-Secret", "")
    expected_secret = settings.INGEST_SHARED_SECRET
    # Compare as bytes: compare_digest on str raises TypeError for non-ASCII,
    # which would surface as a 500 instead of a clean 403.
    if not secrets.compare_digest(
        supplied_secret.encode("utf-8", "surrogatepass"),
        expected_secret.encode("utf-8", "surrogatepass"),
    ):
        return JsonResponse({"detail": "Forbidden."}, status=403)
    if request.content_type != "application/octet-stream":
        return JsonResponse({"detail": "Content-Type must be application/octet-stream."}, status=400)

    content_length = request.META.get("CONTENT_LENGTH")
    try:
        if content_length and int(content_length) > settings.MAX_INBOUND_BYTES:
            return JsonResponse({"detail": "Message exceeds the inbound byte limit."}, status=413)
    except ValueError:
        return JsonResponse({"detail": "Malformed Content-Length."}, status=400)

    try:
        raw_bytes = request.body
    except RequestDataTooBig:
        return JsonResponse({"detail": "Message exceeds the inbound byte limit."}, status=413)
    if len(raw_bytes) > settings.MAX_INBOUND_BYTES:
        return JsonResponse({"detail": "Message exceeds the inbound byte limit."}, status=413)

    try:
        result = store_message(raw_bytes=raw_bytes, headers=request.headers)
    except IngestError as exc:
        return JsonResponse({"detail": exc.detail}, status=exc.status_code)

    message = result.message
    if message is None:
        # CODE FIX 0 (gate R2): a recognized bounce (obe_ recipient) never
        # creates a customer Message row — respond with the BounceEvent's
        # id instead, still 201 (Postfix must treat this as accepted, never
        # requeued/retried).
        event = result.bounce_event
        return JsonResponse(
            {
                "bounce_event_id": event.id,
                "parse_status": event.parse_status,
                "deduplicated": False,
            },
            status=201,
        )
    if not result.deduplicated:
        # Inline parse (D3: parsing is inline, store-raw-first). The raw row is
        # already committed, so /messages/wait sees the OTP in this request
        # instead of after the next reconcile_ingest cron tick. Usage metering
        # and webhook enqueue run inside the parse step and are idempotent
        # (Message.usage_counted / WebhookDelivery get_or_create); a dedup
        # retry never re-parses. Any parse-path failure must not turn a stored
        # message into a Postfix tempfail: log, answer 201 with the durable
        # row's state, and let reconcile_ingest retry it.
        stored_pk = message.pk
        try:
            message = parse_after_commit(stored_pk)
        except Exception:
            logger.exception("inline_parse_failed message_id=%s", stored_pk)
    return JsonResponse(
        {
            "public_id": message.public_id,
            "ingest_status": message.ingest_status,
            "deduplicated": result.deduplicated,
        },
        status=200 if result.deduplicated else 201,
    )
