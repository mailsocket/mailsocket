from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count
from django.db.models.functions import TruncMonth
from django.utils import timezone

from apps.inboxes.models import Message

from ...activity import backfill_weekly_activity
from ...funnel_signals import backfill_active_days
from ...models import MonthlyUsage


class Command(BaseCommand):
    help = (
        "Correct recent monthly received-message counters upward from retained Message rows; "
        "never lower durable counters or inspect periods beyond the maximum retention window."
    )

    def handle(self, *args, **options):
        retention_cutoff = timezone.now() - timedelta(days=max(settings.RETENTION_DAYS.values()))
        counts = list(
            Message.objects.filter(received_at__gte=retention_cutoff)
            .annotate(period=TruncMonth("received_at"))
            .values("inbox__user_id", "period")
            .annotate(total=Count("id"))
            .order_by("inbox__user_id", "period")
        )
        expected = {
            (row["inbox__user_id"], row["period"].date().replace(day=1)): row["total"]
            for row in counts
        }
        updated = 0
        with transaction.atomic():
            Message.objects.filter(
                received_at__gte=retention_cutoff,
                usage_counted=False,
            ).update(usage_counted=True)
            for (user_id, period_start), total in expected.items():
                usage, _ = MonthlyUsage.objects.select_for_update().get_or_create(
                    user_id=user_id,
                    period_start=period_start,
                )
                if usage.messages_received < total:
                    usage.messages_received = total
                    usage.save(update_fields=["messages_received"])
                    updated += 1
            # Funnel signal: messages marked counted above never pass through
            # record_message_once, so record their weeks here (idempotent).
            activity_added = backfill_weekly_activity(retention_cutoff)
            active_days_added = backfill_active_days(retention_cutoff)
        self.stdout.write(
            f"usage_buckets_observed={len(expected)} updated_upward={updated} "
            f"weekly_activity_added={activity_added} active_days_added={active_days_added} "
            f"retention_cutoff={retention_cutoff.isoformat()}"
        )
