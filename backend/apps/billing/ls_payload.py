"Public API."

import hashlib

EVENT_NAME_MAX = 64  # BillingEvent.event_name
EVENT_KEY_MAX = 80  # BillingEvent.event_key


class MalformedPayload(Exception):
    pass


def event_name_and_key(payload, raw_body):
    """``(event_name, event_key)`` or raise MalformedPayload. event_name must
    be a 1..64 char string; a missing/non-string/oversized webhook_id falls
    back to the body hash (the existing dedupe fallback)."""
    try:
        meta = payload["meta"]
        event_name = meta["event_name"]
    except (KeyError, TypeError) as exc:
        raise MalformedPayload() from exc
    if not isinstance(event_name, str) or not 0 < len(event_name) <= EVENT_NAME_MAX:
        raise MalformedPayload()
    webhook_id = meta.get("webhook_id") if isinstance(meta, dict) else None
    if isinstance(webhook_id, str) and 0 < len(webhook_id) <= EVENT_KEY_MAX:
        return event_name, webhook_id
    return event_name, hashlib.sha256(raw_body).hexdigest()
