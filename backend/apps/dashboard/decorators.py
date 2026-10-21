"""Small view decorators shared by the dashboard (and billing) views.

Deliberately imports nothing from apps.dashboard.views / apps.billing so it
is safe to import at module level from any view module (no load-order
cycles).
"""
from functools import wraps

from django.http import HttpResponseNotAllowed
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.cache import patch_cache_control, patch_vary_headers


def private_per_cookie(view):
    """A public URL whose HTML depends on who is signed in (e.g. /pricing/:
    the billing kill switch disables paid CTAs for signed-in visitors only).

    ``Cache-Control: private, no-cache`` keeps it out of every shared cache
    (CDN / proxy) and makes the browser revalidate; ``Vary: Cookie`` keeps
    any cache that ignores ``private`` from crossing the signed-in /
    anonymous variants. Still cacheable by the browser itself (no
    ``no-store``), so back/forward and Lighthouse are unaffected.
    """

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        patch_cache_control(response, private=True, no_cache=True)
        patch_vary_headers(response, ("Cookie",))
        return response

    return wrapped


def post_only(list_url_name, *, url_kwargs=()):
    "Public API."

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method == "POST":
                return view(request, *args, **kwargs)
            if request.method in ("GET", "HEAD"):
                return redirect(
                    reverse(list_url_name, kwargs={k: kwargs[k] for k in url_kwargs})
                )
            return HttpResponseNotAllowed(["POST"])

        return wrapped

    return decorator
