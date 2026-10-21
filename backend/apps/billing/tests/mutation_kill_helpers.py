"""Shared helpers for the test_billing_mutation_kills_*.py modules (mutation
testing on the CP-1 / CP-2 money paths, ). Not a test module."""

from datetime import datetime
from datetime import timezone as dt_tz
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Account
from apps.billing.models import BillingPayment

PAID_AT = datetime(2026, 10, 9, 1, 58, 20, tzinfo=dt_tz.utc)


def _user(name, *, email=None, account_email=None, account=True):
    user = get_user_model().objects.create_user(
        username=name, email=email if email is not None else "", password="pw"
    )
    if account:
        Account.objects.create(
            user=user, email_normalized=(account_email or f"{name}@acct.example.test").lower()
        )
    return user


def _payment(user, invoice="inv_1", **kw):
    fields = dict(
        user=user,
        ls_invoice_id=invoice,
        amount_cents=1200,
        currency="USD",
        reason=BillingPayment.Reason.RENEWAL,
        paid_at=PAID_AT,
    )
    fields.update(kw)
    return BillingPayment.objects.create(**fields)


def _messages(caplog, logger_name):
    return [r.getMessage() for r in caplog.records if r.name == logger_name]


# =============================================================================

# =============================================================================

OPS = "ops@example.test"


def _notify_calls():
    """Spy on apps.ops.notify.notify as alerts.py calls it: the exact
    (level, title, telegram body, link, email title, email body)."""
    return patch("apps.billing.alerts.ops_notify.notify", autospec=True)


# =============================================================================
# apps/billing/views.py — CP-1 plan change (_common_change_guards, _change_plan)
# =============================================================================

PAID_V, SCALE_V, BUSINESS_V = "2176466", "2176478", "2187025"
BILLING_URL = "/app/billing/"
CHANGE_URL = "/app/billing/change/"


_PC_SEQ = iter(range(1, 10**6))


def _pc_user(name, *, plan="paid", variant=PAID_V, status="active", sub_id="sub_mk"):
    from apps.accounts.models import AccountPlan

    name = f"pc{next(_PC_SEQ)}_{name}".lower().replace(" ", "")[:40]
    if (
        AccountPlan.objects.filter(stripe_subscription_id=sub_id)
        .exclude(stripe_subscription_id=None)
        .exists()
    ):
        sub_id = f"{sub_id}_{name}"
    u = get_user_model().objects.create_user(username=name, email=f"{name}@example.test", password="pw")
    Account.objects.create(user=u, email_normalized=f"{name}@example.test", email_verified_at=timezone.now())
    AccountPlan.objects.filter(user=u).delete()
    AccountPlan.objects.create(
        user=u,
        plan=plan,
        status=status,
        stripe_subscription_id=sub_id,
        stripe_price_id=variant,
        stripe_customer_id="cust_mk" if sub_id else "",
    )
    return u


def _live(
    variant=PAID_V,
    *,
    status="active",
    cancelled=False,
    renews_at="2026-11-07T18:12:00.000000Z",
    last_four="6770",
    brand="visa",
):
    attrs = {
        "variant_id": int(variant),
        "status": status,
        "cancelled": cancelled,
        "renews_at": renews_at,
        "card_brand": brand,
        "card_last_four": last_four,
    }
    return lambda path, **kw: {"data": {"attributes": dict(attrs)}}


def _http_error(code):
    import urllib.error

    return urllib.error.HTTPError("https://api.lemonsqueezy.com/v1/x", code, "err", {}, None)


def _confirm(user, target, *, ls_get=None, ls_patch=None, from_variant=PAID_V, estimate=13115, token=None):
    """POST the confirm page with a valid token (unless ``token`` given);
    returns (response, _ls_get mock, _ls_patch mock)."""
    from django.test import Client

    from apps.billing import views as bviews

    if token is None:
        token = bviews._make_token(user.pk, from_variant, bviews._plan_key_to_variant()[target], estimate)
    client = Client()
    client.force_login(user)
    with (
        patch("apps.billing.views._ls_get", side_effect=ls_get or _live()) as g,
        patch("apps.billing.views._ls_patch", side_effect=ls_patch) as p,
    ):
        resp = client.post(CHANGE_URL, {"plan": target, "token": token})
    return resp, g, p


def _flash_html(text):
    from django.utils.html import escape

    return f'<div class="flash" role="alert">{escape(text)}</div>'.encode()


class _Templates:
    """Template names rendered inside the block (the uppercase-template-name
    mutants only 'work' on a case-insensitive filesystem; pin the real name)."""

    def __enter__(self):
        from django.test.signals import template_rendered

        self.names = []
        self._cb = lambda sender, template, context, **kw: self.names.append(template.name)
        template_rendered.connect(self._cb)
        return self

    def __exit__(self, *exc):
        from django.test.signals import template_rendered

        template_rendered.disconnect(self._cb)


def _msgs(resp):
    from django.contrib.messages import get_messages

    return [str(m) for m in get_messages(resp.wsgi_request)]


STATUS_TEXT = "Please resolve your payment status in the billing portal before changing plans."
NOT_FOUND_TEXT = "We couldn't find your subscription — please contact support."
CANCEL_TEXT = (
    "Your subscription is set to cancel at period end. Resume it in the billing portal before changing plans."
)
IN_PROGRESS_TEXT = "A plan change is already in progress — refresh in a minute."


def _raise(exc):
    def _f(*a, **kw):
        raise exc

    return _f


# =============================================================================
# apps/billing/views.py — CP-2 refund helpers (cumulative max, never summed)
# =============================================================================

T_REFUND = datetime(2026, 10, 9, 10, 0, tzinfo=dt_tz.utc)





def _refund_plan(name, *, sub_id="sub_9001"):
    from apps.accounts.models import AccountPlan

    u = _pc_user(name, sub_id=sub_id)
    return u, AccountPlan.objects.get(user=u)


def _refund_spies():
    return (patch("apps.billing.views.queue_refund_alert"), patch("apps.billing.views.queue_problem_alert"))


STILL_PAID = "Pro (paid), status active — STILL ACTIVE (paid); unchanged by this refund"


# --- _record_unmatched_refund: status line per case, never raises ------------


def _unmatched(event, data, attrs, details=(("LS webhook id", "wh_u"),)):
    from apps.billing import views as bviews

    with patch("apps.billing.views.queue_problem_alert") as qp:
        bviews._record_unmatched_refund("missing user_pk", event, {}, data, attrs, details=details)
    qp.assert_called_once()
    return qp.call_args


# --- change_plan_confirm GET: every branch renders with the request context ---


def _confirm_get(user, plan_key="business", *, ls_get=None, method="get"):
    from django.test import Client

    client = Client()
    client.force_login(user)
    with (
        patch("apps.billing.views._ls_get", side_effect=ls_get or _live()) as g,
        patch("apps.billing.views._ls_patch") as p,
        _Templates() as t,
    ):
        resp = getattr(client, method)(CHANGE_URL, {"plan": plan_key})
    p.assert_not_called()
    return resp, g, t.names


def _apply(event, payload):
    from apps.billing import views as bviews

    with (
        patch("apps.billing.views.queue_problem_alert") as qp,
        patch("apps.billing.views._record_unmatched_refund") as unmatched,
    ):
        result = bviews._apply_ls_event(event, payload)
    return result, qp, unmatched


def _ls_payload(
    user, *, sub_id="sub_mk", updated_at=None, status="active", variant=PAID_V, webhook_id="wh_x", extra=None
):
    attrs = {"status": status, "variant_id": int(variant), "customer_id": "cust_mk"}
    if updated_at is not None:
        attrs["updated_at"] = updated_at
    attrs.update(extra or {})
    return {
        "meta": {"event_name": "x", "webhook_id": webhook_id, "custom_data": {"user_pk": str(user.pk)}},
        "data": {"id": sub_id, "attributes": attrs},
    }


def _apply_real(event, payload):
    """Run the REAL _apply_ls_event (incl. _record_payment / _record_refund
    against the test DB) inside a webhook-like transaction, then run the
    on_commit hooks it queued. Only the delivery EDGE is mocked: the ops
    notifier (apps.ops.notify.notify); customer receipts go to Django's
    locmem mail outbox. Returns (result, notify mock)."""
    from django.test import TestCase, override_settings

    from apps.billing import views as bviews

    with (
        override_settings(OPS_ALERT_EMAIL=OPS),  # an ops channel is configured -> alerts queue
        patch("apps.ops.notify.notify", autospec=True) as notify,
        TestCase.captureOnCommitCallbacks(execute=True),
    ):
        with transaction.atomic():
            result = bviews._apply_ls_event(event, payload)
    return result, notify


def _notify_texts(notify):
    """[(level, title, telegram body, email body)] of every notifier call."""
    out = []
    for c in notify.call_args_list:
        level, title, body = c.args[:3]
        out.append((level, title, body, c.kwargs.get("email_body") or ""))
    return out


def _invoice_payload(user, invoice_id, *, webhook_id, sub_id="sub_mk", updated_at=None, **attrs):
    """A complete LS subscription-invoice webhook payload (the object both
    subscription_payment_success and subscription_payment_refunded carry)."""
    attributes = {
        "store_id": 1,
        "subscription_id": sub_id,
        "customer_id": "cust_mk",
        "user_name": "Pat Customer",
        "user_email": "customer@example.test",
        "billing_reason": "renewal",
        "card_brand": "visa",
        "card_last_four": "4242",
        "currency": "USD",
        "currency_rate": "1.00000000",
        "status": "paid",
        "status_formatted": "Paid",
        "refunded": False,
        "refunded_at": None,
        "refunded_amount": 0,
        "subtotal": 1200,
        "discount_total": 0,
        "tax": 0,
        "total": 1200,
        "total_usd": 1200,
        "total_formatted": "$12.00",
        "urls": {"invoice_url": f"https://app.lemonsqueezy.com/my-orders/{invoice_id}/invoice"},
        "created_at": "2026-10-09T01:58:20.000000Z",
        "updated_at": updated_at or "2026-10-09T01:58:20.000000Z",
        "test_mode": False,
    }
    attributes.update(attrs)
    return {
        "meta": {"event_name": "x", "webhook_id": webhook_id, "custom_data": {"user_pk": str(user.pk)}},
        "data": {"type": "subscription-invoices", "id": invoice_id, "attributes": attributes},
    }


def _plan_snapshot(user):
    from apps.accounts.models import AccountPlan

    p = AccountPlan.objects.get(user=user)
    return (
        p.plan,
        p.status,
        p.stripe_subscription_id,
        p.stripe_price_id,
        p.stripe_customer_id,
        p.billing_synced_at,
        p.current_period_end,
    )


# --- ls_webhook: exact responses + size/signature boundaries ------------------

WEBHOOK = "/billing/webhook/ls/"
WH_SECRET = "test-ls-webhook-secret"


def _sig(body, secret=WH_SECRET):
    import hashlib
    import hmac

    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _post_raw(body, *, sig=None, **extra):
    from django.test import Client

    return Client().generic(
        "POST",
        WEBHOOK,
        body,
        content_type="application/json",
        HTTP_X_SIGNATURE=_sig(body) if sig is None else sig,
        **extra,
    )
