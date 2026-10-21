"Public API."

import ipaddress
from pathlib import Path

import pytest
import yaml
from django.test import override_settings

from apps.ingest.middleware import InternalIngestNetworkMiddleware

COMPOSE = Path(__file__).resolve().parents[4] / "docker-compose.yml"


def _compose():
    return yaml.safe_load(COMPOSE.read_text())


def test_ingest_cidr_is_exactly_the_postfix_address():
    compose = _compose()
    services = compose["services"]
    postfix_ip = services["postfix"]["networks"]["mail_internal"]["ipv4_address"]
    cidrs = services["web"]["environment"]["INGEST_ALLOWED_CIDRS"]
    nets = [ipaddress.ip_network(v.strip()) for v in cidrs.split(",") if v.strip()]
    assert nets == [ipaddress.ip_network(f"{postfix_ip}/32")]
    subnet = compose["networks"]["mail_internal"]["ipam"]["config"][0]["subnet"]
    assert ipaddress.ip_address(postfix_ip) in ipaddress.ip_network(subnet)
    for name, svc in services.items():
        if name == "postfix" or not isinstance(svc.get("networks"), dict):
            continue
        for net in svc["networks"].values():
            ip = (net or {}).get("ipv4_address")
            if ip:
                assert ipaddress.ip_address(ip) not in nets[0], name


@pytest.mark.parametrize(
    "remote, allowed",
    [("172.30.0.250", True), ("172.30.0.10", False), ("172.30.0.2", False), ("127.0.0.1", False)],
)
def test_narrowed_cidr_admits_only_postfix(remote, allowed):
    cidr = _compose()["services"]["web"]["environment"]["INGEST_ALLOWED_CIDRS"]
    with override_settings(INGEST_ALLOWED_CIDRS=cidr):
        assert InternalIngestNetworkMiddleware._allowed(remote) is allowed


def test_local_default_still_admits_loopback(settings):
    # dev / e2e (scripts/e2e-local.sh) run without compose: base default.
    # Read base.py's DEFAULT (a developer's local .env may set the variable).
    base = (Path(__file__).resolve().parents[3] / "config" / "settings" / "base.py").read_text()
    assert 'os.environ.get("INGEST_ALLOWED_CIDRS", "127.0.0.0/8,::1/128")' in base
    settings.INGEST_ALLOWED_CIDRS = "127.0.0.0/8,::1/128"
    assert InternalIngestNetworkMiddleware._allowed("127.0.0.1") is True
