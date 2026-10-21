"Public API."

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from django.db.models import Max
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan, ApiKey, MonthlyUsage
from apps.accounts.services import AGENT_ACCOUNT_DOMAIN, AGENT_ACCOUNT_GC_IDLE_DAYS
from apps.inboxes.models import Inbox, Message


class Command(BaseCommand):
    help = (
        "Hard-delete zero-signup agent accounts (email @"
        + AGENT_ACCOUNT_DOMAIN
        + ") idle for --idle-days (no ApiKey.last_used_at in the window, and "
        "old enough that a never-used key isn't swept right after minting). "
        "Never touches a real (non-agent-domain) account. Standalone job only."
    )

    def add_arguments(self, parser):
        parser.add_argument("--idle-days", type=int, default=AGENT_ACCOUNT_GC_IDLE_DAYS)
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting anything.",
        )

    def handle(self, *args, **options):
        idle_days = options["idle_days"]
        batch_size = options["batch_size"]
        dry_run = options["dry_run"]
        if idle_days < 1:
            raise CommandError("--idle-days must be >= 1")
        if not 1 <= batch_size <= 5000:
            raise CommandError("--batch-size must be between 1 and 5000")

        User = get_user_model()
        cutoff = timezone.now() - timedelta(days=idle_days)

        try:
            candidate_ids = list(
                User.objects.filter(email__iendswith=f"@{AGENT_ACCOUNT_DOMAIN}")
                .annotate(last_key_use=Max("api_keys__last_used_at"))
                .filter(
                    # Idle: every key's last_used_at is before the cutoff...
                    last_key_use__lt=cutoff
                )
                .values_list("pk", flat=True)[:batch_size]
            )
            # Accounts with NO api key usage at all (last_key_use is NULL,
            # including accounts with zero keys) are idle only once the
            # ACCOUNT ITSELF predates the cutoff — never sweep a
            
            never_used_ids = list(
                User.objects.filter(email__iendswith=f"@{AGENT_ACCOUNT_DOMAIN}")
                .annotate(last_key_use=Max("api_keys__last_used_at"))
                .filter(last_key_use__isnull=True, date_joined__lt=cutoff)
                .values_list("pk", flat=True)[: max(0, batch_size - len(candidate_ids))]
            )
            user_ids = list(dict.fromkeys(candidate_ids + never_used_ids))
        except DatabaseError as exc:
            raise CommandError(f"gc_agent_accounts lookup failure: {type(exc).__name__}") from exc

        if not user_ids:
            self.stdout.write(f"gc_agent_accounts idle_days={idle_days} deleted_accounts=0")
            return

        if dry_run:
            self.stdout.write(
                f"gc_agent_accounts DRY-RUN idle_days={idle_days} "
                f"would_delete_accounts={len(user_ids)} user_ids={user_ids}"
            )
            return

        deleted_messages = 0
        deleted_keys = 0
        deleted_inboxes = 0
        deleted_usage = 0
        deleted_plans = 0
        deleted_accounts = 0
        deleted_users = 0

        try:
            # FK-safe order: Message -> ApiKey -> Inbox -> MonthlyUsage ->
            # AccountPlan -> Account -> User. Every step is scoped to
            # user_ids (the agent-domain set computed above) so a real
            # customer row is never in scope.
            inbox_ids = list(Inbox.objects.filter(user_id__in=user_ids).values_list("pk", flat=True))
            n, _ = Message.objects.filter(inbox_id__in=inbox_ids).delete()
            deleted_messages += n

            n, _ = ApiKey.objects.filter(user_id__in=user_ids).delete()
            deleted_keys += n

            n, _ = Inbox.objects.filter(pk__in=inbox_ids).delete()
            deleted_inboxes += n

            n, _ = MonthlyUsage.objects.filter(user_id__in=user_ids).delete()
            deleted_usage += n

            n, _ = AccountPlan.objects.filter(user_id__in=user_ids).delete()
            deleted_plans += n

            n, _ = Account.objects.filter(user_id__in=user_ids).delete()
            deleted_accounts += n

            n, _ = User.objects.filter(pk__in=user_ids).delete()
            deleted_users += n
        except DatabaseError as exc:
            raise CommandError(f"gc_agent_accounts delete failure: {type(exc).__name__}") from exc

        self.stdout.write(
            "gc_agent_accounts "
            f"idle_days={idle_days} deleted_users={deleted_users} "
            f"deleted_accounts={deleted_accounts} deleted_plans={deleted_plans} "
            f"deleted_usage={deleted_usage} deleted_inboxes={deleted_inboxes} "
            f"deleted_keys={deleted_keys} deleted_messages={deleted_messages}"
        )
