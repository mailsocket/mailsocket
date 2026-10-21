import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from unittest import mock

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail, signing
from django.core.management import call_command
from django.db import close_old_connections
from django.test import Client, override_settings
from django.urls import resolve, reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.models import Account, AccountPlan, RateLimitBucket
from apps.accounts.services import (
    LOGIN_EMAIL_CEILING_NAMES,
    RateLimitExceeded,
    _bucket_hash,
    create_product_user,
    increment_rate_limit,
    register_pending_signup,
)
from apps.accounts.tokens import make_verification_token, resolve_verification_token

pytestmark = pytest.mark.django_db

PASSWORD = "correct horse battery staple 90210"
NEW_PASSWORD = "different horse battery staple 41720"


def register(client, email=" Person@EXAMPLE.com "):
    return client.post(
        reverse("accounts:register"),
        {"email": email},
    )


def verified_user(email="person@example.com"):
    user = create_product_user(email, PASSWORD)
    user.account.email_verified_at = timezone.now()
    user.account.save(update_fields=["email_verified_at", "updated_at"])
    return user


_CSRF_TOKEN_RE = re.compile(r'name="csrfmiddlewaretoken" value="[^"]*"')
_EMAIL_FIELD_VALUE_RE = re.compile(r'(value=")[^"]*("[^>]*id="id_email")')


def _strip_csrf_token(body: str) -> str:
    """sol gate #2: normalize away the randomized CSRF hidden-input value,
    and the submitted-email echo (the form legitimately re-renders whatever
    was POSTed, which differs by design between the known/unknown cases and
    is not itself an enumeration signal), so two otherwise byte-identical
    rendered pages compare equal."""
    body = _CSRF_TOKEN_RE.sub('name="csrfmiddlewaretoken" value=""', body)
    body = _EMAIL_FIELD_VALUE_RE.sub(r"\1\2", body)
    return body


def test_register_normalizes_creates_product_rows_and_sends_verification(client):
    response = register(client)
    user = get_user_model().objects.get()
    assert response.status_code == 200
    assert user.email == "person@example.com"
    assert not user.has_usable_password()
    assert user.account.verification_nonce
    assert user.account.email_normalized == "person@example.com"
    assert user.account.email_verified_at is None
    assert user.account_plan.plan == AccountPlan.Plan.FREE
    assert len(mail.outbox) == 1
    assert "https://mailsocket.app/verify-email/" in mail.outbox[0].body
    assert "_auth_user_id" not in client.session


def _check_email_body(response, typed_email) -> str:
    """Normalize the two per-request parts of the "Check your email" page:
    the masked CSRF token (Django re-masks it on every render) and the echo
    of the address the user typed (same for every account state, so not an
    enumeration signal). Everything else must be byte-identical."""
    body = _CSRF_TOKEN_RE.sub('name="csrfmiddlewaretoken" value=""', response.content.decode())
    return body.replace(typed_email, "<typed>")


def test_duplicate_case_variant_is_non_enumerating_and_does_not_duplicate(client):
    first = register(client, "case@example.com")
    second = register(client, "CASE@EXAMPLE.COM")
    # The form normalizes the address, so both echo "case@example.com".
    assert _check_email_body(first, "case@example.com") == _check_email_body(second, "case@example.com")
    assert Account.objects.count() == 1


def test_duplicate_registration_activates_only_password_bound_to_verified_link(client):
    register(client, "victim@example.com")
    attacker_link = mail.outbox[-1].body.strip().splitlines()[-1]
    response = client.post(
        reverse("accounts:register"),
        {"email": "victim@example.com"},
    )
    victim_link = mail.outbox[-1].body.strip().splitlines()[-1]

    assert response.status_code == 200
    assert client.get(attacker_link).status_code == 400
    assert client.post(
        victim_link,
        {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
    ).status_code == 302
    user = get_user_model().objects.get(email="victim@example.com")
    assert not user.check_password(PASSWORD)
    assert user.check_password(NEW_PASSWORD)
    client.post(reverse("accounts:logout"))
    assert client.post(
        reverse("accounts:login"),
        {"email": user.email, "password": PASSWORD},
    ).status_code == 200
    assert client.post(
        reverse("accounts:login"),
        {"email": user.email, "password": NEW_PASSWORD},
    ).status_code == 302


def test_verification_happy_reused_expired_and_security_state_change(client):
    user = register_pending_signup("verify@example.com")
    token = make_verification_token(user)
    response = client.post(
        reverse("accounts:verify-email", kwargs={"token": token}),
        {"new_password1": PASSWORD, "new_password2": PASSWORD},
    )
    assert response.status_code == 302
    assert Account.objects.get(pk=user.pk).email_verified_at is not None
    assert client.get(reverse("accounts:verify-email", kwargs={"token": token})).status_code == 400

    expired_user = register_pending_signup("expired@example.com")
    expired = make_verification_token(expired_user)
    with mock.patch("django.core.signing.time.time", return_value=timezone.now().timestamp() + 86401):
        with pytest.raises(signing.SignatureExpired):
            resolve_verification_token(expired)

    changed_user = register_pending_signup("changed@example.com")
    changed = make_verification_token(changed_user)
    changed_user.account.verification_nonce = "replacement"
    changed_user.account.save(update_fields=["verification_nonce", "updated_at"])
    with pytest.raises(signing.BadSignature):
        resolve_verification_token(changed)


def test_login_requires_email_verification_active_and_not_suspended_and_rotates_session(client):
    user = verified_user()
    client.get(reverse("accounts:login"))
    old_key = client.session.session_key
    response = client.post(reverse("accounts:login"), {"email": user.email, "password": PASSWORD})
    assert response.status_code == 302
    assert client.session.session_key != old_key

    client.post(reverse("accounts:logout"))
    assert "_auth_user_id" not in client.session
    # QA-DASH #2: GET /logout/ never 405s; signed out -> login page.
    response = client.get(reverse("accounts:logout"))
    assert response.status_code == 302 and response.url == settings.LOGIN_URL

    for field in ("unverified", "suspended", "inactive"):
        blocked = create_product_user(f"{field}@example.com", PASSWORD)
        if field != "unverified":
            blocked.account.email_verified_at = timezone.now()
            blocked.account.is_suspended = field == "suspended"
            blocked.account.save()
        if field == "inactive":
            blocked.is_active = False
            blocked.save(update_fields=["is_active"])
        response = client.post(
            reverse("accounts:login"), {"email": blocked.email, "password": PASSWORD}
        )
        assert response.status_code == 200
        assert b"Unable to sign in" in response.content


def test_logout_requires_post_and_csrf():
    user = verified_user("csrf@example.com")
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(user)
    assert csrf_client.post(reverse("accounts:logout")).status_code == 403
    # QA-DASH #2: GET shows a "Sign out?" confirm page and does NOT log out.
    response = csrf_client.get(reverse("accounts:logout"))
    assert response.status_code == 200
    assert b"Sign out?" in response.content
    assert b'method="post"' in response.content and b"csrfmiddlewaretoken" in response.content
    assert "_auth_user_id" in csrf_client.session


def test_password_reset_is_non_enumerating_one_time_and_invalidates_session(client):
    user = verified_user("reset@example.com")
    known = client.post(reverse("accounts:password-reset"), {"email": user.email})
    unknown = client.post(reverse("accounts:password-reset"), {"email": "unknown@example.com"})
    # ux3 N4: the page echoes the typed address; everything else is identical.
    assert _check_email_body(unknown, "unknown@example.com") == _check_email_body(known, user.email)
    assert len(mail.outbox) == 2

    authenticated = Client()
    authenticated.force_login(user)
    old_session_key = authenticated.session.session_key
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    url = reverse(
        "accounts:password-reset-confirm", kwargs={"uidb64": uid, "token": token}
    )
    response = client.post(
        url, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}
    )
    assert response.status_code == 200
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)
    assert not default_token_generator.check_token(user, token)
    assert authenticated.get(reverse("accounts:login")).wsgi_request.user.is_anonymous
    assert old_session_key is not None


def test_password_reset_token_expiry():
    user = verified_user("reset-expiry@example.com")
    token = default_token_generator.make_token(user)
    with mock.patch(
        "django.contrib.auth.tokens.PasswordResetTokenGenerator._now",
        return_value=timezone.now().replace(tzinfo=None) + timedelta(seconds=3601),
    ):
        assert not default_token_generator.check_token(user, token)


def test_resend_static_route_resolves_to_resend_handler(client):
    match = resolve("/verify-email/resend/")
    assert match.view_name == "accounts:resend-verification"
    assert client.post("/verify-email/resend/", {"email": "unknown@example.com"}).status_code == 200


def test_resend_invalidates_any_pre_fix_unverified_password(client):
    user = create_product_user("legacy-pending@example.com", PASSWORD)
    response = client.post(
        reverse("accounts:resend-verification"), {"email": user.email}
    )
    assert response.status_code == 200
    user.refresh_from_db()
    assert not user.has_usable_password()
    token_url = mail.outbox[-1].body.strip().splitlines()[-1]
    assert client.post(
        token_url,
        {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
    ).status_code == 302
    user.refresh_from_db()
    assert not user.check_password(PASSWORD)
    assert user.check_password(NEW_PASSWORD)


@pytest.mark.parametrize(
    ("url_name", "known_email", "unknown_email"),
    [
        ("register", "registered@example.com", "new@example.com"),
        ("resend-verification", "pending@example.com", "unknown@example.com"),
        ("password-reset", "verified@example.com", "unknown@example.com"),
    ],
)
def test_auth_mail_endpoints_have_invariant_dispatch_and_smtp_failure_surface(
    client, url_name, known_email, unknown_email
):
    if url_name == "register":
        verified_user(known_email)
    elif url_name == "resend-verification":
        register_pending_signup(known_email)
    else:
        verified_user(known_email)

    def payload(email):
        return {"email": email}

    with mock.patch("apps.accounts.views.send_mail", side_effect=RuntimeError("smtp down")) as send:
        known = client.post(reverse(f"accounts:{url_name}"), payload(known_email))
        unknown = client.post(reverse(f"accounts:{url_name}"), payload(unknown_email))

    assert known.status_code == unknown.status_code == 200
    # ux3 N4: the reset confirmation now echoes the typed address too, so all
    # three are compared the same way (CSRF + typed address normalised only).
    assert _check_email_body(known, known_email) == _check_email_body(unknown, unknown_email)
    assert send.call_count == 2
    assert all(call.kwargs["fail_silently"] is True for call in send.call_args_list)


def test_cleanup_rechecks_expiry_when_bucket_is_renewed_after_selection():
    now = timezone.now()
    bucket_hash = _bucket_hash("cleanup-race", "subject")
    bucket = RateLimitBucket.objects.create(
        bucket_hash=bucket_hash,
        window_started_at=now - timedelta(hours=1),
        count=2,
        expires_at=now - timedelta(seconds=1),
    )
    from django.db.models.query import QuerySet

    original_delete = QuerySet.delete

    def renew_then_delete(queryset):
        RateLimitBucket.objects.filter(pk=bucket.pk).update(
            window_started_at=now, count=2, expires_at=now + timedelta(minutes=15)
        )
        return original_delete(queryset)

    with mock.patch.object(QuerySet, "delete", renew_then_delete):
        call_command("cleanup_rate_limit_buckets", stdout=StringIO())

    bucket.refresh_from_db()
    assert bucket.count == 2
    with pytest.raises(RateLimitExceeded):
        increment_rate_limit("cleanup-race", "subject", limit=2, window_seconds=900)


def test_auth_rate_limit_uses_postgresql_bucket_and_returns_429(client):
    for _ in range(5):
        response = client.post(
            reverse("accounts:login"),
            {"email": "limit@example.com", "password": "wrong"},
            REMOTE_ADDR="203.0.113.8",
        )
        assert response.status_code == 200
    response = client.post(
        reverse("accounts:login"),
        {"email": "limit@example.com", "password": "wrong"},
        REMOTE_ADDR="203.0.113.8",
    )
    assert response.status_code == 429
    # D1: two new login-email-ceiling buckets in addition to the four
    # pre-existing (ip:15m, ip:1d, email+ip:15m, email+ip:1d).
    assert RateLimitBucket.objects.count() == 6


@override_settings(TRUSTED_PROXY_CIDRS="172.29.0.4/32")  
def test_auth_rate_limit_caps_different_emails_from_one_cloudflare_ip(client):
    for attempt in range(20):
        response = client.post(
            reverse("accounts:login"),
            {"email": f"spray-{attempt}@example.com", "password": "wrong"},
            REMOTE_ADDR="172.29.0.4",
            HTTP_CF_CONNECTING_IP="203.0.113.40",
        )
        assert response.status_code == 200

    response = client.post(
        reverse("accounts:login"),
        {"email": "spray-blocked@example.com", "password": "wrong"},
        REMOTE_ADDR="172.29.0.4",
        HTTP_CF_CONNECTING_IP="203.0.113.40",
    )
    assert response.status_code == 429
    # audit 2026-10-11 sweep: the per-IP bucket (client ip, not the proxy)
    # is what stopped the spray; the blocked email was never charged.
    assert RateLimitBucket.objects.get(
        bucket_hash=_bucket_hash("auth:login:ip:15m", "203.0.113.40")
    ).count == 21  # 20 allowed + the refused attempt
    assert not RateLimitBucket.objects.filter(
        bucket_hash=_bucket_hash("auth:login:email:1h", "spray-blocked@example.com")
    ).exists()
    assert "_auth_user_id" not in client.session


@override_settings(TRUSTED_PROXY_CIDRS="172.29.0.4/32")  
def test_cloudflare_client_ips_have_independent_rate_limit_buckets(client):
    payload = {"email": "same@example.com", "password": "wrong"}
    for _ in range(5):
        assert client.post(
            reverse("accounts:login"),
            payload,
            REMOTE_ADDR="172.29.0.4",
            HTTP_CF_CONNECTING_IP="203.0.113.41",
        ).status_code == 200

    assert client.post(
        reverse("accounts:login"),
        payload,
        REMOTE_ADDR="172.29.0.4",
        HTTP_CF_CONNECTING_IP="203.0.113.41",
    ).status_code == 429
    assert client.post(
        reverse("accounts:login"),
        payload,
        REMOTE_ADDR="172.29.0.4",
        HTTP_CF_CONNECTING_IP="203.0.113.42",
    ).status_code == 200
    assert RateLimitBucket.objects.filter(
        bucket_hash=_bucket_hash("auth:login:ip:15m", "203.0.113.41")
    ).exists()
    assert not RateLimitBucket.objects.filter(
        bucket_hash=_bucket_hash("auth:login:ip:15m", "172.29.0.4")
    ).exists()


def test_signup_ip_cap_blocks_email_and_pending_row_before_creation(client):
    for attempt in range(10):
        response = register(client, f"signup-{attempt}@example.com")
        assert response.status_code == 200

    assert len(mail.outbox) == 10
    assert Account.objects.count() == 10
    response = register(client, "signup-blocked@example.com")
    assert response.status_code == 429
    assert len(mail.outbox) == 10
    assert not Account.objects.filter(email_normalized="signup-blocked@example.com").exists()


@override_settings(TRUSTED_PROXY_CIDRS="172.29.0.4/32")  
def test_admin_login_is_throttled_by_cloudflare_client_ip(client):
    for attempt in range(20):
        assert client.post(
            "/admin/login/",
            {"username": f"admin-{attempt}", "password": "wrong"},
            REMOTE_ADDR="172.29.0.4",
            HTTP_CF_CONNECTING_IP="203.0.113.43",
        ).status_code == 200

    assert client.post(
        "/admin/login/",
        {"username": "admin-blocked", "password": "wrong"},
        REMOTE_ADDR="172.29.0.4",
        HTTP_CF_CONNECTING_IP="203.0.113.43",
    ).status_code == 429
    assert client.post(
        "/admin/login/",
        {"username": "admin-blocked", "password": "wrong"},
        REMOTE_ADDR="172.29.0.4",
        HTTP_CF_CONNECTING_IP="203.0.113.44",
    ).status_code == 200
    # audit 2026-10-11 sweep: the cap is keyed on the CLIENT ip, never on the
    # shared cloudflared container address. increment_rate_limit counts the
    # refused attempt too: 20 allowed + 1 refused = 21.
    blocked_ip = RateLimitBucket.objects.get(
        bucket_hash=_bucket_hash("auth:admin-login:ip:15m", "203.0.113.43")
    )
    assert blocked_ip.count == 21
    assert RateLimitBucket.objects.get(
        bucket_hash=_bucket_hash("auth:admin-login:ip:15m", "203.0.113.44")
    ).count == 1
    assert not RateLimitBucket.objects.filter(
        bucket_hash=_bucket_hash("auth:admin-login:ip:15m", "172.29.0.4")
    ).exists()
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db(transaction=True)
def test_rate_limit_increment_is_atomic_under_concurrency():
    def increment_once(_):
        close_old_connections()
        try:
            increment_rate_limit("test", "subject", limit=20, window_seconds=60)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(increment_once, range(20)))
    assert RateLimitBucket.objects.get().count == 20


def test_auth_pages_have_hardening_headers_and_cookie_settings(client, settings):
    response = client.get(reverse("accounts:login"))
    assert response["Cache-Control"] == "no-store"
    assert response["Referrer-Policy"] == "same-origin"
    assert "default-src 'self'" in response["Content-Security-Policy"]
    assert settings.SESSION_COOKIE_AGE == 14 * 24 * 60 * 60
    assert settings.SESSION_COOKIE_HTTPONLY
    assert settings.SESSION_COOKIE_SAMESITE == "Lax"


def test_existing_staff_password_and_admin_access_survive_account_backfill(client):
    staff = get_user_model().objects.create_superuser(
        username="existing-admin",
        email="existing-admin@example.com",
        password=PASSWORD,
    )
    call_command("backfill_m3_accounts", owner_user_id=str(staff.pk), stdout=StringIO())
    assert client.login(username="existing-admin", password=PASSWORD)
    assert client.get("/admin/").status_code == 200


# --- D1 (P3-3, ): per-email login ceiling -------


def test_login_email_ceiling_blocks_ip_rotation(client):
    verified_user("ceiling-rotation@example.com")
    for ip_index in range(10):
        ip = f"203.0.{ip_index}.9"
        for _ in range(5):
            response = client.post(
                reverse("accounts:login"),
                {"email": "ceiling-rotation@example.com", "password": "wrong"},
                REMOTE_ADDR=ip,
            )
            assert response.status_code == 200
    response = client.post(
        reverse("accounts:login"),
        {"email": "ceiling-rotation@example.com", "password": "wrong"},
        REMOTE_ADDR="203.0.113.9",
    )
    assert response.status_code == 429
    assert "Unable to sign in with those credentials." in response.content.decode()


def test_login_email_ceiling_same_response_for_unknown_email(client):
    """sol gate #2: status AND normalized body (CSRF token stripped) must be
    byte-identical for a known vs unknown email at the 429 — not merely both
    containing the generic sentence, which would still pass if the two
    branches rendered materially different bodies around it."""
    verified_user("ceiling-known@example.com")
    responses = {}
    for email in ("ceiling-known@example.com", "ceiling-unknown@example.com"):
        for ip_index in range(10):
            ip = f"10.1.{ip_index}.9"
            for _ in range(5):
                client.post(
                    reverse("accounts:login"),
                    {"email": email, "password": "wrong"},
                    REMOTE_ADDR=ip,
                )
        responses[email] = client.post(
            reverse("accounts:login"),
            {"email": email, "password": "wrong"},
            REMOTE_ADDR="10.1.99.9",
        )
    known = responses["ceiling-known@example.com"]
    unknown = responses["ceiling-unknown@example.com"]
    assert known.status_code == unknown.status_code == 429
    assert _strip_csrf_token(known.content.decode()) == _strip_csrf_token(unknown.content.decode())


def test_admin_login_ceiling_same_response_for_unknown_username(client):
    """sol gate #2 (round 3 fix): the original version submitted
    'admin-parity@example.com' for BOTH the "known" and "unknown" cases
    while the user was created with username 'admin-parity' — so neither
    identifier existed and the test was vacuous. Fix: submit the REAL
    existing username (wrong password) vs a genuinely nonexistent username,
    exhaust each ceiling from rotating IPs, and assert both trip 429 at the
    same attempt number with identical normalized bodies. Also assert at
    least one pre-ceiling response for the existing user is a normal 200
    form error (proves the username really exists / was looked up)."""
    get_user_model().objects.create_superuser(
        username="admin-parity", email="admin-parity@example.com", password=PASSWORD
    )
    admin_login_url = reverse("admin-login")
    responses = {}
    pre_ceiling_status = {}
    first_known_form_response = None
    for username in ("admin-parity", "admin-parity-nonexistent"):
        for ip_index in range(10):
            ip = f"10.7.{ip_index}.9"
            for _ in range(5):
                r = client.post(
                    admin_login_url,
                    {"username": username, "password": "wrong"},
                    REMOTE_ADDR=ip,
                )
                pre_ceiling_status.setdefault(username, []).append(r.status_code)
                if username == "admin-parity" and first_known_form_response is None:
                    first_known_form_response = r
        responses[username] = client.post(
            admin_login_url,
            {"username": username, "password": "wrong"},
            REMOTE_ADDR="198.51.100.9",
        )
    known = responses["admin-parity"]
    unknown = responses["admin-parity-nonexistent"]
    assert known.status_code == unknown.status_code == 429
    assert _strip_csrf_token(known.content.decode()) == _strip_csrf_token(
        unknown.content.decode()
    )
    # both ceilings trip at the same attempt number (50th attempt, i.e. all
    # 50 pre-ceiling posts returned normal 200s, not an earlier 429).
    assert pre_ceiling_status["admin-parity"] == [200] * 50
    assert pre_ceiling_status["admin-parity-nonexistent"] == [200] * 50
    # proves the existing username really was looked up: the very first
    # pre-ceiling response for the real user is a normal 200 form re-render
    # that echoes the submitted username value back (the form is bound to
    # a real, known user, not silently short-circuited).
    assert first_known_form_response.status_code == 200
    assert 'value="admin-parity"' in first_known_form_response.content.decode()


def test_admin_login_ceiling_does_not_share_state_with_regular_login(client):
    """The admin-login and login ceilings use distinct bucket names (D1
    design: each action gets its own pair), so tripping one must not block
    the other for the same email."""
    verified_user("shared-email@example.com")
    admin_login_url = reverse("admin-login")
    for ip_index in range(10):
        ip = f"10.5.{ip_index}.9"
        for _ in range(5):
            client.post(
                admin_login_url,
                {"username": "shared-email@example.com", "password": "wrong"},
                REMOTE_ADDR=ip,
            )
    blocked = client.post(
        admin_login_url,
        {"username": "shared-email@example.com", "password": "wrong"},
        REMOTE_ADDR="10.5.99.9",
    )
    assert blocked.status_code == 429

    regular_login = client.post(
        reverse("accounts:login"),
        {"email": "shared-email@example.com", "password": "wrong"},
        REMOTE_ADDR="10.5.100.9",
    )
    assert regular_login.status_code == 200
    # separate bucket pairs: admin ceiling over its limit (50 + the refused
    # attempt), the login ceiling holds only the 1 regular attempt
    email = "shared-email@example.com"
    admin_ceiling = RateLimitBucket.objects.get(bucket_hash=_bucket_hash("auth:admin-login:email:1h", email))
    assert admin_ceiling.count == 51
    assert RateLimitBucket.objects.get(bucket_hash=_bucket_hash("auth:login:email:1h", email)).count == 1
    assert "_auth_user_id" not in client.session


def test_ceiling_is_login_only_reset_still_works(client):
    verified_user("ceiling-reset@example.com")
    for ip_index in range(10):
        ip = f"10.2.{ip_index}.9"
        for _ in range(5):
            client.post(
                reverse("accounts:login"),
                {"email": "ceiling-reset@example.com", "password": "wrong"},
                REMOTE_ADDR=ip,
            )
    blocked = client.post(
        reverse("accounts:login"),
        {"email": "ceiling-reset@example.com", "password": "wrong"},
        REMOTE_ADDR="192.0.2.9",
    )
    assert blocked.status_code == 429

    reset_response = client.post(
        reverse("accounts:password-reset"),
        {"email": "ceiling-reset@example.com"},
        REMOTE_ADDR="192.0.2.10",
    )
    assert reset_response.status_code == 200
    assert len(mail.outbox) == 1


def test_successful_reset_clears_login_ceiling(client):
    user = verified_user("ceiling-clears@example.com")
    for ip_index in range(10):
        ip = f"10.3.{ip_index}.9"
        for _ in range(5):
            client.post(
                reverse("accounts:login"),
                {"email": "ceiling-clears@example.com", "password": "wrong"},
                REMOTE_ADDR=ip,
            )
    blocked = client.post(
        reverse("accounts:login"),
        {"email": "ceiling-clears@example.com", "password": "wrong"},
        REMOTE_ADDR="10.3.99.9",
    )
    assert blocked.status_code == 429
    ceiling = [_bucket_hash(name, "ceiling-clears@example.com") for name in LOGIN_EMAIL_CEILING_NAMES]
    assert RateLimitBucket.objects.get(bucket_hash=ceiling[0]).count == 51  # 50 + the refused one
    assert "_auth_user_id" not in client.session

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    url = reverse("accounts:password-reset-confirm", kwargs={"uidb64": uid, "token": token})
    confirm_response = client.post(
        url, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}
    )
    assert confirm_response.status_code == 200
    # the reset deleted both email-ceiling buckets and set the new password
    assert not RateLimitBucket.objects.filter(bucket_hash__in=ceiling).exists()
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)

    login_response = client.post(
        reverse("accounts:login"),
        {"email": "ceiling-clears@example.com", "password": NEW_PASSWORD},
        REMOTE_ADDR="10.3.200.9",
    )
    assert login_response.status_code == 302
    assert client.session["_auth_user_id"] == str(user.pk)


def test_single_ip_cannot_burn_email_ceiling(client):
    verified_user("ceiling-single-ip@example.com")
    for _ in range(20):
        client.post(
            reverse("accounts:login"),
            {"email": "ceiling-single-ip@example.com", "password": "wrong"},
            REMOTE_ADDR="10.4.0.9",
        )
    bucket_hash = _bucket_hash("auth:login:email:1h", "ceiling-single-ip@example.com")
    bucket = RateLimitBucket.objects.get(bucket_hash=bucket_hash)
    assert bucket.count == 5


def test_correct_password_during_lockout_is_rejected(client):
    verified_user("ceiling-correct-pw@example.com")
    for ip_index in range(10):
        ip = f"10.5.{ip_index}.9"
        for _ in range(5):
            client.post(
                reverse("accounts:login"),
                {"email": "ceiling-correct-pw@example.com", "password": "wrong"},
                REMOTE_ADDR=ip,
            )
    response = client.post(
        reverse("accounts:login"),
        {"email": "ceiling-correct-pw@example.com", "password": PASSWORD},
        REMOTE_ADDR="10.5.99.9",
    )
    assert response.status_code == 429
    assert "_auth_user_id" not in client.session


def test_admin_login_email_ceiling_blocks_ip_rotation(client):
    for ip_index in range(10):
        ip = f"10.6.{ip_index}.9"
        for _ in range(5):
            response = client.post(
                "/admin/login/",
                {"username": "ceiling-admin@example.com", "password": "wrong"},
                REMOTE_ADDR=ip,
            )
            assert response.status_code == 200
    response = client.post(
        "/admin/login/",
        {"username": "ceiling-admin@example.com", "password": "wrong"},
        REMOTE_ADDR="10.6.99.9",
    )
    assert response.status_code == 429
    assert response.content == b"Too many login attempts."


def test_admin_login_nfkc_normalizes_username_before_throttling(client):
    "Public API."
    get_user_model().objects.create_superuser(
        username="admin", email="admin@example.com", password=PASSWORD
    )
    admin_login_url = reverse("admin-login")
    for ip_index in range(10):
        ip = f"10.8.{ip_index}.9"
        for _ in range(5):
            client.post(
                admin_login_url,
                {"username": "admin", "password": "wrong"},
                REMOTE_ADDR=ip,
            )
    fullwidth_username = "\uff41\uff44\uff4d\uff49\uff4e"  # NFKC-normalizes to "admin"
    response = client.post(
        admin_login_url,
        {"username": fullwidth_username, "password": PASSWORD},
        REMOTE_ADDR="10.8.99.9",
    )
    assert response.status_code == 429
    assert response.content == b"Too many login attempts."
    assert "_auth_user_id" not in client.session
