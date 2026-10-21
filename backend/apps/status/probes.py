"""Cheap liveness probes for the public /status page.

Each probe returns ``(ok, latency_ms)`` and NEVER raises: any exception (DNS,
timeout, refused, DB error, bad response) is a failed check. No probe sends
email or talks to anything outside our own containers.
"""

import logging
import socket
import time
import urllib.error
import urllib.request

from django.conf import settings
from django.db import connection
from django.http import HttpRequest
from django.urls import resolve

from apps.accounts.models import WaitCapacity
from apps.accounts.services import (
    WAIT_CAPACITY_KEY,
    WAIT_CAPACITY_LIMIT,
    WAIT_LEASE_GRACE_SECONDS,
)

from .models import ComponentCheck

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 3
STATUS_PROBE_USER_AGENT = "mailsocket-status-probe/1"


def probe_web() -> bool:
    """DB round trip (SELECT 1) plus the real /healthz view, in-process."""
    from config.views import healthz

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        if cursor.fetchone() != (1,):
            return False
    request = HttpRequest()
    request.method = "GET"
    request.path = "/healthz"
    return healthz(request).status_code == 200


def probe_api() -> bool:
    """The API URLconf loads and the core routes resolve to their views. A
    broken import anywhere in apps.api.views fails this."""
    match = resolve("/api/v1/inboxes")
    wait = resolve("/api/v1/inboxes/probe/messages/wait")
    return match.url_name == "inboxes" and wait.url_name == "inbox-message-wait"


def probe_inbound_mail() -> bool:
    """TCP connect to Postfix on the docker network and read the greeting.
    Only the banner is read; we then say QUIT so Postfix logs a clean
    disconnect. No MAIL FROM / RCPT / DATA is ever sent."""
    host = settings.STATUS_PROBE_SMTP_HOST
    port = settings.STATUS_PROBE_SMTP_PORT
    with socket.create_connection((host, port), timeout=PROBE_TIMEOUT_SECONDS) as sock:
        sock.settimeout(PROBE_TIMEOUT_SECONDS)
        banner = sock.recv(512)
        try:
            sock.sendall(b"QUIT\r\n")
        except OSError:
            pass
    return banner.startswith(b"220")


def probe_wait() -> bool:
    """Read-only check that the global wait pool could grant a slot right now.

    Deliberately does NOT acquire+release: a real acquire takes a row lock and
    briefly occupies a slot, which could turn a real customer's wait into a
    429 at the edge of capacity. Mirrors the acquire logic in
    apps.accounts.services._acquire_counter_row without the write: no row ->
    free; window elapsed -> would reset; otherwise count must be below limit.
    """
    from datetime import timedelta

    from django.utils import timezone

    row = WaitCapacity.objects.filter(key=WAIT_CAPACITY_KEY).values("count", "expires_at").first()
    if row is None:
        return True
    reset_cutoff = timezone.now() - timedelta(seconds=WAIT_LEASE_GRACE_SECONDS)
    if row["expires_at"] is not None and row["expires_at"] <= reset_cutoff:
        return True
    return row["count"] < WAIT_CAPACITY_LIMIT


def probe_mcp() -> bool:
    request = urllib.request.Request(
        settings.STATUS_PROBE_MCP_URL, headers={"User-Agent": STATUS_PROBE_USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
        return response.status == 200


PROBES = {
    ComponentCheck.Component.WEB: probe_web,
    ComponentCheck.Component.API: probe_api,
    ComponentCheck.Component.INBOUND_MAIL: probe_inbound_mail,
    ComponentCheck.Component.WAIT: probe_wait,
    ComponentCheck.Component.MCP: probe_mcp,
}


def run_probe(component) -> tuple[bool, int]:
    """Run one probe, fail closed. Returns (ok, latency_ms)."""
    probe = PROBES[component]
    started = time.monotonic()
    try:
        ok = bool(probe())
    except Exception as exc:  # fail closed: a probe must never raise
        logger.warning("status_probe_failed component=%s error=%s", component, type(exc).__name__)
        ok = False
    latency_ms = int((time.monotonic() - started) * 1000)
    return ok, latency_ms
