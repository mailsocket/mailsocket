from django.core import signing
from django.db.models import Q
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import ValidationError

CURSOR_SALT = "mailsocket.api.v1.cursor"
CURSOR_MAX_AGE = 86400


def parse_limit(request):
    value = request.query_params.get("limit", "25")
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = 0
    if not 1 <= limit <= 100:
        raise ValidationError({"limit": ["Must be an integer from 1 to 100."]})
    return limit


def paginate(request, queryset, *, kind, owner_id, active_filter, timestamp_field):
    limit = parse_limit(request)
    cursor = request.query_params.get("cursor")
    if cursor:
        try:
            payload = signing.loads(cursor, salt=CURSOR_SALT, max_age=CURSOR_MAX_AGE)
            timestamp = parse_datetime(payload["timestamp"])
            if (
                payload["kind"] != kind
                or payload["owner"] != owner_id
                or payload["filter"] != active_filter
                or timestamp is None
            ):
                raise ValueError
            row_id = int(payload["id"])
        except (signing.BadSignature, signing.SignatureExpired, KeyError, TypeError, ValueError):
            raise ValidationError({"cursor": ["Invalid or expired cursor."]}) from None
        queryset = queryset.filter(
            Q(**{f"{timestamp_field}__lt": timestamp})
            | Q(**{timestamp_field: timestamp, "id__lt": row_id})
        )
    rows = list(queryset.order_by(f"-{timestamp_field}", "-id")[: limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more:
        last = rows[-1]
        next_cursor = signing.dumps({
            "kind": kind,
            "owner": owner_id,
            "filter": active_filter,
            "timestamp": getattr(last, timestamp_field).isoformat(),
            "id": last.pk,
        }, salt=CURSOR_SALT, compress=True)
    return rows, {"next_cursor": next_cursor, "has_more": has_more}
