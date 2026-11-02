"""Read-only 24-hour verification sender diversity signal, computed from retained messages."""

import logging
from collections import Counter, defaultdict
from datetime import timedelta
from email.utils import parseaddr
from urllib.parse import urlsplit

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from apps.domains.models import Domain
from apps.inboxes.models import Message
from apps.ops import notify as ops_notify

from .funnel import excluded_accounts_q
from .models import Account

# Built-in set of common second-level public suffixes (no PSL dependency):
# unknown multi-label suffixes fall back to the final label. This is a signal

MULTI_SUFFIXES = frozenset(
    {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk",
        "com.au", "net.au", "org.au", "edu.au",
        "co.nz", "org.nz",
        "co.jp", "ne.jp", "or.jp", "ac.jp",
        "com.br", "net.br", "org.br",
        "com.mx", "com.ar",
        "co.in", "net.in", "org.in",
        "com.cn", "net.cn", "org.cn",
        "com.hk", "com.sg", "com.tw",
        "co.kr", "or.kr",
        "co.za", "com.tr",
        "com.vn", "net.vn", "org.vn", "edu.vn", "gov.vn",
        "co.id", "com.my", "com.ph", "com.pk", "com.ua", "com.pl", "com.ru",
    }
)  # fmt: skip

FLEET_CACHE_KEY = "fleet_many_senders"
FLEET_CACHE_SECONDS = 300
logger = logging.getLogger(__name__)


def _valid_label(label):
    return (
        0 < len(label) <= 63
        and label[0] != "-"
        and label[-1] != "-"
        and all(char.isascii() and (char.isalnum() or char == "-") for char in label)
    )


def normalize_domain(value):
    if not isinstance(value, str):
        return None
    try:
        name = value.strip().rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    labels = name.split(".")
    if not 3 <= len(name) <= 253 or len(labels) < 2:
        return None
    return name if all(_valid_label(label) for label in labels) else None


def registrable_domain(name):
    name = normalize_domain(name)
    if not name:
        return None
    labels = name.split(".")
    count = 3 if ".".join(labels[-2:]) in MULTI_SUFFIXES else 2
    if len(labels) < count:
        return None
    return ".".join(labels[-count:])


def sender_domain(value):
    host = sender_host(value)
    return registrable_domain(host) if host else None


def sender_host(value):
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        return None
    _display, address = parseaddr(value)
    if address.count("@") != 1 or not address.split("@", 1)[0]:
        return None
    return normalize_domain(address.rsplit("@", 1)[1])


def _matches(name, excluded):
    return any(name == item or name.endswith("." + item) for item in excluded)


def _configured_domains(values):
    return {name for value in values if (name := normalize_domain(value))}


def _own_domains():
    own = defaultdict(set)
    for user_id, name in Domain.objects.filter(status=Domain.Status.VERIFIED).values_list("user_id", "name"):
        if clean := normalize_domain(name):
            own[user_id].add(clean)
    return own


def _our_domains():
    our = _configured_domains((settings.MAIL_DOMAIN, settings.APEX_HOST, settings.DASHBOARD_HOST))
    public_host = urlsplit(getattr(settings, "PUBLIC_BASE_URL", "")).hostname
    if clean := normalize_domain(public_host):
        our.add(clean)
    return our


def _count_senders(now, hours):
    own = _own_domains()
    shared = _configured_domains(settings.ABUSE_SENDER_ALLOWLIST) | _our_domains()
    # Exclusion Q stays in SQL; no materialization of all account ids.
    eligible = Account.objects.exclude(excluded_accounts_q()).values_list("user_id", flat=True)
    senders = defaultdict(Counter)
    rows = (
        Message.objects.filter(received_at__gte=now - timedelta(hours=hours), received_at__lte=now)
        .filter(Q(otp__isnull=False) | Q(magic_link__isnull=False))
        .filter(inbox__user_id__in=eligible)
        .values_list("inbox__user_id", "header_from", "envelope_from")
        .iterator(chunk_size=2000)
    )
    for user_id, header, envelope in rows:
        host = sender_host(header) or sender_host(envelope)
        domain = registrable_domain(host) if host else None
        if domain and not _matches(host, own[user_id] | shared) and not _matches(domain, shared):
            senders[user_id][domain] += 1
    return senders


def _row(user_id, counts, plan, watched):
    distinct = len(counts)
    watched_count = len(counts.keys() & watched)
    top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:5]
    return {
        "account_id": user_id,
        "plan": plan or "unknown",
        "distinct_senders": distinct,
        "watched_domains": watched_count,
        "top_domains": [name for name, _count in top],
        "tripped": distinct >= settings.ABUSE_DISTINCT_SENDERS_WARN
        or watched_count >= settings.ABUSE_WATCHED_SENDERS_WARN,
    }


def report(now=None, *, hours=24):
    """One bounded message query, plus account/domain metadata queries.

    Return accounts with at least one qualifying domain; tripped marks the
    threshold. No raw sender addresses or message content leave this module.
    """
    senders = _count_senders(now or timezone.now(), hours)
    watched = {registrable_domain(name) for name in settings.ABUSE_WATCHED_SENDER_DOMAINS}
    plans = dict(
        Account.objects.filter(user_id__in=senders).values_list("user_id", "user__account_plan__plan")
    )
    result = [_row(uid, counts, plans.get(uid), watched) for uid, counts in senders.items()]
    return sorted(result, key=lambda row: (-row["distinct_senders"], row["account_id"]))


def cached_tripped_rows(now):
    """Tripped rows for the fleet page, cached 300 s; [] (logged) if the report raises."""
    rows = cache.get(FLEET_CACHE_KEY)
    if rows is not None:
        return rows
    try:
        rows = [row for row in report(now) if row["tripped"]]
    except Exception:
        logger.exception("fleet sender diversity report failed")
        return []
    cache.set(FLEET_CACHE_KEY, rows, FLEET_CACHE_SECONDS)
    return rows


def _due(now):
    from .abuse_alerts import _claim

    due = []
    for row in report(now):
        if row["tripped"]:
            # AbuseAlert.kind is varchar(32); bigint ids can use all 19 digits.
            kind = f"many_senders:{row['account_id']}"
            if (previous := _claim(kind, now)) is not None:
                due.append((kind, previous, row))
    return due


def _compose(batch):
    lines = [
        f"Account {row['account_id']} | plan {row['plan']} | distinct {row['distinct_senders']} "
        f"| watched {row['watched_domains']} | top {', '.join(row['top_domains'])} "
        f"| https://{settings.DASHBOARD_HOST}/admin/accounts/account/{row['account_id']}/"
        for _kind, _previous, row in batch
    ]
    title = (
        "Accounts with many verification senders"
        if len(batch) > 1
        else "Account with many verification senders"
    )
    body = f"{len(batch)} accounts; alert only, manual review.\n" + "\n".join(
        f"Account {row['account_id']} plan {row['plan']} distinct {row['distinct_senders']} "
        f"watched {row['watched_domains']}"
        for _kind, _previous, row in batch
    )
    return title, body, body + "\n" + "\n".join(lines)


def send_alerts(now):
    """Claim each account/day before notifying; group >10 due accounts."""
    due = _due(now)
    batches = [due] if len(due) > 10 else [[item] for item in due]
    delivered = 0
    for batch in batches:
        title, body, email_body = _compose(batch)
        try:
            result = ops_notify.notify("warn", title, body, email_body=email_body)
            if ops_notify.failed(result):
                raise RuntimeError("sender diversity alert delivery failed")
        except Exception:
            from .abuse_alerts import _unclaim

            for kind, previous, _item in batch:
                try:
                    _unclaim(kind, now, previous)
                except Exception:
                    logger.exception("sender diversity alert unclaim failed")
            continue
        delivered += 1
    return delivered
