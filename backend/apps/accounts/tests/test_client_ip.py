"Public API."

import re
from pathlib import Path

import pytest
from django.test import RequestFactory, override_settings

from apps.accounts.client_ip import client_ip, trusted_cf_ip

BACKEND = Path(__file__).resolve().parents[3]
PROXY = "172.29.0.249"


def _req(remote=None, cf=None):
    meta = {}
    if cf is not None:
        meta["HTTP_CF_CONNECTING_IP"] = cf
    request = RequestFactory().get("/", **meta)
    if remote is None:
        request.META.pop("REMOTE_ADDR", None)
    else:
        request.META["REMOTE_ADDR"] = remote
    return request


@override_settings(TRUSTED_PROXY_CIDRS=f"{PROXY}/32")
def test_header_from_trusted_proxy_is_the_client_ip():
    assert client_ip(_req(PROXY, "203.0.113.5")) == "203.0.113.5"
    assert trusted_cf_ip(_req(PROXY, "203.0.113.5")) == "203.0.113.5"


@override_settings(TRUSTED_PROXY_CIDRS=f"{PROXY}/32")
def test_header_from_untrusted_peer_is_ignored():
    # A spoofed header from a direct/other hop never picks the bucket.
    assert client_ip(_req("198.51.100.7", "203.0.113.5")) == "198.51.100.7"
    assert client_ip(_req("172.29.0.4", "203.0.113.5")) == "172.29.0.4"
    assert trusted_cf_ip(_req("198.51.100.7", "203.0.113.5")) is None


@override_settings(TRUSTED_PROXY_CIDRS="")
def test_loopback_peer_is_always_a_trusted_hop():
    assert client_ip(_req("127.0.0.1", "203.0.113.5")) == "203.0.113.5"
    assert client_ip(_req("::1", "2001:db8::5")) == "2001:db8::5"


@override_settings(TRUSTED_PROXY_CIDRS="")
def test_no_trusted_proxy_configured_means_header_ignored():
    assert client_ip(_req("172.29.0.249", "203.0.113.5")) == "172.29.0.249"


@override_settings(TRUSTED_PROXY_CIDRS=f"{PROXY}/32")
@pytest.mark.parametrize("bad", ["", "not-an-ip", "203.0.113.5, 10.0.0.1", "1.2.3.4\n", "x" * 100])
def test_garbage_header_falls_back_to_remote_addr(bad):
    assert client_ip(_req(PROXY, bad)) == PROXY


@override_settings(TRUSTED_PROXY_CIDRS="garbage, 172.29.0.0/33, 10.0.0.1/8")
def test_malformed_cidrs_trust_nothing():
    assert client_ip(_req("10.0.0.1", "203.0.113.5")) == "10.0.0.1"
    assert client_ip(_req("172.29.0.4", "203.0.113.5")) == "172.29.0.4"


@override_settings(TRUSTED_PROXY_CIDRS=f"{PROXY}/32, 172.29.0.248/32")
def test_cidr_list_is_comma_separated():
    assert client_ip(_req("172.29.0.248", "203.0.113.5")) == "203.0.113.5"


def test_missing_remote_addr_is_a_shared_fail_restrictive_bucket():
    
    # share ONE bucket, which throttles them together (fails closed, not open).
    assert client_ip(_req(None, None)) == "unknown"


def test_no_private_client_ip_copies_left():
    "Public API."
    offenders = []
    for path in (BACKEND / "apps").rglob("*.py"):
        if "/tests/" in str(path) or path.name == "client_ip.py":
            continue
        text = path.read_text()
        if re.search(r"HTTP_CF_CONNECTING_IP|HTTP_X_FORWARDED_FOR", text):
            offenders.append(str(path.relative_to(BACKEND)))
        if re.search(r"def _client_ip\(", text):
            offenders.append(str(path.relative_to(BACKEND)))
    config_views = (BACKEND / "config" / "views.py").read_text()
    if "HTTP_CF_CONNECTING_IP" in config_views:
        offenders.append("config/views.py")
    assert offenders == []


@override_settings(TRUSTED_PROXY_CIDRS=f"{PROXY}/32")
def test_healthz_edge_treats_untrusted_header_as_missing(client):
    ok = client.get("/healthz/edge", REMOTE_ADDR=PROXY, HTTP_CF_CONNECTING_IP="203.0.113.9")
    assert ok.status_code == 200
    spoofed = client.get("/healthz/edge", REMOTE_ADDR="198.51.100.7", HTTP_CF_CONNECTING_IP="203.0.113.9")
    assert spoofed.status_code == 503
    assert spoofed.json() == {"edge": "missing"}


def test_compose_pins_cloudflared_to_the_trusted_proxy_address():
    import ipaddress

    import yaml

    compose = yaml.safe_load((BACKEND.parent / "docker-compose.yml").read_text())
    services = compose["services"]
    cf_ip = services["cloudflared"]["networks"]["app_public"]["ipv4_address"]
    trusted = services["web"]["environment"]["TRUSTED_PROXY_CIDRS"]
    nets = [ipaddress.ip_network(v.strip()) for v in trusted.split(",") if v.strip()]
    assert nets == [ipaddress.ip_network(f"{cf_ip}/32")]
    for name, svc in services.items():
        if name == "cloudflared" or not isinstance(svc.get("networks"), dict):
            continue
        for net in svc["networks"].values():
            ip = (net or {}).get("ipv4_address")
            if ip:
                assert all(ipaddress.ip_address(ip) not in n for n in nets), name
