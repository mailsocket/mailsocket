"Public API."

import logging

from django.core import signing

from apps.accounts.entitlements import _locked_plan

from .alerts import queue_problem_alert
from .models import BillingPayment

logger = logging.getLogger(__name__)

SALT = "mailsocket.ls-checkout.v1"
MAX_SIGNATURE_LENGTH = 200
SUBSCRIPTION_EVENTS = frozenset(
    {
        "subscription_created",
        "subscription_updated",
        "subscription_cancelled",
        "subscription_expired",
        "subscription_paused",
    }
)
# Subscription-invoice payloads: data.id is the INVOICE id; the subscription
# id is attributes.subscription_id.
INVOICE_EVENTS = frozenset(
    {"subscription_payment_success", "subscription_payment_failed", "subscription_payment_refunded"}
)
REFUSED_REASON = "unsigned binding refused"
REFUSED_FOOTER = (
    "",
    "A Lemon Squeezy event tried to attach a subscription or payment to this account",
    "without a valid checkout signature. NOTHING was applied (no plan change, no",
    "payment row). If this is a real customer, check the subscription in Lemon",
    "Squeezy and bind it by hand in admin. See operations notes 'Unsigned binding refused'.",
)


def _signer():
    return signing.Signer(salt=SALT)


def sign_user_pk(pk):
    """The ``user_sig`` value for account ``pk``."""
    return _signer().sign(str(int(pk)))


def checkout_custom(pk):
    """Checkout ``custom`` data: ``user_pk`` (alerts / back-compat) + ``user_sig``."""
    return {"user_pk": str(int(pk)), "user_sig": sign_user_pk(pk)}


def verify(custom_data):
    """The account pk when ``user_sig`` is a valid signature of exactly this
    ``user_pk``; None for anything else (missing, malformed, forged, signed
    for another pk, another salt or key). Never raises."""
    if not isinstance(custom_data, dict):
        return None
    signature = custom_data.get("user_sig")
    if not isinstance(signature, str) or len(signature) > MAX_SIGNATURE_LENGTH:
        return None
    try:
        signed_pk = _signer().unsign(signature)
    except signing.BadSignature:
        return None
    if signed_pk != str(custom_data.get("user_pk")):
        return None
    return int(signed_pk)


def _payload_ids(event_name, payload):
    """(subscription id, object id) of the payload, as strings ('' when absent)."""
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    attributes = data.get("attributes")
    attributes = attributes if isinstance(attributes, dict) else {}
    object_id = str(data.get("id") or "")
    if event_name in INVOICE_EVENTS:
        return str(attributes.get("subscription_id") or ""), object_id
    return object_id, object_id


def _already_bound(event_name, plan, sub_id, object_id):
    if event_name == "subscription_created":
        return False
    if sub_id and sub_id == plan.stripe_subscription_id:
        return True
    if event_name == "subscription_payment_refunded" and object_id:
        return BillingPayment.objects.filter(ls_invoice_id=object_id, user_id=plan.user_id).exists()
    return False


def _claimed_plan(custom_data):
    """The LOCKED plan of the account named by ``user_pk``, or None (missing /
    garbage pk, no such account: _apply_ls_event then alerts as before).
    AccountPlan's primary key IS the user id, so no User lookup is needed;
    _locked_plan is the same select_for_update seam _apply_ls_event uses."""
    try:
        user_pk = int(custom_data.get("user_pk"))
    except (TypeError, ValueError):
        return None
    return _locked_plan(user_pk)


def _well_formed(payload):
    data = payload.get("data")
    return isinstance(data, dict) and isinstance(data.get("attributes") or {}, dict)


def apply_if_bindable(event_name, payload, apply):
    """Run ``apply(event_name, payload)`` (the webhook's _apply_ls_event) unless
    this event would bind a subscription/payment to the claimed account without
    a valid checkout signature; then return None (nothing applied, alerted).

    A pre-filter so _apply_ls_event itself stays unchanged. Malformed data,
    a missing user_pk or an unknown account are left to _apply_ls_event (which
    alerts and writes nothing, as before). Runs inside the webhook transaction;
    the plan row lock taken here is held until commit."""
    meta = payload.get("meta")
    custom_data = meta.get("custom_data") if isinstance(meta, dict) else None
    custom_data = custom_data if isinstance(custom_data, dict) else {}
    plan = _claimed_plan(custom_data) if _well_formed(payload) else None
    if plan is not None and refuse_unsigned_binding(event_name, custom_data, payload, plan):
        return None
    return apply(event_name, payload)


def refuse_unsigned_binding(event_name, custom_data, payload, plan):
    """True (and alert) when this event must NOT be applied to ``plan``.

    Runs inside the webhook transaction, after the account's plan row is
    locked and before any write. Never raises."""
    if event_name not in SUBSCRIPTION_EVENTS and event_name not in INVOICE_EVENTS:
        return False
    if verify(custom_data) == plan.user_id:
        return False
    sub_id, object_id = _payload_ids(event_name, payload)
    if _already_bound(event_name, plan, sub_id, object_id):
        return False
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    signature_state = "missing" if not custom_data.get("user_sig") else "invalid"
    logger.warning(
        "billing_webhook_unsigned_binding_refused user_pk=%s event=%s signature=%s",
        plan.user_id,
        event_name,
        signature_state,
    )
    details = [
        ("Claimed user pk", plan.user_id),
        ("LS webhook id", meta.get("webhook_id")),
        ("LS subscription id (payload)", sub_id),
        ("LS subscription id (ours)", plan.stripe_subscription_id),
        ("Checkout signature", signature_state),
    ]
    if event_name in INVOICE_EVENTS:
        details.append(("LS invoice id", object_id))
    queue_problem_alert(REFUSED_REASON, event_name, details=details, footer=REFUSED_FOOTER)
    return True
