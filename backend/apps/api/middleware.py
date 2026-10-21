"""#15: X-Request-Id on every response.

Reuses a valid incoming X-Request-Id (accepted only if it matches
``_REQUEST_ID_RE`` — no CF-Ray passthrough, no unbounded-length header
echoed back verbatim); otherwise generates ``req_`` + 20 random url-safe
characters. Stashes the value on ``request.request_id`` so
``apps.api.errors.api_exception_handler`` can embed it in the JSON error
envelope, and stamps it on every outgoing response, including the SSE
``StreamingHttpResponse`` from ``StreamMessagesView`` (setting a header on
a streaming response before any content has been consumed/sent is safe —
headers are flushed before the body iterator runs).
"""

import contextvars
import re
import secrets
import string

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_URL_SAFE_ALPHABET = string.ascii_letters + string.digits + "-_"

# Mirrors ``request.request_id`` via a contextvar so apps.api.errors.
# error_response() (called from dozens of view sites with no ``request``
# argument) can embed it in the JSON error envelope without a signature
# change at every call site. Set/reset per request by this middleware —
# each request runs on its own thread (gthread) or async task, so this
# never leaks across requests.
current_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_request_id", default=None
)


def generate_request_id() -> str:
    return "req_" + "".join(secrets.choice(_URL_SAFE_ALPHABET) for _ in range(20))


# The ids WE generate contain "_" (url-safe alphabet), which _REQUEST_ID_RE
# (the rule for accepting a CLIENT-supplied header) does not allow. Anything
# that later receives an id back from a user (the feedback form's ?rid=)
# must accept both shapes.
_GENERATED_REQUEST_ID_RE = re.compile(r"^req_[A-Za-z0-9_-]{20}$")


def is_valid_request_id(value) -> bool:
    """True for an id this middleware can have stamped on a response."""
    value = value if isinstance(value, str) else ""
    return bool(_REQUEST_ID_RE.fullmatch(value) or _GENERATED_REQUEST_ID_RE.fullmatch(value))


class RequestIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.META.get("HTTP_X_REQUEST_ID", "")
        request_id = incoming if _REQUEST_ID_RE.fullmatch(incoming) else generate_request_id()
        request.request_id = request_id
        token = current_request_id.set(request_id)
        try:
            response = self.get_response(request)
        finally:
            current_request_id.reset(token)
        response["X-Request-Id"] = request_id
        return response
