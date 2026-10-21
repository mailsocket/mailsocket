from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from django.db.models import Count
from django.utils import timezone

from apps.accounts.services import DEMO_USER_EMAIL


class Command(BaseCommand):
    help = (
        "Hard-delete stale demo-playground inboxes (owned by the demo user) that "
        "carry no messages and are older than --older-than-hours. Message rows are "
        "already removed by the 1-day retention purge; this reclaims the empty "
        "Inbox rows the try-it-live surface accumulates. Standalone job only."
    )

    def add_arguments(self, parser):
        parser.add_argument("--older-than-hours", type=int, default=24)
        parser.add_argument("--batch-size", type=int, default=500)

    def handle(self, *args, **options):
        older_than_hours = options["older_than_hours"]
        batch_size = options["batch_size"]
        if older_than_hours < 1:
            raise CommandError("--older-than-hours must be >= 1")
        if not 1 <= batch_size <= 5000:
            raise CommandError("--batch-size must be between 1 and 5000")

        # Import here so a missing demo user (never provisioned) is a no-op, not
        # an import-time side effect that creates one.
        from django.contrib.auth import get_user_model

        User = get_user_model()
        demo_user = (
            User.objects.filter(email=DEMO_USER_EMAIL).only("pk").first()
        )
        if demo_user is None:
            self.stdout.write("sweep_demo_inboxes demo_user=absent deleted=0")
            return

        cutoff = timezone.now() - timedelta(hours=older_than_hours)
        try:
            # Only inboxes owned by the demo user, older than the cutoff, with
            # ZERO messages. Message->Inbox is PROTECT, so an inbox holding live
            # mail can never be selected here (annotated count > 0 excludes it),
            # and delete() would raise rather than cascade — belt and braces.
            from apps.inboxes.models import Inbox

            stale_ids = list(
                Inbox.objects.filter(user=demo_user, created_at__lt=cutoff)
                .annotate(n=Count("messages"))
                .filter(n=0)
                .values_list("pk", flat=True)[:batch_size]
            )
            deleted = 0
            if stale_ids:
                deleted, _ = Inbox.objects.filter(pk__in=stale_ids).delete()
        except DatabaseError as exc:
            raise CommandError(
                f"Demo inbox sweep database failure: {type(exc).__name__}"
            ) from exc

        self.stdout.write(
            f"sweep_demo_inboxes demo_user=present older_than_hours={older_than_hours} "
            f"deleted={deleted}"
        )
