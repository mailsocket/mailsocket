"Public API."

import json

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.abuse_senders import report


class Command(BaseCommand):
    help = "Read-only per-account verification sender diversity report"

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=24)
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        hours = options["hours"]
        if not 1 <= hours <= 168:
            raise CommandError("--hours must be between 1 and 168")
        rows = report(hours=hours)
        if options["json"]:
            self.stdout.write(json.dumps(rows))
            return
        self.stdout.write(f"Verification sender domains (last {hours} h; alert only)")
        for row in rows:
            self.stdout.write(
                f"Account {row['account_id']} plan={row['plan']} "
                f"distinct={row['distinct_senders']} watched={row['watched_domains']} "
                f"top={','.join(row['top_domains'])} tripped={row['tripped']}"
            )
