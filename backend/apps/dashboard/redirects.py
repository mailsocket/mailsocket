"""Slash -> no-slash 301s for public pages (ux3 N9, r2 P2-1).

The canonical public URLs have no trailing slash and APPEND_SLASH only adds
one, so each slashed form gets a fixed 301 to its named canonical route,
keeping the query string. Only GET/HEAD redirect: any other method gets 405
(a plain RedirectView would 301 a POST, which browsers replay as a GET).
"""

from django.views.generic import RedirectView


class SlashRedirectView(RedirectView):
    permanent = True
    query_string = True
    http_method_names = ["get", "head"]


def slash_redirect(pattern_name):
    return SlashRedirectView.as_view(pattern_name=pattern_name)
