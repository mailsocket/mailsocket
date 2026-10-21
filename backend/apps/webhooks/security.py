import ipaddress
import queue
import re
import socket
import threading
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit

from django.conf import settings

_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_AMBIGUOUS_IPV4_RE = re.compile(
    r"^(?:0x[0-9a-f]+|[0-9]+|(?:0x[0-9a-f]+|0[0-9]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|0[0-9]+|[0-9]+)){1,3})$",
    re.IGNORECASE,
)
_EXPLICITLY_BLOCKED = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("fd00:ec2::254"),
}
_NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPATIBLE_PREFIX = ipaddress.ip_network("::/96")


class SSRFValidationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _embedded_ipv4(address):
    if not isinstance(address, ipaddress.IPv6Address):
        return None
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address in _NAT64_WELL_KNOWN_PREFIX:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    if address in _IPV4_COMPATIBLE_PREFIX and address not in {
        ipaddress.IPv6Address("::"),
        ipaddress.IPv6Address("::1"),
    }:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def _validate_public_address(address):
    if address in _EXPLICITLY_BLOCKED or not address.is_global:
        raise SSRFValidationError("ssrf_non_public_address")
    embedded = _embedded_ipv4(address)
    if embedded is not None and (embedded in _EXPLICITLY_BLOCKED or not embedded.is_global):
        raise SSRFValidationError("ssrf_non_public_address")


@dataclass(frozen=True)
class ValidatedTarget:
    url: str
    hostname: str
    request_target: str
    addresses: tuple[str, ...]

    @property
    def pinned_ip(self):
        return self.addresses[0]


def _normalize_host(hostname):
    if not hostname or len(hostname) > 253 or "%" in hostname:
        raise SSRFValidationError("ssrf_invalid_host")
    candidate = hostname.rstrip(".").lower()
    if not candidate or candidate == "localhost" or candidate.endswith(".localhost"):
        raise SSRFValidationError("ssrf_blocked_host")
    if _AMBIGUOUS_IPV4_RE.fullmatch(candidate):
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            raise SSRFValidationError("ssrf_ambiguous_ip") from None
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        _validate_public_address(address)
        return str(address)
    try:
        normalized = candidate.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError) as exc:
        raise SSRFValidationError("ssrf_invalid_host") from exc
    if len(normalized) > 253 or any(not _HOST_LABEL_RE.fullmatch(label) for label in normalized.split(".")):
        raise SSRFValidationError("ssrf_invalid_host")
    return normalized


def _require_ascii_path_and_query(parsed):
    
    # >= 128 in the path/query used to pass here and then raise
    # UnicodeEncodeError inside the transport on every delivery (aborting the
    # global drain pass). Refuse it at save time instead of percent-encoding:
    # the stored URL stays exactly what the customer typed, and percent-
    # encoded URLs (the RFC 3986 form) keep working. The HOST is not checked
    # here: internationalised hostnames keep the IDNA path in _normalize_host.
    if not (parsed.path.isascii() and parsed.query.isascii()):
        raise SSRFValidationError("ssrf_invalid_url")


def parse_webhook_url(url):
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise SSRFValidationError("ssrf_invalid_url")
    if any(ord(char) < 33 or ord(char) == 127 for char in url) or "\\" in url or "#" in url:
        raise SSRFValidationError("ssrf_invalid_url")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise SSRFValidationError("ssrf_invalid_url") from exc
    _require_ascii_path_and_query(parsed)
    if parsed.scheme.lower() != "https":
        raise SSRFValidationError("ssrf_https_required")
    if parsed.username is not None or parsed.password is not None:
        raise SSRFValidationError("ssrf_credentials_forbidden")
    if port not in (None, 443) or not parsed.netloc:
        raise SSRFValidationError("ssrf_port_forbidden")
    hostname = _normalize_host(parsed.hostname)
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        netloc = hostname
    else:
        netloc = f"[{hostname}]" if ip.version == 6 else hostname
    path = parsed.path or "/"
    canonical = urlunsplit(SplitResult("https", netloc, path, parsed.query, ""))
    request_target = path + (("?" + parsed.query) if parsed.query else "")
    return canonical, hostname, request_target


def _default_resolver(hostname):
    return socket.getaddrinfo(hostname, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)



# blocked in getaddrinfo (OS timeout ~30 s). Cap the threads in flight per
# process; when all are stuck, fail closed at once instead of piling up more.
_dns_lock = threading.Lock()
_dns_in_flight = [0]


def _dns_slots_in_use():
    with _dns_lock:
        return _dns_in_flight[0]


def _take_dns_slot():
    with _dns_lock:
        if _dns_in_flight[0] >= int(settings.WEBHOOK_DNS_MAX_THREADS):
            return False
        _dns_in_flight[0] += 1
        return True


def _give_dns_slot():
    with _dns_lock:
        _dns_in_flight[0] -= 1


def _resolve_bounded(hostname, resolver, timeout):
    result_queue = queue.Queue(maxsize=1)
    if not _take_dns_slot():
        raise SSRFValidationError("ssrf_dns_timeout")

    def run():
        try:
            result_queue.put((True, resolver(hostname)), block=False)
        except Exception as exc:
            try:
                result_queue.put((False, exc), block=False)
            except queue.Full:
                pass
        finally:
            _give_dns_slot()

    try:
        threading.Thread(target=run, name="webhook-dns", daemon=True).start()
    except RuntimeError as exc:  # cannot start a thread: give the slot back
        _give_dns_slot()
        raise SSRFValidationError("ssrf_dns_timeout") from exc
    try:
        ok, value = result_queue.get(timeout=timeout)
    except queue.Empty as exc:
        raise SSRFValidationError("ssrf_dns_timeout") from exc
    if not ok:
        raise SSRFValidationError("ssrf_dns_failure") from value
    return value


def _extract_addresses(answer):
    addresses = []
    for item in answer:
        if isinstance(item, str):
            raw = item
        else:
            try:
                raw = item[4][0]
            except (IndexError, TypeError) as exc:
                raise SSRFValidationError("ssrf_dns_failure") from exc
        raw = raw.split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise SSRFValidationError("ssrf_dns_failure") from exc
        _validate_public_address(address)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        canonical = str(address)
        if canonical not in addresses:
            addresses.append(canonical)
    if not addresses:
        raise SSRFValidationError("ssrf_dns_failure")
    return tuple(addresses)


def validate_webhook_target(url, *, resolver=None, timeout=None):
    canonical, hostname, request_target = parse_webhook_url(url)
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        answer = _resolve_bounded(
            hostname,
            resolver or _default_resolver,
            timeout if timeout is not None else float(settings.WEBHOOK_DNS_TIMEOUT_SECONDS),
        )
        addresses = _extract_addresses(answer)
    else:
        addresses = _extract_addresses([str(literal)])
    return ValidatedTarget(canonical, hostname, request_target, addresses)
