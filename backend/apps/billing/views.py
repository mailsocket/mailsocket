"Public API."
import calendar
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.core.exceptions import RequestDataTooBig
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponseNotAllowed, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.entitlements import _locked_plan
from apps.accounts.models import AccountPlan
from apps.accounts.tenancy import require_owner, tenant
from apps.dashboard.decorators import post_only

from . import alerts as billing_alerts
from .alerts import note_unhandled_event, queue_event_alert, queue_problem_alert, queue_refund_alert
from .checkout_token import apply_if_bindable, checkout_custom
from .intervals import refresh_period_start
from .kill_switch import ensure_billing_changes_enabled, refuse_when_paused
from .ls_payload import MalformedPayload, event_name_and_key
from .models import BillingEvent, BillingPayment, PlanChangeRequest
from .receipts import queue_payment_receipt, send_downgrade_notice

logger = logging.getLogger(__name__)

LS_API_BASE = "https://api.lemonsqueezy.com/v1"
LS_DASHBOARD_BASE = "https://app.lemonsqueezy.com"
REFUND_EVENTS = ("order_refunded", "subscription_payment_refunded")
CHECKOUT_PLAN_KEYS = ("paid", "scale", "business")
# Single plan ranking for upgrade/downgrade direction (_change_plan) and the
# billing page's "upgradeable" list. Do NOT define a second copy.
_RANK = {"free": 0, "paid": 1, "scale": 2, "business": 3}


def _safe_parse_datetime(raw):
    """parse_datetime() that never raises. Django's parse_datetime returns
    None for an unrecognised format but RAISES ValueError for a well-formed
    yet invalid value (e.g. "2026-13-45T10:00:00Z"); inside the webhook
    that would 500 and roll back the whole delivery. Returns None for any
    non-string / malformed / invalid value — callers choose the fallback."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return parse_datetime(raw)
    except (TypeError, ValueError):
        return None


# --- flag-off guard (exact team_views._teams_enabled / dashboard.views.
# _outbound_enabled idiom) -----------------------------------------------------


def _billing_enabled(view):
    def wrapped(request, *args, **kwargs):
        if not settings.ENABLE_BILLING:
            raise Http404("Billing is not enabled.")
        return view(request, *args, **kwargs)

    return wrapped


def _variant_map():
    return {
        settings.LS_VARIANT_PAID: AccountPlan.Plan.PAID,
        settings.LS_VARIANT_SCALE: AccountPlan.Plan.SCALE,
        settings.LS_VARIANT_BUSINESS: AccountPlan.Plan.BUSINESS,
    }


def _plan_key_to_variant():
    return {
        "paid": settings.LS_VARIANT_PAID,
        "scale": settings.LS_VARIANT_SCALE,
        "business": settings.LS_VARIANT_BUSINESS,
    }


# --- webhook -------------------------------------------------------------------


@csrf_exempt
@require_POST
def ls_webhook(request):
    if not settings.ENABLE_BILLING:
        raise Http404("Billing is not enabled.")

    # b. size cap BEFORE any parse/verify (ingest/views.py:27-39 pattern).
    content_length = request.META.get("CONTENT_LENGTH")
    try:
        if content_length and int(content_length) > settings.LS_WEBHOOK_MAX_BYTES:
            return JsonResponse({"detail": "Payload too large."}, status=400)
    except ValueError:
        return JsonResponse({"detail": "Malformed Content-Length."}, status=400)

    try:
        raw_body = request.body
    except RequestDataTooBig:
        return JsonResponse({"detail": "Payload too large."}, status=400)
    if len(raw_body) > settings.LS_WEBHOOK_MAX_BYTES:
        return JsonResponse({"detail": "Payload too large."}, status=400)

    # c. constant-time signature verify BEFORE any parse/DB touch (ingest
    # /views.py:19-23 + webhooks/services.py:85 discipline).
    supplied_sig = request.headers.get("X-Signature", "")
    expected_sig = hmac.new(
        settings.LS_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    # Compare as bytes: compare_digest on str raises TypeError for non-ASCII,
    # which would surface as a 500 instead of a clean 401 (ingest/views.py:19-22).
    if not hmac.compare_digest(
        supplied_sig.encode("utf-8", "surrogatepass"),
        expected_sig.encode("utf-8", "surrogatepass"),
    ):
        return JsonResponse({"detail": "Invalid signature."}, status=401)

    # d. malformed JSON -> 400.
    try:
        payload = json.loads(raw_body)
    except (ValueError, TypeError):
        return JsonResponse({"detail": "Malformed JSON."}, status=400)

    try:  
        event_name, event_key = event_name_and_key(payload, raw_body)
    except MalformedPayload:
        return JsonResponse({"detail": "Malformed payload."}, status=400)

    with transaction.atomic():
        try:
            event = BillingEvent.objects.create(event_key=event_key, event_name=event_name)
        except IntegrityError:
            # Exactly-once effect (record_message_once idiom): a duplicate
            # delivery never mutates the plan a second time.
            return JsonResponse({"duplicate": True}, status=200)

        resolved_user_pk = apply_if_bindable(event_name, payload, _apply_ls_event)  
        if resolved_user_pk is not None:
            # Same transaction as the insert + mutation: a rollback drops
            # all three together; NULL stays when the account is unresolvable.
            BillingEvent.objects.filter(pk=event.pk).update(user_id=resolved_user_pk)

    return JsonResponse({"ok": True}, status=200)


def meta_custom_data(payload):
    meta = payload.get("meta") or {}
    if not isinstance(meta, dict):
        return {}
    custom_data = meta.get("custom_data")
    return custom_data if isinstance(custom_data, dict) else {}


def _locked_plan_by_pk(user_pk):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        user = User.objects.get(pk=user_pk)
    except User.DoesNotExist:
        return None
    return _locked_plan(user)


def _apply_ls_event(event_name, payload):
    """Runs INSIDE the same transaction as the BillingEvent insert. Never
    raises for a data problem — every failure path logs and returns (no-op),
    so LS never sees a 5xx and never enters a retry storm (design §3).

    Returns the resolved account user pk (the account's AccountPlan exists)
    so ls_webhook can stamp BillingEvent.user, or None when unresolvable."""
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        logger.error("billing_webhook_malformed_data event=%s", event_name)
        queue_problem_alert("malformed data", event_name, details=_alert_ids(payload, None, None))
        return
    attributes = data.get("attributes") or {}
    if not isinstance(attributes, dict):
        logger.error("billing_webhook_malformed_attributes event=%s", event_name)
        queue_problem_alert(
            "malformed attributes", event_name, details=_alert_ids(payload, data, None)
        )
        return
    custom_data = meta_custom_data(payload)

    raw_user_pk = custom_data.get("user_pk") if custom_data else None
    try:
        user_pk = int(raw_user_pk)
    except (TypeError, ValueError):
        logger.error("billing_webhook_missing_user_pk event=%s", event_name)
        if event_name in REFUND_EVENTS:
            _record_unmatched_refund(
                "missing user_pk", event_name, payload, data, attributes,
                details=_alert_ids(payload, data, attributes),
            )
            return
        queue_problem_alert(
            "missing user_pk", event_name, details=_alert_ids(payload, data, attributes)
        )
        return

    plan = _locked_plan_by_pk(user_pk)
    if plan is None:
        logger.error("billing_webhook_no_plan_for_user user_pk=%s event=%s", user_pk, event_name)
        if event_name in REFUND_EVENTS:
            _record_unmatched_refund(
                "no plan for user", event_name, payload, data, attributes,
                details=[("User pk", user_pk)] + _alert_ids(payload, data, attributes),
            )
            return
        queue_problem_alert(
            "no plan for user",
            event_name,
            details=[("User pk", user_pk)] + _alert_ids(payload, data, attributes),
        )
        return

    plan_before = plan.plan

    if event_name in REFUND_EVENTS:
        # refund-webhook-2026-10: a refund is a MONEY fact, never a plan-state sync point.
        # Recorded BEFORE the stale/mismatch guards (data.id is the INVOICE id for
        # subscription_payment_refunded); AccountPlan is NOT touched, not even
        # billing_synced_at (it would stale-skip later real subscription_updated events).
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        _record_refund(user_pk, plan, event_name, data, attributes, webhook_id=meta.get("webhook_id"))
        return user_pk
    if event_name not in billing_alerts.HANDLED_EVENTS:
        note_unhandled_event(event_name, user_pk)  # BL-17: logged; UNKNOWN also alerts. Never moves the plan
        return user_pk

    payment_amount = None
    if event_name == "subscription_payment_success":
        payment_amount = _record_payment_amount(user_pk, plan, event_name, payload, data, attributes)

    # Malformed updated_at -> None: no stale-skip, no billing_synced_at write (never a 500).
    updated_at = _safe_parse_datetime(attributes.get("updated_at"))
    if plan.billing_synced_at is not None and updated_at is not None:
        if updated_at < plan.billing_synced_at:
            logger.info("billing_webhook_stale_skip user_pk=%s event=%s", user_pk, event_name)
            return user_pk

    payload_sub_id = str(data.get("id") or "")
    if plan.stripe_subscription_id and payload_sub_id and payload_sub_id != plan.stripe_subscription_id:
        newer = bool(updated_at and plan.billing_synced_at and updated_at > plan.billing_synced_at)
        if event_name != "subscription_created" and not newer:
            logger.info(
                "billing_webhook_subscription_mismatch user_pk=%s event=%s stored=%s payload=%s",
                user_pk,
                event_name,
                plan.stripe_subscription_id,
                payload_sub_id,
            )
            return user_pk

    update_fields = ["updated_at"]
    if updated_at is not None:
        plan.billing_synced_at = updated_at
        update_fields.append("billing_synced_at")

    status = attributes.get("status")
    by_status = status if event_name == "subscription_updated" else None  # BL-17 r2: catch-all only
    variant_id = str(attributes.get("variant_id") or "")
    variant_map = _variant_map()

    if event_name in ("subscription_created", "subscription_updated") and status in (
        "on_trial",
        "active",
    ):
        mapped_plan = variant_map.get(variant_id)
        if mapped_plan is None:
            logger.error(
                "billing_webhook_unknown_variant user_pk=%s event=%s variant_id=%s",
                user_pk,
                event_name,
                variant_id,
            )
            queue_problem_alert(
                "unknown variant",
                event_name,
                details=[("User pk", user_pk), ("Variant id", variant_id)]
                + _alert_ids(payload, data, attributes),
            )
            return user_pk
        plan.plan = mapped_plan
        plan.status = AccountPlan.Status.ACTIVE
        customer_id = str(attributes.get("customer_id") or "")
        if customer_id:
            plan.stripe_customer_id = customer_id
        plan.stripe_subscription_id = payload_sub_id or plan.stripe_subscription_id
        plan.stripe_price_id = variant_id or plan.stripe_price_id
        renews_raw = attributes.get("renews_at")
        if renews_raw:
            plan.current_period_end = parse_datetime(renews_raw)
            refresh_period_start(plan, variant_id)  
        update_fields += [
            "plan",
            "status",
            "stripe_customer_id",
            "stripe_subscription_id",
            "stripe_price_id",
            "current_period_start",
            "current_period_end",
        ]
    elif event_name == "subscription_payment_failed" or by_status == "past_due":
        plan.status = AccountPlan.Status.PAST_DUE
        update_fields.append("status")
    elif event_name == "subscription_payment_success":
        plan.status = AccountPlan.Status.ACTIVE
        renews_raw = attributes.get("renews_at")
        if renews_raw:
            plan.current_period_end = parse_datetime(renews_raw)
            update_fields.append("current_period_end")
            if refresh_period_start(plan, variant_id or plan.stripe_price_id):
                update_fields.append("current_period_start")
        update_fields.append("status")
    elif event_name == "subscription_cancelled" or by_status == "cancelled":
        # Grace to period end: plan kept, ACTIVE (subscription_expired at ends_at revokes).
        plan.status = AccountPlan.Status.ACTIVE
        ends_raw = attributes.get("ends_at")
        if ends_raw:
            plan.current_period_end = parse_datetime(ends_raw)
            update_fields.append("current_period_end")
        update_fields.append("status")
    elif event_name == "subscription_paused" or by_status == "paused":
        plan.status = AccountPlan.Status.PAST_DUE
        update_fields.append("status")
    elif event_name == "subscription_expired":
        plan.plan = AccountPlan.Plan.FREE
        plan.status = AccountPlan.Status.ACTIVE
        plan.stripe_subscription_id = None
        plan.stripe_price_id = None
        update_fields += ["plan", "status", "stripe_subscription_id", "stripe_price_id"]
    else:
        note_unhandled_event(event_name, user_pk)  # handled event, nothing to apply: logged only
        return user_pk

    plan.save(update_fields=list(dict.fromkeys(update_fields)))
    queue_event_alert(
        event_name,
        user_pk=plan.pk,
        plan_before=plan_before,
        plan_after=plan.plan,
        status_after=plan.status,
        # subscription_payment_* payloads are subscription-invoice objects
        # (data.id = invoice id); they carry the subscription id in
        # attributes.subscription_id. Alert-only — the mutation above is
        # unchanged.
        subscription_id=str(attributes.get("subscription_id") or payload_sub_id),
        amount=payment_amount,
    )
    return user_pk


def _record_payment_amount(user_pk, plan, event_name, payload, data, attributes):
    """Money moved at LS: record it + receipt BEFORE the plan-STATE stale/mismatch guards
    (a payment is a fact whatever the ordering). The display amount, or None."""
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    payment = _record_payment(
        user_pk, plan, data, attributes, event_name=event_name, webhook_id=meta.get("webhook_id")
    )
    return payment.amount_display if payment is not None else None


def _alert_ids(payload, data, attributes):
    """Identifiers for a problem alert — ids/emails only, never the raw
    payload. Tolerates every malformed shape (never raises)."""
    meta = payload.get("meta") if isinstance(payload, dict) else None
    webhook_id = meta.get("webhook_id") if isinstance(meta, dict) else None
    sub_id = data.get("id") if isinstance(data, dict) else None
    attrs = attributes if isinstance(attributes, dict) else {}
    return [
        ("LS webhook id", webhook_id),
        ("LS subscription id", sub_id),
        ("LS customer id", attrs.get("customer_id")),
        ("LS customer email", attrs.get("user_email")),
    ]


# --- checkout --------------------------------------------------------------


def _ls_send_json(method, path, body, timeout):
    """THE LS money-write edge; kill switch first, before any Request/network I/O."""
    ensure_billing_changes_enabled(method, path)
    if method not in ("POST", "PATCH"):
        raise ValueError(f"_ls_send_json is a POST/PATCH money writer, not {method}")
    req = urllib.request.Request(
        f"{LS_API_BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        method=method,
        headers={
            "Authorization": f"Bearer {settings.LS_API_KEY}",
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
        },
    )
    with urllib.request.urlopen(req, timeout=sum(timeout)) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ls_post(path, body, *, timeout=(3, 10)):
    """Money writer (POST /checkouts); kill-switch check lives in _ls_send_json."""
    return _ls_send_json("POST", path, body, timeout)


def _ls_get(path, *, timeout=(3, 10)):
    req = urllib.request.Request(
        f"{LS_API_BASE}{path}",
        method="GET",
        headers={
            "Authorization": f"Bearer {settings.LS_API_KEY}",
            "Accept": "application/vnd.api+json",
        },
    )
    with urllib.request.urlopen(req, timeout=sum(timeout)) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ls_patch(path, body, *, timeout=(3, 10)):
    """Money writer (PATCH /subscriptions/{id}); checked in _ls_send_json."""
    return _ls_send_json("PATCH", path, body, timeout)


def _ls_delete(path, *, timeout=(3, 10)):
    "Public API."
    req = urllib.request.Request(
        f"{LS_API_BASE}{path}",
        method="DELETE",
        headers={
            "Authorization": f"Bearer {settings.LS_API_KEY}",
            "Accept": "application/vnd.api+json",
        },
    )
    with urllib.request.urlopen(req, timeout=sum(timeout)) as resp:
        resp.read()


def _dashboard_required(view):
    """Applied lazily (imports apps.dashboard.views inside the function body,
    NOT at module top level) to avoid a load-order dependency — apps.billing
    loads BEFORE apps.dashboard in INSTALLED_APPS (mirrors apps.accounts.
    entitlements.can_add_domain's local-import discipline for the same
    reason)."""

    def wrapped(request, *args, **kwargs):
        from apps.dashboard.views import dashboard_required

        return dashboard_required(view)(request, *args, **kwargs)

    return wrapped


def _billing_guard(request):
    """Shared guard for EVERY billing-mutating view (checkout_create — which
    also dispatches plan changes via _change_plan — and portal, the LS
    customer portal where cancel/resume/downgrade/card change happen).
    Returns a 403 flash response, or None when the caller may manage billing.
    MUST run before any plan lookup or LS call."""
    from apps.accounts.team_services import is_demo_or_agent_user

    if is_demo_or_agent_user(request.user):
        return render(request, "dashboard/partials/flash.html", {
            "flash": "Billing is not available for this account.",
        }, status=403)
    if not require_owner(request):
        return render(request, "dashboard/partials/flash.html", {
            "flash": "Only the account owner can manage billing.",
        }, status=403)
    return None


def _can_manage_billing(request):
    from apps.accounts.team_services import is_demo_or_agent_user

    return require_owner(request) and not is_demo_or_agent_user(request.user)



#
# Incident 2026-10-09: one click on "Upgrade to Business" PATCHed the live LS
# subscription with invoice_immediately and charged the saved card $131.15
# with no confirmation before and no amount after. Existing subscribers now
# go through a GET confirm page (live LS card + renewal date + estimate) and
# only a POST carrying a fresh signed token bound to (user, current variant,
# target variant) PATCHes. Every LS call here is the module-level
# _ls_get/_ls_patch (mocked in tests).

PLAN_CHANGE_TOKEN_SALT = "mailsocket.billing.plan-change-confirm.v1"
PLAN_CHANGE_TOKEN_MAX_AGE = 600  # seconds (10 min)
# Double-submit marker lifetime: the billing page shows "in progress" and the
# confirm POST refuses until the webhook moves the plan or this expires.
PLAN_CHANGE_PENDING_SECONDS = 300
_PENDING_STATUSES = (
    PlanChangeRequest.Status.SUBMITTING,
    PlanChangeRequest.Status.SUBMITTED,
    PlanChangeRequest.Status.UNKNOWN,
)
_CARD_BRANDS = {
    "visa": "Visa", "mastercard": "Mastercard", "amex": "Amex", "discover": "Discover",
    "jcb": "JCB", "diners": "Diners", "unionpay": "UnionPay",
}


class QuoteError(Exception):
    """The confirm page cannot show a trustworthy quote (no confirm button).

    ``user_message`` (optional) replaces the generic "couldn't load ... try
    again" text when the refusal is permanent (interval mismatch, trial,
    free <-> paid): retrying would not help, the customer needs our team or
    the billing portal. ``portal`` adds the billing-portal button."""

    def __init__(self, reason, *, user_message=None, portal=False):
        super().__init__(reason)
        self.user_message = user_message
        self.portal = portal



# (update-subscription): a plan change "may change the billing date or charge
# immediately if: the variant has a different billing cycle; the subscription
# is no longer free, or is now free; a trial starts or ends."
NEEDS_TEAM_MESSAGE = "This change needs our team — contact sales@mailsocket.app"
TRIAL_MESSAGE = (
    "Plan changes during a free trial can't be made here — your billing date could change. "
    "Manage your trial in the billing portal, or contact sales@mailsocket.app"
)


def _plan_name(key):
    return AccountPlan.DISPLAY_NAMES.get(key, key or "your plan")


def _fmt_cents(cents):
    return f"${cents / 100:,.2f}"


def _fmt_price(cents):
    """Catalog price: "$149" for whole dollars, "$12.50" otherwise."""
    if cents % 100 == 0:
        return f"${cents // 100:,}"
    return _fmt_cents(cents)


def _fmt_utc(dt):
    dt = dt.astimezone(dt_timezone.utc)
    return f"{dt:%B} {dt.day}, {dt.year} at {dt:%H:%M} UTC"


def _fmt_date(dt):
    dt = dt.astimezone(dt_timezone.utc)
    return f"{dt:%B} {dt.day}, {dt.year}"


def _card_label(brand, last_four):
    if not last_four:
        return ""
    name = _CARD_BRANDS.get(str(brand or "").lower(), str(brand or "Card").title() or "Card")
    return f"{name} ••{last_four}"


def _interval_label(interval, count):
    if count == 1:
        return {"day": "day", "week": "week", "month": "month", "year": "year"}.get(interval, interval)
    return f"{count} {interval}s"


def _shift_months(dt, months):
    """dt minus/plus ``months`` calendar months, clamping the day (Mar 31 -
    1 month = Feb 28/29), keeping the time of day and tzinfo."""
    total = dt.year * 12 + (dt.month - 1) + months
    year, month0 = divmod(total, 12)
    day = min(dt.day, calendar.monthrange(year, month0 + 1)[1])
    return dt.replace(year=year, month=month0 + 1, day=day)


def billing_period_start(renews_at, interval, interval_count):
    "Public API."
    count = int(interval_count or 1)
    if count < 1:
        raise QuoteError("bad interval_count")
    if interval == "month":
        return _shift_months(renews_at, -count)
    if interval == "year":
        return _shift_months(renews_at, -12 * count)
    if interval == "week":
        return renews_at - timedelta(weeks=count)
    if interval == "day":
        return renews_at - timedelta(days=count)
    raise QuoteError(f"unknown interval {interval!r}")


def prorated_estimate_cents(old_cents, new_cents, renews_at, interval, interval_count, now):
    """(new − old) × remaining_seconds / period_seconds, period = [renews_at −
    1 interval, renews_at). Rounded half-up to the cent; never negative.
    Verified (): Pro→Business at 2026-10-09 01:58:20 UTC, renews
    2026-11-07 18:12 → 13115 cents = $131.15 = the real LS charge."""
    start = billing_period_start(renews_at, interval, interval_count)
    period = (renews_at - start).total_seconds()
    if period <= 0:
        raise QuoteError("empty billing period")
    remaining = min(max((renews_at - now).total_seconds(), 0.0), period)
    diff = new_cents - old_cents
    if diff <= 0:
        return 0
    return int(diff * remaining / period + 0.5)


def _variant_pricing(variant_id):
    """Live LS variant price (cents) + billing interval. Raises on any
    malformed shape (callers map to QuoteError)."""
    body = _ls_get(f"/variants/{variant_id}")
    attrs = body["data"]["attributes"]
    if not isinstance(attrs, dict):
        raise ValueError("variant attributes is not a mapping")
    price = int(attrs["price"])
    interval = attrs.get("interval")
    count = int(attrs.get("interval_count") or 1)
    if price < 0 or interval not in ("day", "week", "month", "year"):
        raise ValueError("variant is not a recurring price")
    return price, interval, count


def _fetch_live_subscription(sub_id):
    """GET /subscriptions/{id} preflight shared by the confirm page and the
    confirm POST. Returns attributes (dict) or raises. HTTPError is a
    URLError subclass: callers MUST catch it first."""
    sub = _ls_get(f"/subscriptions/{sub_id}")
    attrs = sub["data"]["attributes"]
    if not isinstance(attrs, dict):
        raise ValueError("attributes is not a mapping")
    if attrs.get("variant_id") in (None, ""):
        raise ValueError("preflight missing variant_id")
    return attrs


def _make_token(user_pk, current_variant, target_variant, estimate_cents):
    return signing.dumps(
        {"u": int(user_pk), "f": str(current_variant), "t": str(target_variant), "e": int(estimate_cents)},
        salt=PLAN_CHANGE_TOKEN_SALT,
        compress=True,
    )


def _read_token(token):
    """Signed + unexpired payload dict, or None."""
    if not token or not isinstance(token, str) or len(token) > 1000:
        return None
    try:
        data = signing.loads(token, salt=PLAN_CHANGE_TOKEN_SALT, max_age=PLAN_CHANGE_TOKEN_MAX_AGE)
    except signing.BadSignature:  # SignatureExpired is a subclass
        return None
    if not isinstance(data, dict) or not {"u", "f", "t"} <= set(data):
        return None
    return data


def pending_plan_change(user, plan=None):
    """The in-flight confirmed change for ``user`` (PlanChangeRequest) or
    None. In flight = submitting/submitted/unknown, younger than
    PLAN_CHANGE_PENDING_SECONDS, and the webhook has not yet moved the plan
    to the target."""
    cutoff = timezone.now() - timedelta(seconds=PLAN_CHANGE_PENDING_SECONDS)
    req = (
        PlanChangeRequest.objects.filter(user=user, status__in=_PENDING_STATUSES, created_at__gte=cutoff)
        .order_by("-created_at", "-id")
        .first()
    )
    if req is None:
        return None
    if plan is None:
        plan = AccountPlan.objects.filter(user=user).first()
    if plan is not None and plan.plan == req.to_plan:
        return None  # webhook already landed
    return req


def _confirm_url(plan_key):
    return f"{reverse('dashboard:billing-change')}?{urllib.parse.urlencode({'plan': plan_key})}"


def _back_to_confirm(request, plan_key, text, level=None):
    messages.add_message(request, level or messages.ERROR, text)
    return HttpResponseRedirect(_confirm_url(plan_key), status=303)


def _back_to_billing(request, text, level=None):
    messages.add_message(request, level or messages.INFO, text)
    return HttpResponseRedirect(reverse("dashboard:billing-page"), status=303)


def _flash(request, text, status):
    return render(request, "dashboard/partials/flash.html", {"flash": text}, status=status)


def _build_quote(plan, plan_key, target_variant, now=None):
    """Everything the confirm page shows, from LIVE Lemon Squeezy data.
    Returns ("already", None) when LS already has the target variant,
    ("cancelled", None) for a cancel-at-period-end subscription, or
    ("ok", quote). Raises QuoteError when LS is unreachable/malformed."""
    now = now or timezone.now()
    try:
        attrs = _fetch_live_subscription(plan.stripe_subscription_id)
    except urllib.error.HTTPError as exc:
        raise QuoteError(f"subscription http {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError, TypeError) as exc:
        raise QuoteError("subscription unreachable") from exc

    if attrs.get("cancelled") or attrs.get("status") == "cancelled":
        return "cancelled", None
    live_variant = str(attrs.get("variant_id"))
    if live_variant == str(target_variant):
        return "already", None
    if attrs.get("status") == "on_trial":
        
        # ends through a plan change — no in-app quote, portal or sales@.
        raise QuoteError("subscription on_trial", user_message=TRIAL_MESSAGE, portal=True)
    if attrs.get("status") != "active":
        raise QuoteError(f"subscription status {attrs.get('status')!r}")

    renews_raw = attrs.get("renews_at")
    renews_at = parse_datetime(renews_raw) if isinstance(renews_raw, str) else None
    if renews_at is None or timezone.is_naive(renews_at):
        raise QuoteError("renews_at missing")

    try:
        old_cents, old_interval, old_count = _variant_pricing(live_variant)
        new_cents, new_interval, new_count = _variant_pricing(target_variant)
    except urllib.error.HTTPError as exc:
        raise QuoteError(f"variant http {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError, TypeError) as exc:
        raise QuoteError("variant unreachable") from exc

    
    # the billing date / charge the full price at LS — our proration quote
    # would be wrong, so no confirm button.
    if (old_interval, old_count) != (new_interval, new_count):
        raise QuoteError(
            f"interval mismatch {old_interval}x{old_count} -> {new_interval}x{new_count}",
            user_message=NEEDS_TEAM_MESSAGE,
        )
    if old_cents == 0 or new_cents == 0:
        raise QuoteError("free <-> paid variant", user_message=NEEDS_TEAM_MESSAGE)

    current_key = _variant_map().get(live_variant, plan.plan)
    is_downgrade = _RANK.get(plan_key, -1) < _RANK.get(current_key, -1)
    estimate = 0 if is_downgrade else prorated_estimate_cents(
        old_cents, new_cents, renews_at, old_interval, old_count, now
    )
    if renews_at <= now:
        raise QuoteError("renews_at in the past")
    period_start = billing_period_start(renews_at, old_interval, old_count)
    return "ok", {
        "current_key": current_key,
        "current_name": _plan_name(current_key),
        "current_price": f"{_fmt_price(old_cents)}/{_interval_label(old_interval, old_count)}",
        "target_key": plan_key,
        "target_name": _plan_name(plan_key),
        "target_price": f"{_fmt_price(new_cents)}/{_interval_label(new_interval, new_count)}",
        "target_price_short": _fmt_price(new_cents),
        "target_interval": _interval_label(new_interval, new_count),
        "is_downgrade": is_downgrade,
        "estimate_cents": estimate,
        "estimate": _fmt_cents(estimate),
        "card": _card_label(attrs.get("card_brand"), attrs.get("card_last_four")),
        "renews_at": renews_at,
        "renews_display": _fmt_utc(renews_at),
        "renews_date": _fmt_date(renews_at),
        "period_start_display": _fmt_utc(period_start),
        "live_variant": live_variant,
    }


def _common_change_guards(request, plan_key):
    """Shared guards for checkout_create (existing sub), the confirm page and
    the confirm POST. Returns (response, plan, variant)."""
    denied = _billing_guard(request)
    if denied is not None:
        return denied, None, None
    if plan_key not in CHECKOUT_PLAN_KEYS:
        return _flash(request, "Unknown plan.", 400), None, None
    plan = AccountPlan.objects.filter(user=tenant(request)).first()
    if plan is None or not plan.stripe_subscription_id:
        if plan and plan.plan == AccountPlan.Plan.BUSINESS:
            # Manually provisioned Business/Enterprise (operations notes).
            return _flash(request, "Your plan is managed by our team — contact sales@mailsocket.app", 409), None, None
        # New subscribers buy through the LS hosted checkout (price + card
        # form are shown there), never through this page.
        return HttpResponseRedirect(reverse("dashboard:billing-page"), status=303), None, None
    if plan.status != AccountPlan.Status.ACTIVE:
        return _flash(
            request,
            "Please resolve your payment status in the billing portal before changing plans.",
            409,
        ), None, None
    return None, plan, _plan_key_to_variant()[plan_key]


@_billing_enabled
@_dashboard_required
@post_only("dashboard:billing-page")
@refuse_when_paused  # kill switch: BILLING_CHANGES_ENABLED=false -> no LS call
def checkout_create(request):
    denied = _billing_guard(request)
    if denied is not None:
        return denied

    plan_key = request.POST.get("plan")
    if plan_key not in CHECKOUT_PLAN_KEYS:
        return render(request, "dashboard/partials/flash.html", {
            "flash": "Unknown plan.",
        }, status=400)

    variant = _plan_key_to_variant()[plan_key]
    tenant_user = tenant(request)

    plan = AccountPlan.objects.filter(user=tenant_user).first()
    if plan and plan.stripe_subscription_id:
        # billing-confirm: an existing subscriber's click NEVER charges. It
        # only opens the confirm page; the PATCH needs that page's token.
        if plan.status != AccountPlan.Status.ACTIVE:
            return render(request, "dashboard/partials/flash.html", {
                "flash": "Please resolve your payment status in the billing portal before changing plans.",
            }, status=409)
        return HttpResponseRedirect(_confirm_url(plan_key), status=303)
    
    # Enterprise (operations notes). A fresh checkout here would let the
    # webhook silently move a contract customer down to Pro/Scale — refuse.
    if plan and plan.plan == AccountPlan.Plan.BUSINESS:
        return render(request, "dashboard/partials/flash.html", {
            "flash": "Your plan is managed by our team — contact sales@mailsocket.app",
        }, status=409)

    host = getattr(settings, "DASHBOARD_HOST", "") or "dash.mailsocket.app"
    body = {
        "data": {
            "type": "checkouts",
            "attributes": {
                "checkout_data": {"email": request.user.email, "custom": checkout_custom(tenant_user.pk)},
                # billing-confirm item 4: bring the buyer back to the
                # dashboard after paying on LS. The ?checkout=success param
                # only ever drives a banner text (never plan state).
                "product_options": {
                    "redirect_url": f"https://{host}{reverse('dashboard:billing-page')}?checkout=success",
                },
            },
            "relationships": {
                "store": {"data": {"type": "stores", "id": str(settings.LS_STORE_ID)}},
                "variant": {"data": {"type": "variants", "id": str(variant)}},
            },
        },
    }
    try:
        response = _ls_post("/checkouts", body)
        checkout_url = response["data"]["attributes"]["url"]
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError):
        logger.exception("billing_checkout_create_failed user_pk=%s plan=%s", tenant_user.pk, plan_key)
        return render(request, "dashboard/partials/flash.html", {
            "flash": "Could not start checkout — please try again.",
        }, status=502)

    return HttpResponseRedirect(checkout_url, status=303)


@_billing_enabled
@_dashboard_required
@refuse_when_paused  # kill switch: GET quote (LS reads) and POST confirm both refused
def change_plan_confirm(request):
    """GET: the confirm page (no LS write). POST: the ONLY path that PATCHes
    a live subscription (via _change_plan), and only with a valid token."""
    if request.method == "POST":
        plan_key = request.POST.get("plan")
        resp, plan, variant = _common_change_guards(request, plan_key)
        if resp is not None:
            return resp
        return _change_plan(request, plan, plan_key, variant, token=request.POST.get("token"))
    if request.method not in ("GET", "HEAD"):
        return HttpResponseNotAllowed(["GET", "POST"])

    plan_key = request.GET.get("plan")
    resp, plan, variant = _common_change_guards(request, plan_key)
    if resp is not None:
        return resp

    tenant_user = tenant(request)
    ctx = {
        "title": "Confirm plan change",
        "plan_key": plan_key,
        "target_name": _plan_name(plan_key),
        "quote": None,
        "quote_error": None,
        "token": None,
        "pending": None,
    }
    pending = pending_plan_change(tenant_user, plan)
    if pending is not None:
        ctx["pending"] = pending
        ctx["pending_target_name"] = _plan_name(pending.to_plan)
        return render(request, "dashboard/billing_change.html", ctx)

    try:
        outcome, quote = _build_quote(plan, plan_key, variant)
    except QuoteError as exc:
        logger.warning("billing_change_quote_failed user_pk=%s plan=%s reason=%s", tenant_user.pk, plan_key, exc)
        ctx["quote_error"] = exc.user_message or (
            "We couldn't load your current subscription details from our billing provider, "
            "so we can't show what you'd be charged. Nothing has been changed or charged. "
            "Please try again in a minute."
        )
        ctx["quote_error_portal"] = exc.portal
        return render(request, "dashboard/billing_change.html", ctx)
    if outcome == "already":
        return _back_to_billing(request, "You're already on this plan.")
    if outcome == "cancelled":
        return _flash(
            request,
            "Your subscription is set to cancel at period end. Resume it in the billing portal before changing plans.",
            409,
        )
    ctx["quote"] = quote
    ctx["token"] = _make_token(tenant_user.pk, quote["live_variant"], variant, quote["estimate_cents"])
    return render(request, "dashboard/billing_change.html", ctx)


@refuse_when_paused  # kill switch, defence in depth: no PlanChangeRequest, no _ls_patch
def _change_plan(request, plan, plan_key, target_variant, *, token=None):
    """Confirmed existing-subscriber plan change (design Q1/Q2/Q3/Q5 +
    billing-confirm): PATCH the LIVE Lemon Squeezy subscription's variant
    instead of creating a second checkout/subscription. Reached ONLY from the
    confirm POST with a signed token bound to (tenant pk, live current
    variant, target variant, 10 min). Writes NOTHING to AccountPlan on any
    path — the ls_webhook handler (under _locked_plan) is the SOLE mutator.
    No fall-through to checkout creation from here.
    """
    tenant_user = tenant(request)
    sub_id = plan.stripe_subscription_id

    if plan.status != AccountPlan.Status.ACTIVE:
        return render(request, "dashboard/partials/flash.html", {
            "flash": "Please resolve your payment status in the billing portal before changing plans.",
        }, status=409)

    # 1. Token: signed, fresh, same tenant, same target. Checked BEFORE any LS
    # call — a click without the confirm page's token never reaches LS.
    claims = _read_token(token)
    if claims is None or claims["u"] != tenant_user.pk or claims["t"] != str(target_variant):
        logger.info("billing_change_plan_token_rejected user_pk=%s plan=%s", tenant_user.pk, plan_key)
        return _back_to_confirm(
            request, plan_key,
            "This confirmation has expired or doesn't match — please review the details and confirm again.",
        )

    # 2. Double-submit guard (fast path; re-checked under the row lock below).
    if pending_plan_change(tenant_user, plan) is not None:
        return _back_to_billing(request, "A plan change is already in progress — refresh in a minute.")

    # 3. Pre-flight: live truth from LS (mirrors the portal idiom).
    # HTTPError MUST be caught before URLError — HTTPError is a URLError
    # subclass, and a generic except would swallow the status code the Q5
    # table depends on.
    try:
        attrs = _fetch_live_subscription(sub_id)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.error("billing_change_plan_preflight_404 user_pk=%s", tenant_user.pk)
            return render(request, "dashboard/partials/flash.html", {
                "flash": "We couldn't find your subscription — please contact support.",
            }, status=409)
        logger.exception("billing_change_plan_preflight_failed user_pk=%s", tenant_user.pk)
        return render(request, "dashboard/partials/flash.html", {
            "flash": "Could not reach billing — please try again.",
        }, status=502)
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError, TypeError):
        logger.exception("billing_change_plan_preflight_failed user_pk=%s", tenant_user.pk)
        return render(request, "dashboard/partials/flash.html", {
            "flash": "Could not reach billing — please try again.",
        }, status=502)

    if attrs.get("cancelled") or attrs.get("status") == "cancelled":
        return render(request, "dashboard/partials/flash.html", {
            "flash": "Your subscription is set to cancel at period end. Resume it in the billing portal before changing plans.",
        }, status=409)
    if attrs.get("status") == "on_trial":
        # r2: same refusal as the confirm page (a token minted before the
        # trial state was visible must not PATCH a trialing subscription).
        return render(request, "dashboard/partials/flash.html", {"flash": TRIAL_MESSAGE}, status=409)
    if attrs.get("status") != "active":
        
        # life before our webhook flips AccountPlan.status — never PATCH then.
        return render(request, "dashboard/partials/flash.html", {
            "flash": "Please resolve your payment status in the billing portal before changing plans.",
        }, status=409)

    # 4. Same-variant preflight against LS truth (): the token is
    # NOT single-use and outlives the pending marker; this is what makes a
    # replayed confirm harmless once LS already has the target variant.
    live_variant = str(attrs.get("variant_id"))
    if live_variant == str(target_variant):
        from django.contrib import messages
        messages.add_message(request, messages.INFO, "You're already on this plan.")
        return HttpResponseRedirect(reverse("dashboard:billing-page"), status=302)

    # 5. Token bound to the variant the user saw on the confirm page: if the
    # subscription changed since (portal, another tab), re-confirm.
    if claims["f"] != live_variant:
        return _back_to_confirm(
            request, plan_key,
            "Your subscription changed since this page was opened — please review the new details and confirm again.",
        )

    current_key = _variant_map().get(live_variant, plan.plan)
    is_downgrade = _RANK.get(plan_key, -1) < _RANK.get(current_key, -1)
    estimate_cents = 0 if is_downgrade else int(claims.get("e") or 0)

    # 6. Claim the pending marker under the AccountPlan row lock (read lock
    # only — AccountPlan is NOT written): two concurrent confirms serialize
    # here and the second is refused.
    with transaction.atomic():
        locked = AccountPlan.objects.select_for_update().filter(pk=plan.pk).first()
        if pending_plan_change(tenant_user, locked) is not None:
            return _back_to_billing(request, "A plan change is already in progress — refresh in a minute.")
        change = PlanChangeRequest.objects.create(
            user=tenant_user,
            ls_subscription_id=str(sub_id),
            from_plan=current_key,
            to_plan=plan_key,
            from_variant=live_variant,
            to_variant=str(target_variant),
            estimate_cents=estimate_cents,
        )

    attributes = {"variant_id": int(target_variant)}
    if is_downgrade:
        attributes["disable_prorations"] = True
    else:
        attributes["invoice_immediately"] = True

    patch_body = {
        "data": {
            "type": "subscriptions",
            "id": str(sub_id),
            "attributes": attributes,
        },
    }

    def _mark(status):
        PlanChangeRequest.objects.filter(pk=change.pk).update(status=status, updated_at=timezone.now())

    try:
        _ls_patch(f"/subscriptions/{sub_id}", patch_body)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            _mark(PlanChangeRequest.Status.FAILED)
            logger.error("billing_change_plan_patch_404 user_pk=%s", tenant_user.pk)
            return render(request, "dashboard/partials/flash.html", {
                "flash": "We couldn't find your subscription — please contact support.",
            }, status=409)
        if exc.code == 402:
            _mark(PlanChangeRequest.Status.FAILED)
            logger.error("billing_change_plan_patch_402 user_pk=%s", tenant_user.pk)
            return render(request, "dashboard/partials/flash.html", {
                "flash": "We couldn't charge your payment method for this change. Update it in the billing portal, then try again.",
            }, status=402)
        if 400 <= exc.code < 500:
            _mark(PlanChangeRequest.Status.FAILED)
        else:
            _mark(PlanChangeRequest.Status.UNKNOWN)
        logger.exception("billing_change_plan_patch_failed user_pk=%s", tenant_user.pk)
        return render(request, "dashboard/partials/flash.html", {
            "flash": "Could not change plan — please try again or contact support.",
        }, status=502)
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError):
        # LS may have applied (and charged) before the connection dropped:
        # keep the marker as UNKNOWN (still blocks a retry for a few minutes)
        # and point the customer at the payment record + receipt mail.
        _mark(PlanChangeRequest.Status.UNKNOWN)
        logger.exception("billing_change_plan_patch_failed user_pk=%s", tenant_user.pk)
        return render(request, "dashboard/partials/flash.html", {
            "flash": (
                "Could not reach billing — we can't tell yet whether the change went through. "
                "Check Recent payments on the billing page in a minute (we email a receipt if you "
                "were charged) before trying again."
            ),
        }, status=502)

    _mark(PlanChangeRequest.Status.SUBMITTED)
    card = _card_label(attrs.get("card_brand"), attrs.get("card_last_four"))
    target_name = _plan_name(plan_key)
    if is_downgrade:
        renews_raw = attrs.get("renews_at")
        renews_at = parse_datetime(renews_raw) if isinstance(renews_raw, str) else None
        when = _fmt_date(renews_at) if renews_at and not timezone.is_naive(renews_at) else "your next renewal"
        current_name = _plan_name(current_key)
        
        # proration ("The plan change takes effect immediately, regardless of
        # the proration option chosen"); disable_prorations only defers the
        # PRICE. Say so — the current plan's limits end now.
        flash = (
            f"Your plan changed to {target_name} now — {current_name} limits end immediately. "
            f"No charge now; your new price applies from {when}."
        )
        send_downgrade_notice(tenant_user, target_name=target_name, from_name=current_name, when=when)
    else:
        payer = f"Your card {card.split(' ', 1)[-1]}" if card else "Your payment method"
        flash = (
            f"Upgrade to {target_name} confirmed. {payer} is being charged about "
            f"{_fmt_cents(estimate_cents)}; the receipt will be emailed to you."
        )
    from django.contrib import messages
    messages.add_message(request, messages.SUCCESS, flash)
    return HttpResponseRedirect(reverse("dashboard:billing-page"), status=303)


# --- payments (subscription_payment_success) ------------------------------


_REASONS = {
    "initial": BillingPayment.Reason.INITIAL,
    "renewal": BillingPayment.Reason.RENEWAL,
    "updated": BillingPayment.Reason.UPDATED,
}


def _safe_invoice_url(value):
    if not isinstance(value, str) or len(value) > 1000:
        return ""
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        return ""
    return value


def _invoice_row_defaults(user_pk, attributes):
    """BillingPayment column values from a subscription-invoice object
    (subscription_payment_success AND subscription_payment_refunded carry the
    same full invoice object). Raises on a malformed total (callers catch)."""
    currency = str(attributes.get("currency") or "USD")[:8]
    raw_total = attributes.get("total")
    if raw_total is None and attributes.get("total_usd") is not None:
        raw_total, currency = attributes.get("total_usd"), "USD"
    amount_cents = _strict_cents(raw_total)  # BL-19: bool / None / negative / non-numeric rejected
    if amount_cents is None:  # (int(True) == 1 would be a 1-cent invoice; 0 stays valid)
        raise ValueError("malformed invoice total")
    reason = _REASONS.get(attributes.get("billing_reason"), BillingPayment.Reason.OTHER)
    urls = attributes.get("urls") if isinstance(attributes.get("urls"), dict) else {}
    paid_at = _safe_parse_datetime(attributes.get("created_at"))
    last_four = str(attributes.get("card_last_four") or "")[:4]
    return {
        "user_id": user_pk,
        "ls_subscription_id": str(attributes.get("subscription_id") or "")[:64],
        "amount_cents": amount_cents,
        "currency": currency,
        "amount_formatted": str(attributes.get("total_formatted") or "")[:32],
        "reason": reason,
        "invoice_url": _safe_invoice_url(urls.get("invoice_url")),
        "card_brand": str(attributes.get("card_brand") or "")[:24],
        "card_last_four": last_four if last_four.isdigit() else "",
        "paid_at": paid_at or timezone.now(),
    }


# --- refunds (order_refunded / subscription_payment_refunded) ---------------


def _ls_dashboard_url(kind, object_id):
    "Public API."
    object_id = str(object_id or "")
    if not object_id or not all(c.isalnum() or c in "-_" for c in object_id) or len(object_id) > 64:
        return ""
    return f"{LS_DASHBOARD_BASE}/{kind}/{object_id}"


def _money_text(cents, currency):
    if currency == "USD":
        return f"${cents / 100:,.2f}"
    return f"{cents / 100:,.2f} {currency}"


def _strict_cents(raw):
    """Integer cents >= 0 from an LS amount, or None. ``bool`` is rejected
    (``int(True)`` == 1 would otherwise pass as a 1-cent amount)."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        cents = int(raw)
    except (TypeError, ValueError):
        return None
    return cents if cents >= 0 else None


def _parse_refunded_cents(attributes, total_cents):
    """LS ``refunded_amount`` = CUMULATIVE cents refunded on this object so
    far (webhooks ship the object's current state). Returns None when it is
    missing/malformed and cannot be inferred. ``refunded: true`` with the
    amount missing or 0 means FULLY refunded -> the invoice total."""
    raw = attributes.get("refunded_amount")
    fully = attributes.get("refunded") is True
    if isinstance(raw, bool):
        return None  # never int(True) == 1 cent
    cents = _strict_cents(raw)
    if fully and not cents and total_cents is not None:
        return total_cents  # fully refunded, amount omitted / 0
    return cents


def _plan_state_line(plan):
    still_paid = plan.plan != AccountPlan.Plan.FREE and plan.status == AccountPlan.Status.ACTIVE
    state = "STILL ACTIVE (paid)" if still_paid else "not an active paid plan"
    return f"{_plan_name(plan.plan)} ({plan.plan}), status {plan.status} — {state}; unchanged by this refund"


_UNMATCHED_REFUND_LINE = (
    "A refund was received but could not be matched to an account — check Lemon Squeezy;"
    " no plan was changed"
)


_REFUND_FOOTER = (
    "",
    "Plan unchanged — a refund never changes the plan by itself. If access should",
    "end, cancel the subscription in Lemon Squeezy (it then expires at the end of",
    "the paid period) or contact the customer. A dispute/chargeback also arrives",
    "as a refund: LS handles it, do NOT refund again. See operations notes 'Refund procedure'.",
)


def _refund_time(attributes):
    """refunded_at (else updated_at) from the payload; timezone.now() when
    missing, malformed or invalid (never raises)."""
    for key in ("refunded_at", "updated_at"):
        raw = attributes.get(key)
        if raw:
            return _safe_parse_datetime(raw) or timezone.now()
    return timezone.now()


def _apply_refund_to_row(payment, refunded_cents, payload_fully, refunded_at):
    """Read-modify-write of the refund fields on a LOCKED BillingPayment row.
    LS refunded_amount is CUMULATIVE: keep the max, never add. Returns True
    when something changed (and was saved)."""
    if payload_fully and not refunded_cents:
        # refunded:true with amount 0/missing (and no payload total to infer
        # it from): fully refunded = the stored invoice total.
        refunded_cents = payment.amount_cents
    before_cents = payment.refunded_amount_cents
    new_cents = max(before_cents, refunded_cents)
    fully = payment.refunded or payload_fully or new_cents >= payment.amount_cents
    changed = new_cents != before_cents or fully != payment.refunded
    if changed:
        payment.refunded_amount_cents = new_cents
        payment.refunded = fully
        payment.refunded_at = refunded_at or timezone.now()
        payment.save(update_fields=["refunded_amount_cents", "refunded", "refunded_at"])
    return changed


def _record_unmatched_refund(reason, event_name, payload, data, attributes, *, details):
    """A refund whose account cannot be resolved (custom_data.user_pk
    missing, or the user/plan is gone). Never touches any AccountPlan and
    never raises. For subscription_payment_refunded, when a BillingPayment
    row for that LS invoice id already exists, the refund IS recorded on it
    (cumulative max, same rule as _record_refund); no row is invented. One
    problem alert with a refund-specific footer either way."""
    object_id = str(data.get("id") or "") if isinstance(data, dict) else ""
    attrs = attributes if isinstance(attributes, dict) else {}
    status_line = "The refund was NOT recorded (no payment row for this invoice)."
    if event_name == "order_refunded":
        status_line = "The refund was NOT recorded (order refunds have no payment row)."
    elif object_id:
        refunded_cents = _parse_refunded_cents(attrs, _strict_cents(attrs.get("total")))
        if (refunded_cents is None and attrs.get("refunded") is True
                and not isinstance(attrs.get("refunded_amount"), bool)):
            refunded_cents = 0  # fully refunded: _apply_refund_to_row uses the row total
        if refunded_cents is None:
            status_line = "The refund was NOT recorded (refunded_amount missing/malformed)."
        else:
            try:
                with transaction.atomic():  # savepoint: never poison the webhook txn
                    payment = (
                        BillingPayment.objects.select_for_update()
                        .filter(ls_invoice_id=object_id)
                        .first()
                    )
                    if payment is not None:
                        changed = _apply_refund_to_row(
                            payment, refunded_cents, attrs.get("refunded") is True, _refund_time(attrs)
                        )
                        state = "Refunded" if payment.refunded else "Partially refunded"
                        status_line = (
                            f"Recorded on the existing payment row for invoice {object_id}: {state} "
                            f"{payment._money(payment.refunded_amount_cents)} of {payment.amount_display}"
                            + ("" if changed else " (already recorded, unchanged)")
                            + f"; row user pk {payment.user_id if payment.user_id is not None else 'none (deleted)'}."
                        )
            except Exception:
                logger.exception("billing_refund_unmatched_record_failed invoice=%s", object_id)
                status_line = "Recording on the payment row FAILED (DB error) — record it by hand."
    queue_problem_alert(
        reason,
        event_name,
        details=list(details) + [("LS invoice/order id", object_id)],
        footer=("", _UNMATCHED_REFUND_LINE, status_line),
    )


def _record_refund(user_pk, plan, event_name, data, attributes, *, webhook_id=None):
    "Public API."
    currency = str(attributes.get("currency") or "USD")[:8]
    total_cents = _strict_cents(attributes.get("total"))
    refunded_cents = _parse_refunded_cents(attributes, total_cents)
    object_id = str(data.get("id") or "")
    sub_id = str(attributes.get("subscription_id") or "")
    if event_name == "order_refunded":
        # An order is not tied to a subscription in the payload: the only
        # subscription we know is the account's CURRENT one, which may not
        
        base_details = [
            ("User pk", user_pk),
            ("LS webhook id", webhook_id),
            ("LS order id", object_id),
            ("Current LS subscription id (ours)", plan.stripe_subscription_id),
        ]
    else:
        base_details = [
            ("User pk", user_pk),
            ("LS webhook id", webhook_id),
            ("LS invoice id", object_id),
            ("LS subscription id", sub_id or plan.stripe_subscription_id),
        ]

    if event_name == "order_refunded":
        amount_line = (
            _money_text(refunded_cents, currency) if refunded_cents is not None else "unknown"
        )
        total_line = _money_text(total_cents, currency) if total_cents is not None else "unknown"
        queue_refund_alert(
            title=f"Refund on LS order {object_id or '?'}: {amount_line} of {total_line} — no matching payment row",
            details=base_details
            + [
                ("Refunded (cumulative)", amount_line),
                ("Order total", total_line),
                ("Our plan", _plan_state_line(plan)),
                ("LS order", _ls_dashboard_url("orders", object_id) or "-"),
                (
                    "Current LS subscription (ours)",
                    _ls_dashboard_url("subscriptions", plan.stripe_subscription_id) or "-",
                ),
            ],
            footer=(
                "",
                "No BillingPayment row matched this order (we record subscription INVOICES,",
                "not orders), so Recent payments is NOT updated by this event. If this was a",
                "subscription payment, a separate subscription_payment_refunded alert follows",
                "and records it; otherwise check the order in Lemon Squeezy.",
            )
            + _REFUND_FOOTER,
            event_name=event_name,
            user_pk=user_pk,
        )
        return

    # subscription_payment_refunded
    if not object_id or refunded_cents is None:
        logger.error("billing_refund_malformed user_pk=%s invoice=%s", user_pk, object_id)
        queue_problem_alert(
            "refund amount/invoice missing",
            event_name,
            details=base_details,
            footer=("", "A refund webhook arrived but could not be recorded (missing invoice id or",
                    "refunded_amount). The plan was NOT changed. Check the invoice in Lemon Squeezy.")
            + _REFUND_FOOTER,
        )
        return

    try:
        defaults = _invoice_row_defaults(user_pk, attributes)
    except Exception:
        defaults = None
    if defaults is None:
        logger.error("billing_refund_malformed_total user_pk=%s invoice=%s", user_pk, object_id)
        queue_problem_alert(
            "refund invoice total missing",
            event_name,
            details=base_details + [("Refunded (cumulative)", _money_text(refunded_cents, currency))],
            footer=("", "The refund could not be recorded (invoice total missing/malformed).",
                    "The plan was NOT changed. Check the invoice in Lemon Squeezy.")
            + _REFUND_FOOTER,
        )
        return

    refunded_at = _refund_time(attributes)
    payload_fully = attributes.get("refunded") is True
    mismatch = None
    changed = False
    try:
        with transaction.atomic():  # savepoint: never poison the webhook txn
            payment, created = BillingPayment.objects.get_or_create(
                ls_invoice_id=object_id, defaults=defaults
            )
            # Row lock (defence-in-depth). Two refund deliveries for the SAME
            # account are already serialized by the AccountPlan
            # select_for_update in _apply_ls_event (held for the whole webhook
            # transaction); this lock additionally covers a writer that does
            # not hold that plan lock (e.g. _record_unmatched_refund).
            payment = BillingPayment.objects.select_for_update().get(pk=payment.pk)
            if payment.user_id is not None and payment.user_id != user_pk:
                mismatch = payment.user_id
            else:
                changed = _apply_refund_to_row(payment, refunded_cents, payload_fully, refunded_at)
    except Exception:
        logger.exception("billing_refund_record_failed user_pk=%s invoice=%s", user_pk, object_id)
        queue_problem_alert(
            "refund record failed",
            event_name,
            details=base_details + [("Refunded (cumulative)", _money_text(refunded_cents, currency))],
            footer=("", "The refund webhook returned 200 but the refund was NOT recorded on the",
                    "payment row. The plan was NOT changed. Check the invoice in Lemon Squeezy.")
            + _REFUND_FOOTER,
        )
        return

    if mismatch is not None:
        logger.error(
            "billing_refund_user_mismatch invoice=%s row_user=%s payload_user=%s", object_id, mismatch, user_pk
        )
        queue_problem_alert(
            "refund invoice belongs to another account",
            event_name,
            details=base_details + [("Payment row user pk", mismatch)],
            footer=("", "The refund was NOT recorded and no plan was changed. Check the invoice",
                    "and both accounts in Lemon Squeezy / admin.")
            + _REFUND_FOOTER,
        )
        return

    if not changed and not created:
        # Same (or an older, lower) cumulative amount than already recorded:
        # a re-sent / out-of-order delivery. Nothing new; already alerted.
        logger.info(
            "billing_refund_no_change user_pk=%s invoice=%s stored=%s payload=%s",
            user_pk, object_id, payment.refunded_amount_cents, refunded_cents,
        )
        return

    kind = "Full refund" if payment.refunded else "Partial refund"
    amount_text = _money_text(payment.refunded_amount_cents, payment.currency)
    total_text = payment.amount_display
    sub_for_link = payment.ls_subscription_id or sub_id or plan.stripe_subscription_id
    queue_refund_alert(
        title=f"{kind}: {amount_text} of {total_text} (invoice {object_id})",
        details=base_details
        + [
            ("Refund", f"{kind} — {amount_text} of {total_text} refunded (cumulative)"),
            ("Recorded before", "yes (payment row existed)" if not created else
             "no — row created from the refund payload (refund arrived before the payment)"),
            ("Our plan", _plan_state_line(plan)),
            ("LS subscription", _ls_dashboard_url("subscriptions", sub_for_link) or "-"),
        ],
        footer=_REFUND_FOOTER,
        event_name=event_name,
        user_pk=user_pk,
    )


def _record_payment(user_pk, plan, data, attributes, *, event_name="subscription_payment_success", webhook_id=None):
    """Idempotent per LS invoice id (data.id). Runs inside the webhook
    transaction; never raises (a payment record problem must not lose the
    plan update). Queues our customer receipt on commit for NEW rows only.
    On failure an ops problem alert is queued (ids only): the BillingEvent
    commits, so LS never redelivers and the row + our receipt are lost.

    A row that already exists (e.g. created earlier from a refund payload
    that arrived BEFORE this payment_success) is never overwritten:
    get_or_create only reads it, so its refunded fields are kept."""
    savepoint_entered = False
    try:
        invoice_id = str(data.get("id") or "")
        if not invoice_id:
            return None
        defaults = _invoice_row_defaults(user_pk, attributes)
        # Savepoint: a DB error here must not poison the outer webhook
        # transaction (the plan update below still has to commit).
        savepoint_entered = True
        with transaction.atomic():
            payment, created = BillingPayment.objects.get_or_create(
                ls_invoice_id=invoice_id, defaults=defaults
            )
    except Exception:
        logger.exception("billing_payment_record_failed user_pk=%s", user_pk)
        if not savepoint_entered:
            return None  # malformed amount etc.: logged only (unchanged)
        
        # visible to ops. Ids only: no customer email, no amounts/card
        # data, no raw payload.
        queue_problem_alert(
            "payment record failed",
            event_name,
            details=[
                ("User pk", user_pk),
                ("LS webhook id", webhook_id),
                ("LS invoice id", data.get("id") if isinstance(data, dict) else None),
                ("LS subscription id", attributes.get("subscription_id") if isinstance(attributes, dict) else None),
            ],
            footer=(
                "",
                "The webhook returned 200 and the plan status was applied, but the payment",
                "row (Recent payments) was NOT recorded and our customer receipt was NOT sent.",
                "Lemon Squeezy's own receipt still exists — check the invoice in Lemon Squeezy.",
            ),
        )
        return None
    if created:
        try:
            description = _payment_description(user_pk, plan, payment)
        except Exception:
            logger.exception("billing_payment_description_failed user_pk=%s", user_pk)
            description = _plan_name(plan.plan)
        queue_payment_receipt(payment.pk, description)
    return payment


def _payment_description(user_pk, plan, payment):
    "Public API."
    if payment.reason == BillingPayment.Reason.UPDATED:
        current_variant = str(plan.stripe_price_id or "")
        change = None
        if current_variant:
            change = (
                PlanChangeRequest.objects.filter(
                    user_id=user_pk,
                    to_variant=current_variant,
                    status__in=_PENDING_STATUSES,
                    created_at__gte=timezone.now() - timedelta(seconds=PLAN_CHANGE_PENDING_SECONDS * 12),
                )
                .order_by("-created_at", "-id")
                .first()
            )
        if change is not None:
            return f"{_plan_name(change.from_plan)} → {_plan_name(change.to_plan)}"
        return "Plan change"
    name = _plan_name(plan.plan)
    if payment.reason == BillingPayment.Reason.RENEWAL:
        return f"{name} renewal"
    return name


# --- billing page + portal --------------------------------------------------


@_billing_enabled
@_dashboard_required
@require_GET
def billing_page(request):
    # Lazy import — same load-order discipline as _dashboard_required
    # (apps.billing loads before apps.dashboard in INSTALLED_APPS).
    from apps.dashboard.views import _pricing_context

    plan = AccountPlan.objects.filter(user=tenant(request)).first()
    current_key = plan.plan if plan else AccountPlan.Plan.FREE
    plan_status = plan.status if plan else AccountPlan.Status.ACTIVE

    current_rank = _RANK.get(current_key, 99)  # unknown plan -> never upsell

    tiers = _pricing_context()["tiers"]  # single source of truth, drift-guarded
    tier_names = {t["key"]: t["name"] for t in tiers}
    # qafix2 #12: the current plan's price, from the same tier dicts (never a
    # literal). None for a plan key the pricing table doesn't list.
    current_tier = next((t for t in tiers if t["key"] == current_key), None)
    upgradeable = [
        t for t in tiers
        if t["key"] in CHECKOUT_PLAN_KEYS  # ("paid","scale","business") — only purchasable tiers
        and t["available"]  # respects ENABLE_BILLING gating on Scale/Business
        and _RANK.get(t["key"], -1) > current_rank
    ]

    tenant_user = tenant(request)
    pending = pending_plan_change(tenant_user, plan) if plan is not None else None
    if pending is not None:
        # Double-submit guard: no upgrade buttons until the webhook lands
        # (or the marker expires).
        upgradeable = []
    # ?checkout=success drives ONLY this banner text, never plan state.
    checkout_banner = None
    if request.GET.get("checkout") == "success":
        if plan is not None and plan.stripe_subscription_id and current_key != AccountPlan.Plan.FREE:
            checkout_banner = f"Payment received — you're on {tier_names.get(current_key, plan.display_name)}."
        else:
            checkout_banner = "Payment received — your plan updates within a minute. Refresh this page shortly."
    recent_payments = list(
        BillingPayment.objects.filter(user=tenant_user).order_by("-paid_at", "-id")[:5]
    ) if _can_manage_billing(request) else []

    return render(request, "dashboard/billing.html", {
        "title": "Billing",
        "plan": plan,
        "pending_change": pending,
        "pending_target_name": _plan_name(pending.to_plan) if pending else "",
        "checkout_banner": checkout_banner,
        "recent_payments": recent_payments,
        "has_subscription": bool(plan and plan.stripe_subscription_id),
        "plan_status": plan_status,  # "active"|"past_due"|"suspended"
        "current_plan_name": tier_names.get(
            current_key, plan.display_name if plan else "Free"
        ),
        "current_plan_price": current_tier["price"] if current_tier else "",
        "upgradeable": upgradeable,  # list of _pricing_context tier dicts, verbatim
        # R2: members/demo/agent see plan + status read-only (no portal or
        # upgrade forms); the views enforce the same via _billing_guard.
        "is_owner": _can_manage_billing(request),
    })


@_billing_enabled
@_dashboard_required
@post_only("dashboard:billing-page")
def portal(request):
    
    
    denied = _billing_guard(request)
    if denied is not None:
        return denied

    plan = AccountPlan.objects.filter(user=tenant(request)).first()
    if plan is None or not plan.stripe_subscription_id:
        return render(request, "dashboard/partials/flash.html", {
            "flash": "No active subscription to manage.",
        }, status=404)

    try:
        response = _ls_get(f"/subscriptions/{plan.stripe_subscription_id}")
        portal_url = response["data"]["attributes"]["urls"]["customer_portal"]
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError):
        logger.exception("billing_portal_fetch_failed user_pk=%s", tenant(request).pk)
        return render(request, "dashboard/partials/flash.html", {
            "flash": "Could not open billing portal — please try again.",
        }, status=502)

    return HttpResponseRedirect(portal_url, status=303)
