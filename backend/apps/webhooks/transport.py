import email.utils
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import timezone as datetime_timezone

from django.conf import settings
from django.utils import timezone

from .security import SSRFValidationError, validate_webhook_target


@dataclass(frozen=True)
class DeliveryResponse:
    status_code: int
    retry_after_seconds: int | None = None


class DeliveryTransportError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _retry_after(value):
    if not value:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime_timezone.utc)
            return max(0, int((parsed - timezone.now()).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DeliveryTransportError("timeout")
    return remaining


def _open_pinned_socket(ip, *, timeout, socket_factory=socket.socket):
    address = __import__("ipaddress").ip_address(ip)
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    sock = socket_factory(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sockaddr = (ip, 443, 0, 0) if family == socket.AF_INET6 else (ip, 443)
    sock.connect(sockaddr)
    return sock


def _encode_request(request_target, request_headers, body):
    "Public API."
    head = (
        f"POST {request_target} HTTP/1.1\r\n"
        + "".join(f"{name}: {value}\r\n" for name, value in request_headers.items())
        + "\r\n"
    )
    try:
        return head.encode("ascii") + body
    except UnicodeEncodeError as exc:
        raise DeliveryTransportError("invalid_request_encoding") from exc


def post_json_pinned(
    url,
    body,
    headers,
    *,
    resolver=None,
    socket_factory=socket.socket,
    ssl_context=None,
):
    try:
        target = validate_webhook_target(url, resolver=resolver)
    except SSRFValidationError:
        raise

    total_timeout = float(settings.WEBHOOK_TOTAL_TIMEOUT_SECONDS)
    connect_timeout = min(float(settings.WEBHOOK_CONNECT_TIMEOUT_SECONDS), total_timeout)
    response_cap = int(settings.WEBHOOK_RESPONSE_MAX_BYTES)
    header_cap = int(settings.WEBHOOK_RESPONSE_HEADER_MAX_BYTES)
    request_headers = {
        "Host": f"[{target.hostname}]" if ":" in target.hostname else target.hostname,
        "Connection": "close",
        "Content-Length": str(len(body)),
        **headers,
    }
    request = _encode_request(target.request_target, request_headers, body)
    deadline = time.monotonic() + total_timeout
    raw_sock = tls_sock = response_file = None
    try:
        raw_sock = _open_pinned_socket(
            target.pinned_ip,
            timeout=min(connect_timeout, _remaining(deadline)),
            socket_factory=socket_factory,
        )
        context = ssl_context or ssl.create_default_context()
        # TCP is connected to the validated numeric IP. The original normalized
        # hostname is supplied only to TLS for SNI and certificate verification;
        # wrap_socket performs no DNS lookup.
        tls_sock = context.wrap_socket(raw_sock, server_hostname=target.hostname)
        raw_sock = None
        tls_sock.settimeout(_remaining(deadline))
        tls_sock.sendall(request)
        response_file = tls_sock.makefile("rb")
        tls_sock.settimeout(_remaining(deadline))
        status_line = response_file.readline(header_cap + 1)
        if len(status_line) > header_cap or not status_line.endswith(b"\n"):
            raise DeliveryTransportError("response_headers_too_large")
        try:
            status_code = int(status_line.split(b" ", 2)[1])
        except (IndexError, ValueError) as exc:
            raise DeliveryTransportError("invalid_http_response") from exc
        header_bytes = len(status_line)
        response_headers = {}
        while True:
            tls_sock.settimeout(_remaining(deadline))
            line = response_file.readline(header_cap - header_bytes + 1)
            header_bytes += len(line)
            if header_bytes > header_cap:
                raise DeliveryTransportError("response_headers_too_large")
            if line in (b"\r\n", b"\n"):
                break
            if not line or b":" not in line:
                raise DeliveryTransportError("invalid_http_response")
            name, value = line.split(b":", 1)
            try:
                response_headers[name.decode("ascii").lower()] = value.decode("latin-1").strip()
            except UnicodeDecodeError as exc:
                raise DeliveryTransportError("invalid_http_response") from exc
        tls_sock.settimeout(_remaining(deadline))
        # Read at most the configured cap plus one byte, then discard and close.
        # No response bytes are returned to callers, persisted, or logged.
        response_file.read(response_cap + 1)
        return DeliveryResponse(status_code, _retry_after(response_headers.get("retry-after")))
    except SSRFValidationError:
        raise
    except (socket.timeout, TimeoutError) as exc:
        raise DeliveryTransportError("timeout") from exc
    except ssl.SSLCertVerificationError as exc:
        raise DeliveryTransportError("tls_certificate_error") from exc
    except ssl.SSLError as exc:
        raise DeliveryTransportError("tls_error") from exc
    except OSError as exc:
        raise DeliveryTransportError("network_error") from exc
    finally:
        if response_file is not None:
            response_file.close()
        if tls_sock is not None:
            tls_sock.close()
        if raw_sock is not None:
            raw_sock.close()
