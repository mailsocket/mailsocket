import ipaddress

from django.conf import settings
from django.http import HttpResponseNotFound


class InternalIngestNetworkMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    @staticmethod
    def _allowed(remote_addr: str | None) -> bool:
        if not remote_addr:
            return False
        try:
            address = ipaddress.ip_address(remote_addr)
            networks = [
                ipaddress.ip_network(value.strip(), strict=True)
                for value in settings.INGEST_ALLOWED_CIDRS.split(",")
                if value.strip()
            ]
        except ValueError:
            return False
        return any(address in network for network in networks)

    def __call__(self, request):
        if request.path == "/internal/ingest" and not self._allowed(
            request.META.get("REMOTE_ADDR")
        ):
            return HttpResponseNotFound()
        return self.get_response(request)
