"""D3 (N1): edge-header monitor middleware,
healthz/edge probe, and the check_edge_health management command."""
import urllib.request
from io import StringIO
from unittest import mock

import pytest
from django.core import mail
from django.core.management import CommandError, call_command
from django.test import override_settings

from apps.accounts.models import RateLimitBucket
from apps.accounts.services import _bucket_hash

pytestmark = pytest.mark.django_db


@override_settings(EDGE_HEADER_MONITOR=True)
def test_missing_cf_header_from_proxy_net_is_counted(client):
    import apps.accounts.middleware as mw

    mw._last_edge_anomaly_write = 0.0
    client.get("/", REMOTE_ADDR="172.29.0.4")
    bucket_hash = _bucket_hash("ops:edge-missing", "global")
    bucket = RateLimitBucket.objects.get(bucket_hash=bucket_hash)
    assert bucket.count == 1


@override_settings(EDGE_HEADER_MONITOR=True, TRUSTED_PROXY_CIDRS="172.29.0.4/32")
def test_cf_header_present_writes_nothing(client):
    import apps.accounts.middleware as mw

    mw._last_edge_anomaly_write = 0.0
    client.get("/", REMOTE_ADDR="172.29.0.4", HTTP_CF_CONNECTING_IP="203.0.113.9")
    bucket_hash = _bucket_hash("ops:edge-missing", "global")
    assert not RateLimitBucket.objects.filter(bucket_hash=bucket_hash).exists()


@override_settings(EDGE_HEADER_MONITOR=True)
def test_loopback_and_ingest_excluded(client):
    import apps.accounts.middleware as mw

    mw._last_edge_anomaly_write = 0.0
    client.get("/healthz", REMOTE_ADDR="127.0.0.1")
    mw._last_edge_anomaly_write = 0.0
    client.post("/internal/ingest", REMOTE_ADDR="172.30.0.9")
    bucket_hash = _bucket_hash("ops:edge-missing", "global")
    assert not RateLimitBucket.objects.filter(bucket_hash=bucket_hash).exists()


@override_settings(EDGE_HEADER_MONITOR=True)
def test_anomaly_writes_throttled_per_process(client):
    import apps.accounts.middleware as mw

    mw._last_edge_anomaly_write = 0.0
    for _ in range(50):
        client.get("/", REMOTE_ADDR="172.29.0.4")
    bucket_hash = _bucket_hash("ops:edge-missing", "global")
    bucket = RateLimitBucket.objects.get(bucket_hash=bucket_hash)
    assert bucket.count == 1


MCP_ADDR = "172.29.0.250"  # docker-compose.yml mcp ipv4_address


@override_settings(EDGE_HEADER_MONITOR=True, EDGE_MONITOR_EXEMPT_CIDRS=f"{MCP_ADDR}/32")
def test_exempt_mcp_addr_without_cf_header_is_not_counted(client):
    "Public API."
    import apps.accounts.middleware as mw

    mw._last_edge_anomaly_write = 0.0
    client.get(
        "/api/v1/inboxes",
        REMOTE_ADDR=MCP_ADDR,
        HTTP_AUTHORIZATION="Bearer ms_live_notarealkey000000",
        HTTP_X_FORWARDED_PROTO="https",
    )
    bucket_hash = _bucket_hash("ops:edge-missing", "global")
    assert not RateLimitBucket.objects.filter(bucket_hash=bucket_hash).exists()
    # the throttle memo was not consumed by the exempt request either
    assert mw._last_edge_anomaly_write == 0.0


@override_settings(EDGE_HEADER_MONITOR=True, EDGE_MONITOR_EXEMPT_CIDRS=f"{MCP_ADDR}/32")
def test_non_exempt_addr_still_counted_when_exemption_set(client):
    """Any OTHER non-loopback header-less source (e.g. cloudflared dropping
    the header) is still an anomaly — the exemption is exactly one /32."""
    import apps.accounts.middleware as mw

    mw._last_edge_anomaly_write = 0.0
    client.get("/", REMOTE_ADDR="172.29.0.4")
    bucket_hash = _bucket_hash("ops:edge-missing", "global")
    assert RateLimitBucket.objects.get(bucket_hash=bucket_hash).count == 1


@override_settings(EDGE_HEADER_MONITOR=True, EDGE_MONITOR_EXEMPT_CIDRS="not-a-cidr, 172.29.0.0/33,")
def test_malformed_exempt_cidrs_fail_toward_counting(client):
    import apps.accounts.middleware as mw

    mw._last_edge_anomaly_write = 0.0
    client.get("/", REMOTE_ADDR=MCP_ADDR)
    bucket_hash = _bucket_hash("ops:edge-missing", "global")
    assert RateLimitBucket.objects.get(bucket_hash=bucket_hash).count == 1


def test_exempt_cidrs_default_empty(settings):
    assert settings.EDGE_MONITOR_EXEMPT_CIDRS == ""


@override_settings(EDGE_MONITOR_EXEMPT_CIDRS=f"{MCP_ADDR}/32")
def test_exemption_does_not_touch_client_ip():
    """The exemption is monitor-only: IP-keyed limits still see REMOTE_ADDR."""
    from django.test import RequestFactory

    from apps.accounts.views import _client_ip as accounts_client_ip
    from apps.api.views import _client_ip as api_client_ip

    request = RequestFactory().get("/", REMOTE_ADDR=MCP_ADDR)
    assert accounts_client_ip(request) == MCP_ADDR
    assert api_client_ip(request) == MCP_ADDR


def test_monitor_off_by_default(client, settings):
    assert settings.EDGE_HEADER_MONITOR is False
    client.get("/", REMOTE_ADDR="172.29.0.4")
    bucket_hash = _bucket_hash("ops:edge-missing", "global")
    assert not RateLimitBucket.objects.filter(bucket_hash=bucket_hash).exists()


def test_healthz_edge(client, django_assert_num_queries):
    "Public API."
    with django_assert_num_queries(0):
        response = client.get("/healthz/edge", HTTP_CF_CONNECTING_IP="203.0.113.9")
    assert response.status_code == 200
    assert response.json() == {"edge": "ok"}
    assert "203.0.113.9" not in response.content.decode()

    with django_assert_num_queries(0):
        response = client.get("/healthz/edge")
    assert response.status_code == 503
    assert response.json() == {"edge": "missing"}


def test_check_edge_health_alerts_on_counter(settings):
    settings.OPS_ALERT_EMAIL = "ops@example.test"
    bucket_hash = _bucket_hash("ops:edge-missing", "global")
    from datetime import timedelta

    from django.utils import timezone

    RateLimitBucket.objects.create(
        bucket_hash=bucket_hash,
        window_started_at=timezone.now(),
        count=2,
        expires_at=timezone.now() + timedelta(hours=1),
    )
    with mock.patch(
        "apps.accounts.management.commands.check_edge_health._probe", return_value=True
    ):
        with pytest.raises(CommandError):
            call_command("check_edge_health", stdout=StringIO())
    assert len(mail.outbox) == 1
    assert "ops@example.test" in mail.outbox[0].to
    assert "203." not in mail.outbox[0].body


def test_check_edge_health_alerts_on_probe_missing(settings):
    settings.OPS_ALERT_EMAIL = "ops@example.test"
    with mock.patch(
        "apps.accounts.management.commands.check_edge_health._probe", return_value=False
    ):
        with pytest.raises(CommandError):
            call_command("check_edge_health", stdout=StringIO())
    assert len(mail.outbox) == 1


def test_check_edge_health_ok():
    with mock.patch(
        "apps.accounts.management.commands.check_edge_health._probe", return_value=True
    ):
        out = StringIO()
        call_command("check_edge_health", stdout=out)
    assert "edge OK" in out.getvalue()
    assert len(mail.outbox) == 0


def test_probe_retry_once():
    from apps.accounts.management.commands.check_edge_health import _probe

    responses = [OSError("boom"), mock.MagicMock(status=200)]

    def fake_urlopen(url, timeout=None):
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return mock.MagicMock(__enter__=lambda self: result, __exit__=lambda *a: False)

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        assert _probe("http://example.test/healthz/edge", timeout=1) is True

    with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
        assert _probe("http://example.test/healthz/edge", timeout=1) is False


def test_probe_sends_edge_probe_user_agent():
    "Public API."
    from apps.accounts.management.commands.check_edge_health import (
        EDGE_PROBE_USER_AGENT,
        _probe,
    )

    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["request"] = request
        response = mock.MagicMock(status=200)
        return mock.MagicMock(__enter__=lambda self: response, __exit__=lambda *a: False)

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        assert _probe("http://example.test/healthz/edge", timeout=1) is True

    sent_request = captured["request"]
    assert isinstance(sent_request, urllib.request.Request)
    assert sent_request.get_header("User-agent") == EDGE_PROBE_USER_AGENT


def test_compose_mcp_address_matches_edge_monitor_exemption():
    """sol r2 suggestion: the mcp static IP, the web exemption CIDR and this
    test's MCP_ADDR must stay in sync (a drift = hourly false edge alarm, or
    an exemption covering some other container)."""
    import ipaddress
    from pathlib import Path

    import yaml

    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[4] / "docker-compose.yml").read_text()
    )
    services = compose["services"]
    mcp_ip = services["mcp"]["networks"]["app_public"]["ipv4_address"]
    exempt = services["web"]["environment"]["EDGE_MONITOR_EXEMPT_CIDRS"]
    nets = [ipaddress.ip_network(v.strip()) for v in exempt.split(",") if v.strip()]
    assert mcp_ip == MCP_ADDR
    assert nets == [ipaddress.ip_network(f"{mcp_ip}/32")]
    subnet = compose["networks"]["app_public"]["ipam"]["config"][0]["subnet"]
    assert ipaddress.ip_address(mcp_ip) in ipaddress.ip_network(subnet)
    # No other service may pin an address inside the exemption.
    for name, svc in services.items():
        if name == "mcp":
            continue
        for net in (svc.get("networks") or {}).values() if isinstance(svc.get("networks"), dict) else []:
            ip = (net or {}).get("ipv4_address")
            if ip:
                assert all(ipaddress.ip_address(ip) not in n for n in nets), name


# --- tg-notify ------------------------------------------------------------------


def test_check_edge_health_alert_goes_to_telegram_as_down(settings):
    from apps.ops.testing import fake_telegram

    settings.OPS_ALERT_EMAIL = "ops@example.test"
    with mock.patch(
        "apps.accounts.management.commands.check_edge_health._probe", return_value=False
    ), fake_telegram() as tg:
        out = StringIO()
        with pytest.raises(CommandError):
            call_command("check_edge_health", stdout=out)
    assert len(mail.outbox) == 1
    assert mail.outbox[0].subject == "[mailsocket] edge probe failed (could be CF outage)"
    assert len(tg.texts) == 1
    assert tg.texts[0].startswith("🔴 edge probe failed (could be CF outage)")
    assert "probe=missing" in tg.texts[0]
    assert "alert email=ok telegram=ok" in out.getvalue()


def test_check_edge_health_telegram_failure_still_emails_and_exits_nonzero(settings):
    from apps.ops.testing import fake_telegram

    settings.OPS_ALERT_EMAIL = "ops@example.test"
    with mock.patch(
        "apps.accounts.management.commands.check_edge_health._probe", return_value=False
    ), fake_telegram(fail=OSError("tg down")):
        with pytest.raises(CommandError):
            call_command("check_edge_health", stdout=StringIO())
    assert len(mail.outbox) == 1
