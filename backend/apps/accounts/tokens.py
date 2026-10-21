import hashlib
import logging

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare

VERIFICATION_SALT = "mailsocket.accounts.verify-email.v1"

logger = logging.getLogger(__name__)


def _security_nonce(user, account) -> str:
    state = "|".join(
        [
            user.password,
            user.email,
            str(user.is_active),
            account.email_normalized,
            account.email_verified_at.isoformat() if account.email_verified_at else "",
            account.verification_nonce,
        ]
    )
    return hashlib.sha256(state.encode()).hexdigest()


def make_verification_token(user) -> str:
    account = user.account
    return signing.dumps(
        {
            "user": str(user.pk),
            "email": account.email_normalized,
            "nonce": _security_nonce(user, account),
        },
        salt=VERIFICATION_SALT,
        compress=True,
    )


def resolve_verification_token(token: str):
    from django.contrib.auth import get_user_model

    payload = signing.loads(
        token,
        salt=VERIFICATION_SALT,
        max_age=settings.EMAIL_VERIFICATION_TIMEOUT,
    )
    user = get_user_model().objects.select_related("account").get(pk=payload["user"])
    account = user.account
    if account.email_verified_at is not None:
        raise signing.BadSignature("Verification token has already been used.")
    if payload.get("email") != account.email_normalized:
        raise signing.BadSignature("Email state changed.")
    if not constant_time_compare(payload.get("nonce", ""), _security_nonce(user, account)):
        raise signing.BadSignature("Security state changed.")
    if not account.verification_nonce:
        raise signing.BadSignature("No pending signup is bound to this token.")
    return user


def activate_verification_token(token: str, password: str):
    candidate = resolve_verification_token(token)
    from .models import Account

    was_unverified = None
    with transaction.atomic():
        account = Account.objects.select_for_update().select_related("user").get(
            user_id=candidate.pk
        )
        # Registration updates take the same Account lock. Revalidate after the
        # lock so an older link can never activate a newer pending credential.
        resolve_verification_token(token)
        user = account.user
        was_unverified = account.email_verified_at is None
        user.set_password(password)
        user.save(update_fields=["password"])
        account.verification_nonce = ""
        account.email_verified_at = timezone.now()
        account.save(
            update_fields=[
                "verification_nonce",
                "email_verified_at",
                "updated_at",
            ]
        )

    # Onboarding runs AFTER the verification transaction has committed, so an
    # inbox/seed failure can never roll back (or be rolled back by) the
    # critical verify path. Idempotency of the auto-inbox lives in
    # apps.inboxes.services.provision_first_inbox (zero-inbox guard + plan cap),
    # which is also re-checked on every activate, so a repeat/re-send never
    # mints a second inbox or welcome message.
    if was_unverified:
        from apps.inboxes.services import provision_onboarding

        try:
            provision_onboarding(user)
        except Exception:
            # Best-effort: onboarding must never surface as a verify failure.
            logger.exception("onboarding_failed_after_verify user_id=%s", user.pk)

    return user
