from django.contrib import admin

from .models import ComponentAlertState, ComponentCheck, Incident


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = ("title", "status", "started_at", "resolved_at")
    list_filter = ("status",)
    search_fields = ("title", "body")


@admin.register(ComponentCheck)
class ComponentCheckAdmin(admin.ModelAdmin):
    list_display = ("component", "ok", "latency_ms", "checked_at")
    list_filter = ("component", "ok")
    date_hierarchy = "checked_at"

    # Probe history is machine-written evidence; admin can read it only.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ComponentAlertState)
class ComponentAlertStateAdmin(admin.ModelAdmin):
    list_display = ("component", "state", "down_since", "changed_at")
    readonly_fields = ("component", "state", "down_since", "changed_at")

    # Written only by status_probe's transition claim.
    def has_add_permission(self, request):
        return False
