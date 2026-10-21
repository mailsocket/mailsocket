"""Management command: periodic re-check of pending_dns + verified domains.

operations notes cadence ( / build spec step 4): pending_dns every
~10 minutes, verified hourly. Stage A does NOT install a cron/systemd-timer
for this — that's an explicit follow-up (see operations notes), this command
just needs to exist and work when invoked manually or by a future timer.
"""

from django.core.management.base import BaseCommand

from apps.domains.models import Domain
from apps.domains.services import recheck_domain, release_expired_pending_claims, verify_domain


class Command(BaseCommand):
    help = "Re-check DNS for every pending_dns and verified Domain."

    def handle(self, *args, **options):
        checked = 0
        for domain in Domain.objects.filter(status=Domain.Status.PENDING_DNS).iterator():
            verify_domain(domain)
            checked += 1
        for domain in Domain.objects.filter(status=Domain.Status.VERIFIED).iterator():
            recheck_domain(domain)
            checked += 1
        
        # a >7-day claim whose DNS is now correct is verified above and is no
        # longer a pending claim, so only the still-unverified ones go.
        released = release_expired_pending_claims()
        if released:
            self.stdout.write(f"verify_domains: released {released} expired pending claim(s)")
        self.stdout.write(f"verify_domains: checked {checked} domain(s)")
