import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, models, transaction
from django.db.models.functions import Lower, Now
from django.utils import timezone


class Account(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        primary_key=True,
        on_delete=models.PROTECT,
        related_name="account",
    )
    email_normalized = models.CharField(max_length=254, unique=True, validators=[validate_email])
    email_verified_at = models.DateTimeField(null=True, blank=True)
    verification_nonce = models.CharField(max_length=64, blank=True, default="")
    is_suspended = models.BooleanField(default=False)
    created_at = models.DateTimeField(db_default=Now(), editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    
    # additive, nullable. Set by the auto-pause circuit breaker
    # (apps.outbound.services.maybe_auto_pause) when this account's rolling
    # bounce ratio over its last OUTBOUND_AUTO_PAUSE_WINDOW sent emails
    # exceeds OUTBOUND_AUTO_PAUSE_BOUNCE_RATIO. The drain claim rejects this
    
    # it (out of scope for this build — no unpause endpoint shipped in V1,
    # per design; ops clears it directly).
    outbound_paused_at = models.DateTimeField(null=True, blank=True)
    
    # ENFORCEMENT flag, additive, default False, no backfill. Deliberately
    # keyed to the ACCOUNT, never the plan tier — a plan downgrade/lapse with
    # this ON can never silently reopen sending (fail-closed survives billing
    # state). The TIER only gates who may CONFIGURE the feature (see
    # apps.accounts.entitlements.can_configure_outbound_allowlist); this flag
    # is what the two enforcement points (apps.outbound.services enqueue gate
    # #7.5, apps.outbound.engine.deliver_claim drain re-check) actually read.
    outbound_allowlist_enabled = models.BooleanField(default=False)
    # Signup source attribution (apps.accounts.attribution): first-touch
    # channel copied from the signed ``ms_src`` cookie at signup. Additive,
    # nullable, no backfill (NULL = unknown/direct). Never full URLs/queries.
    src_ref = models.CharField(max_length=40, null=True, blank=True)
    src_referrer_host = models.CharField(max_length=253, null=True, blank=True)
    src_landing_path = models.CharField(max_length=120, null=True, blank=True)
    src_first_seen = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(email_normalized=Lower("email_normalized")),
                name="account_email_normalized_lower",
            )
        ]
        # signup_burst (abuse_launch.signups_last_hour) scans the last hour.
        indexes = [models.Index(fields=["created_at"], name="account_created_idx")]

    def __str__(self) -> str:
        return self.email_normalized


class AccountPlan(models.Model):
    class Plan(models.TextChoices):
        FREE = "free", "Free"
        PAID = "paid", "Paid"
        SCALE = "scale", "Scale"
        BUSINESS = "business", "Business"

    # QA #7: the customer-facing tier names (the stored value "paid" is sold
    # as "Pro"). Kept separate from Plan.choices labels on purpose — changing
    # those would be a schema-level (migration) change for pure copy. This is
    # the one mapping every dashboard/public surface renders; the pricing
    # ladder (apps.dashboard.views._pricing_context) uses the same names.
    DISPLAY_NAMES = {
        "free": "Free",
        "paid": "Pro",
        "scale": "Scale",
        "business": "Business",
        "enterprise": "Enterprise",
    }

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Past due"
        SUSPENDED = "suspended", "Suspended"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        primary_key=True,
        on_delete=models.PROTECT,
        related_name="account_plan",
    )
    plan = models.CharField(max_length=16, choices=Plan.choices, default=Plan.FREE)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    
    # stripe_customer_id is a CATALOG id shared by every LS customer at this
    
    
    # same LS customer_id on multiple rows. Neither may be unique; write for
    # audit only, plan/state is always derived at webhook time from the
    # settings variant->plan map. stripe_subscription_id stays unique — it is
    # genuinely per-subscription identity.
    stripe_customer_id = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    stripe_subscription_id = models.CharField(max_length=255, null=True, blank=True, unique=True)
    stripe_price_id = models.CharField(max_length=255, null=True, blank=True)
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now(), editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    # Out-of-order guard (design §P0-5): the LS webhook payload's
    # data.attributes.updated_at, set only under _locked_plan. An event whose
    # updated_at is older than this is a stale/reordered delivery and is
    # skipped (200, no mutation) — see apps.billing.views.ls_webhook.
    billing_synced_at = models.DateTimeField(null=True, blank=True)

    @property
    def display_name(self) -> str:
        """Customer-facing plan name (QA #7): "Pro", never the internal "Paid"."""
        return self.DISPLAY_NAMES.get(self.plan, self.get_plan_display())


class MonthlyUsage(models.Model):
    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="monthly_usage",
    )
    period_start = models.DateField()
    messages_received = models.PositiveBigIntegerField(default=0)
    api_requests = models.PositiveBigIntegerField(default=0)
    webhook_attempts = models.PositiveBigIntegerField(default=0)
    
    
    # a hard block. db_default is REQUIRED (not just style) because both raw
    # INSERTs in entitlements.py (_upsert_counter + record_message_once)
    # enumerate columns explicitly — a first raw INSERT that omits this
    # column would violate NOT NULL without it.
    verifications_used = models.PositiveBigIntegerField(default=0, db_default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "period_start"],
                name="monthly_usage_user_period_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(period_start__day=1),
                name="monthly_usage_period_first_day",
            ),
        ]
        indexes = [
            models.Index(fields=["period_start", "user"], name="monthly_usage_period_user_idx"),
        ]


class AccountWeeklyActivity(models.Model):
    """Funnel / North Star durable signal (manage.py funnel_report, apps.accounts.funnel).

    One row per (account, ISO week) in which the account received its first
    EXTERNAL message of that week (self-generated welcome / test-code / demo
    mail and mailsocket outbound VERP mail never count, see
    activity.SYSTEM_SENDERS / system_sender_domains). Written append-only
    (INSERT ... ON CONFLICT DO NOTHING) by entitlements.record_weekly_activity
    when a message is first usage-counted. Stores the account id, the week
    bucket and the first message time only: no content, no addresses.
    Survives the Free 1-day message purge (MonthlyUsage cannot: monthly
    granularity, and it counts the welcome mail). CASCADE on purpose: an
    account erase removes these rows with the user.
    """

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="weekly_activity",
    )
    week_start = models.DateField()
    first_message_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "week_start"], name="account_weekly_activity_uniq"),
        ]
        indexes = [
            models.Index(fields=["week_start", "user"], name="account_weekly_activity_wk_idx"),
        ]


class AccountActiveDay(models.Model):
    "Public API."

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="early_active_days",
    )
    day = models.DateField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "day"], name="account_active_day_uniq"),
        ]


class AccountIntent(models.Model):
    "Public API."

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="commercial_intent",
    )
    viewed_pricing_at = models.DateTimeField(null=True, blank=True)
    started_checkout_at = models.DateTimeField(null=True, blank=True)


class DemoStartDaily(models.Model):
    "Public API."

    day = models.DateField()
    ref = models.CharField(max_length=40, blank=True, default="")
    count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["day", "ref"], name="demo_start_daily_day_ref_uniq"),
        ]


class RateLimitBucket(models.Model):
    bucket_hash = models.CharField(max_length=64, primary_key=True)
    window_started_at = models.DateTimeField()
    count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(db_index=True)


class WaitLease(models.Model):
    """Per-account guard capping concurrent long-poll wait requests.

    One row per user holds the number of wait requests currently in flight.
    A slot is granted by atomically incrementing ``count`` under a row lock
    (see ``apps.accounts.services.acquire_wait_lease``) and released in a
    ``finally`` block. The row self-heals: once the newest outstanding wait
    would have ended (timeout + grace), the next acquire resets ``count`` to
    zero, so a worker crash can never leak a permanent slot.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="wait_lease",
    )
    count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(count__gte=0),
                name="wait_lease_count_nonnegative",
            ),
        ]


class WaitCapacity(models.Model):
    """Global singleton capping ALL in-flight wait requests across every account.

    One fixed-key row (key="global") counts every wait request currently in
    flight across all accounts, so a fleet of cheap signups cannot exhaust the
    web pool. A slot is acquired after the per-account WaitLease and released
    together with it in the same ``finally`` (see
    ``apps.accounts.services.acquire_wait_capacity``). Self-heals exactly like
    WaitLease: once the newest outstanding wait would have ended (timeout +
    grace), the next acquire resets ``count``, so a crashed worker can never
    wedge the endpoint shut.
    """

    key = models.CharField(primary_key=True, max_length=32)
    count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(count__gte=0),
                name="wait_capacity_count_nonnegative",
            ),
        ]


class WaitCapacityDaily(models.Model):
    """walert: per-day saturation stats for the global and free wait pools.

    Written best-effort from ``apps.accounts.services.acquire_wait_capacity``
    AFTER the counter-row transaction (single INSERT ... ON CONFLICT upsert,
    failures logged and swallowed). ``peak`` is the highest in-flight count
    seen right after a successful acquire; ``refusals`` counts pool-full 429s
    (per-account caps are NOT counted). ``warned_at`` / ``saturated_at`` are the
    LATEST time the pool reached >= 80% / refused an acquire: they mark an
    alert as due; the 5-minute ``status_probe`` cron sends it (see
    ``apps.accounts.wait_alerts``).
    """

    date = models.DateField()
    pool = models.CharField(max_length=16)
    peak = models.PositiveIntegerField(default=0)
    refusals = models.PositiveIntegerField(default=0)
    warned_at = models.DateTimeField(null=True, blank=True)
    saturated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["date", "pool"], name="wait_capacity_daily_date_pool_uniq"),
        ]


class WaitCapacityAlert(models.Model):
    """walert: DB-enforced throttle, one row per (pool, level). Only the
    sender whose conditional UPDATE moves ``last_alert_at`` forward sends."""

    pool = models.CharField(max_length=16)
    level = models.CharField(max_length=16)
    last_alert_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["pool", "level"], name="wait_capacity_alert_pool_level_uniq"),
        ]


class AbuseAlert(models.Model):
    "Public API."

    kind = models.CharField(max_length=32, unique=True)
    last_alert_at = models.DateTimeField(null=True, blank=True)


class ApiKey(models.Model):
    id = models.BigAutoField(primary_key=True)
    public_id = models.CharField(max_length=32, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="api_keys",
    )
    name = models.CharField(max_length=80)
    prefix = models.CharField(max_length=12, db_index=True, editable=False)
    key_hash = models.CharField(max_length=64, unique=True, editable=False)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now(), editable=False)
    revoked_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)  # NULL = never expires
    #  (Team Seats): additive, nullable. NOT consumed by any
    # behavior yet — Stage B is what starts recording/using it (member-minted
    # keys under a shared tenant, cascade-revoke on membership revoke). No
    # backfill for existing rows: they stay NULL and are read exactly as
    # before (ApiKey.user is still the sole tenancy signal everywhere).
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="api_keys_created",
    )

    class Meta:
        indexes = [
            models.Index(fields=["user", "-created_at", "-id"], name="api_key_owner_created_idx"),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding:
            for _ in range(10):
                self.public_id = f"key_{secrets.token_urlsafe(18)}"
                try:
                    with transaction.atomic():
                        return super().save(*args, **kwargs)
                except IntegrityError as exc:
                    constraint = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "")
                    if constraint != "accounts_apikey_public_id_key":
                        raise
                    self.pk = None
            raise RuntimeError("Unable to save a unique ApiKey public_id")
        original = type(self).objects.only("public_id", "prefix", "key_hash").get(pk=self.pk)
        if (self.public_id, self.prefix, self.key_hash) != (
            original.public_id,
            original.prefix,
            original.key_hash,
        ):
            raise ValidationError("API key identity fields are immutable.")
        return super().save(*args, **kwargs)

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= timezone.now()

    def __str__(self) -> str:
        return f"{self.name} ({self.prefix})"


class IdempotencyRecord(models.Model):
    "Public API."

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="idempotency_records",
    )
    api_key = models.ForeignKey(
        ApiKey,
        on_delete=models.CASCADE,
        related_name="idempotency_records",
    )
    key = models.CharField(max_length=255)
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=512)
    request_fingerprint = models.CharField(max_length=64)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True, default="")
    response_headers = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "api_key", "key"], name="idempotency_user_apikey_key_uniq"
            ),
        ]

    def __str__(self) -> str:
        return f"idempotency user={self.user_id} {self.method} {self.path}"


# ---  (Team Seats) -------------------------------------------


# user.pk unchanged). These two models are purely additive and, in Stage A,
# grant NO access to anything — tenant(request) does not exist yet, and
# every request.user site in apps/api/views.py + apps/dashboard/views.py is
# untouched. A TeamMembership row is inert data until Stage B.


class TeamMembership(models.Model):
    "Public API."

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        MEMBER = "member", "Member"

    id = models.BigAutoField(primary_key=True)
    account = models.ForeignKey(
        Account,
        on_delete=models.CASCADE,
        related_name="team_memberships",
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="team_membership",
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.MEMBER)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="team_memberships_invited",
    )
    created_at = models.DateTimeField(db_default=Now(), editable=False)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["account"], name="team_membership_account_idx"),
        ]

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def __str__(self) -> str:
        return f"membership user={self.user_id} account={self.account_id}"




# Account.pk == user.pk is UNCHANGED — a "second account" is a NEW linked
# workspace User (set_unusable_password), never a re-key of Account/AccountPlan/
# Domain/Inbox/ApiKey or any tenancy FK. This model is purely additive.


class AccountOwnership(models.Model):
    "Public API."

    id = models.BigAutoField(primary_key=True)
    owner_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_accounts",
    )
    account_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_by_link",
    )
    name = models.CharField(max_length=80)
    created_at = models.DateTimeField(db_default=Now(), editable=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(owner_user=models.F("account_user")),
                name="account_ownership_no_self_link",
            ),
            models.UniqueConstraint(
                fields=["owner_user", "name"],
                name="account_ownership_owner_name_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["owner_user"], name="account_ownership_owner_idx"),
        ]

    def __str__(self) -> str:
        return f"ownership owner={self.owner_user_id} account={self.account_user_id} name={self.name!r}"


def _new_invite_token() -> tuple[str, str]:
    """Return (raw_token, token_hash). The raw token is NEVER persisted —
    same discipline as ApiKey.key_hash/hash_api_key (accounts/services.py)."""
    raw = secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return raw, digest


def _default_invite_expiry():
    return timezone.now() + timedelta(days=7)


class TeamInvite(models.Model):
    "Public API."

    id = models.BigAutoField(primary_key=True)
    account = models.ForeignKey(
        Account,
        on_delete=models.CASCADE,
        related_name="team_invites",
    )
    email_normalized = models.CharField(max_length=254, validators=[validate_email])
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    role = models.CharField(
        max_length=16, choices=TeamMembership.Role.choices, default=TeamMembership.Role.MEMBER
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="team_invites_sent",
    )
    created_at = models.DateTimeField(db_default=Now(), editable=False)
    expires_at = models.DateTimeField(default=_default_invite_expiry, editable=False)
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(email_normalized=Lower("email_normalized")),
                name="team_invite_email_normalized_lower",
            ),
            # One live invite per address per team (D6/§5): "live" means not
            # yet accepted and not revoked. Partial unique so a new invite
            # can be issued again after the prior one is accepted/revoked.
            models.UniqueConstraint(
                fields=["account", "email_normalized"],
                condition=models.Q(accepted_at__isnull=True, revoked_at__isnull=True),
                name="team_invite_live_account_email_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["account"], name="team_invite_account_idx"),
        ]

    @property
    def is_pending(self) -> bool:
        return (
            self.accepted_at is None
            and self.revoked_at is None
            and self.expires_at > timezone.now()
        )

    def __str__(self) -> str:
        return f"invite {self.email_normalized} -> account={self.account_id}"
