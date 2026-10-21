import urllib.error
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.accounts.models import RateLimitBucket
from apps.accounts.services import _bucket_hash
from apps.ops.notify import notify

EDGE_PROBE_USER_AGENT = "mailsocket-edge-probe/1"


def _probe(url: str, timeout: float) -> bool:
    "Public API."
    for _ in range(2):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": EDGE_PROBE_USER_AGENT}
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return False


class Command(BaseCommand):
    help = (
        "Read-only liveness check: probes /healthz/edge and reads the edge "
        "anomaly counter, alerting (email + non-zero exit) if the edge "
        "appears to be dropping CF-Connecting-IP."
    )

    def add_arguments(self, parser):
        parser.add_argument("--no-probe", action="store_true", help="Skip the HTTP probe (local runs).")

    def handle(self, *args, **options):
        probe_ok = True
        if not options["no_probe"]:
            probe_ok = _probe(settings.EDGE_PROBE_URL, timeout=10)

        bucket_hash = _bucket_hash("ops:edge-missing", "global")
        row = RateLimitBucket.objects.filter(
            bucket_hash=bucket_hash, expires_at__gt=timezone.now()
        ).first()
        missing_count = row.count if row is not None else 0

        if probe_ok and missing_count < settings.EDGE_MISSING_ALERT_THRESHOLD:
            self.stdout.write(f"edge OK probe=ok missing_count={missing_count}")
            return

        probe_label = "ok" if probe_ok else "missing"
        summary = (
            "edge probe failed (could be CF outage) "
            f"probe={probe_label} missing_count={missing_count} "
            f"threshold={settings.EDGE_MISSING_ALERT_THRESHOLD}"
        )
        self.stdout.write(summary)

        # tg-notify: email + Telegram via the one notifier (never raises).
        # The summary holds counters only (no IPs). No recovery alert: this
        # command keeps no state between runs, so it cannot see a recovery.
        result = notify("down", "edge probe failed (could be CF outage)", summary)
        if result["email"] == "disabled":
            self.stdout.write("alert email skipped (OPS_ALERT_EMAIL unset)")
        self.stdout.write(f"alert email={result['email']} telegram={result['telegram']}")

        raise CommandError("edge health check failed")
