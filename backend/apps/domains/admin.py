from django.contrib import admin

from .models import Domain


@admin.register(Domain)
class DomainAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "status", "verified_at", "last_checked_at")
    list_filter = ("status",)
    search_fields = ("name", "user__username", "user__email")
    readonly_fields = (
        "public_id",
        "name",
        "verification_token",
        "verification_txt_host",
        "created_at",
        "updated_at",
    )
