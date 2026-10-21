from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import SetPasswordForm

from .models import Account
from .services import normalize_email


class EmailForm(forms.Form):
    email = forms.EmailField(
        max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )

    def clean_email(self):
        return normalize_email(self.cleaned_data["email"])


class RegisterForm(EmailForm):
    """Email only. The credential is chosen exclusively on the token-bound
    verification form (anti pre-account-takeover: only whoever controls the
    mailbox may set it), so register never asks for a password."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].help_text = "We'll email you a link to set your password."

    def clean_email(self):
        email = super().clean_email()
        # Reserved for internal system accounts (demo, agents, workspaces).
        if email.rsplit("@", 1)[-1].endswith(".internal"):
            raise forms.ValidationError("Enter a valid email address.")
        return email


class PlainLabelsSetPasswordForm(SetPasswordForm):
    "Public API."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["new_password1"].label = "Password"
        self.fields["new_password2"].label = "Confirm password"
        self.fields["new_password2"].help_text = "Type the same password again."


class VerifySetPasswordForm(PlainLabelsSetPasswordForm):
    """Set-password form on the verification link AND the password-reset link
    (ux3 N5: one voice for the same field): one plain help line instead
    of Django's validator bullet list. AUTH_PASSWORD_VALIDATORS still run
    (SetPasswordForm.clean_new_password2) and their errors still render."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["new_password1"].help_text = "At least 8 characters. Avoid common passwords."


class LoginForm(EmailForm):
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )

    def resolve_user(self):
        try:
            account = Account.objects.select_related("user").get(
                email_normalized=self.cleaned_data["email"]
            )
        except Account.DoesNotExist:
            get_user_model()().set_password(self.cleaned_data["password"])
            return None
        user = account.user
        if (
            not user.check_password(self.cleaned_data["password"])
            or not user.is_active
            or account.email_verified_at is None
            or account.is_suspended
        ):
            return None
        return user
