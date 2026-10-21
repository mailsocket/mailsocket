"""``{% signup_href %}``: the "Start free" / "Sign up" link of a public page.

Consent-free attribution (apps.accounts.attribution): a landing link's
``?ref=<channel>`` is carried into the register link SERVER-SIDE, so the
signup request itself carries the campaign tag and nothing is stored in the
browser. Only the VALIDATED tag (``[a-z0-9_-]``, <= 40 chars) is ever echoed
— never the raw query, path or anything else request-derived — so a page
without a valid ``ref`` renders byte-identical to before.
"""

from urllib.parse import quote

from django import template
from django.urls import reverse

from apps.accounts.attribution import ref_from_request

register = template.Library()


@register.simple_tag(takes_context=True)
def signup_href(context, next=None, absolute=True):
    """``{{ DASH_ORIGIN }}/register/`` + ``?next=`` (when given) + ``ref=``.

    ``next`` is quoted like Django's ``urlencode`` filter (``/`` kept), so
    existing links stay byte-identical. ``absolute=False`` keeps the link
    relative (auth pages, already on the dashboard host). The returned string
    is HTML-escaped by simple_tag (``&`` -> ``&amp;``).
    """
    origin = (context.get("DASH_ORIGIN", "") or "") if absolute else ""
    params = []
    if next:
        params.append("next=" + quote(str(next), safe="/"))
    request = context.get("request")
    ref = ref_from_request(request) if request is not None else None
    if ref:
        params.append("ref=" + ref)  # [a-z0-9_-] only: nothing to quote
    url = origin + reverse("accounts:register")
    if params:
        url += "?" + "&".join(params)
    return url


@register.simple_tag(takes_context=True)
def ref_query(context):
    "Public API."
    request = context.get("request")
    ref = ref_from_request(request) if request is not None else None
    return f"?ref={ref}" if ref else ""


@register.simple_tag(takes_context=True)
def try_href(context):
    "Public API."
    return reverse("dashboard:try-it") + ref_query(context)
