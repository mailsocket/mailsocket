"""Central renderer for dashboard htmx partials (QA-DASH r2, item 1).

app.js swaps a 4xx htmx response into the page ONLY when the server has
positively marked it as one of our own flash/list partials via the
``HX-Partial`` response header. Anything else that comes back with a 4xx —
a proxy/WAF/Cloudflare page, JSON, plain text, a Django error page — gets a
fixed, static-text generic flash instead of being injected as markup.

Every dashboard view that returns a ``dashboard/partials/*`` template goes
through :func:`render_partial`, so the marker is set in exactly one place.
"""
from django.shortcuts import render

PARTIAL_HEADER = "HX-Partial"
PARTIAL_HEADER_VALUE = "flash"
PARTIAL_TEMPLATE_PREFIX = "dashboard/partials/"


def mark_partial(response):
    """Mark ``response`` as a server-rendered flash/list partial."""
    response.headers[PARTIAL_HEADER] = PARTIAL_HEADER_VALUE
    return response


def render_partial(request, template_name, context=None, status=200):
    """``render()`` for a dashboard partial; 4xx responses get ``HX-Partial``."""
    if not template_name.startswith(PARTIAL_TEMPLATE_PREFIX):
        raise ValueError(f"render_partial only renders dashboard partials, got {template_name!r}")
    response = render(request, template_name, context, status=status)
    if status >= 400:
        mark_partial(response)
    return response
