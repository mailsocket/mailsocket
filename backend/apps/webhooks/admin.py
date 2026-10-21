from django import forms
from django.contrib import admin

from .models import Webhook, WebhookDelivery
from .security import SSRFValidationError, validate_webhook_target


class WebhookAdminForm(forms.ModelForm):
    "Public API."

    class Meta:
        model = Webhook
        exclude = ("secret_salt",)

    def clean(self):
        # Validate whenever Webhook.save() would: a new/changed URL, or
        
        # have moved to a private address since; save() would raise -> 500).
        cleaned = super().clean()
        url = cleaned.get("url")
        if not url:
            return cleaned
        existing = self.instance.pk is not None
        changed = not existing or url != self.instance.url
        reenabled = existing and cleaned.get("is_enabled") and not self.instance.is_enabled
        if changed or reenabled:
            try:
                cleaned["url"] = validate_webhook_target(url).url
            except SSRFValidationError as exc:
                self.add_error(
                    "url", forms.ValidationError(f"Refused webhook URL ({exc.code}).", code="invalid")
                )
        return cleaned


@admin.register(Webhook)
class WebhookAdmin(admin.ModelAdmin):
    form = WebhookAdminForm
    list_display = ("public_id", "inbox", "is_enabled", "created_at", "last_success_at", "last_failure_at")
    exclude = ("secret_salt",)
    readonly_fields = ("public_id", "created_at", "updated_at", "last_success_at", "last_failure_at")
    search_fields = ("public_id", "inbox__public_id")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(admin.ModelAdmin):
    list_display = ("public_id", "event_id", "webhook", "status", "attempt_count", "last_attempt_at")
    search_fields = ("public_id", "event_id", "webhook__public_id", "message__public_id")
    exclude = ("payload",)

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields if field.name != "payload")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
