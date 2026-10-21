"Public API."

from rest_framework.renderers import BaseRenderer

from .errors import error_response

AGENT_NAME_MAX = 60


def agent_name_or_error(data):
    "Public API."
    raw = data.get("name", "") if isinstance(data, dict) else ""
    if not isinstance(raw, str):
        return "", None
    if len(raw) > AGENT_NAME_MAX:
        message = f"Ensure this field has no more than {AGENT_NAME_MAX} characters."
        return None, error_response("validation_error", 422, fields={"name": [message]})
    return raw, None


class ClosingStream:
    "Public API."

    def __init__(self, gen, on_close):
        self._gen = gen
        self._on_close = on_close

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._gen)

    def close(self):
        try:
            self._gen.close()
        finally:
            self._on_close()


def once(fn):
    """Wrap ``fn`` so only the first call runs (the SSE slot release is
    reached from both the generator's finally and ClosingStream.close)."""
    done = []

    def run():
        if not done:
            done.append(True)
            fn()

    return run


class EventStreamRenderer(BaseRenderer):
    """B1: lets DRF content-negotiate `Accept: text/event-stream`.

    Real browser `EventSource` clients ALWAYS send
    `Accept: text/event-stream` and cannot override it, so without a renderer
    whose media_type matches, DRF's content negotiation raises 406 before the
    view body ever runs. On the happy path this renderer's .render() is never
    actually invoked: the view returns a raw django.http.StreamingHttpResponse
    (see StreamMessagesView.get in views.py), which DRF's finalize_response passes straight through
    un-rewrapped.

    F-A: error Responses (401/404/429 from _owned_inbox/wait_slot/etc, raised
    as normal DRF Response objects via api_exception_handler/error_response)
    DO go through DRF's Response.rendered_content, which picks this renderer
    (it's first in StreamMessagesView.renderer_classes and its media_type
    matches `Accept: text/event-stream`, `*/*`, and no-Accept). Previously
    render() just returned the raw {"error": {...}} dict, which Django then
    coerced to the literal string "error" under a text/event-stream
    content-type. Detect the error case (renderer_context response status
    >= 400, i.e. NOT the streaming path) and JSON-encode instead, fixing up
    Content-Type since Response.rendered_content already stamped it with our
    media_type before calling render().
    """

    media_type = "text/event-stream"
    format = "event-stream"
    charset = None

    def render(self, data, accepted_media_type=None, renderer_context=None):
        del accepted_media_type  # DRF renderer signature; negotiation is done
        response = (renderer_context or {}).get("response")
        if response is not None and response.status_code >= 400:
            import json as _json

            response["Content-Type"] = "application/json"
            return _json.dumps(data).encode("utf-8")
        return data
