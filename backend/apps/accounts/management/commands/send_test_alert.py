from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.ops.notify import OK, notify


class Command(BaseCommand):
    help = (
        "Send one 'info' test alert through every ops channel (email + Telegram) "
        "and print the per-channel result (ok / disabled / error:<type>). Use "
        "after a deploy or a bot-token rotation (see operations notes 'Ops alerts')."
    )

    def handle(self, *args, **options):
        now = timezone.now()
        result = notify(
            "info",
            "Test alert",
            f"Test of the mailsocket ops alert channels at {now:%Y-%m-%d %H:%M} UTC. "
            "No action needed.",
        )
        for channel in ("email", "telegram"):
            self.stdout.write(f"{channel}: {result[channel]}")
        if OK not in result.values():
            raise CommandError("no channel delivered the test alert")
