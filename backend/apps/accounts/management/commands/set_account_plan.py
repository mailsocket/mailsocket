from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import Account, AccountPlan


class Command(BaseCommand):
    help = (
        "Manually set an account's billing plan (and optionally status) — the "
        "auditable path for onboarding a Scale (or any) customer before "
        "self-serve checkout exists. Resolves the account by its normalized "
        "email."
    )

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Account email (Account.email_normalized).")
        parser.add_argument(
            "--plan",
            required=True,
            choices=AccountPlan.Plan.values,
            help=f"New plan. One of: {', '.join(AccountPlan.Plan.values)}.",
        )
        parser.add_argument(
            "--status",
            required=False,
            choices=AccountPlan.Status.values,
            help=(
                "Optional new status (defaults to leaving the current status "
                f"unchanged). One of: {', '.join(AccountPlan.Status.values)}."
            ),
        )

    def handle(self, *args, **options):
        email = options["email"].strip().lower()
        new_plan = options["plan"]
        new_status = options.get("status")

        with transaction.atomic():
            try:
                account = Account.objects.select_related("user__account_plan").get(
                    email_normalized=email
                )
            except Account.DoesNotExist as exc:
                raise CommandError(f"No account found for email {email!r}") from exc

            try:
                plan = AccountPlan.objects.select_for_update().get(user=account.user)
            except AccountPlan.DoesNotExist as exc:
                raise CommandError(
                    f"Account {email!r} has no AccountPlan row (data integrity issue)"
                ) from exc

            old_plan, old_status = plan.plan, plan.status
            plan.plan = new_plan
            update_fields = ["plan", "updated_at"]
            if new_status is not None:
                plan.status = new_status
                update_fields.append("status")
            plan.save(update_fields=update_fields)

        self.stdout.write(
            f"account={email} plan: {old_plan} -> {plan.plan}"
            + (f", status: {old_status} -> {plan.status}" if new_status is not None else "")
        )
