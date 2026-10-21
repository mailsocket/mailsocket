import logging
import unicodedata
from urllib.parse import unquote, urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.core.mail import send_mail
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import (
    url_has_allowed_host_and_scheme,
    urlencode,
    urlsafe_base64_decode,
    urlsafe_base64_encode,
)
from django.views.decorators.http import require_http_methods

from .attribution import (
    attribution_from_request,
    form_referrer_host,
    record_signup_attribution,
    referrer_host,
)
from .client_ip import client_ip as _client_ip  
from .forms import EmailForm, LoginForm, RegisterForm, VerifySetPasswordForm
from .models import Account
from .services import (
    RateLimitExceeded,
    clear_login_email_ceiling,
    enforce_auth_rate_limits,
    increment_rate_limit,
    refresh_pending_verification,
    register_pending_signup,
)
from .tokens import activate_verification_token, make_verification_token, resolve_verification_token

GENERIC_AUTH_ERROR = "Unable to sign in with those credentials."

# so instead of a bare "invalid" (the resend form stays on the page).
INVALID_VERIFICATION_LINK = (
    "This link has expired or was replaced by a newer one. Use the most recent "
    "email from us, or request a new link below."
)
RESEND_TITLE = "Resend verification email"

logger = logging.getLogger(__name__)


def _check_email_page(request, email: str, next_url: str, status: int = 200):
    """The "Check your email" page shown after register AND resend.

    Enumeration-safe: rendered from the address the user typed (normalized
    by the form) and the validated next only — never from whether an account
    exists or its state — so new / pending / verified / unknown branches get
    the same body and status. The template carries the resend form."""
    return render(
        request,
        "accounts/check_email.html",
        {"email": email, "next_url": next_url},
        status=status,
    )


def _invalid_verification_link(request):
    # Recovery path for an old/expired link: the page carries a resend form
    # with a visible email input (the token's identity is never echoed).
    return render(
        request,
        "accounts/confirmation.html",
        {
            "message": INVALID_VERIFICATION_LINK,
            "show_resend_form": True,
            "next_url": _safe_next(request),
        },
        status=400,
    )


def _limited(request, action: str, email: str) -> bool:
    try:
        enforce_auth_rate_limits(action, email, _client_ip(request))
    except RateLimitExceeded:
        return True
    return False


def _absolute_url(path: str) -> str:
    return settings.PUBLIC_BASE_URL.rstrip("/") + path


def _send_auth_mail(subject: str, body: str, recipient: str | None) -> None:
    # Enumeration-safe: the no-account branch sends to a sink on a domain we
    # control, so the SMTP submission path (resolution, connection, transfer) is
    # the SAME shape as the real-account branch — no timing oracle from a bogus
    # domain. Response body/status are already identical across branches, and
    # send_mail here uses fail_silently plus a broad guard so SMTP health never
    # changes the HTTP response (B4).
    discard: str = getattr(settings, "AUTH_DISCARD_EMAIL", "auth-discard@mailsocket.app")
    target = recipient or discard
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [target], fail_silently=True)
    except Exception:
        pass



AUTH_MAIL_ABOUT = "mailsocket is an inbox API that turns incoming email into JSON (OTP codes and magic links)."
AUTH_MAIL_IGNORE = "If you didn't request this, ignore this email."


def _human_duration(seconds: int) -> str:
    """\"24 hours\" / \"1 hour\" / \"2 days\" / \"30 minutes\" from a settings value
    (EMAIL_VERIFICATION_TIMEOUT / PASSWORD_RESET_TIMEOUT) — never hard-coded."""
    seconds = int(seconds)
    for unit_seconds, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= unit_seconds and seconds % unit_seconds == 0:
            
            if unit == "day" and seconds == 86400:
                return "24 hours"
            count = seconds // unit_seconds
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return f"{seconds} seconds"


def _auth_mail_body(*lines: str) -> str:
    """Plain-text auth mail: short lines, the link (when any) LAST so it stays
    the final line of the body."""
    return "\n\n".join(lines) + "\n"


def _send_verification(user, next_url: str = "") -> None:
    if user is None:
        _send_auth_mail("Verify your mailsocket email", "Verification request discarded.", None)
        return
    token = make_verification_token(user)
    path = reverse("accounts:verify-email", kwargs={"token": token})
    if next_url:
        # Gate R2: carry the (already _safe_next-validated) same-site return
        # target through the emailed link. verify_email re-validates it with
        # _safe_next before rendering, and login re-validates again before
        # redirecting, so a tampered link can never become an open redirect.
        path += "?" + urlencode({"next": next_url})
    url = _absolute_url(path)
    expiry = _human_duration(settings.EMAIL_VERIFICATION_TIMEOUT)
    _send_auth_mail(
        "Verify your mailsocket email",
        _auth_mail_body(
            AUTH_MAIL_ABOUT,
            f"Someone asked to create a mailsocket account with this address. {AUTH_MAIL_IGNORE}",
            f"This link expires in {expiry}. Open it to verify your email and set your password:",
            url,
        ),
        user.account.email_normalized,
    )


def _send_password_reset(user) -> None:
    if user is None:
        _send_auth_mail("Reset your mailsocket password", "Password reset request discarded.", None)
        return
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    url = _absolute_url(
        reverse("accounts:password-reset-confirm", kwargs={"uidb64": uid, "token": token})
    )
    expiry = _human_duration(settings.PASSWORD_RESET_TIMEOUT)
    _send_auth_mail(
        "Reset your mailsocket password",
        _auth_mail_body(
            AUTH_MAIL_ABOUT,
            f"Someone asked to reset the password of the mailsocket account for this address. {AUTH_MAIL_IGNORE}",
            f"This link expires in {expiry}. Open it to choose a new password:",
            url,
        ),
        user.account.email_normalized,
    )


# : per-normalized-email ceiling for the "You already have an
# account" mail, across all IPs (the register limits are per email+IP and per
# IP only, so many IPs could otherwise flood a verified user). Over the cap the
# request goes to the discard sink instead: same response, same single SMTP
# submission, so the cap is no oracle.
ALREADY_REGISTERED_MAIL_RATE_NAME = "auth:already-registered-mail:email:1d"
ALREADY_REGISTERED_MAIL_LIMIT = 3
ALREADY_REGISTERED_MAIL_WINDOW = 86400


def _already_registered_capped(email_normalized: str) -> bool:
    try:
        increment_rate_limit(
            ALREADY_REGISTERED_MAIL_RATE_NAME,
            email_normalized,
            limit=ALREADY_REGISTERED_MAIL_LIMIT,
            window_seconds=ALREADY_REGISTERED_MAIL_WINDOW,
        )
    except RateLimitExceeded:
        return True
    return False


def _send_already_registered(email: str) -> None:
    "Public API."
    account = Account.objects.select_related("user").filter(email_normalized=email).first()
    # Account.user is the non-null OneToOne primary key (PROTECT): once
    # account is not None its user always exists, so read it via account.
    if (
        account is None
        or account.email_verified_at is None
        or not account.user.is_active
        or account.is_suspended
        or _reset_forbidden_user(account.user)
        or _already_registered_capped(account.email_normalized)
    ):
        _send_verification(None)
        return
    _send_auth_mail(
        "Your mailsocket account",
        _auth_mail_body(
            AUTH_MAIL_ABOUT,
            "You already have a mailsocket account for this address, so no new account was created.",
            "Sign in: " + _absolute_url(reverse("accounts:login")),
            "Forgot password: " + _absolute_url(reverse("accounts:password-reset")),
            AUTH_MAIL_IGNORE,
        ),
        account.email_normalized,
    )


# Slash / backslash lookalikes. NFKC folds some of them (／ ＼ ﹨) to "/" or
# "\\" (also caught below); the rest survive NFKC and are listed explicitly.
_SLASH_LOOKALIKES = frozenset(
    "\u2044\u2215\u29f8\u2571\u27cb\u2f03\u3033\u1735"  # ⁄ ∕ ⧸ ╱ ⟋ ⼃ 〳 ᜵
    "\uff0f"  # ／
    "\u29f5\u29f9\u2216\u2572\u27cd\ufe68\uff3c"  # ⧵ ⧹ ∖ ╲ ⟍ ﹨ ＼
)
_NEXT_DECODE_ROUNDS = 3


def _has_space_or_control(value: str) -> bool:
    return any(ch.isspace() or ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in value)


def _is_relative_path(candidate: str) -> bool:
    "Public API."
    if not candidate.startswith("/") or candidate[1:2] in ("/", "\\"):
        return False
    if _has_space_or_control(candidate):
        return False
    parts = urlsplit(candidate)
    if parts.scheme or parts.netloc:
        return False

    whole, path = candidate, parts.path
    for _ in range(_NEXT_DECODE_ROUNDS):
        whole, path = unquote(whole), unquote(path)
    if unquote(whole) != whole:
        return False  # more encoding layers than we are willing to peel
    for form in (whole, unicodedata.normalize("NFKC", whole)):
        if "\\" in form or any(ch in _SLASH_LOOKALIKES for ch in form):
            return False
    for form in (path, unicodedata.normalize("NFKC", path)):
        if not form.startswith("/") or form.startswith("//") or "//" in form:
            return False
        if _has_space_or_control(form):
            return False
        if any(seg in (".", "..") for seg in form.split("/")):
            return False
    return True


def _safe_next(request) -> str:
    "Public API."
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if (
        candidate
        and _is_relative_path(candidate)
        and url_has_allowed_host_and_scheme(
            candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        )
    ):
        return candidate
    return ""


@require_http_methods(["GET", "POST"])
def register_view(request):
    if request.user.is_authenticated:
        
        # (/register/?next=/app/billing/) lands on that page, not "/".
        return redirect(_safe_next(request) or settings.LOGIN_REDIRECT_URL)
    next_url = _safe_next(request)
    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        limited = _limited(request, "register", email)
        if not limited:
            user = register_pending_signup(email)
            # Source attribution from THIS request only (?ref= on the register
            # URL, the external Referer host carried by the form, an
            # allowlisted next/from). No cookie; nothing -> nothing recorded.
            record_signup_attribution(user, attribution_from_request(request))
            if user is None:
                # Verified account already exists (register_pending_signup
                
                _send_already_registered(email)
            else:
                _send_verification(user, next_url)
        return _check_email_page(request, email, next_url, status=429 if limited else 200)
    return render(
        request,
        "accounts/form.html",
        {
            "form": form,
            "title": "Create your account",
            
            # email field says a link is emailed.
            "submit_label": "Start free",
            "next_url": next_url,
            # External Referer host of the register GET, carried to the POST
            # (whose own Referer is this page). Re-validated on the POST.
            "src_host": referrer_host(request) or form_referrer_host(request),
        },
    )


@require_http_methods(["GET", "POST"])
def verify_email(request, token):
    try:
        user = resolve_verification_token(token)
    except (signing.BadSignature, signing.SignatureExpired, KeyError, ValueError, get_user_model().DoesNotExist):
        return _invalid_verification_link(request)
    next_url = _safe_next(request)
    form = VerifySetPasswordForm(user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            user = activate_verification_token(token, form.cleaned_data["new_password1"])
        except (signing.BadSignature, signing.SignatureExpired):
            return _invalid_verification_link(request)
        # Session rotation: drop whatever (anonymous) session arrived with the
        # link, then login() mints a fresh key — no session fixation.
        request.session.flush()
        account = user.account
        if user.is_active and not account.is_suspended:
            login(request, user)
            # next_url already passed _safe_next (same-site relative only).
            return redirect(next_url or settings.LOGIN_REDIRECT_URL)
        return render(
            request,
            "accounts/confirmation.html",
            {"message": "Email verified. You can now sign in.", "next_url": next_url},
        )
    return render(
        request,
        "accounts/form.html",
        {
            "form": form,
            "title": "Set your password",
            "submit_label": "Set password and continue",
            # The token already binds this address, so showing it is not an
            # oracle; it tells someone holding another person's link whose
            # account they are about to sign into.
            "for_email": user.account.email_normalized,
            "next_url": next_url,
        },
    )


@require_http_methods(["GET", "POST"])
def resend_verification(request):
    
    # with the resend form is re-checked by _safe_next and carried through
    # the new emailed link; anything non-relative is dropped.
    next_url = _safe_next(request)
    form = EmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        limited = _limited(request, "resend", email)
        if not limited:
            account = Account.objects.select_related("user").filter(
                email_normalized=email, email_verified_at__isnull=True
            ).first()
            user = refresh_pending_verification(account.user) if account else None
            _send_verification(user, next_url)
        return _check_email_page(request, email, next_url, status=429 if limited else 200)
    # GET (linked from sign in), or a POST whose address is not even a
    # syntactically valid email: the plain resend form, errors under the
    # field. Nothing account-dependent happens on this branch.
    return render(
        request,
        "accounts/form.html",
        {
            "form": form,
            "title": RESEND_TITLE,
            "submit_label": "Resend email",
            "next_url": next_url,
        },
    )


@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        
        return redirect(_safe_next(request) or settings.LOGIN_REDIRECT_URL)
    next_url = _safe_next(request)
    form = LoginForm(request.POST or None)
    status = 200
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        if _limited(request, "login", email):
            form.add_error(None, GENERIC_AUTH_ERROR)
            status = 429
        else:
            user = form.resolve_user()
            if user is not None:
                login(request, user)
                return redirect(next_url or settings.LOGIN_REDIRECT_URL)
            form.add_error(None, GENERIC_AUTH_ERROR)
    return render(
        request,
        "accounts/form.html",
        {"form": form, "title": "Sign in", "submit_label": "Sign in", "next_url": next_url},
        status=status,
    )


@require_http_methods(["GET", "HEAD", "POST"])
def logout_view(request):
    if not request.user.is_authenticated:
        return redirect(settings.LOGIN_URL)
    if request.method != "POST":
        # QA-DASH #2: a bookmarked / typed /logout/ used to be a bare 405.
        # GET only renders a "Sign out?" confirm page; the actual logout stays
        # POST + CSRF, so a cross-site <img src=/logout/> cannot sign anyone out.
        return render(request, "accounts/logout_confirm.html")
    logout(request)
    return redirect(settings.LOGIN_URL)


def _reset_forbidden_user(user) -> bool:
    "Public API."
    from .models import AccountOwnership
    from .multi_account_services import is_workspace_user
    from .team_services import is_demo_or_agent_user

    if is_workspace_user(user) or is_demo_or_agent_user(user):
        return True
    return AccountOwnership.objects.filter(account_user_id=user.pk).exists()


@require_http_methods(["GET", "POST"])
def password_reset_request(request):
    form = EmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        limited = _limited(request, "password-reset", email)
        if not limited:
            account = Account.objects.select_related("user").filter(email_normalized=email).first()
            user = account.user if account and account.user.is_active and not account.is_suspended else None
            
            # users never get a reset link — they have no human mailbox and
            # must never acquire a usable password. Routed to the SAME
            # discard branch as an unknown address, so the response (and the
            # SMTP-submission shape) is identical: no oracle.
            if user is not None and _reset_forbidden_user(user):
                user = None
            _send_password_reset(user)
        return render(
            request,
            "accounts/confirmation.html",
            
            # (as normalized by the form), identical for every account
            # state (unknown / pending / verified / suspended / agent /
            # workspace / rate-limited): enumeration-safe, like the
            # register "Check your email" page.
            {"reset_requested_for": email},
            status=429 if limited else 200,
        )
    return render(
        request,
        "accounts/form.html",
        {"form": form, "title": "Reset password", "submit_label": "Send reset link"},
    )


def _reset_user(uidb64: str):
    try:
        user_id = urlsafe_base64_decode(uidb64).decode()
        return get_user_model().objects.get(pk=user_id)
    except (ValueError, TypeError, OverflowError, UnicodeDecodeError, get_user_model().DoesNotExist):
        return None


@require_http_methods(["GET", "POST"])
def password_reset_confirm(request, uidb64, token):
    user = _reset_user(uidb64)
    # sec-gap r2: also refuse workspace/agent/demo users here (same guard as
    # password_reset_request), so a reset link issued before that guard
    # shipped can never set a usable password. Same generic invalid-link
    # response as a bad token: no oracle.
    if (
        user is None
        or not default_token_generator.check_token(user, token)
        or _reset_forbidden_user(user)
        or not _reset_allowed_state(user)
    ):
        return render(request, "accounts/confirmation.html", {"message": "This reset link is invalid or expired."}, status=400)
    # ux3 N5: same labels and one-line help as the verify set-password form.
    form = VerifySetPasswordForm(user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        result = _complete_password_reset(form, user, token)
        if result is None:
            return render(request, "accounts/confirmation.html", {"message": "This reset link is invalid or expired."}, status=400)
        try:
            clear_login_email_ceiling(user.account.email_normalized)
        except Account.DoesNotExist:
            pass
        request.session.flush()
        return render(request, "accounts/confirmation.html", {"message": "Password reset complete."})
    # ux4 R3-9: say what the button does (no generic "Continue").
    return render(
        request,
        "accounts/form.html",
        {"form": form, "title": "Choose a new password", "submit_label": "Set new password"},
    )


def _reset_allowed_state(user, account=None) -> bool:
    """: the confirm path refuses inactive / suspended users exactly
    like password_reset_request (which never issues them a link), so a link
    issued before a suspension can no longer verify, onboard or set a
    password. Same generic 400 invalid-link response: no oracle."""
    if not user.is_active:
        return False
    if account is None:
        account = Account.objects.filter(user_id=user.pk).first()
    return account is None or not account.is_suspended


def _complete_password_reset(form, user, token):
    "Public API."
    with transaction.atomic():
        account = (
            Account.objects.select_for_update().select_related("user").filter(user_id=user.pk).first()
        )
        locked_user = account.user if account is not None else get_user_model().objects.get(pk=user.pk)
        if not default_token_generator.check_token(locked_user, token):
            return None
        # Re-checked under the lock: a suspension landing after the GET
        # validation still refuses the reset (no verify, no onboarding).
        if not _reset_allowed_state(locked_user, account):
            return None
        form.user = locked_user
        form.save()
        was_unverified = account is not None and account.email_verified_at is None
        if was_unverified:
            account.email_verified_at = timezone.now()
            account.verification_nonce = ""
            account.save(update_fields=["email_verified_at", "verification_nonce", "updated_at"])

    if was_unverified:
        from apps.inboxes.services import provision_onboarding

        try:
            provision_onboarding(locked_user)
        except Exception:
            # Best-effort: onboarding must never surface as a reset failure.
            logger.exception("onboarding_failed_after_reset user_id=%s", locked_user.pk)
    return locked_user
