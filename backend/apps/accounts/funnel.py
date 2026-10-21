"Public API."

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from apps.inboxes.models import Inbox, Message

from . import funnel_extras, funnel_verdict
from .activity import SYSTEM_SENDERS, system_sender_domains
from .models import Account, AccountWeeklyActivity

ACTIVATION_DAYS = 7
DEFAULT_PASS_SIGNUPS = 30
DEFAULT_PASS_ACTIVATIONS = 10
NORTH_STAR_WEEKS = 8


@contextmanager
def read_only_snapshot():
    """Run the block in a READ ONLY transaction that is always rolled back.

    Works both outside a transaction (management command) and nested in one
    (tests): the savepoint rollback also reverts SET TRANSACTION READ ONLY.
    """
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
        try:
            yield
        finally:
            transaction.set_rollback(True)


def _glob_q(pattern: str) -> Q:
    pattern = pattern.strip().lower()
    if pattern.count("*") > 1:
        raise ValueError(f"funnel exclusion glob may hold at most one '*': {pattern!r}")
    if "*" not in pattern:
        return Q(email_normalized=pattern)
    prefix, suffix = pattern.split("*")
    return Q(email_normalized__startswith=prefix, email_normalized__endswith=suffix)


def excluded_accounts_q() -> Q:
    """Q matching every Account the funnel never counts (see module doc)."""
    from .services import DEMO_USER_EMAIL

    q = Q(user__is_staff=True) | Q(user__is_superuser=True)
    q |= Q(email_normalized=DEMO_USER_EMAIL.lower())
    q |= Q(email_normalized__endswith="@" + settings.WORKSPACE_ACCOUNT_DOMAIN.lower())
    emails = [email.strip().lower() for email in settings.FUNNEL_EXCLUDED_EMAILS if email.strip()]
    if emails:
        q |= Q(email_normalized__in=emails)
    for pattern in settings.FUNNEL_EXCLUDED_EMAIL_GLOBS:
        q |= _glob_q(pattern)
    return q


def counted_accounts():
    return Account.objects.exclude(excluded_accounts_q())


def source_of(src_ref, src_referrer_host) -> str:
    if src_ref:
        return src_ref
    if src_referrer_host:
        return f"referrer:{src_referrer_host}"
    return "direct"


def _external_messages():
    """Same predicate as activity.EXTERNAL_SQL, in the ORM."""
    system = Q(envelope_from__iregex=_system_sender_regex())
    for domain in system_sender_domains():
        system |= Q(envelope_from__iendswith="@" + domain) | Q(envelope_from__iendswith="." + domain)
    return Message.objects.exclude(system)


def _system_sender_regex() -> str:
    return "^(" + "|".join(sender.replace(".", r"\.") for sender in SYSTEM_SENDERS) + ")$"


def _first_times(rows) -> dict[int, list[datetime]]:
    out: dict[int, list[datetime]] = defaultdict(list)
    for user_id, stamp in rows:
        out[user_id].append(stamp)
    return out


def _any_within(stamps, start, end) -> bool:
    return any(start <= stamp < end for stamp in stamps)


@dataclass
class SourceRow:
    source: str
    signups: int = 0
    verified: int = 0
    with_inbox: int = 0
    activated: int = 0
    activation_open: int = 0
    retained: int = 0
    pricing_viewed: int = 0
    checkout_started: int = 0


@dataclass
class Report:
    since: str
    until: str
    days: int
    activation_days: int
    generated_at: str
    rows: list[SourceRow] = field(default_factory=list)
    totals: SourceRow = field(default_factory=lambda: SourceRow("TOTAL"))
    tags: list[str] | None = None
    verdict: dict | None = None
    north_star: list[dict] | None = None
    demo_started: dict | None = None
    time_to_first_mail: dict | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def window_bounds(since: date, days: int) -> tuple[datetime, datetime]:
    start = datetime.combine(since, time.min, tzinfo=dt_timezone.utc)
    return start, start + timedelta(days=days)


def _signups(start, end, tags):
    qs = counted_accounts().filter(created_at__gte=start, created_at__lt=end)
    accounts = list(
        qs.values_list("user_id", "created_at", "email_verified_at", "src_ref", "src_referrer_host")
    )
    if tags is not None:
        accounts = [row for row in accounts if source_of(row[3], row[4]) in tags]
    return accounts


def _activation_signals(user_ids, start, end):
    """(inbox times, message times) per user, over [start, end + ACTIVATION_DAYS)."""
    horizon = end + timedelta(days=ACTIVATION_DAYS)
    inbox_rows = Inbox.objects.filter(
        user_id__in=user_ids, created_at__gte=start, created_at__lt=horizon
    ).values_list("user_id", "created_at")
    activity_rows = AccountWeeklyActivity.objects.filter(
        user_id__in=user_ids, first_message_at__gte=start, first_message_at__lt=horizon
    ).values_list("user_id", "first_message_at")
    message_rows = (
        _external_messages()
        .filter(inbox__user_id__in=user_ids, received_at__gte=start, received_at__lt=horizon)
        .values_list("inbox__user_id", "received_at")
    )
    messages = _first_times(activity_rows)
    for user_id, stamps in _first_times(message_rows).items():
        messages[user_id].extend(stamps)
    return _first_times(inbox_rows), messages


def _activation(account, inboxes, messages) -> tuple[bool, bool]:
    """(has_inbox, activated) within ACTIVATION_DAYS of the account's signup."""
    user_id, created_at = account[0], account[1]
    deadline = created_at + timedelta(days=ACTIVATION_DAYS)
    has_inbox = _any_within(inboxes.get(user_id, ()), created_at, deadline)
    has_message = _any_within(messages.get(user_id, ()), created_at, deadline)
    return has_inbox, has_inbox and has_message


def _add_account(row: SourceRow, account, inboxes, messages, now, signals):
    created_at, verified_at = account[1], account[2]
    deadline = created_at + timedelta(days=ACTIVATION_DAYS)
    has_inbox, activated = _activation(account, inboxes, messages)
    row.signups += 1
    row.verified += verified_at is not None
    row.with_inbox += has_inbox
    row.activated += activated
    row.activation_open += (not activated) and deadline > now
    return funnel_extras.add_extras(row, account, activated, signals)


def _sum_rows(rows) -> SourceRow:
    total = SourceRow("TOTAL")
    for row in rows:
        for name in COLUMNS[1:]:
            setattr(total, name, getattr(total, name) + getattr(row, name))
    return total


def _channel_counts(tag, start, end) -> tuple[int, int]:
    """(signups, activated) of one tag over its OWN window [start, end)."""
    accounts = _signups(start, end, [tag])
    inboxes, messages = _activation_signals([row[0] for row in accounts], start, end)
    return len(accounts), sum(_activation(account, inboxes, messages)[1] for account in accounts)


def _verdict(tags, start, now, channel_days, pass_signups, pass_activations) -> dict:
    """Per-channel clocks + the IDEA.md decision rule (apps.accounts.funnel_verdict)."""
    channels = [
        funnel_verdict.judge_channel(
            tag,
            _channel_counts,
            start=start,
            days=funnel_verdict.channel_days_for(tag, channel_days),
            activation_days=ACTIVATION_DAYS,
            now=now,
            pass_signups=pass_signups,
            pass_activations=pass_activations,
        )
        for tag in tags
    ]
    return funnel_verdict.verdict(channels, pass_signups, pass_activations)


def build_report(
    since: date,
    days: int = 7,
    tags=None,
    *,
    pass_signups=DEFAULT_PASS_SIGNUPS,
    pass_activations=DEFAULT_PASS_ACTIVATIONS,
    north_star=False,
    channel_days=None,
    now=None,
) -> Report:
    now = now or timezone.now()
    start, end = window_bounds(since, days)
    tag_list = sorted(set(tags)) if tags is not None else None
    with read_only_snapshot():
        accounts = _signups(start, end, tag_list)
        user_ids = [row[0] for row in accounts]
        inboxes, messages = _activation_signals(user_ids, start, end)
        signals = funnel_extras.Signals(user_ids, _external_messages())
        by_source: dict[str, SourceRow] = {tag: SourceRow(tag) for tag in tag_list or ()}
        hours = []
        for account in accounts:
            source = source_of(account[3], account[4])
            row = by_source.setdefault(source, SourceRow(source))
            hours.append(_add_account(row, account, inboxes, messages, now, signals))
        stars = weekly_activated(now=now) if north_star else None
        demo = funnel_extras.demo_started(start, end)
        verdict = None
        if tag_list is not None:
            verdict = _verdict(tag_list, start, now, channel_days, pass_signups, pass_activations)
    rows = sorted(by_source.values(), key=lambda row: (-row.signups, row.source))
    report = Report(
        since=start.date().isoformat(),
        until=end.date().isoformat(),
        days=days,
        activation_days=ACTIVATION_DAYS,
        generated_at=now.astimezone(dt_timezone.utc).isoformat(timespec="seconds"),
        rows=rows,
        totals=_sum_rows(rows),
        tags=tag_list,
        north_star=stars,
        demo_started=demo,
        time_to_first_mail=funnel_extras.time_to_first_mail(hours),
    )
    report.verdict = verdict
    return report


def iso_week_start(value: datetime) -> date:
    day = value.astimezone(dt_timezone.utc).date()
    return day - timedelta(days=day.weekday())


def weekly_activated(*, now=None, weeks=NORTH_STAR_WEEKS) -> list[dict]:
    """North Star: distinct counted accounts with >= 1 external message per
    ISO week, for the last ``weeks`` weeks (the last one is the current,
    partial week). Caller provides the read-only transaction."""
    now = now or timezone.now()
    current = iso_week_start(now)
    first = current - timedelta(weeks=weeks - 1)
    first_dt = datetime.combine(first, time.min, tzinfo=dt_timezone.utc)
    counted = counted_accounts().values("user_id")
    pairs = set(
        AccountWeeklyActivity.objects.filter(week_start__gte=first, user_id__in=counted).values_list(
            "week_start", "user_id"
        )
    )
    for user_id, stamp in (
        _external_messages()
        .filter(received_at__gte=first_dt, inbox__user_id__in=counted)
        .values_list("inbox__user_id", "received_at")
    ):
        pairs.add((iso_week_start(stamp), user_id))
    per_week: dict[date, int] = defaultdict(int)
    for week, _user in pairs:
        per_week[week] += 1
    out = []
    for offset in range(weeks):
        week = first + timedelta(weeks=offset)
        iso = week.isocalendar()
        out.append(
            {
                "week": f"{iso.year}-W{iso.week:02d}",
                "week_start": week.isoformat(),
                "activated_accounts": per_week.get(week, 0),
                "partial": week == current,
            }
        )
    return out


COLUMNS = (
    "source",
    "signups",
    "verified",
    "with_inbox",
    "activated",
    "activation_open",
    "retained",
    "pricing_viewed",
    "checkout_started",
)


def _render_table(report: Report) -> list[str]:
    body = [COLUMNS] + [tuple(str(getattr(row, col)) for col in COLUMNS) for row in report.rows]
    total = tuple(str(getattr(report.totals, col)) for col in COLUMNS)
    widths = [max(len(line[i]) for line in [*body, total]) for i in range(len(COLUMNS))]

    def fmt(line):
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(line)).rstrip()

    return [fmt(line) for line in body] + ["  ".join("-" * width for width in widths), fmt(total)]


def _render_north_star(weeks: list[dict]) -> list[str]:
    lines = ["North Star: weekly activated accounts (>= 1 external message in the ISO week)"]
    for week in weeks:
        mark = "  (partial, current week)" if week["partial"] else ""
        lines.append(f"  {week['week']}  {week['week_start']}  {week['activated_accounts']}{mark}")
    return lines


def render_text(report: Report) -> str:
    lines = [
        f"funnel_report window {report.since} .. {report.until} (UTC, end exclusive, {report.days} days); "
        f"activated = inbox + external message within {report.activation_days} days of signup",
        *_render_table(report),
        *funnel_extras.render_extras(report),
    ]
    if report.verdict is not None:
        lines.extend(funnel_verdict.render(report.verdict, report.tags or []))
    if report.north_star is not None:
        lines.extend(_render_north_star(report.north_star))
    return "\n".join(lines) + "\n"
