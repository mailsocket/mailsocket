from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.accounts.models import RateLimitBucket


class Command(BaseCommand):
    help = "Delete a finite batch of expired authentication rate-limit buckets."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=1000)

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        if not 1 <= batch_size <= 10000:
            raise ValueError("--batch-size must be between 1 and 10000")
        cutoff = timezone.now()
        ids = list(
            RateLimitBucket.objects.filter(expires_at__lte=cutoff)
            .order_by("expires_at")
            .values_list("pk", flat=True)[:batch_size]
        )
        deleted, _ = RateLimitBucket.objects.filter(
            pk__in=ids, expires_at__lte=cutoff
        ).delete()
        self.stdout.write(f"deleted={deleted}")
