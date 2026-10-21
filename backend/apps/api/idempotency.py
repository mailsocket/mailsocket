"Public API."

import functools
import hashlib
import json
import re
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from apps.accounts.models import IdempotencyRecord

from .errors import error_response

HEADER = "Idempotency-Key"
_META_KEY = "HTTP_IDEMPOTENCY_KEY"
REPLAYED_HEADER = "Idempotent-Replayed"
KEY_RE = re.compile(r"[\x20-\x7e]{1,255}")
RETENTION = timedelta(hours=24)
# A placeholder older than this is an abandoned claim (worker killed
# mid-request; gunicorn --timeout is 120s), not a request still in flight.
# Reclaiming it is safe: the view's work and the completion commit together,
# so an abandoned placeholder never has committed work behind it.
IN_PROGRESS_TIMEOUT = timedelta(minutes=5)
# Response headers worth replaying (the rest are per-request: rate limits,
# X-Request-Id, and so on, and are set fresh on the replay).
_REPLAYED_HEADERS = ("Location",)
# Statuses whose answer is deterministic for the same request.
STORED_ERROR_STATUSES = frozenset({400, 404, 422})
# Error codes inside those statuses that the caller can fix and then retry
# with the same key (raise a limit, approve a recipient, verify a domain).
# They are not stored, so the retry runs again.
RETRYABLE_CODES = frozenset({
    "outbound_allowlist_limit_reached",
    "recipient_not_approved",
    "domain_not_found_or_unverified",
})
UNREADABLE_BODY_MESSAGE = (
    "The request body is too large or could not be read, so it cannot be "
    "used with Idempotency-Key."
)


class _BodyUnreadable(Exception):
    pass


def _raw_body(request) -> bytes:
    """The raw request body. Raises _BodyUnreadable instead of guessing:
    Django raises RequestDataTooBig past DATA_UPLOAD_MAX_MEMORY_SIZE, and
    RawPostDataException if the stream was already consumed."""
    try:
        return request.body
    except Exception as exc:
        raise _BodyUnreadable() from exc


def _parse_body(raw: bytes):
    """(parsed_json_or_None, fingerprint). Fingerprint = sha256 of the
    canonical JSON body (sorted keys, compact), or of the raw bytes when the
    body is not JSON."""
    if not raw:
        return None, hashlib.sha256(b"empty:").hexdigest()
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None, hashlib.sha256(b"raw:" + raw).hexdigest()
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return parsed, hashlib.sha256(b"json:" + canonical.encode("utf-8")).hexdigest()


def _error_code(response):
    data = getattr(response, "data", None)
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        return data["error"].get("code")
    return None


def _is_stored(response) -> bool:
    if not isinstance(response, Response):
        return False
    status_code = response.status_code
    if 200 <= status_code < 300:
        return True
    return status_code in STORED_ERROR_STATUSES and _error_code(response) not in RETRYABLE_CODES


def _replay(record):
    from .middleware import current_request_id

    data = json.loads(record.response_body) if record.response_body else None
    # A stored error body is replayed with THIS request's id, so
    # error.request_id still matches the X-Request-Id header.
    request_id = current_request_id.get()
    if isinstance(data, dict) and isinstance(data.get("error"), dict) and request_id is not None:
        data["error"]["request_id"] = request_id
    response = Response(data, status=record.status_code)
    for name, value in (record.response_headers or {}).items():
        response[name] = value
    response[REPLAYED_HEADER] = "true"
    return response


def _claim(user, api_key, key, method, path, fingerprint):
    """Insert and commit the placeholder. Returns (record, created)."""
    now = timezone.now()
    for _ in range(2):
        try:
            with transaction.atomic():
                return IdempotencyRecord.objects.create(
                    user=user,
                    api_key=api_key,
                    key=key,
                    method=method,
                    path=path,
                    request_fingerprint=fingerprint,
                    created_at=now,
                ), True
        except IntegrityError:
            pass
        existing = IdempotencyRecord.objects.filter(user=user, api_key=api_key, key=key).first()
        if existing is None:
            continue  # deleted between our insert and read; try again
        expired = existing.created_at <= now - RETENTION
        abandoned = existing.status_code is None and existing.created_at <= now - IN_PROGRESS_TIMEOUT
        if expired or abandoned:
            stale = IdempotencyRecord.objects.filter(pk=existing.pk, created_at=existing.created_at)
            if not expired:
                # Reclaim only a placeholder that is STILL unfinished. Without
                # this, a DELETE that waited on the row lock while the original
                # request completed would re-check only created_at (unchanged),
                # delete the completed record and let us repeat the mutation.
                stale = stale.filter(status_code__isnull=True)
            stale.delete()
            continue  # re-insert; if the original completed meanwhile we re-read and replay it
        return existing, False
    return None, False


def _complete(record, response):
    """Turn the placeholder into the stored outcome. Runs inside the same
    transaction as the view, so it commits together with the view's work."""
    data = response.data
    if isinstance(data, dict) and isinstance(data.get("error"), dict) and "request_id" in data["error"]:
        data = {**data, "error": {k: v for k, v in data["error"].items() if k != "request_id"}}
    body = "" if data is None else JSONRenderer().render(data).decode("utf-8")
    headers = {name: response[name] for name in _REPLAYED_HEADERS if response.has_header(name)}
    updated = IdempotencyRecord.objects.filter(pk=record.pk, status_code__isnull=True).update(
        status_code=response.status_code,
        response_body=body,
        response_headers=headers,
    )
    if updated != 1:
        # Our placeholder was reclaimed by another request (we outlived
        # IN_PROGRESS_TIMEOUT). Committing now could duplicate that
        # request's work, so roll this attempt back instead.
        raise RuntimeError("idempotency placeholder lost before completion")


def run_idempotent(view, request, handler, applies=None, enabled=None, gate=None):
    if _META_KEY not in request.META:
        return handler()
    # A switched-off surface must look exactly as it does without the header
    # (e.g. outbound's uniform 404), so the header is not even validated.
    if enabled is not None and not enabled():
        return handler()

    try:
        raw = _raw_body(request)
    except _BodyUnreadable:
        return error_response("validation_error", 400, fields={"body": [UNREADABLE_BODY_MESSAGE]})
    parsed, fingerprint = _parse_body(raw)
    if applies is not None and not applies(parsed):
        return handler()

    key = request.META[_META_KEY]
    if not KEY_RE.fullmatch(key):
        return error_response(
            "validation_error",
            400,
            fields={HEADER: ["Must be 1-255 printable ASCII characters."]},
        )

    method = request.method
    path = request.path
    
    # before any claim or replay, so a stored success is never handed to a
    # caller the endpoint would refuse today. Their refusals are never
    # stored.
    if gate is not None:
        refused = gate(request)
        if refused is not None:
            return refused
    record, created = _claim(request.user, request.auth, key, method, path, fingerprint)
    if record is None:
        return error_response("idempotency_in_progress", 409)
    if not created:
        if (record.method, record.path, record.request_fingerprint) != (method, path, fingerprint):
            return error_response("idempotency_key_reused", 422)
        if record.status_code is None:
            return error_response("idempotency_in_progress", 409)
        return _replay(record)

    stored = False
    try:
        # ONE transaction for the view's work and the completion: both
        # commit or neither does. ``stored`` flips only after the COMMIT
        # succeeded; if the commit itself fails, the placeholder is deleted
        # below like any other failure (nothing of the request committed).
        completed = False
        with transaction.atomic():
            try:
                response = handler()
            except Exception as exc:
                # Convert exactly as DRF's dispatch would, so a stored 4xx
                # body is what the caller receives. Anything not stored is
                # re-raised untouched (DRF/Django handle it the same way as
                # a request without the header) and rolls the view's work
                # back with the whole block. A stored 4xx keeps whatever
                # the view did before raising (e.g. a rate-limit tick), the
                # same as without the header.
                response = view.handle_exception(exc)
                if not _is_stored(response):
                    raise
            if _is_stored(response):
                _complete(record, response)
                completed = True
            # A non-stored RESPONSE (402/403/409/429/...) is a refusal that
            # created nothing; its side effects (rate-limit counters) commit
            # exactly as they do without the header, so rotating keys never
            # refunds a rate-limit budget.
        stored = completed
        return response
    finally:
        if not stored:
            _release(record)


def _release(record):
    """Remove an uncompleted placeholder (separate step, after the request's
    transaction has rolled back or committed without storing). A killed
    worker never gets here; its placeholder is reclaimed after
    IN_PROGRESS_TIMEOUT."""
    IdempotencyRecord.objects.filter(pk=record.pk, status_code__isnull=True).delete()


def idempotent(applies=None, enabled=None, gate=None):
    """Decorator for BearerAPIView POST handlers.

    ``enabled()`` returning False makes the header a no-op (checked first,
    before the header is validated). ``applies(parsed_body)`` returning
    False skips idempotency for that request (e.g. read-only ``fetch``
    batches). ``gate(request)`` returning a Response refuses the request
    before any claim or replay (the view's own cheap caller gates, which
    the view still re-runs itself)."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, request, *args, **kwargs):
            return run_idempotent(
                self, request, lambda: func(self, request, *args, **kwargs), applies, enabled, gate
            )

        return wrapper

    return decorator


def purge_expired_idempotency_records(*, batch_size=500, now=None):
    """Delete records older than 24h in finite batches. Returns the count."""
    cutoff = (now or timezone.now()) - RETENTION
    total = 0
    while True:
        ids = list(
            IdempotencyRecord.objects.filter(created_at__lt=cutoff)
            .order_by("created_at")
            .values_list("pk", flat=True)[:batch_size]
        )
        if not ids:
            return total
        deleted, _ = IdempotencyRecord.objects.filter(pk__in=ids).delete()
        total += deleted
