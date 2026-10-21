from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.inboxes.models import Message

from .parser import claim_message, process_claim

MAX_BATCH_SIZE = 1000


@dataclass(frozen=True)
class ReconcileResult:
    claimed: int
    parsed: int
    retryable: int
    failed: int


def claim_batch(*, stale_seconds: int, batch_size: int) -> list[tuple[int, UUID]]:
    now = timezone.now()
    stale_cutoff = now - timedelta(seconds=stale_seconds)
    eligible = Q(
        ingest_status=Message.IngestStatus.RECEIVED,
        next_parse_at__isnull=True,
    ) | Q(
        ingest_status=Message.IngestStatus.RECEIVED,
        next_parse_at__lte=now,
    ) | Q(
        ingest_status=Message.IngestStatus.PARSING,
        parse_lease_expires_at__lte=now,
    ) | Q(
        ingest_status=Message.IngestStatus.PARSING,
        parse_lease_expires_at__isnull=True,
        parse_started_at__lte=stale_cutoff,
    )

    claims = []
    with transaction.atomic():
        rows = list(
            Message.objects.select_for_update(skip_locked=True)
            .filter(eligible)
            .order_by("received_at", "id")[:batch_size]
        )
        for row in rows:
            if row.parse_attempts >= int(settings.MAX_PARSE_ATTEMPTS):
                Message.objects.filter(pk=row.pk).update(
                    ingest_status=Message.IngestStatus.FAILED,
                    parse_error="Parser resource limit: parse attempt limit exceeded",
                    parse_lease_token=None,
                    parse_lease_expires_at=None,
                    next_parse_at=None,
                )
                continue
            if row.ingest_status == Message.IngestStatus.PARSING:
                Message.objects.filter(
                    pk=row.pk,
                    ingest_status=Message.IngestStatus.PARSING,
                    parse_lease_token=row.parse_lease_token,
                ).update(
                    ingest_status=Message.IngestStatus.RECEIVED,
                    parse_lease_token=None,
                    parse_lease_expires_at=None,
                    next_parse_at=None,
                )
            token = claim_message(row.pk)
            if token is not None:
                claims.append((row.pk, token))
    return claims


def reconcile(*, stale_seconds: int = 300, batch_size: int = 100) -> ReconcileResult:
    claims = claim_batch(stale_seconds=stale_seconds, batch_size=batch_size)
    parsed = retryable = failed = 0
    for message_id, token in claims:
        message = process_claim(message_id, token)
        if message.ingest_status == Message.IngestStatus.PARSED:
            parsed += 1
        elif message.ingest_status == Message.IngestStatus.FAILED:
            failed += 1
        else:
            retryable += 1
    return ReconcileResult(
        claimed=len(claims),
        parsed=parsed,
        retryable=retryable,
        failed=failed,
    )
