"Public API."

import ipaddress

from django.conf import settings

UNKNOWN = "unknown"


def _parse_ip(value):
    if not isinstance(value, str) or not value or len(value) > 45:
        return None
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _trusted_networks():
    networks = []
    for value in (getattr(settings, "TRUSTED_PROXY_CIDRS", "") or "").split(","):
        value = value.strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=True))
        except ValueError:
            # One bad entry poisons the whole list: trust nothing rather than
            # guess which entries the operator meant.
            return []
    return networks


def is_trusted_proxy(remote_addr) -> bool:
    address = _parse_ip(remote_addr)
    if address is None:
        return False
    if address.is_loopback:
        return True
    return any(address in network for network in _trusted_networks())


def trusted_cf_ip(request):
    """The CF-Connecting-IP value when it came through the trusted hop and is a
    well-formed IP address, else None."""
    if not is_trusted_proxy(request.META.get("REMOTE_ADDR")):
        return None
    header = request.META.get("HTTP_CF_CONNECTING_IP")
    address = _parse_ip(header)
    return str(address) if address is not None else None


def client_ip(request) -> str:
    return trusted_cf_ip(request) or request.META.get("REMOTE_ADDR") or UNKNOWN
