from django.contrib import admin, messages
from django.db import transaction
from django.utils import timezone

from apps.inboxes.models import Inbox

from .models import (
    Account,
    AccountOwnership,
    AccountPlan,
    ApiKey,
    MonthlyUsage,
    RateLimitBucket,
    WaitCapacity,
    WaitLease,
)


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("email_normalized", "user", "email_verified_at", "is_suspended")
    search_fields = ("email_normalized", "user__username")
    list_filter = ("is_suspended",)
    readonly_fields = ("created_at", "updated_at")
    actions = ("suspend_and_disable_inboxes",)

    @admin.action(description="Suspend account + disable all inboxes")
    def suspend_and_disable_inboxes(self, request, queryset):
        from apps.domains.services import CatchAllToggleRefused, assert_not_active_catch_all_target

        user_ids = list(queryset.values_list("user_id", flat=True))
        with transaction.atomic():
            
            # suspended human owns are suspended + inbox-disabled with them
            # (same path as the human's own account), and every live key the
            
            # created_by=human) is revoked — the same plain revoked_at write
            # the team-membership cascade uses.
            owned_workspace_ids = list(
                AccountOwnership.objects.filter(owner_user_id__in=user_ids).values_list(
                    "account_user_id", flat=True
                )
            )
            target_user_ids = list(dict.fromkeys(user_ids + owned_workspace_ids))
            Account.objects.filter(user_id__in=target_user_ids).update(is_suspended=True)
            ApiKey.objects.filter(
                created_by_id__in=user_ids, revoked_at__isnull=True
            ).update(revoked_at=timezone.now())
            
            # gate catch-all routing on inbox/domain liveness only, never on
            # Account.is_suspended — so a catch-all sentinel skipped below
            # would keep receiving mail for a suspended tenant. Turn catch-all
            # OFF on every domain the suspended tenants own FIRST (same
            # direct write as settings_delete step 0: catch_all=False +
            # catch_all_inbox=None, satisfying domain_catch_all_requires_inbox)
            # so no sentinel is an active target any more and none is skipped.
            from apps.domains.models import Domain

            Domain.objects.filter(user_id__in=target_user_ids, catch_all=True).update(
                catch_all=False, catch_all_inbox=None, updated_at=timezone.now()
            )
            inboxes = Inbox.objects.filter(user_id__in=target_user_ids)
            live = inboxes.filter(deleted_at__isnull=True)
            allowed_pks = []
            blocked_keys = []
            for inbox in live:
                try:
                    assert_not_active_catch_all_target(inbox)
                except CatchAllToggleRefused:
                    blocked_keys.append(inbox.routing_key)
                else:
                    allowed_pks.append(inbox.pk)
            if allowed_pks:
                live.filter(pk__in=allowed_pks).update(
                    is_enabled=False,
                    deleted_at=timezone.now(),
                )
        # Only reachable if some inbox is still the active catch-all target of
        # a domain NOT owned by a suspended tenant (should not happen: an
        # inbox's domain is owned by the inbox's user).
        if blocked_keys:
            self.message_user(
                request,
                "Account suspended, but the following inboxes are the "
                "active catch-all target for their domain and were NOT "
                "disabled/deleted (disable catch-all on the domain "
                "first): " + ", ".join(blocked_keys),
                level=messages.WARNING,
            )


@admin.register(AccountPlan)
class AccountPlanAdmin(admin.ModelAdmin):
    list_display = ("user", "plan", "status", "updated_at")
    list_filter = ("plan", "status")
    search_fields = (
        "user__username",
        "user__email",
        "stripe_customer_id",
        "stripe_subscription_id",
    )
    readonly_fields = ("created_at", "updated_at")


@admin.register(MonthlyUsage)
class MonthlyUsageAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "period_start",
        "messages_received",
        "api_requests",
        "webhook_attempts",
    )
    list_filter = ("period_start",)
    search_fields = ("user__username", "user__email")
    readonly_fields = (
        "user",
        "period_start",
        "messages_received",
        "api_requests",
        "webhook_attempts",
    )

admin.site.register(RateLimitBucket)


@admin.register(WaitLease)
class WaitLeaseAdmin(admin.ModelAdmin):
    list_display = ("user", "count", "expires_at", "updated_at")
    readonly_fields = ("user", "count", "expires_at", "updated_at")


@admin.register(WaitCapacity)
class WaitCapacityAdmin(admin.ModelAdmin):
    list_display = ("key", "count", "expires_at", "updated_at")
    readonly_fields = ("key", "count", "expires_at", "updated_at")


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("public_id", "user", "name", "prefix", "created_at", "last_used_at", "revoked_at", "expires_at")
    search_fields = ("public_id", "user__username", "name", "prefix")
    readonly_fields = ("public_id", "prefix", "key_hash", "created_at", "last_used_at", "revoked_at")
