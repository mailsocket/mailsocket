from django.contrib import admin

from .models import FeedbackAlertBreaker, FeedbackReport


@admin.register(FeedbackReport)
class FeedbackReportAdmin(admin.ModelAdmin):
    """Staff answer by email (support@) and set the status here. The message
    is shown as plain, escaped text (read-only field; admin autoescapes)."""

    list_display = ("id", "created_at", "status", "plan", "page_path", "email", "short_message")
    list_filter = ("status", "plan")
    search_fields = ("message", "email", "request_id", "page_path")
    date_hierarchy = "created_at"
    readonly_fields = (
        "message",
        "email",
        "page_path",
        "user",
        "plan",
        "request_id",
        "user_agent",
        "ip_hash",
        "created_at",
    )
    fields = ("status",) + readonly_fields

    def has_add_permission(self, request):
        return False

    @admin.display(description="Message")
    def short_message(self, obj):
        text = " ".join((obj.message or "").split())
        return text[:80] + ("\u2026" if len(text) > 80 else "")


@admin.register(FeedbackAlertBreaker)
class FeedbackAlertBreakerAdmin(admin.ModelAdmin):
    list_display = ("key", "window_started_at", "sent", "suppressed", "last_digest_at")
    readonly_fields = ("key", "window_started_at", "sent", "suppressed", "last_digest_at")

    def has_add_permission(self, request):
        return False
