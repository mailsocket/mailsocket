from urllib.parse import urlsplit

from django import forms

from apps.accounts.views import _is_relative_path
from apps.api.middleware import is_valid_request_id

from .models import MESSAGE_MAX_CHARS, PAGE_PATH_MAX_CHARS


def clean_page_path(value):
    """Path only: a same-site relative path (same rules as the login
    ``?next=`` guard, so never absolute or protocol-relative), with any query
    string or fragment dropped. Returns "" for anything else."""
    value = (value or "").strip()
    if not value or len(value) > 2048 or not _is_relative_path(value):
        return ""
    path = urlsplit(value).path
    if not path.startswith("/") or len(path) > PAGE_PATH_MAX_CHARS:
        return ""
    return path


def clean_request_id(value):
    value = (value or "").strip()
    return value if is_valid_request_id(value) else ""


class _PlainTextField(forms.CharField):
    """Browsers count a textarea newline as 1 for maxlength but submit CRLF;
    normalise first so the server limit matches what the UI allowed."""

    def to_python(self, value):
        if isinstance(value, str):
            value = value.replace("\r\n", "\n").replace("\r", "\n")
        return super().to_python(value)


class FeedbackForm(forms.Form):
    message = _PlainTextField(
        max_length=MESSAGE_MAX_CHARS,
        error_messages={
            "required": "Tell us what happened.",
            "max_length": f"Keep it under {MESSAGE_MAX_CHARS:,} characters.",
        },
    )
    email = forms.EmailField(
        required=False,
        max_length=254,
        error_messages={"invalid": "Enter a valid email address so we can reply."},
    )
    page_path = forms.CharField(required=False, max_length=2048)
    request_id = forms.CharField(required=False, max_length=200)
    # Honeypot: hidden from people by CSS; bots fill it.
    website = forms.CharField(required=False, max_length=500)

    def __init__(self, *args, email_required=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.email_required = email_required

    def clean_email(self):
        email = self.cleaned_data.get("email", "")
        if self.email_required and not email:
            raise forms.ValidationError("We need your email to reply.")
        return email

    def clean_page_path(self):
        raw = self.cleaned_data.get("page_path", "")
        if not raw.strip():
            return ""
        path = clean_page_path(raw)
        if not path:
            raise forms.ValidationError("Use a path on this site, like /app/inboxes/.")
        return path

    def clean_request_id(self):
        raw = self.cleaned_data.get("request_id", "")
        if not raw.strip():
            return ""
        request_id = clean_request_id(raw)
        if not request_id:
            raise forms.ValidationError("That doesn't look like a mailsocket request id (req_…).")
        return request_id

    @property
    def is_bot(self):
        return bool((self.data.get("website") or "").strip())
