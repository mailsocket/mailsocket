from datetime import timezone as dt_timezone

from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.cache import patch_cache_control
from django.views.decorators.http import require_safe

from .services import (
    HISTORY_DAYS,
    OPERATIONAL,
    OVERALL_LABELS,
    affected_names,
    build_components,
    overall_state,
    recent_incidents,
)

# Public, identical for every visitor, and the probe only writes every 5
# minutes: 60s of server-side cache (and the same max-age for browsers / the
# edge) keeps a burst of visitors from re-running the aggregate queries.
STATUS_CACHE_SECONDS = 60


_SNAPSHOT_CACHE_KEY = "status:snapshot:v1"


def _snapshot():
    """Aggregates shared by the page and the JSON, cached under a FIXED key.

    cache_page keys on the full URL, so /status?x=1, ?x=2 ... would each miss
    and re-run the aggregate queries. Caching the data itself makes the DB
    cost independent of the query string."""
    snap = cache.get(_SNAPSHOT_CACHE_KEY)
    if snap is None:
        now = timezone.now()
        components = build_components(now)
        snap = (now, components, recent_incidents(now))
        cache.set(_SNAPSHOT_CACHE_KEY, snap, STATUS_CACHE_SECONDS)
    return snap


def _iso(value):
    if value is None:
        return None
    return value.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@require_safe
def status_page(request):
    now, components, incidents = _snapshot()
    state = overall_state(components)
    response = render(request, "status/status.html", {
        "title": "Status",
        "components": components,
        "overall_state": state,
        "overall_label": OVERALL_LABELS[state],
        "affected": affected_names(components) if state != OPERATIONAL else [],
        "incidents": incidents,
        "history_days": HISTORY_DAYS,
        "generated_at": now,
    })
    patch_cache_control(response, public=True, max_age=STATUS_CACHE_SECONDS)
    return response


@require_safe
def status_json(request):
    now, components, incidents = _snapshot()
    state = overall_state(components)
    body = {
        "status": state,
        "description": OVERALL_LABELS[state],
        "generated_at": _iso(now),
        "components": [
            {
                "id": c.key,
                "name": c.name,
                "status": c.state,
                "last_checked_at": _iso(c.last_checked_at),
                "latency_ms": c.last_latency_ms,
                "uptime": {
                    "days": len(c.days),
                    "checks": c.total,
                    "ok": c.ok,
                    "percent": None if c.uptime is None else round(c.uptime, 3),
                },
                "daily": [
                    {"date": d.day.isoformat(), "checks": d.total, "ok": d.ok}
                    for d in c.days
                ],
            }
            for c in components
        ],
        "incidents": [
            {
                "id": i.id,
                "title": i.title,
                "status": i.status,
                "body": i.body,
                "started_at": _iso(i.started_at),
                "resolved_at": _iso(i.resolved_at),
            }
            for i in incidents
        ],
    }
    response = JsonResponse(body)
    patch_cache_control(response, public=True, max_age=STATUS_CACHE_SECONDS)
    return response
