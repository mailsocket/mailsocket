from django.contrib import admin, messages
from django.utils import timezone

from .models import Inbox, Message


@admin.register(Inbox)
class InboxAdmin(admin.ModelAdmin):
    list_display = ("routing_key", "label", "is_enabled", "deleted_at", "created_at")
    list_filter = ("is_enabled", "deleted_at")
    search_fields = ("routing_key", "label", "public_id")
    readonly_fields = ("public_id", "created_at", "updated_at", "deleted_at")
    actions = ("enable", "disable", "soft_delete")

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            fields.append("routing_key")
        return fields

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def _split_catch_all_blocked(self, queryset):
        "Public API."
        from apps.domains.services import CatchAllToggleRefused, assert_not_active_catch_all_target

        allowed_pks = []
        blocked_keys = []
        for inbox in queryset:
            try:
                assert_not_active_catch_all_target(inbox)
            except CatchAllToggleRefused:
                blocked_keys.append(inbox.routing_key)
            else:
                allowed_pks.append(inbox.pk)
        return allowed_pks, blocked_keys

    def _warn_blocked(self, request, blocked_keys):
        if blocked_keys:
            self.message_user(
                request,
                "Refused: the following inboxes are the active catch-all "
                "target for their domain and cannot be disabled/deleted "
                "until catch-all is turned off on the domain first: "
                + ", ".join(blocked_keys),
                level=messages.ERROR,
            )

    @admin.action(description="Enable selected inboxes")
    def enable(self, request, queryset):
        queryset.filter(deleted_at__isnull=True).update(is_enabled=True)

    @admin.action(description="Disable selected inboxes")
    def disable(self, request, queryset):
        allowed_pks, blocked_keys = self._split_catch_all_blocked(queryset)
        if allowed_pks:
            queryset.filter(pk__in=allowed_pks).update(is_enabled=False)
        self._warn_blocked(request, blocked_keys)

    @admin.action(description="Soft-delete selected inboxes")
    def soft_delete(self, request, queryset):
        allowed_pks, blocked_keys = self._split_catch_all_blocked(queryset)
        if allowed_pks:
            queryset.filter(pk__in=allowed_pks).update(
                is_enabled=False, deleted_at=timezone.now()
            )
        self._warn_blocked(request, blocked_keys)

    def delete_model(self, request, obj):
        from apps.domains.services import CatchAllToggleRefused, assert_not_active_catch_all_target

        try:
            assert_not_active_catch_all_target(obj)
        except CatchAllToggleRefused:
            self._warn_blocked(request, [obj.routing_key])
            return
        obj.is_enabled = False
        obj.deleted_at = timezone.now()
        obj.save(update_fields=["is_enabled", "deleted_at", "updated_at"])

    def delete_queryset(self, request, queryset):
        allowed_pks, blocked_keys = self._split_catch_all_blocked(queryset)
        if allowed_pks:
            queryset.filter(pk__in=allowed_pks).update(
                is_enabled=False, deleted_at=timezone.now()
            )
        self._warn_blocked(request, blocked_keys)


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = (
        "public_id",
        "inbox",
        "ingest_status",
        "size_bytes",
        "received_at",
    )
    list_filter = ("ingest_status", "received_at")
    search_fields = (
        "public_id",
        "provider_message_id",
        "postfix_queue_id",
        "envelope_recipient",
    )
    # Live credentials (OTP, magic link, links, raw bytes) must never surface in
    # the admin change form. `exclude` alone is NOT enough: get_readonly_fields
    # feeds every model field back through the readonly list, and Django renders
    # readonly fields regardless of `exclude`. Keep the secrets out of BOTH.
    SECRET_FIELDS = ("raw_bytes", "otp", "detected_otp", "magic_link", "links")
    exclude = SECRET_FIELDS
    actions = ("retry_parsing",)

    @admin.action(
        description="Retry parsing for selected failed messages",
        permissions=["change"],
    )
    def retry_parsing(self, request, queryset):
        # Explicit admin replay is the only failed -> received transition.
        # Reset the exhausted attempt budget before the reconciler claims it.
        queryset.filter(ingest_status=Message.IngestStatus.FAILED).update(
            ingest_status=Message.IngestStatus.RECEIVED,
            parse_attempts=0,
            parse_error="",
            parse_started_at=None,
            parse_lease_token=None,
            parse_lease_expires_at=None,
            next_parse_at=None,
        )

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def get_readonly_fields(self, request, obj=None):
        return tuple(
            field.name
            for field in self.model._meta.fields
            if field.name not in self.exclude
        )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
