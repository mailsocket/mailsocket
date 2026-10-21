"""manage.py funnel_report — signups and activations per source (READ-ONLY).

  manage.py funnel_report --since 2026-10-13                       # 7-day window, every source
  manage.py funnel_report --since 2026-10-13 --tags hn,mcp,free-for-dev,tensorblock   # + verdict
  manage.py funnel_report --since 2026-10-13 --tags mcp,free-for-dev --channel-days free-for-dev=45
  manage.py funnel_report --since 2026-10-13 --north-star --json

Counts only (never emails, names or ids). Definitions: apps.accounts.funnel.
Verdict (per channel PASS / ITERATE / KILL / PROVISIONAL; overall also
NEXT_CHANNEL, KILL only when product KILL/PIVOT is ready), one clock per
channel: apps.accounts.funnel_verdict. Directory PR tags (free-for-dev,
tensorblock) default to a 30-day window, every other tag to 7 days; --days
never changes a verdict clock, only --channel-days does.
"""

import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.attribution import sanitize_ref
from apps.accounts.funnel import (
    DEFAULT_PASS_ACTIVATIONS,
    DEFAULT_PASS_SIGNUPS,
    build_report,
    render_text,
)


def _parse_since(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CommandError(f"--since must be YYYY-MM-DD, got {value!r}") from None


def _parse_tags(value):
    if value is None:
        return None
    tags = []
    for raw in value.split(","):
        if not raw.strip():
            continue
        tag = sanitize_ref(raw)
        if tag is None:
            raise CommandError(f"--tags: invalid tag {raw.strip()!r} (allowed: a-z 0-9 _ -)")
        tags.append(tag)
    if not tags:
        raise CommandError("--tags needs at least one tag")
    return tags


def _parse_channel_days(value):
    """``tag=N[,tag=N]`` -> {tag: N}; N between 1 and 366."""
    if value is None:
        return None
    out = {}
    for raw in value.split(","):
        tag, sep, days = raw.strip().partition("=")
        clean = sanitize_ref(tag)
        if not sep or clean is None or not days.strip().isdigit() or not 1 <= int(days) <= 366:
            raise CommandError(f"--channel-days: expected tag=DAYS (1-366), got {raw.strip()!r}")
        out[clean] = int(days)
    return out


class Command(BaseCommand):
    help = (
        "Read-only signup -> activation funnel per signup source, with a per-channel "
        "PASS / ITERATE / KILL / PROVISIONAL verdict for --tags (overall NEXT_CHANNEL when a "
        "channel failed but product KILL/PIVOT is not ready)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--since", required=True, help="window start, YYYY-MM-DD (UTC)")
        parser.add_argument("--days", type=int, default=7, help="window length in days (default 7)")
        parser.add_argument("--tags", help="comma-separated ?ref= tags; limits rows and adds a verdict")
        parser.add_argument("--pass-signups", type=int, default=DEFAULT_PASS_SIGNUPS)
        parser.add_argument("--pass-activations", type=int, default=DEFAULT_PASS_ACTIVATIONS)
        parser.add_argument("--north-star", action="store_true", help="also print the last 8 ISO weeks")
        parser.add_argument(
            "--channel-days",
            help="per-tag verdict window, tag=DAYS[,tag=DAYS] (default: 30 for free-for-dev and "
            "tensorblock, 7 for every other tag; independent of --days)",
        )
        parser.add_argument("--json", action="store_true", help="machine-readable output")

    def handle(self, *args, **options):
        if not 1 <= options["days"] <= 366:
            raise CommandError("--days must be between 1 and 366")
        if options["pass_signups"] < 0 or options["pass_activations"] < 0:
            raise CommandError("--pass-signups / --pass-activations must be >= 0")
        report = build_report(
            _parse_since(options["since"]),
            options["days"],
            _parse_tags(options["tags"]),
            pass_signups=options["pass_signups"],
            pass_activations=options["pass_activations"],
            north_star=options["north_star"],
            channel_days=_parse_channel_days(options["channel_days"]),
        )
        if options["json"]:
            self.stdout.write(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        else:
            self.stdout.write(render_text(report), ending="")
