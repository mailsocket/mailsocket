from django.http import Http404
from rest_framework.exceptions import APIException, NotAuthenticated, NotFound, ParseError, ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from apps.accounts.services import RateLimitExceeded

MESSAGES = {
    "authentication_required": "Authentication required.",
    "account_forbidden": "Account is not permitted to use the API.",
    "not_found": "Resource not found.",
    "method_not_allowed": "Method not allowed.",
    "not_acceptable": "Requested response format is not acceptable.",
    "conflict": "Request conflicts with the current state.",
    "plan_limit_reached": "Plan limit reached.",
    "plan_upgrade_required": "This feature requires a paid plan. Upgrade to use it.",
    "slug_taken": "That inbox address is already taken. Choose a different routing_key.",
    "unsupported_media_type": "Request media type is not supported.",
    "validation_error": "Request validation failed.",
    "rate_limited": "Rate limit exceeded.",
    "too_many_wait_requests": "Too many concurrent wait requests.",
    "wait_capacity": "Too many concurrent wait requests across all accounts.",
    "webhook_entitlement_required": "Webhooks require a paid plan.",
    "webhook_not_configured": "No webhook is configured for this inbox.",
    "webhook_ssrf_rejected": "The webhook URL was rejected by SSRF validation.",
    "webhook_unreachable": "The webhook receiver could not be reached.",
    "server_error": "An internal error occurred.",
    #  (BYOD)
    "byod_disabled": "Custom domains are not enabled for this account.",
    "domain_entitlement_required": "Custom domains require a Scale or Business plan.",
    "domain_limit_reached": "Custom domain limit reached for this plan.",
    "domain_taken": "That domain is already registered.",
    "domain_has_inboxes": "Domain has bound inboxes; remove them first.",
    "domain_verify_rate_limited": "Too many verification attempts. Try again shortly.",
    #  (BYOD): inbox<->domain binding
    "domain_not_found_or_unverified": "Domain not found, not owned by this account, or not verified.",
    
    "public_not_allowed_on_custom_domain": "A custom-domain inbox cannot be made public.",
    
    "metadata_must_be_object": "metadata must be a JSON object.",
    "metadata_too_large": "metadata exceeds the size or key-count limit.",
    
    "catch_all_entitlement_required": "Catch-all requires a Business plan.",
    "domain_not_verified": "Domain must be verified before enabling catch-all.",
    "catch_all_target": "This inbox is the active catch-all target for its domain; disable catch-all on the domain first.",
    "catch_all_target_invalid": "The designated inbox must be a live inbox bound to this domain.",
    "inbox_limit_reached": "Inbox limit reached for this plan.",
    "catch_all_rate_limited": "Too many catch-all toggle attempts. Try again shortly.",
    
    "outbound_disabled": "Outbound email is not enabled for this account.",
    "outbound_entitlement_required": "Outbound email requires a Business plan.",
    "sending_domain_not_eligible": "This domain is not eligible to send from (not owned, not verified, or DKIM not verified).",
    "recipient_rejected": "One or more recipients are on the suppression list.",
    "outbound_quota_exceeded": "The outbound send quota for this plan has been reached.",
    "outbound_rate_limited": "Too many outbound send requests. Try again shortly.",
    
    "recipient_not_approved": "One or more recipients are not on the approved-recipients allowlist.",
    "outbound_allowlist_entitlement_required": "The approved-recipients allowlist requires a Business plan.",
    "approved_recipient_exists": "That email address is already on the approved-recipients allowlist.",
    "outbound_allowlist_limit_reached": "The approved-recipients allowlist entry limit has been reached.",
    "outbound_allowlist_rate_limited": "Too many allowlist changes. Try again shortly.",
    
    "smtp_verify_disabled": "Full SMTP verification is not enabled for this account.",
    "verify_full_entitlement_required": "Full SMTP verification requires a Business plan.",
    
    "reply_target_invalid": "No usable reply address could be derived from the original message.",
    
    "batch_too_large": "The batch exceeds the maximum number of unique rows for this mode.",
    "file_too_large": "The uploaded file exceeds the maximum allowed size.",
    "unsupported_file_type": "The uploaded file must be a .csv or .txt file.",
    "malformed_file": "The uploaded file could not be decoded as UTF-8 text, or contains binary data.",
    # Idempotency-Key (trust audit #19)
    "idempotency_key_reused": "This Idempotency-Key was already used with a different request body or endpoint.",
    "idempotency_in_progress": "A request with this Idempotency-Key is still being processed. Retry shortly.",
    # ux3 N1 (API test code)
    "inbox_disabled": "This inbox is disabled. Enable it to receive a test code.",
}

DRF_STATUS_CODES = {
    400: "validation_error",
    401: "authentication_required",
    403: "account_forbidden",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    409: "conflict",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
}


# top-level error envelope code (instead of the generic "validation_error"
# with a fields message) — these are documented, distinct API contract
# codes per the design (see docs/openapi.yaml Error enum + dashboard
# _ERROR_ROWS), not just internal validator labels like routing_key's
# "invalid_format"/"slug_reserved" (which stay nested under validation_error).
PROMOTABLE_FIELD_CODES = {"metadata_must_be_object", "metadata_too_large"}


def error_response(code, status_code, *, fields=None):
    from .middleware import current_request_id

    error = {"code": code, "message": MESSAGES[code], "fields": fields or {}}
    request_id = current_request_id.get()
    if request_id is not None:
        error["request_id"] = request_id
    return Response({"error": error}, status=status_code)


def _safe_fields(detail):
    if not isinstance(detail, dict):
        return {"non_field_errors": ["Invalid value."]}
    fields = {}
    for name, messages in detail.items():
        if isinstance(messages, (list, tuple)):
            fields[str(name)] = [str(message) for message in messages]
        else:
            fields[str(name)] = [str(messages)]
    return fields


def _promoted_field_code(detail):
    if not isinstance(detail, dict):
        return None
    for messages in detail.values():
        items = messages if isinstance(messages, (list, tuple)) else [messages]
        for item in items:
            code = getattr(item, "code", None)
            if code in PROMOTABLE_FIELD_CODES:
                return code
    return None


def _unauthenticated_response(request):
    "Public API."
    from .auth_failures import record_failure

    retry_after = record_failure(request) if request is not None else None
    if retry_after is not None:
        response = error_response("rate_limited", 429)
        response["Retry-After"] = str(retry_after)
        return response
    response = error_response("authentication_required", 401)
    response["WWW-Authenticate"] = "Bearer"
    return response


def api_exception_handler(exc, context):
    if isinstance(exc, RateLimitExceeded):
        response = error_response("rate_limited", 429)
        response.rate_limit = (exc.limit, 0, exc.reset_at)
        return response
    if isinstance(exc, (ValidationError, ParseError)):
        if isinstance(exc, ValidationError):
            promoted = _promoted_field_code(exc.detail)
            if promoted:
                return error_response(promoted, 422)
        fields = _safe_fields(exc.detail) if isinstance(exc, ValidationError) else {"body": ["Malformed JSON body."]}
        return error_response("validation_error", 422, fields=fields)
    if isinstance(exc, (Http404, NotFound)):
        return error_response("not_found", 404)
    if isinstance(exc, NotAuthenticated) or (
        isinstance(exc, APIException) and exc.status_code == 401
    ):
        return _unauthenticated_response(context.get("request"))
    if isinstance(exc, APIException) and exc.status_code == 403:
        return error_response("account_forbidden", 403)
    response = drf_exception_handler(exc, context)
    if response is not None:
        code = DRF_STATUS_CODES.get(response.status_code, "server_error")
        translated = error_response(code, response.status_code)
        for name, value in response.items():
            translated[name] = value
        return translated
    return error_response("server_error", 500)
