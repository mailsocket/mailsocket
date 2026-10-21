"""Shared message-list filter helpers for the REST API and the dashboard.

Both surfaces expose the same optional filters — ``has_otp``,
``subject_contains``, ``from``, and ``read`` — combined with AND. They share this module so
the filtering semantics cannot drift, and so each surface can bind the *exact*
set of applied filters into its pagination cursor. A cursor minted under one
filter combination must never be replayed under another, so the returned
``signature`` is the canonical string both callers embed in their cursor and
re-check on decode.
"""

import json

SUBJECT_CONTAINS_MAX = 200

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def parse_bool(value):
    """Return True/False for a truthy/falsy string, or None when invalid.

    ``None`` (invalid or absent) means "do not apply this filter", so garbage
    input is ignored gracefully rather than raising a 500.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def _text_param(params, name, *, cap=None):
    value = params.get(name)
    if value is None:
        return ""
    text = str(value).strip()
    if cap is not None and len(text) > cap:
        text = text[:cap]
    return text


def apply_message_filters(queryset, params):
    """Apply ``has_otp`` / ``subject_contains`` / ``from`` / ``read`` / ``q``
    to ``queryset``.

    ``params`` is any mapping with ``.get`` (``request.query_params`` or
    ``request.GET``). Returns ``(queryset, signature)`` where ``signature`` is a
    canonical, stable string encoding exactly the filters that were applied (in
    a fixed order). An empty signature means no filters were applied. The
    caller is responsible for binding ``signature`` into its pagination cursor.
    """
    parts = {}

    has_otp = parse_bool(params.get("has_otp"))
    if has_otp is True:
        queryset = queryset.filter(otp__isnull=False).exclude(otp="")
        parts["has_otp"] = True

    subject = _text_param(params, "subject_contains", cap=SUBJECT_CONTAINS_MAX)
    if subject:
        queryset = queryset.filter(subject__icontains=subject)
        parts["subject_contains"] = subject

    sender = _text_param(params, "from")
    if sender:
        queryset = queryset.filter(header_from__icontains=sender)
        parts["from"] = sender

    # read=true|false|all — "all"/absent/garbage means "do not filter" (back-
    # compat default). The parts[...] assignment below MUST run so read=true|false is
    # bound into the pagination cursor's filter_signature (views.py:339-340) —
    # otherwise a cursor minted under one read filter could be replayed under
    # another and silently skip/duplicate rows.
    read_raw = params.get("read")
    read_norm = str(read_raw).strip().lower() if read_raw is not None else None
    if read_norm == "true":
        queryset = queryset.filter(read_at__isnull=False)
        parts["read"] = True
    elif read_norm == "false":
        queryset = queryset.filter(read_at__isnull=True)
        parts["read"] = False
    # else: "all", absent, or unrecognized -> no filter applied.

    
    # capped at SUBJECT_CONTAINS_MAX (same _text_param truncation, never a
    # 500), pure ORM, AND-composed with everything above. Bound into the
    # signature exactly like subject_contains so a cursor minted under one
    # `q` can never be replayed under another.
    from django.db.models import Q

    q = _text_param(params, "q", cap=SUBJECT_CONTAINS_MAX)
    if q:
        queryset = queryset.filter(Q(subject__icontains=q) | Q(header_from__icontains=q))
        parts["q"] = q

    return queryset, filter_signature(parts)


def filter_signature(parts: dict) -> str:
    "Public API."
    if not parts:
        return ""
    return "f2:" + json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
