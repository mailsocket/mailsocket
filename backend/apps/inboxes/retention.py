from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Min, Q
from django.utils import timezone

from apps.accounts.models import AccountPlan
from apps.webhooks.models import WebhookDelivery

from .models import Message

MAX_BATCH_SIZE = 5000
SENSITIVE_FIELD_DEFAULTS: dict[str, Any] = {
    "raw_bytes": b"",
    "subject": "",
    "text_body": "",
    "html_body": "",
    "otp": None,
    "otp_confidence": None,
    "magic_link": None,
    "links": [],
    "detected_otp": None,
}


@dataclass(frozen=True)
class PurgeResult:
    messages: int
    deliveries: int
    batches: int


def _cutoffs(now):
    """The per-tier expiry cutoffs the purge uses to decide a message is expired.

    ``now - RETENTION_DAYS[tier]`` for EVERY plan defined in RETENTION_DAYS
    (not a hard-coded pair) — the single source of truth shared by the purge
    and the health check, so the two can never drift apart, and a newly added
    tier is picked up automatically.
    """
    retention = settings.RETENTION_DAYS
    return {plan: now - timedelta(days=days) for plan, days in retention.items()}


def _plan_expiry_filter(now):
    """Expired-message predicate: every message whose effective timestamp
    (``received_at``, falling back to ``created_at`` when null) precedes its
    plan tier's cutoff.

    Builds one branch per plan actually present in RETENTION_DAYS, so a
    future tier is purged the moment it is added there — it can never fall
    through and accumulate silently. A tenant with no AccountPlan row, OR
    with a plan value that is NOT a known key in RETENTION_DAYS, is treated
    as the SHORTEST (free) window: never longer retention than intended,
    fail safe toward purging sooner rather than never.

    The "unknown plan" case is handled as an EXCLUSION (any persisted value
    not in the known RETENTION_DAYS keys), NOT an enumeration of the
    AccountPlan.Plan enum members. The Plan enum's choices are a Django
    validation convenience, not a database constraint — a CharField can and
    does hold arbitrary strings (e.g. a stale "legacy" value left over from
    before a plan was renamed/removed). Enumerating only
    set(Plan.values) - known_plans would miss any such out-of-enum string
    entirely, so that row would match none of the per-plan branches NOR the
    unknown-enum-member branch and escape purge forever — exactly the
    silent-infinite-retention class this function exists to prevent.
    """
    cutoffs = _cutoffs(now)
    free_plan = AccountPlan.Plan.FREE
    known_plans = list(cutoffs)

    def _timestamp_before(cutoff):
        return Q(received_at__lt=cutoff) | Q(received_at__isnull=True, created_at__lt=cutoff)

    condition = Q(inbox__user__account_plan__isnull=True) & _timestamp_before(
        cutoffs[free_plan]
    )
    for plan, cutoff in cutoffs.items():
        condition |= Q(inbox__user__account_plan__plan=plan) & _timestamp_before(cutoff)
    # Any row that DOES have an AccountPlan but whose plan value is NOT one of
    # the known RETENTION_DAYS keys — whether a removed/future enum member or
    # an arbitrary out-of-enum string persisted directly in the CharField —
    # falls back to the free cutoff rather than escaping purge forever.
    condition |= (
        Q(inbox__user__account_plan__isnull=False)
        & ~Q(inbox__user__account_plan__plan__in=known_plans)
    ) & _timestamp_before(cutoffs[free_plan])
    return condition


def purge_expired_messages(*, batch_size=500, now=None):
    """Hard-delete expired committed messages in short, independent transactions."""
    # Defense in depth: clamp the batch size to a sane bound inside the function
    # itself, not only in the management command, so any caller (cron, shell,
    # future code) can never pass an unbounded or non-positive batch size.
    batch_size = max(1, min(int(batch_size), MAX_BATCH_SIZE))
    now = now or timezone.now()
    deleted_messages = 0
    deleted_deliveries = 0
    batches = 0

    while True:
        with transaction.atomic():
            message_ids = list(
                Message.objects.select_for_update(skip_locked=True, of=("self",))
                .filter(_plan_expiry_filter(now))
                .order_by("received_at", "id")
                .values_list("id", flat=True)[:batch_size]
            )
            if not message_ids:
                break

            # Django's Collector cannot fast-delete Message because WebhookDelivery
            # has a PROTECT FK, so deletion SELECTs full Message rows. Empty the
            # potentially 10 MB raw payloads before that load-bearing fetch.
            Message.objects.filter(id__in=message_ids).update(**SENSITIVE_FIELD_DEFAULTS)
            delivery_count, _ = WebhookDelivery.objects.filter(
                message_id__in=message_ids
            ).delete()
            message_count, _ = Message.objects.filter(id__in=message_ids).delete()

        deleted_messages += message_count
        deleted_deliveries += delivery_count
        batches += 1

    return PurgeResult(
        messages=deleted_messages,
        deliveries=deleted_deliveries,
        batches=batches,
    )


@dataclass(frozen=True)
class RetentionHealth:
    ok: bool
    overdue_free: int = 0
    overdue_paid: int = 0
    overdue_scale: int = 0
    oldest_free: "timedelta | None" = None
    oldest_paid: "timedelta | None" = None
    oldest_scale: "timedelta | None" = None


def _age_from(now, received_at):
    return now - received_at


def check_retention_health(*, grace_hours=2, now=None):
    """Report whether any message is overdue for purge beyond a grace margin.

    A message is *overdue* when its effective timestamp precedes its plan
    tier's expiry cutoff by more than ``grace_hours`` — i.e. the purge has not
    removed it even though it was due (plus a grace buffer to avoid
    false-alarming on a merely-late run). Pure read-only; uses bounded
    aggregates only, never loads message rows.
    """
    now = now or timezone.now()
    grace = timedelta(hours=grace_hours)
    # Overdue == "expired as of now-grace": a message whose timestamp precedes
    # (now - grace) - RETENTION_DAYS, i.e. older than window + grace. Reuses the
    # purge's exact expiry predicate so the check and the purge share one source
    # of truth — the grace margin is applied to the effective clock, never to the
    # window constants.
    overdue_filter = _plan_expiry_filter(now - grace)

    def _tier_stats(plan):
        tier_filter = Q(inbox__user__account_plan__plan=plan)
        if plan == AccountPlan.Plan.FREE:
            tier_filter |= Q(inbox__user__account_plan__isnull=True)
            # ANY plan value that isn't a known RETENTION_DAYS key falls back
            # to the free window in the purge predicate (_plan_expiry_filter),
            # so it must also be counted under Free here or its overdue rows
            # would be invisible to monitoring — the #5 landmine the spec
            # calls out. This is an EXCLUSION against known keys, not an
            # enumeration of the AccountPlan.Plan enum: an arbitrary
            # out-of-enum string persisted in the CharField (e.g. "legacy")
            # must be caught too, not just removed/future enum members.
            known_plans = list(settings.RETENTION_DAYS)
            tier_filter |= Q(inbox__user__account_plan__isnull=False) & ~Q(
                inbox__user__account_plan__plan__in=known_plans
            )
        overdue = Message.objects.filter(overdue_filter & tier_filter)
        overdue_count = overdue.count()
        oldest_received = Message.objects.filter(tier_filter).aggregate(
            oldest=Min("received_at")
        )["oldest"]
        if oldest_received is None:
            # All rows on this tier have a null received_at: fall back to the
            # oldest created_at so the OK line still reports a real age.
            oldest_received = Message.objects.filter(tier_filter).aggregate(
                oldest=Min("created_at")
            )["oldest"]
        oldest_age = _age_from(now, oldest_received) if oldest_received else None
        return overdue_count, oldest_age

    # Iterate every plan defined in RETENTION_DAYS (not a hard-coded pair) so
    # a newly added tier (e.g. Scale) is counted in the health check the
    # moment it lands, instead of being invisible to monitoring.
    overdue_by_plan = {}
    oldest_by_plan = {}
    for plan in settings.RETENTION_DAYS:
        overdue_by_plan[plan], oldest_by_plan[plan] = _tier_stats(plan)

    overdue_free = overdue_by_plan.get(AccountPlan.Plan.FREE, 0)
    overdue_paid = overdue_by_plan.get(AccountPlan.Plan.PAID, 0)
    overdue_scale = overdue_by_plan.get(AccountPlan.Plan.SCALE, 0)
    oldest_free = oldest_by_plan.get(AccountPlan.Plan.FREE)
    oldest_paid = oldest_by_plan.get(AccountPlan.Plan.PAID)
    oldest_scale = oldest_by_plan.get(AccountPlan.Plan.SCALE)

    return RetentionHealth(
        ok=all(count == 0 for count in overdue_by_plan.values()),
        overdue_free=overdue_free,
        overdue_paid=overdue_paid,
        overdue_scale=overdue_scale,
        oldest_free=oldest_free,
        oldest_paid=oldest_paid,
        oldest_scale=oldest_scale,
    )
