import json
import re
import socket
import urllib.error
from datetime import timedelta
from io import StringIO
from unittest import mock

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.db import DatabaseError
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from apps.accounts.models import WaitCapacity
from apps.accounts.services import WAIT_CAPACITY_KEY, WAIT_CAPACITY_LIMIT
from apps.status import probes
from apps.status.models import ComponentCheck, Incident
from apps.status.services import (
    DEGRADED,
    DOWN,
    OPERATIONAL,
    UNKNOWN,
    bar_level,
    build_components,
    current_state,
    format_percent,
    overall_state,
    purge_old_component_checks,
    uptime_percent,
)

COMPONENTS = ComponentCheck.Component.values


@pytest.fixture(autouse=True)
def _clear_cache():
    # /status is cache_page'd in the shared locmem cache; isolate every test.
    cache.clear()
    yield
    cache.clear()


def _check(component, ok, at, latency=5):
    return ComponentCheck.objects.create(component=component, ok=ok, latency_ms=latency, checked_at=at)


def _all_ok(at):
    for c in COMPONENTS:
        _check(c, True, at)


# --- probes: fail closed, never raise ---------------------------------------


@pytest.mark.parametrize("component", COMPONENTS)
def test_every_probe_fails_closed_without_raising(component):
    boom = mock.Mock(side_effect=RuntimeError("boom"))
    with mock.patch.dict(probes.PROBES, {component: boom}):
        ok, latency = probes.run_probe(component)
    assert ok is False
    assert latency >= 0


def test_inbound_mail_probe_refused_is_false(settings):
    settings.STATUS_PROBE_SMTP_HOST = "127.0.0.1"
    settings.STATUS_PROBE_SMTP_PORT = 9  # discard; nothing listens in CI
    with mock.patch("socket.create_connection", side_effect=ConnectionRefusedError()):
        assert probes.run_probe(ComponentCheck.Component.INBOUND_MAIL)[0] is False


def _fake_socket(banner):
    sock = mock.MagicMock()
    sock.__enter__.return_value = sock
    sock.recv.return_value = banner
    return sock


def test_inbound_mail_probe_reads_banner_only():
    sock = _fake_socket(b"220 mx.inboxpipe.net ESMTP\r\n")
    with mock.patch("socket.create_connection", return_value=sock):
        assert probes.run_probe(ComponentCheck.Component.INBOUND_MAIL)[0] is True
    sent = b"".join(call.args[0] for call in sock.sendall.call_args_list)
    # Never an SMTP transaction: no HELO/MAIL/RCPT/DATA, only QUIT.
    assert sent == b"QUIT\r\n"


def test_inbound_mail_probe_non_220_banner_is_false():
    with mock.patch("socket.create_connection", return_value=_fake_socket(b"421 busy\r\n")):
        assert probes.run_probe(ComponentCheck.Component.INBOUND_MAIL)[0] is False


def test_inbound_mail_probe_timeout_is_false():
    with mock.patch("socket.create_connection", side_effect=socket.timeout()):
        assert probes.run_probe(ComponentCheck.Component.INBOUND_MAIL)[0] is False


def test_mcp_probe_url_error_is_false():
    with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
        assert probes.run_probe(ComponentCheck.Component.MCP)[0] is False


def test_mcp_probe_uses_3s_timeout_and_200_is_ok(settings):
    settings.STATUS_PROBE_MCP_URL = "http://mcp:8080/healthz"
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.status = 200
    with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
        assert probes.run_probe(ComponentCheck.Component.MCP)[0] is True
    request = urlopen.call_args.args[0]
    assert request.full_url == "http://mcp:8080/healthz"
    assert urlopen.call_args.kwargs["timeout"] == 3


@pytest.mark.django_db
def test_web_and_api_probes_ok_in_process():
    assert probes.run_probe(ComponentCheck.Component.WEB)[0] is True
    assert probes.run_probe(ComponentCheck.Component.API)[0] is True


@pytest.mark.django_db
def test_web_probe_db_error_is_false():
    with mock.patch("apps.status.probes.connection") as conn:
        conn.cursor.side_effect = DatabaseError("down")
        assert probes.run_probe(ComponentCheck.Component.WEB)[0] is False


@pytest.mark.django_db
def test_wait_probe_is_read_only_and_reflects_capacity():
    assert probes.run_probe(ComponentCheck.Component.WAIT)[0] is True
    assert not WaitCapacity.objects.exists()  # never acquires/creates a row

    row = WaitCapacity.objects.create(
        key=WAIT_CAPACITY_KEY, count=WAIT_CAPACITY_LIMIT,
        expires_at=timezone.now() + timedelta(seconds=60),
    )
    assert probes.run_probe(ComponentCheck.Component.WAIT)[0] is False
    row.refresh_from_db()
    assert row.count == WAIT_CAPACITY_LIMIT  # untouched

    row.count = WAIT_CAPACITY_LIMIT - 1
    row.save()
    assert probes.run_probe(ComponentCheck.Component.WAIT)[0] is True
    row.refresh_from_db()
    assert row.count == WAIT_CAPACITY_LIMIT - 1


@pytest.mark.django_db
def test_wait_probe_db_error_is_false():
    with mock.patch.object(WaitCapacity.objects, "filter", side_effect=DatabaseError("x")):
        assert probes.run_probe(ComponentCheck.Component.WAIT)[0] is False


@pytest.mark.django_db
def test_status_probe_command_records_one_row_per_component():
    with mock.patch("socket.create_connection", side_effect=OSError()), \
            mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("x")):
        out = StringIO()
        call_command("status_probe", stdout=out)
    rows = {r.component: r for r in ComponentCheck.objects.all()}
    assert set(rows) == set(COMPONENTS)
    assert rows["web"].ok and rows["api"].ok and rows["wait"].ok
    assert not rows["inbound_mail"].ok and not rows["mcp"].ok
    assert len({r.checked_at for r in rows.values()}) == 1
    assert "inbound_mail=FAIL" in out.getvalue()


# --- state and uptime math ---------------------------------------------------


def test_current_state_from_last_two_checks():
    assert current_state([]) == UNKNOWN
    assert current_state([True]) == OPERATIONAL
    assert current_state([False]) == DEGRADED
    assert current_state([True, True]) == OPERATIONAL
    assert current_state([False, True]) == DEGRADED
    assert current_state([True, False]) == DEGRADED
    assert current_state([False, False]) == DOWN
    # only the newest two count
    assert current_state([True, True, False]) == OPERATIONAL


def test_uptime_math_and_display():
    assert uptime_percent(0, 0) is None
    assert uptime_percent(288, 288) == 100.0
    assert uptime_percent(287, 288) == pytest.approx(99.6527, rel=1e-4)
    assert format_percent(None) == "No data"
    assert format_percent(100.0) == "100%"
    assert format_percent(99.999) == "99.99%"  # floored, never rounded up to 100
    assert format_percent(uptime_percent(287, 288)) == "99.65%"
    assert bar_level(None) == "none"
    assert bar_level(100.0) == "up"
    assert bar_level(96.0) == "partial"
    assert bar_level(94.9) == "down"


def test_overall_state():
    class C:
        def __init__(self, state):
            self.state = state

    assert overall_state([]) == UNKNOWN
    assert overall_state([C(OPERATIONAL)] * 3) == OPERATIONAL
    assert overall_state([C(OPERATIONAL), C(DEGRADED)]) == DEGRADED
    assert overall_state([C(OPERATIONAL), C(DOWN), C(DEGRADED)]) == DOWN
    assert overall_state([C(UNKNOWN)] * 2) == UNKNOWN


@pytest.mark.django_db
def test_build_components_daily_counts_only_days_we_have():
    now = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
    two_days_ago = now - timedelta(days=2)
    _check("web", True, two_days_ago)
    _check("web", False, two_days_ago + timedelta(minutes=5))
    _check("web", True, now - timedelta(minutes=5))
    _check("web", True, now)

    comps = {c.key: c for c in build_components(now)}
    web = comps["web"]
    assert [d.day for d in web.days] == [(now - timedelta(days=i)).date() for i in (2, 1, 0)]
    assert [(d.ok, d.total) for d in web.days] == [(1, 2), (0, 0), (2, 2)]
    assert [d.level for d in web.days] == ["down", "none", "up"]
    assert (web.ok, web.total) == (3, 4)
    assert web.uptime_display == "75.00%"
    assert web.state == OPERATIONAL
    # a component with no checks shares the span but has no data / no state
    assert comps["mcp"].state == UNKNOWN
    assert comps["mcp"].total == 0
    assert len(comps["mcp"].days) == 3


@pytest.mark.django_db
def test_build_components_caps_history_at_90_days_and_marks_stale():
    now = timezone.now()
    _check("api", True, now - timedelta(days=200))
    _check("api", True, now - timedelta(days=89))
    _check("api", True, now - timedelta(minutes=30))
    api = {c.key: c for c in build_components(now)}["api"]
    assert len(api.days) == 90
    assert api.total == 2
    # newest check is 30 min old: the probe has stopped, don't claim "Operational"
    assert api.state == UNKNOWN


@pytest.mark.django_db
def test_two_failures_in_a_row_is_down():
    now = timezone.now()
    _check("inbound_mail", True, now - timedelta(minutes=10))
    _check("inbound_mail", False, now - timedelta(minutes=5))
    _check("inbound_mail", False, now)
    assert {c.key: c for c in build_components(now)}["inbound_mail"].state == DOWN


# --- retention ---------------------------------------------------------------


@pytest.mark.django_db
def test_purge_keeps_90_days_and_runs_in_hourly_retention_job():
    now = timezone.now()
    old = _check("web", True, now - timedelta(days=90, seconds=1))
    keep = _check("web", True, now - timedelta(days=89, hours=23))
    assert purge_old_component_checks(now=now) == 1
    assert not ComponentCheck.objects.filter(pk=old.pk).exists()
    assert ComponentCheck.objects.filter(pk=keep.pk).exists()

    _check("api", True, now - timedelta(days=120))
    out = StringIO()
    call_command("purge_expired_messages", stdout=out)
    assert "status_checks=1" in out.getvalue()
    assert ComponentCheck.objects.filter(pk=keep.pk).exists()


# --- page --------------------------------------------------------------------


def _get_page():
    response = Client().get(reverse("status:page"))
    assert response.status_code == 200
    return response, response.content.decode()


def _assert_csp_safe(page):
    for tag in re.findall(r"<script\b[^>]*>", page):
        assert "src=" in tag, tag
    assert "<style" not in page
    assert " style=" not in page


@pytest.mark.django_db
def test_page_renders_with_no_data():
    response, page = _get_page()
    assert "No recent checks" in page
    assert page.count("No checks recorded yet.") == len(COMPONENTS)
    assert "No incidents reported." in page
    assert 'class="status-bar ' not in page
    assert "Bars show one day each" not in page  # no legend for bars that aren't there
    # no invented numbers
    assert "99." not in page and "100%" not in page
    _assert_csp_safe(page)


@pytest.mark.django_db
def test_page_renders_with_data():
    now = timezone.now()
    _all_ok(now - timedelta(days=1))
    _all_ok(now - timedelta(minutes=5))
    for c in COMPONENTS:
        _check(c, c != "mcp", now)
    _, page = _get_page()
    for _, name in ComponentCheck.Component.choices:
        assert escape(name) in page
    assert "Some systems are degraded" in page
    assert page.count('class="status-bar status-bar-') == 2 * len(COMPONENTS)
    assert "1 day ago" in page  # two bars: yesterday and today
    assert "Affected: MCP server" in page
    assert "100% uptime (3/3 checks)" in page
    assert "Degraded" in page
    assert "No incidents reported." in page
    _assert_csp_safe(page)


@pytest.mark.django_db
def test_page_renders_incident():
    now = timezone.now()
    Incident.objects.create(
        title="Inbound mail delayed", status=Incident.Status.RESOLVED,
        body="Postfix queue backed up.\n\nAll mail delivered.",
        started_at=now - timedelta(hours=3), resolved_at=now - timedelta(hours=1),
    )
    Incident.objects.create(  # resolved long ago: out of the window
        title="Ancient", status=Incident.Status.RESOLVED,
        started_at=now - timedelta(days=200), resolved_at=now - timedelta(days=199),
    )
    _, page = _get_page()
    assert "Inbound mail delayed" in page
    assert "Resolved" in page
    assert "<p>Postfix queue backed up.</p>" in page
    assert "Ancient" not in page
    assert "No incidents reported." not in page
    _assert_csp_safe(page)


@pytest.mark.django_db
def test_incident_body_is_escaped():
    Incident.objects.create(title="<b>x</b>", body="<script>alert(1)</script>", started_at=timezone.now())
    _, page = _get_page()
    assert "<script>alert(1)" not in page
    assert "&lt;script&gt;" in page


@pytest.mark.django_db
def test_page_is_cached_60s():
    response, page = _get_page()
    assert "max-age=60" in response["Cache-Control"]
    _all_ok(timezone.now())
    _, again = _get_page()
    assert again == page  # served from cache
    cache.clear()
    _, fresh = _get_page()
    assert "All systems operational" in fresh


@pytest.mark.django_db
def test_json_shape_and_cache():
    now = timezone.now()
    _all_ok(now)
    Incident.objects.create(title="Investigating", started_at=now)
    response = Client().get(reverse("status:json"))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/json"
    assert "max-age=60" in response["Cache-Control"]
    body = json.loads(response.content)
    assert set(body) == {"status", "description", "generated_at", "components", "incidents"}
    assert body["status"] == "operational"
    assert [c["id"] for c in body["components"]] == COMPONENTS
    comp = body["components"][0]
    assert set(comp) == {"id", "name", "status", "last_checked_at", "latency_ms", "uptime", "daily"}
    assert comp["uptime"] == {"days": 1, "checks": 1, "ok": 1, "percent": 100.0}
    assert comp["daily"] == [{"date": now.date().isoformat(), "checks": 1, "ok": 1}]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", comp["last_checked_at"])
    assert body["incidents"][0]["title"] == "Investigating"
    assert body["incidents"][0]["resolved_at"] is None


@pytest.mark.django_db
def test_json_with_no_data():
    body = json.loads(Client().get(reverse("status:json")).content)
    assert body["status"] == "unknown"
    assert all(c["uptime"]["percent"] is None and c["daily"] == [] for c in body["components"])
    assert body["incidents"] == []


@pytest.mark.django_db
def test_status_post_not_allowed():
    assert Client().post(reverse("status:page")).status_code == 405


@pytest.mark.django_db
def test_status_linked_from_footer_and_security():
    status_url = reverse("status:page")
    for name in ("dashboard:root", "dashboard:docs", "dashboard:security"):
        html = Client().get(reverse(name)).content.decode()
        footer = html.split('<footer class="site-footer">', 1)[1]
        assert f'href="{status_url}">Status</a>' in footer, name
    security = Client().get(reverse("dashboard:security")).content.decode()
    main = security.split('<footer class="site-footer">', 1)[0]
    assert f'href="{status_url}"' in main


@pytest.mark.django_db
def test_incident_admin_registered(admin_user):
    client = Client()
    client.force_login(admin_user)
    assert client.get("/admin/status/incident/add/").status_code == 200
    assert client.get("/admin/status/componentcheck/").status_code == 200
    assert client.get("/admin/status/componentcheck/add/").status_code == 403


@pytest.mark.django_db
def test_query_string_does_not_bust_the_snapshot_cache(client, django_assert_num_queries):
    """ P2: cache_page keyed on the full URL, so ?junk=N re-ran the
    aggregate queries on every request. The data is now cached under a fixed
    key: after one warm-up, arbitrary query strings cost zero DB queries."""
    client.get("/status")
    for i in range(3):
        with django_assert_num_queries(0):
            assert client.get(f"/status?junk={i}").status_code == 200
        with django_assert_num_queries(0):
            assert client.get(f"/status.json?junk={i}").status_code == 200
