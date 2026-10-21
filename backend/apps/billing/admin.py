from django.contrib import admin

from .models import BillingEvent, BillingPayment, PlanChangeRequest


@admin.register(BillingEvent)
class BillingEventAdmin(admin.ModelAdmin):
    list_display = ("event_key", "event_name", "user", "created_at")
    list_filter = ("event_name",)
    search_fields = ("event_key", "user__username", "user__email")
    readonly_fields = ("event_key", "event_name", "user", "created_at")


@admin.register(BillingPayment)
class BillingPaymentAdmin(admin.ModelAdmin):
    list_display = (
        "ls_invoice_id", "user", "amount_cents", "currency", "reason", "paid_at", "receipt_sent_at",
        "refunded_amount_cents", "refunded",
    )
    list_filter = ("reason", "refunded")
    search_fields = ("ls_invoice_id", "ls_subscription_id", "user__email")
    readonly_fields = [f.name for f in BillingPayment._meta.fields]


@admin.register(PlanChangeRequest)
class PlanChangeRequestAdmin(admin.ModelAdmin):
    list_display = ("user", "from_plan", "to_plan", "estimate_cents", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("user__email", "ls_subscription_id")
    readonly_fields = [f.name for f in PlanChangeRequest._meta.fields]
