"""Erase the throwaway accounts the e2e journey suite signs up.

Dry-run by default; ``--apply`` deletes. A user is a candidate only when BOTH:

  * its Account email matches the e2e signup pattern:
    ``<prefix><anything>@<MAIL_DOMAIN>`` (default prefix ``e2e-``; the suite
    creates the per-run signup inbox with routing_key ``e2e-<run id>``), and
  * the Account was created within the last ``--hours`` (default 48).

``--qa-account EMAIL`` (scoped mode) is MANDATORY unless the settings module
is ``config.settings.testing`` or ``config.settings.e2e``: on production (or
any other settings) the command refuses to run without it. In scoped mode:

  * the candidate's Account email must ALSO be the address of an inbox (live
    or soft-deleted) OWNED BY that QA account, so a customer who signed up
    with their own ``e2e-...`` mailsocket inbox can never match;
  * ``--prefix`` is forbidden (only the default ``e2e-`` pattern is used);
  * the QA account must exist, and the command refuses outright if the QA
    account itself would be a candidate (its email matches the signup
    pattern or is the address of one of its own inboxes).

Fail-closed refusals (nothing at all is deleted if any candidate is refused):
an AccountPlan with a ``stripe_subscription_id`` or on a non-Free plan, any
BillingPayment row, a staff/superuser, live TeamMembership rows in its team,
a membership in another team, or AccountOwnership rows (workspaces).

Deletes in the FULL account-erase FK order (skill mailsocket-ops, same as
apps.dashboard.settings_views.settings_delete), inside ONE transaction,
printing every step.
"""
import os
import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import (
    Account,
    AccountOwnership,
    AccountPlan,
    ApiKey,
    MonthlyUsage,
    TeamInvite,
    TeamMembership,
)
from apps.billing.models import BillingPayment
from apps.domains.models import Domain
from apps.inboxes.models import Inbox, Message
from apps.outbound.models import ApprovedRecipient, OutboundEmail
from apps.webhooks.models import Webhook, WebhookDelivery

DEFAULT_PREFIX = "e2e-"
DEFAULT_HOURS = 48
# Only under these settings modules may the command run unscoped (without
# --qa-account). Anything else, including an unknown module, needs the scope.
UNSCOPED_SETTINGS = frozenset({"config.settings.testing", "config.settings.e2e"})


def settings_modules() -> set[str]:
    """Every settings-module name this process claims (env + loaded settings)."""
    return {
        m for m in (os.environ.get("DJANGO_SETTINGS_MODULE", ""), getattr(settings, "SETTINGS_MODULE", "") or "")
        if m
    }


def unscoped_allowed() -> bool:
    """Fail-closed: True only when every known module name is testing/e2e."""
    modules = settings_modules()
    return bool(modules) and modules <= UNSCOPED_SETTINGS

# The order IS the contract (unit-tested). Step 0 breaks the
# Inbox.domain <-> Domain.catch_all_inbox PROTECT cycle and must run first.
ERASE_STEPS = (
    "domain_catch_all_unset",
    "webhook_delivery",
    "webhook",
    "outbound_email",
    "approved_recipient",
    "message",
    "inbox",
    "domain",
    "api_key",
    "team_invite",
    "team_membership",
    "monthly_usage",
    "account_plan",
    "account",
    "user",
)


def erase_user(user_pk: int, log) -> list[tuple[str, int]]:
    """Delete every row of one account in ERASE_STEPS order. Caller holds the
    transaction. Returns [(step, rows)] in execution order."""
    done = []

    def step(name, count):
        done.append((name, count))
        log(f"    {name}: {count}")

    step("domain_catch_all_unset",
         Domain.objects.filter(user_id=user_pk).update(catch_all=False, catch_all_inbox=None))
    step("webhook_delivery", WebhookDelivery.objects.filter(webhook__inbox__user_id=user_pk).delete()[0])
    step("webhook", Webhook.objects.filter(inbox__user_id=user_pk).delete()[0])
    step("outbound_email", OutboundEmail.objects.filter(user_id=user_pk).delete()[0])
    step("approved_recipient", ApprovedRecipient.objects.filter(user_id=user_pk).delete()[0])
    step("message", Message.objects.filter(inbox__user_id=user_pk).delete()[0])
    step("inbox", Inbox.objects.filter(user_id=user_pk).delete()[0])
    step("domain", Domain.objects.filter(user_id=user_pk).delete()[0])
    step("api_key", ApiKey.objects.filter(user_id=user_pk).delete()[0])
    step("team_invite", TeamInvite.objects.filter(account_id=user_pk).delete()[0])
    step("team_membership", TeamMembership.objects.filter(account_id=user_pk).delete()[0])
    step("monthly_usage", MonthlyUsage.objects.filter(user_id=user_pk).delete()[0])
    # WaitLease / IdempotencyRecord / PlanChangeRequest CASCADE with the user.
    step("account_plan", AccountPlan.objects.filter(user_id=user_pk).delete()[0])
    step("account", Account.objects.filter(pk=user_pk).delete()[0])
    step("user", get_user_model().objects.filter(pk=user_pk).delete()[0])
    return done


def refusal_reason(user) -> str | None:
    if user.is_staff or user.is_superuser:
        return "staff/superuser"
    plan = AccountPlan.objects.filter(user_id=user.pk).first()
    if plan is not None and plan.stripe_subscription_id:
        return "has stripe_subscription_id"
    if plan is not None and plan.plan != AccountPlan.Plan.FREE:
        # Admin-comped / manually upgraded: a real customer signal even
        # without a subscription id or a payment row.
        return f"on non-Free plan {plan.plan!r}"
    if BillingPayment.objects.filter(user_id=user.pk).exists():
        return "has BillingPayment rows"
    if TeamMembership.objects.filter(account_id=user.pk, revoked_at__isnull=True).exists():
        return "has live team members"
    if TeamMembership.objects.filter(user_id=user.pk, revoked_at__isnull=True).exists():
        return "is a member of another team"
    if AccountOwnership.objects.filter(owner_user_id=user.pk).exists() or AccountOwnership.objects.filter(
        account_user_id=user.pk
    ).exists():
        return "has workspace ownership rows"
    return None


class Command(BaseCommand):
    help = "Erase e2e throwaway signups (dry-run by default; --apply to delete)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Actually delete (default: dry run).")
        parser.add_argument("--prefix", default=None,
                            help=f"Local-part prefix of e2e signup addresses (default {DEFAULT_PREFIX!r}). "
                                 "Unscoped (testing/e2e settings) only; forbidden with --qa-account.")
        parser.add_argument("--hours", type=int, default=DEFAULT_HOURS,
                            help=f"Only accounts created within this many hours (default {DEFAULT_HOURS}, max 48).")
        parser.add_argument("--qa-account", default="",
                            help="Email of the QA account that owns the per-run signup inboxes. MANDATORY "
                                 "unless settings are config.settings.testing/e2e. A candidate's address "
                                 "must be one of that account's inboxes (live or soft-deleted).")

    def handle(self, *args, **options):
        qa_email = options["qa_account"].strip().lower()
        if not qa_email and not unscoped_allowed():
            raise CommandError(
                f"--qa-account is required under settings {sorted(settings_modules()) or ['<unknown>']}: "
                f"only {sorted(UNSCOPED_SETTINGS)} may run unscoped"
            )
        if qa_email and options["prefix"] is not None:
            raise CommandError("--prefix is not allowed with --qa-account (scoped mode uses the default "
                               f"{DEFAULT_PREFIX!r} pattern)")
        prefix = (options["prefix"] if options["prefix"] is not None else DEFAULT_PREFIX).strip().lower()
        hours = options["hours"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,30}-", prefix):
            raise CommandError("--prefix must be 2-31 chars of [a-z0-9_-], start alphanumeric, and end with '-'")
        if not 1 <= hours <= DEFAULT_HOURS:
            raise CommandError(f"--hours must be between 1 and {DEFAULT_HOURS}")
        mail_domain = settings.MAIL_DOMAIN.lower()
        pattern = re.compile(rf"^{re.escape(prefix)}[a-z0-9_-]+@{re.escape(mail_domain)}$")
        cutoff = timezone.now() - timedelta(hours=hours)

        qa_addresses = None
        if qa_email:
            qa_account = Account.objects.filter(email_normalized=qa_email).first()
            if qa_account is None:
                raise CommandError(f"QA account {qa_email!r} not found")
            # Every inbox the QA account owns, live or soft-deleted, as a full
            # address (Inbox.address resolves shared vs custom domains).
            qa_addresses = {
                inbox.address.lower()
                for inbox in Inbox.objects.filter(user_id=qa_account.pk).select_related("domain")
            }
            if pattern.fullmatch(qa_email) or qa_email in qa_addresses:
                raise CommandError(
                    f"QA account {qa_email!r} itself looks like an e2e throwaway signup; refusing to run"
                )

        rows = (
            Account.objects.select_related("user")
            .filter(email_normalized__startswith=prefix,
                    email_normalized__endswith="@" + mail_domain,
                    created_at__gte=cutoff)
            .order_by("created_at", "pk")
        )
        candidates = []
        for account in rows:
            email = account.email_normalized
            if not pattern.fullmatch(email):
                continue
            if qa_addresses is not None and email not in qa_addresses:
                self.stdout.write(f"skip {email}: not an inbox of the QA account")
                continue
            candidates.append(account)

        mode = "APPLY" if options["apply"] else "DRY-RUN"
        self.stdout.write(
            f"e2e_cleanup {mode}: pattern={pattern.pattern} created_after={cutoff.isoformat()} "
            f"candidates={len(candidates)}"
        )
        refused = []
        for account in candidates:
            reason = refusal_reason(account.user)
            line = f"  user_pk={account.pk} email={account.email_normalized} created_at={account.created_at.isoformat()}"
            if reason:
                refused.append((account, reason))
                line += f" REFUSED ({reason})"
            self.stdout.write(line)
        if refused:
            raise CommandError(
                f"refusing: {len(refused)} candidate(s) are not plain throwaway accounts; nothing deleted"
            )
        if not options["apply"]:
            self.stdout.write("dry run: nothing deleted (re-run with --apply)")
            return

        deleted = 0
        with transaction.atomic():
            for account in candidates:
                locked = Account.objects.select_for_update().filter(pk=account.pk).first()
                if locked is None:
                    continue
                # Re-check under the locks with FRESH rows: the User is
                # re-fetched (is_staff/is_superuser may have changed since the
                # select_related above) and the AccountPlan row is locked, so a
                # concurrent subscription/plan write serialises with us.
                user = get_user_model().objects.select_for_update().filter(pk=account.pk).first()
                if user is None:
                    continue
                AccountPlan.objects.select_for_update().filter(user_id=account.pk).first()
                reason = refusal_reason(user)
                if reason:
                    raise CommandError(f"user_pk={account.pk} became non-deletable ({reason}); rolled back")
                self.stdout.write(f"  erasing user_pk={account.pk} {account.email_normalized}")
                erase_user(account.pk, self.stdout.write)
                deleted += 1
        self.stdout.write(f"e2e_cleanup: deleted {deleted} account(s)")
