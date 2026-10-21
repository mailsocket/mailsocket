import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from datetime import timezone as datetime_timezone
from io import StringIO
from unittest.mock import patch

import pytest
from django.contrib import admin
from django.contrib.admin.models import CHANGE, LogEntry
from django.core.management import call_command
from django.db import close_old_connections, transaction
from django.test import override_settings
from django.utils import timezone

from apps.accounts.admin import AccountAdmin
from apps.accounts.entitlements import can_create_inbox, record_message_once, usage_summary
from apps.accounts.models import Account, AccountPlan, MonthlyUsage
from apps.accounts.services import create_product_user
from apps.inboxes.models import Inbox, Message
from apps.ingest.services import IngestError, parse_after_commit, store_message
from apps.webhooks.models import Webhook, WebhookDelivery
from apps.webhooks.services import create_webhook, update_webhook

pytestmark = pytest.mark.django_db(transaction=True)


def verified_user(name, *, plan=AccountPlan.Plan.FREE):
    user = create_product_user(f"{name}@example.test", "test-password")
    Account.objects.filter(user=user).update(email_verified_at=timezone.now())
    AccountPlan.objects.filter(user=user).update(plan=plan)
    return user


def message_values(inbox, suffix):
    raw = f"Subject: {suffix}\r\n\r\nbody".encode()
    digest = hashlib.sha256(raw).hexdigest()
    return {
        "inbox": inbox,
        "dedupe_key": digest,
        "envelope_recipient": inbox.address,
        "raw_bytes": raw,
        "raw_sha256": digest,
        "size_bytes": len(raw),
    }


@override_settings(BILLING_PLAN_LIMITS={
    "free": {"inboxes": 1, "messages_per_month": 1, "webhooks": False},
    "paid": {"inboxes": 10, "messages_per_month": 100, "webhooks": True},
})
def test_case_39_concurrent_free_inbox_creation_cannot_exceed_limit():
    user = verified_user("concurrent")
    barrier = threading.Barrier(2)

    def create(index):
        close_old_connections()
        barrier.wait(timeout=10)
        with transaction.atomic():
            allowed = can_create_inbox(user)
            if allowed:
                Inbox.objects.create(user=user, routing_key=f"concurrent_{index}")
        close_old_connections()
        return allowed

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert sorted(results) == [False, True]
    assert Inbox.objects.filter(user=user, deleted_at__isnull=True).count() == 1


def test_case_40_webhook_entitlement_downgrade_preserves_disable_and_history(monkeypatch):
    user = verified_user("webhook-plan")
    inbox = Inbox.objects.create(user=user, routing_key="webhook_plan")
    def resolver(_):
        return ["93.184.216.34"]
    with pytest.raises(PermissionError):
        create_webhook(inbox, "https://receiver.example/hook", resolver=resolver)

    AccountPlan.objects.filter(user=user).update(plan=AccountPlan.Plan.PAID)
    webhook, _ = create_webhook(inbox, "https://receiver.example/hook", resolver=resolver)
    message = Message.objects.create(**message_values(inbox, "history"))
    delivery = WebhookDelivery.objects.create(
        webhook=webhook,
        message=message,
        event_id="evt_history",
        payload={},
    )
    AccountPlan.objects.filter(user=user).update(plan=AccountPlan.Plan.FREE)
    with pytest.raises(PermissionError):
        update_webhook(webhook, webhook.url, enabled=True, resolver=resolver)
    webhook.is_enabled = False
    webhook.save(update_fields=["is_enabled", "updated_at"])
    assert WebhookDelivery.objects.get(pk=delivery.pk).webhook_id == webhook.pk


def test_webhook_enable_waits_for_concurrent_downgrade_and_is_rejected():
    user = verified_user("webhook-race", plan=AccountPlan.Plan.PAID)
    inbox = Inbox.objects.create(user=user, routing_key="webhook_race")
    def resolver(_):
        return ["93.184.216.34"]
    webhook, _ = create_webhook(
        inbox, "https://receiver.example/hook", enabled=False, resolver=resolver
    )
    downgrade_written = threading.Event()
    allow_commit = threading.Event()

    def downgrade():
        close_old_connections()
        with transaction.atomic():
            plan = AccountPlan.objects.select_for_update().get(user=user)
            plan.plan = AccountPlan.Plan.FREE
            plan.save(update_fields=["plan", "updated_at"])
            downgrade_written.set()
            assert allow_commit.wait(timeout=10)
        close_old_connections()

    def enable():
        close_old_connections()
        local = Webhook.objects.select_related("inbox__user").get(pk=webhook.pk)
        try:
            update_webhook(local, local.url, enabled=True, resolver=resolver)
        except PermissionError:
            result = "rejected"
        else:
            result = "enabled"
        close_old_connections()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        downgrade_future = pool.submit(downgrade)
        assert downgrade_written.wait(timeout=10)
        enable_future = pool.submit(enable)
        time.sleep(0.2)
        assert not enable_future.done()
        allow_commit.set()
        downgrade_future.result(timeout=10)
        assert enable_future.result(timeout=10) == "rejected"
    webhook.refresh_from_db()
    assert webhook.is_enabled is False


@override_settings(
    BILLING_PLAN_LIMITS={
        "free": {"inboxes": 1, "messages_per_month": 1, "webhooks": False},
        "paid": {"inboxes": 10, "messages_per_month": 100, "webhooks": True},
    },
    RETENTION_DAYS={"free": 1, "paid": 40, "scale": 30},
)
def test_cases_41_42_usage_once_utc_boundary_soft_limit_isolation_and_repair():
    user = verified_user("usage")
    inbox = Inbox.objects.create(user=user, routing_key="usage_box")
    headers = {
        "X-Envelope-To": inbox.address,
        "X-Envelope-From": "sender@example.test",
    }
    first = store_message(raw_bytes=b"Subject: one\r\n\r\none", headers=headers)
    duplicate = store_message(raw_bytes=b"Subject: one\r\n\r\none", headers=headers)
    assert not first.deduplicated and duplicate.deduplicated
    parse_after_commit(first.message.pk)
    assert not record_message_once(duplicate.message)
    current = MonthlyUsage.objects.get(user=user, period_start=timezone.now().date().replace(day=1))
    assert current.messages_received == 1

    # Last minute of the PREVIOUS UTC month, relative to today: a fixed date (it was
    # 2026-08-31) fell out of the 40-day reconcile window on 2026-10-11 (time bomb).
    now_utc = timezone.now().astimezone(datetime_timezone.utc)
    month_start = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous_stamp = month_start - timedelta(minutes=1)
    previous = Message.objects.create(**message_values(inbox, "previous"))
    Message.objects.filter(pk=previous.pk).update(received_at=previous_stamp)
    call_command("reconcile_monthly_usage", stdout=StringIO())
    assert MonthlyUsage.objects.get(user=user, period_start=previous_stamp.date().replace(day=1)).messages_received == 1

    with patch("apps.accounts.entitlements.record_message_once", side_effect=RuntimeError("meter down")):
        second = store_message(raw_bytes=b"Subject: two\r\n\r\ntwo", headers=headers)
        parse_after_commit(second.message.pk)
    assert not second.deduplicated and Message.objects.filter(pk=second.message.pk).exists()
    assert usage_summary(user)["limit_exceeded"] is False

    call_command("reconcile_monthly_usage", stdout=StringIO())
    current.refresh_from_db()
    assert current.messages_received == 2
    assert usage_summary(user)["limit_exceeded"] is True

    current.messages_received = 999
    current.save(update_fields=["messages_received"])
    call_command("reconcile_monthly_usage", stdout=StringIO())
    current.refresh_from_db()
    assert current.messages_received == 999


@override_settings(RETENTION_DAYS={"free": 1, "paid": 7, "scale": 30})
def test_reconcile_only_corrects_upward_and_skips_past_retention():
    user = verified_user("reconcile-retention")
    inbox = Inbox.objects.create(user=user, routing_key="reconcile_retention")
    current_period = timezone.now().date().replace(day=1)
    durable = MonthlyUsage.objects.create(
        user=user,
        period_start=current_period,
        messages_received=10,
    )
    recent = Message.objects.create(**message_values(inbox, "recent-reconcile"))
    old = Message.objects.create(**message_values(inbox, "old-reconcile"))
    old_stamp = timezone.now() - timedelta(days=40)
    Message.objects.filter(pk=old.pk).update(received_at=old_stamp)

    call_command("reconcile_monthly_usage", stdout=StringIO())

    durable.refresh_from_db()
    recent.refresh_from_db()
    old.refresh_from_db()
    assert durable.messages_received == 10
    assert recent.usage_counted is True
    assert old.usage_counted is False
    assert not MonthlyUsage.objects.filter(
        user=user,
        period_start=old_stamp.date().replace(day=1),
    ).exists()


def test_account_admin_action_suspends_and_soft_deletes_all_inboxes(admin_user):
    user = verified_user("admin-ban")
    inboxes = [
        Inbox.objects.create(user=user, routing_key=f"admin_ban_{index}")
        for index in range(2)
    ]
    model_admin = AccountAdmin(Account, admin.site)
    queryset = Account.objects.filter(user=user)

    model_admin.suspend_and_disable_inboxes(None, queryset)
    model_admin.suspend_and_disable_inboxes(None, queryset)

    assert Account.objects.get(user=user).is_suspended is True
    assert Inbox.objects.filter(
        pk__in=[inbox.pk for inbox in inboxes],
        is_enabled=False,
        deleted_at__isnull=False,
    ).count() == 2
    with pytest.raises(IngestError) as exc_info:
        store_message(
            raw_bytes=b"Subject: rejected\r\n\r\nrejected",
            headers={
                "X-Envelope-To": inboxes[0].address,
                "X-Envelope-From": "sender@example.test",
            },
        )
    assert exc_info.value.status_code == 422


def test_usage_is_off_store_path_and_increment_has_no_message_recount():
    user = verified_user("usage-lock")
    inbox = Inbox.objects.create(user=user, routing_key="usage_lock")
    period = timezone.now().date().replace(day=1)
    MonthlyUsage.objects.create(user=user, period_start=period)
    lock_held = threading.Event()
    release_lock = threading.Event()

    def hold_usage_lock():
        close_old_connections()
        with transaction.atomic():
            MonthlyUsage.objects.select_for_update().get(user=user, period_start=period)
            lock_held.set()
            assert release_lock.wait(timeout=10)
        close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        lock_future = pool.submit(hold_usage_lock)
        assert lock_held.wait(timeout=10)
        started = time.monotonic()
        stored = store_message(
            raw_bytes=b"Subject: lock proof\r\n\r\nbody",
            headers={
                "X-Envelope-To": inbox.address,
                "X-Envelope-From": "sender@example.test",
            },
        )
        elapsed = time.monotonic() - started
        assert elapsed < 1
        assert not stored.message.usage_counted
        release_lock.set()
        lock_future.result(timeout=10)

    with patch.object(Message.objects, "filter", wraps=Message.objects.filter) as filtered:
        assert record_message_once(stored.message)
        assert not record_message_once(stored.message)
    assert all(call.kwargs == {"pk": stored.message.pk, "usage_counted": False} for call in filtered.call_args_list)
    assert MonthlyUsage.objects.get(user=user, period_start=period).messages_received == 1


def test_case_43_nullable_provider_metadata_and_admin_plan_change_is_auditable(admin_user):
    user = verified_user("manual-plan")
    plan = AccountPlan.objects.get(user=user)
    assert plan.stripe_customer_id is plan.stripe_subscription_id is plan.stripe_price_id is None
    plan.plan = AccountPlan.Plan.PAID
    plan.save(update_fields=["plan", "updated_at"])
    LogEntry.objects.log_actions(
        user_id=admin_user.pk,
        queryset=AccountPlan.objects.filter(pk=plan.pk),
        action_flag=CHANGE,
        change_message="Changed plan.",
        single_object=True,
    )
    assert LogEntry.objects.filter(object_id=str(plan.pk), action_flag=CHANGE).exists()


def test_plan_suspension_blocks_dashboard_but_not_inbound_storage(client):
    user = verified_user("suspended-plan")
    inbox = Inbox.objects.create(user=user, routing_key="suspended_plan")
    AccountPlan.objects.filter(user=user).update(status=AccountPlan.Status.SUSPENDED)
    client.force_login(user)
    assert client.get("/app/").status_code == 302
    stored = store_message(
        raw_bytes=b"Subject: accepted\r\n\r\nstored",
        headers={"X-Envelope-To": inbox.address, "X-Envelope-From": "sender@example.test"},
    )
    assert not stored.deduplicated and Message.objects.filter(pk=stored.message.pk).exists()


# --- Scale ($49) tier: enforcement-only, no checkout (T1-T6, T11, T13) ------


def test_t1_scale_can_create_inbox_caps_at_fifty():
    user = verified_user("scale-inboxes", plan=AccountPlan.Plan.SCALE)
    for index in range(49):
        Inbox.objects.create(user=user, routing_key=f"scale_inbox_{index}")
    with transaction.atomic():
        assert can_create_inbox(user) is True
    Inbox.objects.create(user=user, routing_key="scale_inbox_49")
    with transaction.atomic():
        assert can_create_inbox(user) is False


def test_t2_scale_usage_metering_limit_is_fifty_thousand():
    user = verified_user("scale-usage", plan=AccountPlan.Plan.SCALE)
    period_start = timezone.now().date().replace(day=1)
    summary = usage_summary(user)
    assert summary["message_limit"] == 50000

    MonthlyUsage.objects.create(user=user, period_start=period_start, messages_received=49_999)
    assert usage_summary(user)["limit_exceeded"] is False

    MonthlyUsage.objects.filter(user=user, period_start=period_start).update(
        messages_received=50_000
    )
    assert usage_summary(user)["limit_exceeded"] is False

    MonthlyUsage.objects.filter(user=user, period_start=period_start).update(
        messages_received=50_001
    )
    assert usage_summary(user)["limit_exceeded"] is True


def test_t3_scale_can_enable_and_configure_webhooks():
    from apps.accounts.entitlements import can_configure_webhook, can_enable_webhook

    user = verified_user("scale-webhooks", plan=AccountPlan.Plan.SCALE)
    with transaction.atomic():
        assert can_enable_webhook(user) is True
    assert can_configure_webhook(user) is True


def test_t4_scale_wait_lease_limit_is_twenty_not_two():
    from apps.accounts.services import wait_lease_limit_for

    user = verified_user("scale-wait", plan=AccountPlan.Plan.SCALE)
    assert wait_lease_limit_for(user.pk) == 20
    assert wait_lease_limit_for(user.pk) != 2


def test_t5_scale_is_not_free_plan_routes_to_paid_pool():
    from apps.accounts.services import is_free_plan

    user = verified_user("scale-pool", plan=AccountPlan.Plan.SCALE)
    assert is_free_plan(user.pk) is False


def test_t6_plan_limits_scale_returns_dict_no_key_error():
    from apps.accounts.entitlements import plan_limits

    limits = plan_limits("scale")
    assert isinstance(limits, dict)
    assert limits["inboxes"] == 50
    assert limits["messages_per_month"] == 50000
    assert limits["retention_days"] == 30
    assert limits["wait_concurrency"] == 20
    assert limits["is_paid_tier"] is True
    assert limits["webhooks"] is True


def test_t11_free_and_paid_limits_are_unchanged_by_the_scale_addition():
    
    # 2026-10-03; everything else here is still unchanged.
    from apps.accounts.entitlements import plan_limits
    from apps.accounts.services import is_free_plan, wait_lease_limit_for

    free_user = verified_user("regression-free", plan=AccountPlan.Plan.FREE)
    paid_user = verified_user("regression-paid", plan=AccountPlan.Plan.PAID)

    free_limits = plan_limits("free")
    paid_limits = plan_limits("paid")
    assert free_limits["inboxes"] == 3
    assert free_limits["messages_per_month"] == 1000
    assert free_limits["retention_days"] == 1
    assert free_limits["webhooks"] is False
    assert paid_limits["inboxes"] == 10
    assert paid_limits["messages_per_month"] == 10000
    assert paid_limits["retention_days"] == 7
    assert paid_limits["webhooks"] is True

    assert wait_lease_limit_for(free_user.pk) == 2
    assert wait_lease_limit_for(paid_user.pk) == 10
    assert is_free_plan(free_user.pk) is True
    assert is_free_plan(paid_user.pk) is False


def test_t13_set_account_plan_command_promotes_account_to_scale():
    user = verified_user("onboard-scale", plan=AccountPlan.Plan.FREE)
    email = Account.objects.get(user=user).email_normalized
    for index in range(3):
        Inbox.objects.create(user=user, routing_key=f"onboard_scale_{index}")

    output = StringIO()
    call_command("set_account_plan", "--email", email, "--plan", "scale", stdout=output)

    plan = AccountPlan.objects.get(user=user)
    assert plan.plan == AccountPlan.Plan.SCALE
    assert "free -> scale" in output.getvalue()
    with transaction.atomic():
        assert can_create_inbox(user) is True  # 3 < 50, was False under Free (3 inbox cap)
