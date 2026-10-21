from django import forms

from apps.inboxes.services import RoutingKeyInvalid, normalize_routing_key


class InboxCreateForm(forms.Form):
    label = forms.CharField(max_length=120, required=False, strip=True)
    # Optional custom slug. Empty -> caller keeps the existing random-key
    # behavior. When present, normalization/validation is delegated to
    # apps.inboxes.services.normalize_routing_key — the SAME function
    # apps.api.serializers.InboxWriteSerializer calls — so the web form and
    # the API enforce identical pattern + reserved-blocklist rules (single
    # source of truth, no duplicated regex/blocklist here).
    routing_key = forms.CharField(max_length=64, required=False, strip=True)

    def clean_routing_key(self):
        value = self.cleaned_data.get("routing_key", "")
        if not value:
            return ""
        # : the dashboard inbox-create form never binds a
        # domain either -> is_shared=True always (see serializers.py comment).
        try:
            return normalize_routing_key(value, is_shared=True)
        except RoutingKeyInvalid as exc:
            raise forms.ValidationError(exc.message, code=exc.code) from exc


class InboxRenameForm(forms.Form):
    label = forms.CharField(max_length=120, required=False, strip=True)


class ApiKeyCreateForm(forms.Form):
    name = forms.CharField(max_length=80, strip=True)
    expires_in_days = forms.IntegerField(required=False, min_value=1, max_value=3650)


class WebhookForm(forms.Form):
    url = forms.CharField(max_length=2048, strip=True)
    is_enabled = forms.BooleanField(required=False)


class TeamInviteForm(forms.Form):
    email = forms.EmailField(max_length=254)


class AccountCreateForm(forms.Form):
    name = forms.CharField(max_length=80, strip=True, min_length=1)
