import logging
from datetime import datetime
from datetime import timezone as datetime_timezone

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from apps.inboxes.models import Inbox, Message

from .models import AccountPlan, MonthlyUsage

logger = logging.getLogger(__name__)


def utc_month_start(value=None):
    value = value or timezone.now()
    if isinstance(value, datetime):
        if timezone.is_naive(value):
            value = timezone.make_aware(value, datetime_timezone.utc)
        value = value.astimezone(datetime_timezone.utc).date()
    return value.replace(day=1)


def plan_limits(plan):
    return settings.BILLING_PLAN_LIMITS[plan]


def _locked_plan(user, *, lock=True):
    "Public API."
    qs = AccountPlan.objects.filter(user=user)
    if lock:
        qs = qs.select_for_update()
    return qs.first()


def can_create_inbox(user) -> bool:
    "Public API."
    plan = _locked_plan(user)
    if plan is None or plan.status != AccountPlan.Status.ACTIVE:
        return False
    inbox_count = Inbox.objects.filter(user=user, deleted_at__isnull=True).count()
    return inbox_count < plan_limits(plan.plan)["inboxes"]


def _plan_can_enable_webhook(plan) -> bool:
    return bool(
        plan
        and plan.status == AccountPlan.Status.ACTIVE
        and plan_limits(plan.plan)["webhooks"]
    )


def can_enable_webhook(user) -> bool:
    "Public API."
    return _plan_can_enable_webhook(_locked_plan(user))


def can_configure_webhook(user) -> bool:
    return _plan_can_enable_webhook(AccountPlan.objects.filter(user=user).first())


def can_add_domain(user, *, lock=True) -> bool:
    "Public API."
    from apps.domains.models import Domain

    plan = _locked_plan(user, lock=lock)
    if plan is None or plan.status != AccountPlan.Status.ACTIVE:
        return False
    domain_count = Domain.objects.filter(user=user).count()
    return domain_count < plan_limits(plan.plan)["custom_domains"]


def team_seat_usage(owner_user) -> dict:
    "Public API."
    from django.utils import timezone

    from .models import TeamInvite, TeamMembership

    plan = AccountPlan.objects.filter(user=owner_user).first()
    total = plan_limits(plan.plan)["seats"] if plan is not None else 1
    member_count = TeamMembership.objects.filter(
        account_id=owner_user.pk, revoked_at__isnull=True
    ).count()
    pending_invite_count = TeamInvite.objects.filter(
        account_id=owner_user.pk,
        accepted_at__isnull=True,
        revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).count()
    return {
        "used": 1 + member_count + pending_invite_count,
        "total": total,
        "members": member_count,
        "pending": pending_invite_count,
    }


def can_invite_member(owner_user, *, lock=True) -> bool:
    "Public API."
    from django.utils import timezone

    from .models import TeamInvite, TeamMembership

    plan = _locked_plan(owner_user, lock=lock)
    if plan is None or plan.status != AccountPlan.Status.ACTIVE:
        return False
    member_count = TeamMembership.objects.filter(
        account_id=owner_user.pk, revoked_at__isnull=True
    ).count()
    pending_invite_count = TeamInvite.objects.filter(
        account_id=owner_user.pk,
        accepted_at__isnull=True,
        revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).count()
    return (member_count + pending_invite_count) < plan_limits(plan.plan)["seats"] - 1


def can_accept_member(owner_user) -> bool:
    "Public API."
    from .models import TeamMembership

    plan = _locked_plan(owner_user)
    if plan is None or plan.status != AccountPlan.Status.ACTIVE:
        return False
    member_count = TeamMembership.objects.filter(
        account_id=owner_user.pk, revoked_at__isnull=True
    ).count()
    return member_count < plan_limits(plan.plan)["seats"] - 1


def can_toggle_catch_all(user) -> bool:
    "Public API."
    plan = _locked_plan(user)
    if plan is None or plan.status != AccountPlan.Status.ACTIVE:
        return False
    return bool(plan_limits(plan.plan)["catch_all"])






def can_use_full_verify(user) -> bool:
    "Public API."
    plan = _locked_plan(user)
    if plan is None or plan.status != AccountPlan.Status.ACTIVE:
        return False
    return bool(plan_limits(plan.plan)["verify_full"])




# feature (this function); enforcement itself reads ONLY
# Account.outbound_allowlist_enabled (apps.outbound.services/engine), never
# the tier, so a plan downgrade/lapse with the flag ON can never silently
# reopen sending.


def can_configure_outbound_allowlist(user) -> bool:
    "Public API."
    plan = _locked_plan(user)
    if plan is None or plan.status != AccountPlan.Status.ACTIVE:
        return False
    return bool(plan_limits(plan.plan)["outbound_allowlist"])





# row, then count the DURABLE OutboundEmail rows already created inside the
# rolling day/hour windows — no separate counter to drift, refused create or
# a crash rolls back the whole transaction and nothing is counted (atomic by
# construction, exactly like can_create_inbox counts Inbox rows).


def can_send_outbound(user, recipient_count: int) -> tuple[bool, str]:
    """Return (ok, reason). reason is "" on success, else one of
    "outbound_entitlement_required" (tier cannot send outbound at all —
    free/paid/scale have outbound_per_day=outbound_per_hour=0 unconditionally,
    demo/agent accounts are blocked earlier by a SEPARATE independent wall —
    see apps.accounts.team_services.is_demo_or_agent_user) or
    "outbound_quota_exceeded" (a Business tenant's own day/hour recipient cap
    would be exceeded by this send).

    Caller MUST hold this inside the SAME transaction as the OutboundEmail
    create (mirrors can_add_domain's docstring) — _locked_plan's
    select_for_update keeps the row locked until the caller's transaction
    commits/rolls back, so two concurrent enqueues at the boundary can never
    both slip past the cap (design §7 "Quota atomicity" test group).
    """
    from datetime import timedelta

    from django.db.models import Sum
    from django.utils import timezone

    plan = _locked_plan(user)
    if plan is None or plan.status != AccountPlan.Status.ACTIVE:
        return False, "outbound_entitlement_required"
    limits = plan_limits(plan.plan)
    day_limit = limits["outbound_per_day"]
    hour_limit = limits["outbound_per_hour"]
    if day_limit <= 0 or hour_limit <= 0:
        return False, "outbound_entitlement_required"

    from apps.outbound.models import OutboundEmail

    now = timezone.now()
    day_sum = (
        OutboundEmail.objects.filter(user=user, created_at__gte=now - timedelta(hours=24))
        .aggregate(total=Sum("recipient_count"))["total"]
        or 0
    )
    if day_sum + recipient_count > day_limit:
        return False, "outbound_quota_exceeded"
    hour_sum = (
        OutboundEmail.objects.filter(user=user, created_at__gte=now - timedelta(hours=1))
        .aggregate(total=Sum("recipient_count"))["total"]
        or 0
    )
    if hour_sum + recipient_count > hour_limit:
        return False, "outbound_quota_exceeded"
    return True, ""


def _upsert_counter(user_id, period_start, column, amount=1):
    table = connection.ops.quote_name(MonthlyUsage._meta.db_table)
    if column not in {"api_requests", "webhook_attempts", "verifications_used"}:
        raise ValueError("Unsupported usage counter")
    initial = {
        "messages_received": 0,
        "api_requests": amount if column == "api_requests" else 0,
        "webhook_attempts": amount if column == "webhook_attempts" else 0,
        "verifications_used": amount if column == "verifications_used" else 0,
    }
    quoted_column = connection.ops.quote_name(column)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO {table}
                (user_id, period_start, messages_received, api_requests, webhook_attempts, verifications_used)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, period_start) DO UPDATE
            SET {quoted_column} = {table}.{quoted_column} + %s
            """,
            [
                user_id,
                period_start,
                initial["messages_received"],
                initial["api_requests"],
                initial["webhook_attempts"],
                initial["verifications_used"],
                amount,
            ],
        )


def record_message_once(message):
    """Increment once using the durable Message row as the idempotency guard."""
    period_start = utc_month_start(message.received_at)
    with transaction.atomic():
        newly_counted = Message.objects.filter(pk=message.pk, usage_counted=False).update(
            usage_counted=True
        )
        if newly_counted:
            table = connection.ops.quote_name(MonthlyUsage._meta.db_table)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO {table}
                        (user_id, period_start, messages_received, api_requests, webhook_attempts, verifications_used)
                    VALUES (%s, %s, 1, 0, 0, 0)
                    ON CONFLICT (user_id, period_start) DO UPDATE
                    SET messages_received = {table}.messages_received + 1
                    """,
                    [message.inbox.user_id, period_start],
                )
            _record_activity_best_effort(message.pk)
    return newly_counted


def _record_activity_best_effort(message_id):
    """Funnel signal (apps.accounts.activity). Own savepoint: a failure here
    is logged and never rolls back or blocks the usage count above. The
    import is inside the guard too: an import-time failure of the activity
    module must not escape into record_message_once's transaction."""
    try:
        with transaction.atomic():
            from .activity import record_weekly_activity

            record_weekly_activity(message_id)
    except Exception:
        logger.exception("weekly_activity_failed message_id=%s", message_id)
    try:  # activation item 2b: early active days (retained activation)
        from .funnel_signals import record_active_day

        record_active_day(message_id)
    except Exception:
        logger.exception("active_day_failed message_id=%s", message_id)


def record_api_request(user):
    try:
        _upsert_counter(user.pk, utc_month_start(), "api_requests")
    except Exception:
        logger.exception("usage_meter_failed counter=api_requests user_id=%s", user.pk)


def record_webhook_attempt(user_id):
    try:
        _upsert_counter(user_id, utc_month_start(), "webhook_attempts")
    except Exception:
        logger.exception("usage_meter_failed counter=webhook_attempts user_id=%s", user_id)


def record_verifications(user_id, amount=1):
    "Public API."
    try:
        _upsert_counter(user_id, utc_month_start(), "verifications_used", amount=amount)
    except Exception:
        logger.exception("usage_meter_failed counter=verifications_used user_id=%s", user_id)




# forward elements should render AT ALL. Deliberately NO select_for_update
# (can_send_outbound's _locked_plan row lock is a POST-time transactional
# gate, unsafe to acquire on every page render). This is y only — the
# real transactional gate (enqueue_outbound_email -> can_send_outbound) always
# re-runs at POST time, so a stale render before a downgrade still hard-fails
# cleanly, never a 500.


def outbound_ui_visible(request) -> bool:
    from django.conf import settings as django_settings

    from apps.accounts.team_services import is_demo_or_agent_user
    from apps.accounts.tenancy import require_owner, tenant

    if not django_settings.ENABLE_OUTBOUND:
        return False
    if is_demo_or_agent_user(request.user):
        return False
    if not require_owner(request):
        return False
    plan = AccountPlan.objects.filter(user=tenant(request)).first()
    if plan is None or plan.status != AccountPlan.Status.ACTIVE:
        return False
    return plan_limits(plan.plan)["outbound_per_day"] > 0


def usage_summary(user, *, at=None):
    period_start = utc_month_start(at)
    plan = AccountPlan.objects.get(user=user)
    limits = plan_limits(plan.plan)
    inbox_count = Inbox.objects.filter(user=user, deleted_at__isnull=True).count()
    usage = MonthlyUsage.objects.filter(user=user, period_start=period_start).first()
    messages_received = usage.messages_received if usage else 0
    return {
        "plan": plan,
        "period_start": period_start,
        "messages_received": messages_received,
        "message_limit": limits["messages_per_month"],
        "limit_exceeded": messages_received > limits["messages_per_month"],
        "inbox_count": inbox_count,
        "inbox_limit": limits["inboxes"],
        "can_create_inbox": plan.status == AccountPlan.Status.ACTIVE and inbox_count < limits["inboxes"],
        "can_enable_webhook": can_configure_webhook(user),
        "api_requests": usage.api_requests if usage else 0,
        "webhook_attempts": usage.webhook_attempts if usage else 0,
        "verifications_used": usage.verifications_used if usage else 0,
    }
