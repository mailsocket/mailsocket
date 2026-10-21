"""Billing kill switch: BILLING_CHANGES_ENABLED (operations notes).

Off (``false`` / ``0``, or an invalid value that slipped past the
mailsocket.E008 startup check): every path that could start a new
subscription or change a live one refuses BEFORE any DB read or Lemon
Squeezy call — checkout_create, the plan-change confirm page (GET) and
confirm POST (change_plan_confirm), and _change_plan itself (defence in
depth). The customer is sent back to /app/billing with PAUSED_MESSAGE.

Policy (coordinator decision, billing-kill r2): the switch stops money
moving INTO a new or changed subscription during an incident. Stopping
future charges is a customer right and stays available.

  GUARDED (refuse while paused):  checkout_create, change_plan_confirm
                                  (GET and POST), _change_plan.
  EXEMPT  (work while paused):    ls_webhook (LS payments, refunds and
                                  cancellations must still be recorded),
                                  portal (cancel / update card at LS),
                                  account erasure (settings_delete ->
                                  _ls_delete cancels the subscription; it
                                  already fails closed: an LS cancel failure
                                  aborts the delete).

GUARDED / EXEMPT below are the machine-readable form of that list.
test_billing_kill_switch_edge.py walks every function in backend/ (all production
modules, not only apps/) that references an LS write helper (_ls_send_json /
_ls_post / _ls_patch / _ls_delete) and fails unless it is decorated with
@refuse_when_paused or named in EXEMPT; a writer NAME as a string literal
(getattr / vars / __dict__) outside apps/billing/views.py fails it too.

Edge (billing-kill r4): the registry is static, so the lowest LS write
function enforces the switch itself. _ls_send_json (POST/PATCH only, any other
method -> ValueError; only callers _ls_post and _ls_patch, asserted by the
registry) calls ensure_billing_changes_enabled() as its first statement and
raises BillingChangesPaused BEFORE building the request, so a dynamic / future
caller that skips the decorator fails closed. The check is not scoped by path: every current _ls_post call is
POST /checkouts and every _ls_patch call is PATCH /subscriptions/{id}, both
money-moving. _ls_delete (cancellation, used only by account erasure) is
EXEMPT and has no edge check.

There is no JSON caller of the guarded views (they are HTML form
endpoints), so there is no 503 variant.
"""

import functools
import logging

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.urls import reverse

logger = logging.getLogger(__name__)

GUARDED = frozenset(
    {
        "apps.billing.views.checkout_create",
        "apps.billing.views.change_plan_confirm",
        "apps.billing.views._change_plan",
    }
)

EXEMPT = {
    "apps.billing.views.ls_webhook": "records LS payments, refunds, cancellations",
    "apps.billing.views.portal": "customer can still cancel / update card at LS",
    "apps.dashboard.settings_views.settings_delete": "account erasure cancels the sub (stops charges)",
}

PAUSED_MESSAGE = (
    "Plan changes are paused for maintenance. Your current plan keeps working. "
    "Email sales@mailsocket.app if you need help."
)


def billing_changes_enabled():
    """True only for an explicit True setting: None (invalid env value) and
    False both mean paused (fail closed)."""
    return settings.BILLING_CHANGES_ENABLED is True


class BillingChangesPaused(Exception):
    """An LS money write (_ls_post / _ls_patch) was attempted while
    BILLING_CHANGES_ENABLED is off. Deliberately NOT a URLError / OSError /
    ValueError / KeyError subclass, so no caller's "LS failed, try again"
    handler can swallow it: an unguarded caller fails closed with a 500."""


def ensure_billing_changes_enabled(method, path):
    """Edge guard for the LS money writers; call before any network I/O."""
    if not billing_changes_enabled():
        logger.warning("billing_changes_paused_ls_write_refused method=%s path=%s", method, path)
        raise BillingChangesPaused(f"{method} {path} refused: BILLING_CHANGES_ENABLED is off")


def paused_response(request):
    user_pk = getattr(getattr(request, "user", None), "pk", None)
    logger.warning("billing_changes_paused_refused path=%s user_pk=%s", request.path, user_pk)
    messages.add_message(request, messages.INFO, PAUSED_MESSAGE)
    return HttpResponseRedirect(reverse("dashboard:billing-page"), status=303)


def refuse_when_paused(view):
    """Decorator for a billing view (or _change_plan, whose first argument is
    also the request): with the switch off, nothing below it runs."""

    @functools.wraps(view)
    def wrapped(request, *args, **kwargs):
        if not billing_changes_enabled():
            return paused_response(request)
        return view(request, *args, **kwargs)

    return wrapped
