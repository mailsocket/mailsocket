import logging
import secrets

from django.db import IntegrityError, transaction

from apps.inboxes.models import ROUTING_KEY_RE, Inbox

logger = logging.getLogger(__name__)


# shared catch-all domain. Built from apps.api.verification.ROLE_LOCALPARTS
# (the same role-address heuristic used for /verify) PLUS mailsocket's own
# service localparts and a few RFC 2142 addresses ROLE_LOCALPARTS doesn't
# carry (mailer-daemon isn't a "role mailbox" a human reads, but MUST route
# to the MTA, never to a customer inbox).
#
# Imported lazily inside _reserved_routing_keys() to avoid apps.inboxes ->
# apps.api at import time (apps.inboxes loads before apps.api in
# INSTALLED_APPS; a top-level import here would risk an AppRegistryNotReady
# during Django startup).
def _reserved_routing_keys() -> set[str]:
    from apps.api.verification import ROLE_LOCALPARTS

    return ROLE_LOCALPARTS | {
        "mailer-daemon",
        "hostmaster",
        "welcome",
        "billing",
        "notifications",
        "notify",
        "system",
    }


class RoutingKeyInvalid(Exception):
    """Raised by ``normalize_routing_key`` on a bad-format or reserved slug.

    ``code`` is "invalid_format" or "slug_reserved" so callers (the API
    serializer, the dashboard form) can map it to their own error surface
    without re-deriving what went wrong.
    """

    def __init__(self, message: str, code: str):
        self.message = message
        self.code = code
        super().__init__(message)


def normalize_routing_key(value: str, *, is_shared: bool = True) -> str:
    "Public API."
    value = value.lower()
    if not ROUTING_KEY_RE.match(value):
        raise RoutingKeyInvalid(
            "routing_key must be 3-64 chars, lowercase letters/digits, "
            "start alphanumeric, and use only a-z 0-9 _ -.",
            code="invalid_format",
        )
    if is_shared and value in _reserved_routing_keys():
        raise RoutingKeyInvalid(
            "routing_key is reserved and cannot be claimed.", code="slug_reserved"
        )
    return value

FIRST_INBOX_LABEL = "My first inbox"
WELCOME_FROM = "mailsocket <welcome@mailsocket.app>"
WELCOME_SUBJECT = "Welcome to mailsocket - here's your first OTP"
WELCOME_OTP = "481920"
WELCOME_MAGIC_LINK = "https://example.com/auth/magic?token=demo-abcdefghijklmnopqrstuvwxyz"

# Max attempts to allocate a random routing_key on the rare collision (or
# leading-`_`/`-` check-constraint reject). Shared by every random-key inbox
# creation path via create_random_inbox().
RANDOM_INBOX_MAX_ATTEMPTS = 10


def random_routing_key() -> str:
    """Generate a fresh inbox routing key.

    Single source of truth for every inbox-minting path (dashboard, REST API,
    and first-signup onboarding). Collisions, and the rare token whose leading
    character lands outside ``[a-z0-9]`` after lowercasing, are handled by the
    callers' ``IntegrityError`` retry loops exactly as before.
    """
    return secrets.token_urlsafe(12).lower().replace("-", "_")


class InboxCreatePlanLimitReached(Exception):
    """Raised by a create_random_inbox guard to veto creation immediately
    (distinct from an IntegrityError collision, which retries)."""


_RANDOM_INBOX_FIELDS = frozenset({"domain", "metadata", "is_public"})


def create_random_inbox(user, label: str = "", *, guard=None, **fields):
    "Public API."
    unknown = set(fields) - _RANDOM_INBOX_FIELDS
    if unknown:
        raise TypeError(f"create_random_inbox: unexpected fields {sorted(unknown)}")
    fields["metadata"] = fields.get("metadata") or {}
    for _ in range(RANDOM_INBOX_MAX_ATTEMPTS):
        try:
            with transaction.atomic():
                if guard is not None:
                    guard()
                return Inbox.objects.create(
                    user=user, label=label, routing_key=random_routing_key(), **fields
                )
        except IntegrityError:
            continue
    return None


def provision_first_inbox(user):
    """Create the user's first inbox on first verification. Best-effort.

    Returns the new :class:`Inbox`, or ``None`` when the account already owns an
    inbox, has reached its plan cap, or creation failed. The function never
    raises — verification is the critical path and must be able to proceed
    without onboarding (see ``apps.accounts.tokens.activate_verification_token``).

    Idempotency: a zero-inbox guard inside the same transaction as the create
    means a resend/re-verify (even one that somehow re-enters this path) can
    never mint a second auto-inbox. The plan cap is respected through
    ``can_create_inbox`` (free plan = 1 inbox; auto-provision consumes it).
    """
    from apps.accounts.entitlements import can_create_inbox

    try:
        if Inbox.objects.filter(user=user, deleted_at__isnull=True).exists():
            return None

        def _guard():
            # Re-check under the plan-row lock so two concurrent first
            # provisions cannot both pass the guard.
            if Inbox.objects.filter(user=user, deleted_at__isnull=True).exists():
                raise InboxCreatePlanLimitReached()
            if not can_create_inbox(user):
                raise InboxCreatePlanLimitReached()

        return create_random_inbox(user, label=FIRST_INBOX_LABEL, guard=_guard)
    except InboxCreatePlanLimitReached:
        return None
    except Exception:
        logger.exception("first_inbox_provision_failed user_id=%s", user.pk)
    return None


def _welcome_raw(inbox) -> bytes:
    body = (
        "Welcome to mailsocket!\r\n"
        "\r\n"
        "Your first inbox is live and already receiving mail. This message\r\n"
        "was delivered to your new address so you can see parsing in action.\r\n"
        "\r\n"
        f"Your mailsocket verification code is {WELCOME_OTP}\r\n"
        "\r\n"
        "Or continue with your magic link:\r\n"
        f"{WELCOME_MAGIC_LINK}\r\n"
    )
    raw = (
        f"From: {WELCOME_FROM}\r\n"
        f"To: {inbox.address}\r\n"
        f"Message-ID: <welcome-{inbox.public_id}@mailsocket.app>\r\n"
        f"Subject: {WELCOME_SUBJECT}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
    ).encode("utf-8") + body.encode("utf-8")
    return raw


def seed_welcome_message(inbox):
    """Deliver one welcome demo mail through the REAL ingest/parse pipeline.

    Constructs a raw RFC822 message and feeds it through ``store_message`` +
    ``parse_message`` — the same code ``/internal/ingest`` and the reconciler
    use — so parsing and sanitization are identical to real mail and the demo
    OTP/magic-link are extracted by the production parser. Raises on failure;
    ``provision_onboarding`` is the fail-silent boundary that contains it.
    """
    from apps.ingest.parser import parse_message
    from apps.ingest.services import store_message

    raw = _welcome_raw(inbox)
    result = store_message(
        raw_bytes=raw,
        headers={
            "X-Envelope-To": inbox.address,
            "X-Envelope-From": "welcome@mailsocket.app",
        },
    )
    parse_message(result.message.pk)
    return result.message


def provision_onboarding(user) -> None:
    """Best-effort first-signup onboarding: first inbox + seeded welcome mail.

    Each step is independently fail-silent so a seed/parse failure can never
    break verification or login. Called only AFTER the verification transaction
    has committed (see ``apps.accounts.tokens.activate_verification_token``).
    """
    inbox = provision_first_inbox(user)
    if inbox is None:
        return
    try:
        seed_welcome_message(inbox)
    except Exception:
        logger.exception("welcome_seed_failed user_id=%s inbox_id=%s", user.pk, inbox.pk)
