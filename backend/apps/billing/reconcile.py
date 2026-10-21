"Public API."

import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.accounts.models import AccountPlan
from apps.ops import notify as ops_notify

from . import views as bviews
from .models import BillingPayment

logger = logging.getLogger(__name__)

EXIT_CLEAN, EXIT_MISMATCH, EXIT_ERROR = 0, 1, 2

PAGE_SIZE = 100  # LS maximum
MAX_PAGES = 50  # 5,000 subscriptions; more -> exit 2 (raise the cap on purpose)
PAGE_PAUSE_SECONDS = 0.3  # stays far below LS's 300 requests/minute
DEADLINE_SECONDS = 240  # whole LS read; each request is bounded by _ls_get's timeout
# A subscription LS (or we) touched this recently may have a webhook in
# flight: not compared this run (the next run will).
RECENT_GRACE = timedelta(minutes=15)
RENEWS_TOLERANCE = timedelta(hours=1)

INVOICE_DAYS_DEFAULT = 35
INVOICE_DAYS_MAX = 400
# A paid invoice this young may still have its payment webhook in flight: not
# reported as invoice_missing_locally yet (the next run will).
INVOICE_MISSING_GRACE = timedelta(hours=2)
# LS subscription-invoice statuses that mean money moved (a BillingPayment row is
# expected); pending / void never had a charge. Any other status is alerted.
INVOICE_MONEY_STATUSES = ("paid", "refunded", "partial_refund")
INVOICE_NO_MONEY_STATUSES = ("pending", "void")
PING_TIMEOUT_SECONDS = 10
ALERT_MAX_LINES = 40

# LS subscription status -> the AccountPlan.status our webhook leaves behind
# (apps.billing.views._apply_ls_event): cancelled keeps access to period end;
# past_due / unpaid (dunning) / paused are PAST_DUE. "expired" is handled
# separately (the plan must no longer hold the subscription).
LIVE_STATUS_TO_LOCAL = {
    "active": AccountPlan.Status.ACTIVE,
    "on_trial": AccountPlan.Status.ACTIVE,
    "cancelled": AccountPlan.Status.ACTIVE,
    "past_due": AccountPlan.Status.PAST_DUE,
    "unpaid": AccountPlan.Status.PAST_DUE,
    "paused": AccountPlan.Status.PAST_DUE,
}

_SITE_PRICE = re.compile(r"\$(\d{1,6})(?:\.(\d{2}))?/mo")

ALERT_FOOTER = (
    "",
    "Alert only: nothing was changed. Check each subscription in Lemon Squeezy and",
    "fix the AccountPlan in admin by hand if needed (operations notes 'Billing reconciliation').",
    "inv= lines: check the invoice in Lemon Squeezy; a missing payment row is added only",
    "by billing_backfill_payments --apply (operations notes 'Payment backfill').",
)


class ReconcileError(Exception):
    """The run cannot prove anything (LS down/slow/malformed, too many pages)."""


@dataclass(frozen=True)
class Mismatch:
    kind: str
    sub_id: str
    user_pk: object
    local: str
    ls: str
    key: str = "sub"  # "inv" for the subscription-invoice pass (sub_id is then the invoice id)

    def line(self):
        user = "-" if self.user_pk is None else self.user_pk
        return f"{self.kind} {self.key}={self.sub_id} user_pk={user} local={self.local} ls={self.ls}"


# --- Lemon Squeezy (read-only) ---------------------------------------------


def _ls_read(path):
    """One GET through the shared LS read helper; every failure -> ReconcileError.
    HTTPError is a URLError (and OSError) subclass: caught first."""
    try:
        return bviews._ls_get(path)
    except urllib.error.HTTPError as exc:
        raise ReconcileError(f"LS HTTP {exc.code} on {path.split('?')[0]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ReconcileError(f"LS unreachable ({type(exc).__name__})") from exc


# One LS list endpoint read by the strict pager: (resource path, plural label,
# singular label). The labels only shape error text ("malformed <plural> page").
SUBSCRIPTIONS = ("subscriptions", "subscriptions", "subscription")
INVOICES = ("subscription-invoices", "subscription-invoices", "subscription-invoice")


def _page_path(number, resource=SUBSCRIPTIONS[0]):
    query = urllib.parse.urlencode(
        {"filter[store_id]": settings.LS_STORE_ID, "page[number]": number, "page[size]": PAGE_SIZE}
    )
    return f"/{resource}?{query}"


def _page_int(page, key):
    """An int pagination field (bool is not a page number), else None."""
    value = page.get(key) if isinstance(page, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_page(body, label=SUBSCRIPTIONS[1]):
    """(items, meta.page) of one list page, or ReconcileError."""
    meta = body.get("meta") if isinstance(body, dict) else None
    page = meta.get("page") if isinstance(meta, dict) else None
    last = _page_int(page, "lastPage")
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or last is None or last < 1:
        raise ReconcileError(f"malformed {label} page")
    if "total" in page and _page_int(page, "total") is None:
        raise ReconcileError(f"malformed {label} page")
    return data, page


def _parse_item(item, label=SUBSCRIPTIONS[2]):
    """(object id, attributes); a malformed item fails the run (it could hide a mismatch)."""
    sub_id = item.get("id") if isinstance(item, dict) else None
    attrs = item.get("attributes") if isinstance(item, dict) else None
    if sub_id in (None, "") or not isinstance(attrs, dict):
        raise ReconcileError(f"malformed {label} item")
    return str(sub_id), attrs


def _check_pagination(page, number, pinned, empty):
    """Page N must say currentPage=N with page 1's lastPage and total (pinned), and
    no page before the last may be empty: a listing that shifts or shrinks while we
    read it could stop early or skip items, so it can never be reported clean."""
    seen = (_page_int(page, "currentPage"), page["lastPage"], page.get("total"))
    if seen != (number, *pinned):
        got, want = "/".join(map(str, seen)), "/".join(map(str, (number, *pinned)))
        raise ReconcileError(
            f"inconsistent pagination on page {number}: currentPage/lastPage/total={got}, expected {want}"
        )
    if empty and number < pinned[0]:
        raise ReconcileError(f"empty page {number} before lastPage {pinned[0]}")


def _read_page(number, pinned, spec=SUBSCRIPTIONS):
    """(items, pinned) of page ``number``; page 1 pins (lastPage, total)."""
    resource, plural, singular = spec
    data, page = _parse_page(_ls_read(_page_path(number, resource)), plural)
    if pinned is None:
        pinned = (page["lastPage"], page.get("total"))
        if pinned[0] > MAX_PAGES:
            raise ReconcileError(f"{pinned[0]} pages > MAX_PAGES {MAX_PAGES}")
    items = [_parse_item(item, singular) for item in data]
    _check_pagination(page, number, pinned, empty=not items)
    return items, pinned


def fetch_ls_list(spec, *, sleep=None, clock=None):
    """{object id: attributes} for every object of our store on one LS list
    endpoint, all pages. lastPage / total are pinned from page 1 (never re-read
    from a later page). Used for subscriptions and subscription-invoices."""
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    started = clock()
    objects, number, pinned = {}, 1, None
    while pinned is None or number <= pinned[0]:
        if clock() - started > DEADLINE_SECONDS:
            raise ReconcileError(f"deadline of {DEADLINE_SECONDS}s exceeded at page {number}")
        if number > 1:
            sleep(PAGE_PAUSE_SECONDS)
        items, pinned = _read_page(number, pinned, spec)
        objects.update(items)
        number += 1
    if pinned[1] is not None and len(objects) != pinned[1]:
        raise ReconcileError(f"LS listed {len(objects)} distinct {spec[1]}, page 1 said total={pinned[1]}")
    return objects


def fetch_ls_subscriptions(*, sleep=None, clock=None):
    """{subscription id: attributes} for every subscription of our store, all pages."""
    return fetch_ls_list(SUBSCRIPTIONS, sleep=sleep, clock=clock)


# --- comparison --------------------------------------------------------------


def _iso(dt):
    return dt.isoformat() if dt is not None else "none"


def _recent(dt, now):
    return dt is not None and dt > now - RECENT_GRACE


def _check_status(sub_id, attrs, plan):
    status = attrs.get("status")
    expected = LIVE_STATUS_TO_LOCAL.get(status)
    if expected is None:
        return [Mismatch("unknown_status", sub_id, plan.user_id, plan.status, f"status={status}")]
    if plan.status != expected:
        return [Mismatch("status", sub_id, plan.user_id, plan.status, f"status={status}")]
    return []


def _check_variant(sub_id, attrs, plan):
    variant = str(attrs.get("variant_id") or "")
    mapped = bviews._variant_map().get(variant)
    if mapped is None:
        return [Mismatch("unknown_variant", sub_id, plan.user_id, plan.plan, f"variant={variant}")]
    if mapped != plan.plan or variant != str(plan.stripe_price_id or ""):
        local = f"{plan.plan}/{plan.stripe_price_id or 'none'}"
        return [Mismatch("variant", sub_id, plan.user_id, local, f"{mapped}/{variant}")]
    return []


def _check_period(sub_id, attrs, plan):
    """Local current_period_end vs LS renews_at (ends_at once cancelled)."""
    key = "ends_at" if attrs.get("status") == "cancelled" else "renews_at"
    ls_dt = bviews._safe_parse_datetime(attrs.get(key))
    if ls_dt is None:
        return []  # paused / unpaid may carry none: nothing to compare
    local = plan.current_period_end
    if local is None or abs(local - ls_dt) > RENEWS_TOLERANCE:
        return [Mismatch("renews_at", sub_id, plan.user_id, _iso(local), f"{key}={_iso(ls_dt)}")]
    return []


def _compare_ls_sub(sub_id, attrs, plan):
    if attrs.get("status") == "expired":
        if plan is None:
            return []  # history: an ended subscription nobody holds
        return [
            Mismatch("expired_at_ls", sub_id, plan.user_id, f"{plan.plan}/{plan.status}", "status=expired")
        ]
    if plan is None:
        ls = (
            f"status={attrs.get('status')} variant={attrs.get('variant_id')}"
            f" customer={attrs.get('customer_id')}"
        )
        return [Mismatch("missing_local", sub_id, None, "no plan holds it", ls)]
    return (
        _check_status(sub_id, attrs, plan)
        + _check_variant(sub_id, attrs, plan)
        + _check_period(sub_id, attrs, plan)
    )


def _local_plans():
    rows = (
        AccountPlan.objects.exclude(stripe_subscription_id__isnull=True)
        .exclude(stripe_subscription_id="")
        .order_by("pk")
    )
    return {str(p.stripe_subscription_id): p for p in rows}


def reconcile(subs, now):
    """(mismatches, skipped_recent) for LS ``subs`` vs every AccountPlan that
    holds a subscription id. Manually provisioned plans without an id
    (Enterprise, operations notes) are not LS-billed and not compared."""
    plans = _local_plans()
    mismatches, skipped = [], 0
    for sub_id, attrs in subs.items():
        if _recent(bviews._safe_parse_datetime(attrs.get("updated_at")), now):
            skipped += 1
            continue
        mismatches += _compare_ls_sub(sub_id, attrs, plans.get(sub_id))
    for sub_id, plan in plans.items():
        if sub_id in subs:
            continue
        if _recent(plan.updated_at, now):
            skipped += 1
            continue
        local = f"{plan.plan}/{plan.status}"
        mismatches.append(Mismatch("local_only", sub_id, plan.user_id, local, "not listed by LS"))
    return sorted(mismatches, key=lambda m: (m.kind, m.sub_id)), skipped


def _site_price_cents(text):
    match = _SITE_PRICE.fullmatch(str(text or ""))
    if match is None:
        return None
    return int(match.group(1)) * 100 + int(match.group(2) or 0)


def check_prices():
    """BL-18: live LS variant price == the pricing page price, monthly."""
    from apps.dashboard.views import _pricing_context  # lazy: billing loads first

    site = {t["key"]: t["price"] for t in _pricing_context()["tiers"]}
    out = []
    for key, variant in bviews._plan_key_to_variant().items():
        try:
            cents, interval, count = bviews._variant_pricing(variant)
        except urllib.error.HTTPError as exc:
            raise ReconcileError(f"LS HTTP {exc.code} on /variants/{variant}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
            raise ReconcileError(f"LS variant {variant} unreadable ({type(exc).__name__})") from exc
        if _site_price_cents(site.get(key)) != cents or (interval, count) != ("month", 1):
            ls = f"variant={variant} {cents} cents/{interval}x{count}"
            out.append(Mismatch("price", "-", None, f"{key} site={site.get(key)}", ls))
    return out





def fetch_ls_invoices(*, sleep=None, clock=None):
    """{invoice id: attributes} for every subscription invoice of our store (all
    pages, the same strict pagination guard as the subscription walk)."""
    return fetch_ls_list(INVOICES, sleep=sleep, clock=clock)


def _invoice_created(inv_id, attrs):
    created = bviews._safe_parse_datetime(attrs.get("created_at"))
    if created is None:
        raise ReconcileError(f"malformed subscription-invoice {inv_id} (created_at)")
    return created


def _ls_money(inv_id, attrs):
    """(total cents, currency, refunded cents, fully refunded) of an LS invoice, the
    way the webhook stores them (_invoice_row_defaults / _record_refund). A
    malformed amount on a money invoice fails the run: it could hide a mismatch."""
    currency = str(attrs.get("currency") or "USD")[:8]
    total = bviews._strict_cents(attrs.get("total"))
    refunded = bviews._parse_refunded_cents(attrs, total)
    if total is None or refunded is None:
        raise ReconcileError(f"malformed subscription-invoice {inv_id} (total/refunded_amount)")
    fully = attrs.get("refunded") is True or attrs.get("status") == "refunded" or (0 < total <= refunded)
    if fully and not refunded:
        refunded = total  # fully refunded, amount omitted / 0 (same rule as the webhook)
    return total, currency, refunded, fully


def _invoice_ls_text(attrs, total, currency, refunded):
    return (
        f"status={attrs.get('status')} total={total} {currency} refunded={refunded}"
        f" sub={attrs.get('subscription_id') or '-'}"
    )


def _compare_invoice_row(inv_id, attrs, payment):
    """Mismatches between one LS money invoice and its local BillingPayment row."""
    total, currency, refunded, fully = _ls_money(inv_id, attrs)
    ls = _invoice_ls_text(attrs, total, currency, refunded)
    local = (
        f"total={payment.amount_cents} {payment.currency} refunded={payment.refunded_amount_cents}"
        f" fully={'yes' if payment.refunded else 'no'}"
    )
    out = []
    if (payment.amount_cents, payment.currency) != (total, currency):
        out.append(Mismatch("amount_mismatch", inv_id, payment.user_id, local, ls, key="inv"))
    if (payment.refunded_amount_cents, payment.refunded) != (refunded, fully):
        # LS shows more refund than we recorded -> the refund webhook was lost or
        # partial; anything else (we show a refund LS does not) -> status_mismatch.
        missing = refunded > payment.refunded_amount_cents or (fully and not payment.refunded)
        kind = "refund_not_recorded" if missing else "status_mismatch"
        out.append(Mismatch(kind, inv_id, payment.user_id, local, ls, key="inv"))
    return out


def _compare_invoice(inv_id, attrs, payment, plan_users, now):
    """Mismatches for one in-window LS invoice; None = skipped (too young)."""
    status = attrs.get("status")
    if status in INVOICE_NO_MONEY_STATUSES:
        # Fail closed on malformed money fields even when no money moved
        
        _ls_money(inv_id, attrs)
        if payment is None:
            return []  # no charge, no row: consistent
        local = f"row total={payment.amount_cents} {payment.currency}"
        return [Mismatch("status_mismatch", inv_id, payment.user_id, local, f"status={status}", key="inv")]
    if status not in INVOICE_MONEY_STATUSES:
        user = payment.user_id if payment is not None else None
        local = "row" if payment is not None else "no row"
        return [Mismatch("status_mismatch", inv_id, user, local, f"status={status}", key="inv")]
    if payment is not None:
        return _compare_invoice_row(inv_id, attrs, payment)
    if _invoice_created(inv_id, attrs) > now - INVOICE_MISSING_GRACE:
        return None
    total, currency, refunded, _fully = _ls_money(inv_id, attrs)
    user = plan_users.get(str(attrs.get("subscription_id") or ""))
    ls = _invoice_ls_text(attrs, total, currency, refunded)
    return [Mismatch("invoice_missing_locally", inv_id, user, "no BillingPayment row", ls, key="inv")]


def reconcile_invoices(invoices, now, days=INVOICE_DAYS_DEFAULT):
    """(mismatches, compared, skipped_recent) for LS ``invoices`` created in the
    last ``days`` days vs BillingPayment (matched by ls_invoice_id). Read-only."""
    since = now - timedelta(days=days)
    window = {i: a for i, a in invoices.items() if _invoice_created(i, a) >= since}
    payments = {p.ls_invoice_id: p for p in BillingPayment.objects.filter(ls_invoice_id__in=list(window))}
    plan_users = {sub: plan.user_id for sub, plan in _local_plans().items()}
    mismatches, skipped = [], 0
    for inv_id, attrs in window.items():
        if _recent(bviews._safe_parse_datetime(attrs.get("updated_at")), now):
            skipped += 1  # a payment / refund webhook may be in flight
            continue
        found = _compare_invoice(inv_id, attrs, payments.get(inv_id), plan_users, now)
        if found is None:
            skipped += 1
            continue
        mismatches += found
    return sorted(mismatches, key=lambda m: (m.kind, m.sub_id)), len(window), skipped


# --- alert + dead-man -------------------------------------------------------


def send_alert(mismatches):
    kinds = Counter(m.kind for m in mismatches)
    summary = ", ".join(f"{kind} x{n}" for kind, n in sorted(kinds.items()))
    title = f"Billing reconcile: {len(mismatches)} mismatch(es) — {summary}"
    lines = [m.line() for m in mismatches]
    shown = lines[:ALERT_MAX_LINES]
    if len(lines) > ALERT_MAX_LINES:
        shown.append(f"... and {len(lines) - ALERT_MAX_LINES} more (full list in the email and the cron log)")
    return ops_notify.notify(
        "money",
        title,
        "\n".join(shown + list(ALERT_FOOTER)),
        email_body="\n".join(lines + list(ALERT_FOOTER)),
    )


def ping_deadman(write):
    """GET HEALTHCHECKS_RECONCILE_URL once. Never raises; the URL is never printed."""
    url = getattr(settings, "HEALTHCHECKS_RECONCILE_URL", "")
    if not url:
        write("healthchecks: skipped (HEALTHCHECKS_RECONCILE_URL not set)")
        return "skipped"
    if not url.startswith("https://"):
        write("healthchecks: skipped (HEALTHCHECKS_RECONCILE_URL is not https)")
        return "invalid"
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "mailsocket-reconcile/1"})
    try:
        with urllib.request.urlopen(request, timeout=PING_TIMEOUT_SECONDS) as response:
            response.read(1024)
    except Exception as exc:  # noqa: BLE001 — a failed ping must not change the verdict
        write(f"healthchecks: ping failed ({type(exc).__name__})")
        return "error"
    write("healthchecks: ping ok")
    return "ok"


@dataclass(frozen=True)
class RunResult:
    subs: dict
    mismatches: list
    skipped: int
    invoices: int
    invoices_compared: int
    invoices_skipped: int


def _compare_all(now, invoice_days=INVOICE_DAYS_DEFAULT):
    subs = fetch_ls_subscriptions()
    mismatches, skipped = reconcile(subs, now)
    mismatches += check_prices()
    invoices = fetch_ls_invoices()
    inv_mismatches, compared, inv_skipped = reconcile_invoices(invoices, now, invoice_days)
    ordered = sorted(mismatches + inv_mismatches, key=lambda m: (m.kind, m.sub_id))
    return RunResult(subs, ordered, skipped, len(invoices), compared, inv_skipped)


def _valid_invoice_days(days):
    return isinstance(days, int) and not isinstance(days, bool) and 1 <= days <= INVOICE_DAYS_MAX


def _preflight_error(invoice_days):
    """The reason a run cannot start (printed after "ERROR: "), else None."""
    if not _valid_invoice_days(invoice_days):
        return f"--invoice-days must be 1..{INVOICE_DAYS_MAX}"
    if not settings.ENABLE_BILLING:
        return "ENABLE_BILLING is off"
    if not settings.LS_API_KEY:
        return "LS_API_KEY is not set"
    return None


def run(*, write, write_err, now=None, invoice_days=INVOICE_DAYS_DEFAULT):
    """One reconciliation run; returns the exit code (0 clean, 1 mismatches, 2 error)."""
    problem = _preflight_error(invoice_days)
    if problem is not None:
        write_err(f"billing_reconcile ERROR: {problem} — nothing was compared")
        return EXIT_ERROR

    try:
        result = _compare_all(now or timezone.now(), invoice_days)
    except ReconcileError as exc:
        write_err(f"billing_reconcile ERROR: {exc} — nothing was compared, no ping")
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 — never let an error look like exit 1 (mismatch)
        logger.exception("billing_reconcile_failed")
        write_err(f"billing_reconcile ERROR: {type(exc).__name__} — nothing was compared, no ping")
        return EXIT_ERROR
    mismatches = result.mismatches
    verdict = "MISMATCH" if mismatches else "clean"
    write(
        f"billing_reconcile {verdict} ls_subscriptions={len(result.subs)} mismatches={len(mismatches)}"
        f" skipped_recent={result.skipped}"
    )
    write(
        f"billing_reconcile invoices ls_invoices={result.invoices} in_window={result.invoices_compared}"
        f" days={invoice_days} skipped_recent={result.invoices_skipped}"
    )
    for mismatch in mismatches:
        write(mismatch.line())
    if mismatches:
        result = send_alert(mismatches)
        write(f"alert email={result.get('email')} telegram={result.get('telegram')}")
    ping_deadman(write)
    return EXIT_MISMATCH if mismatches else EXIT_CLEAN
