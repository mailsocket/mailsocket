"""Full-page flash messages for dashboard POST-redirect-GET fallbacks.

The ``messages`` context variable is shadowed on several dashboard pages (the
messages list passes its Message rows as ``messages``), so base.html cannot
iterate the django.contrib.messages context processor directly. This tag
reads the request's message storage instead.

Messages tagged ``inline`` are rendered by the page itself next to the form
they belong to (e.g. the Settings password / delete cards) and are skipped
here so they never show twice.
"""
from django import template
from django.contrib import messages as django_messages

register = template.Library()


@register.inclusion_tag("dashboard/partials/_flashes.html", takes_context=True)
def dashboard_flashes(context):
    request = context.get("request")
    flashes = []
    if request is not None:
        for message in django_messages.get_messages(request):
            if "inline" in (message.extra_tags or "").split():
                continue
            flashes.append({
                "text": str(message),
                "is_error": message.level >= django_messages.WARNING,
            })
    return {"flashes": flashes}
