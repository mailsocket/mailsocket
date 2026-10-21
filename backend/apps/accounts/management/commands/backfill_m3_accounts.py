from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import Account, AccountPlan
from apps.accounts.services import create_product_user, normalize_email
from apps.inboxes.models import Inbox


class Command(BaseCommand):
    help = "Create missing M3 accounts/free plans and atomically assign every null-owned Inbox."

    def add_arguments(self, parser):
        owners = parser.add_mutually_exclusive_group(required=True)
        owners.add_argument("--owner-user-id")
        owners.add_argument("--create-quarantine-owner")

    def _preflight_accounts(self):
        User = get_user_model()
        normalized_owners = {}
        rows = []
        for user in User.objects.order_by("pk"):
            try:
                normalized = normalize_email(user.email)
            except ValidationError as exc:
                raise CommandError(f"User id={user.pk} has an absent or invalid email.") from exc
            other = normalized_owners.get(normalized)
            if other is not None:
                raise CommandError(
                    f"Normalized email collision for user ids={other},{user.pk}."
                )
            normalized_owners[normalized] = user.pk
            rows.append((user, normalized))

        existing = dict(Account.objects.values_list("user_id", "email_normalized"))
        for user, normalized in rows:
            if user.pk in existing and existing[user.pk] != normalized:
                raise CommandError(f"Account email mismatch for user id={user.pk}.")
        return rows

    def _resolve_owner(self, options):
        User = get_user_model()
        owner_id = options.get("owner_user_id")
        if owner_id is not None:
            try:
                return User.objects.get(pk=owner_id)
            except (User.DoesNotExist, ValueError) as exc:
                raise CommandError(f"Owner user id={owner_id} does not exist.") from exc

        email = options["create_quarantine_owner"]
        try:
            normalized = normalize_email(email)
        except ValidationError as exc:
            raise CommandError("Quarantine owner email is invalid.") from exc
        if Account.objects.filter(email_normalized=normalized).exists() or User.objects.filter(
            email__iexact=normalized
        ).exists():
            raise CommandError("Quarantine owner email is already in use.")
        return create_product_user(normalized, None, is_active=False)

    def handle(self, *args, **options):
        with transaction.atomic():
            account_rows = self._preflight_accounts()
            owner = self._resolve_owner(options)
            if not any(user.pk == owner.pk for user, _ in account_rows):
                account_rows.append((owner, normalize_email(owner.email)))

            created_accounts = 0
            created_plans = 0
            for user, normalized in account_rows:
                _, created = Account.objects.get_or_create(
                    user=user, defaults={"email_normalized": normalized}
                )
                _, plan_created = AccountPlan.objects.get_or_create(user=user)
                created_accounts += int(created)
                created_plans += int(plan_created)

            inboxes = list(
                # .only() is load-bearing, NOT an optimization: this command must
                # run against historical schema states (test_ownership.py migrates
                # to pre-BYOD CONTRACT state where inboxes_inbox.domain_id does not
                # exist). Selecting only the two columns actually used below (pk,
                # public_id) keeps the query valid across all migration states.
                Inbox.objects.select_for_update()
                .only("pk", "public_id")
                .filter(user__isnull=True)
                .order_by("pk")
            )
            inbox_ids = [inbox.public_id for inbox in inboxes]
            updated = Inbox.objects.filter(pk__in=[inbox.pk for inbox in inboxes]).update(user=owner)
            remaining = Inbox.objects.filter(user__isnull=True).count()
            if remaining:
                raise CommandError(f"Backfill incomplete: null_owned_remaining={remaining}")

        self.stdout.write(
            f"owner_user_id={owner.pk} accounts_created={created_accounts} "
            f"plans_created={created_plans} "
            f"inboxes_assigned={updated} null_owned_remaining=0"
        )
        self.stdout.write("inbox_public_ids=" + (",".join(inbox_ids) if inbox_ids else "none"))
