"""Address rendering helpers for dashboard templates."""

import re

from django import template
from django.utils.html import conditional_escape
from django.utils.safestring import mark_safe

register = template.Library()

# Break opportunities: after -, _ and . only (not mid-word). Matched on the
# ESCAPED string — none of these characters appear in HTML entities.
_BREAK_AFTER = re.compile(r"([-_.])")


@register.filter(is_safe=False)
def wbr_breaks(value):
    """Escape ``value`` then insert ``<wbr>`` after each ``-``, ``_`` and ``.``.

    Only the escaped text is marked safe, so ``<script>`` in the input renders
    as ``&lt;script&gt;``. ``<wbr>`` adds no text, so ``textContent`` (Copy
    button) still yields the plain value.
    """
    escaped = str(conditional_escape("" if value is None else value))
    return mark_safe(_BREAK_AFTER.sub(r"\1<wbr>", escaped))
