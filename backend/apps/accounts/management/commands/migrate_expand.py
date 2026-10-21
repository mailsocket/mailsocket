from django.core.management.base import BaseCommand
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.executor import MigrationExecutor


class Command(BaseCommand):
    help = "Apply safe expand migrations while leaving inbox ownership gate/contract explicit."

    def add_arguments(self, parser):
        parser.add_argument("--database", default=DEFAULT_DB_ALIAS)
        parser.add_argument("--noinput", action="store_false", dest="interactive")

    def handle(self, *args, **options):
        connection = connections[options["database"]]
        executor = MigrationExecutor(connection)
        applied = executor.loader.applied_migrations
        applied_inbox = sorted(node for node in applied if node[0] == "inboxes")
        # Never reverse a partially or fully completed operator-run contract.
        # Fresh/pre-contract databases stay pinned at the safe expand migration.
        ownership_contract_applied = ("inboxes", "0003_m3a_ownership_contract") in applied
        if ownership_contract_applied:
            inbox_target = max(
                target for target in executor.loader.graph.leaf_nodes() if target[0] == "inboxes"
            )
        else:
            inbox_target = applied_inbox[-1] if applied_inbox else ("inboxes", "0001_initial")
        gated_apps = {"accounts", "webhooks", "outbound"}
        targets = [
            target
            for target in executor.loader.graph.leaf_nodes()
            if target[0] != "inboxes" and (target[0] not in gated_apps or ownership_contract_applied)
        ]
        if not ownership_contract_applied:
            targets.append(("accounts", "0002_pending_signup_credentials"))
        targets.append(inbox_target)
        executor.migrate(targets)
        self.stdout.write(self.style.SUCCESS("Safe expand migrations applied; inbox contract remains explicit."))
