"Public API."

import json
import os
import subprocess
import sys
from datetime import timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.conf import settings as dj_settings
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.management import call_command
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.checks import billing_kill_switch_errors
from apps.accounts.models import AccountPlan
from apps.billing import views as bviews
from apps.billing.kill_switch import PAUSED_MESSAGE
from apps.billing.models import BillingEvent, BillingPayment, PlanChangeRequest
from apps.billing.tests.test_billing_ls import SECRET, event_body, post_event
from apps.billing.tests.test_billing_plan_change import login, make_user
from apps.billing.tests.test_billing_refund import paid_user, payment_body, refund_body
from config.settings.base import strict_bool_env

pytestmark = pytest.mark.django_db(transaction=True)

PAID, SCALE, BUSINESS = "2176466", "2176478", "2187025"
BILLING_URL = reverse("dashboard:billing-page")
CHECKOUT_URL = reverse("dashboard:billing-checkout")
CHANGE_URL = reverse("dashboard:billing-change")
PORTAL_URL = reverse("dashboard:billing-portal")
PRICING_URL = reverse("dashboard:pricing")

ON = dict(ENABLE_BILLING=True, BILLING_CHANGES_ENABLED=True, LS_WEBHOOK_SECRET=SECRET)
OFF = dict(ENABLE_BILLING=True, BILLING_CHANGES_ENABLED=False, LS_WEBHOOK_SECRET=SECRET)


# --- the HTTP edge -------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False


def ls_edge(routes=None):
    """patch urllib.request.urlopen — the only network edge of the LS client.
    ``routes`` maps (method, path-prefix) -> payload; an unrouted call fails
    the test loudly. The mock records every Request."""
    routes = routes or {}

    def _urlopen(req, *args, **kwargs):
        method = req.get_method()
        path = req.full_url.removeprefix(bviews.LS_API_BASE)
        for (m, prefix), payload in routes.items():
            if m == method and path.startswith(prefix):
                return _FakeResponse(payload)
        raise AssertionError(f"unexpected LS call {method} {path}")

    return patch("urllib.request.urlopen", side_effect=_urlopen)


def sub_payload(variant=PAID):
    return {
        "data": {
            "attributes": {
                "variant_id": int(variant),
                "status": "active",
                "cancelled": False,
                "card_brand": "visa",
                "card_last_four": "4242",
                "renews_at": (timezone.now() + timedelta(days=20)).isoformat(),
                "urls": {"customer_portal": "https://mailsocket.lemonsqueezy.com/billing/x"},
            }
        }
    }


def variant_payload(price):
    return {"data": {"attributes": {"price": price, "interval": "month", "interval_count": 1}}}


def db_state(user):
    """Every billing row count + the money-relevant AccountPlan columns."""
    plan = AccountPlan.objects.get(user=user)
    return (
        PlanChangeRequest.objects.count(),
        list(PlanChangeRequest.objects.order_by("pk").values_list("pk", "status", "updated_at")),
        BillingEvent.objects.count(),
        BillingPayment.objects.count(),
        (plan.plan, plan.status, plan.stripe_subscription_id, plan.stripe_price_id, plan.updated_at),
    )


def seed_old_change_request(user):
    """A finished (failed) request from earlier: never pending, so it does not
    block a new confirm; it must stay byte-identical."""
    return PlanChangeRequest.objects.create(
        user=user,
        ls_subscription_id="sub_ks",
        from_plan="paid",
        to_plan="scale",
        from_variant=PAID,
        to_variant=SCALE,
        status=PlanChangeRequest.Status.FAILED,
    )


def assert_paused_redirect(resp):
    assert resp.status_code == 303
    assert resp["Location"] == BILLING_URL


def assert_message_shown(client):
    page = client.get(BILLING_URL).content.decode()
    assert PAUSED_MESSAGE in page
    return page


# --- 1. Off: the three entry points never reach Lemon Squeezy -------------------


@override_settings(**OFF)
def test_off_checkout_new_subscriber_zero_ls_calls_no_rows_redirect_with_message():
    # BL-22: billing-lifecycle scenario (skill billing-lifecycle)
    user = make_user("ks_new", plan=AccountPlan.Plan.FREE, sub_id=None, price_id=None)
    client = login(user)
    before = db_state(user)
    with ls_edge() as edge:
        resp = client.post(CHECKOUT_URL, {"plan": "scale"})
    assert edge.call_count == 0
    assert_paused_redirect(resp)
    assert db_state(user) == before
    assert_message_shown(client)


@override_settings(**OFF)
def test_off_checkout_existing_subscriber_zero_ls_calls_no_rows():
    user = make_user("ks_exist", sub_id="sub_ks")
    seed_old_change_request(user)
    client = login(user)
    before = db_state(user)
    with ls_edge() as edge:
        resp = client.post(CHECKOUT_URL, {"plan": "business"})
    assert edge.call_count == 0
    assert_paused_redirect(resp)
    assert db_state(user) == before


@override_settings(**OFF)
def test_off_confirm_get_zero_ls_calls_no_quote_no_token_redirect_with_message():
    user = make_user("ks_get", sub_id="sub_ks")
    client = login(user)
    before = db_state(user)
    with ls_edge() as edge:
        resp = client.get(CHANGE_URL, {"plan": "business"})
    assert edge.call_count == 0
    assert_paused_redirect(resp)
    assert b"Confirm and pay" not in resp.content
    assert db_state(user) == before
    page = assert_message_shown(client)
    assert "Confirm plan change" not in page


@override_settings(**OFF)
def test_off_confirm_post_with_valid_token_zero_ls_calls_no_rows_created_or_consumed():
    # BL-22: billing-lifecycle scenario (skill billing-lifecycle)
    user = make_user("ks_post", sub_id="sub_ks")
    seed_old_change_request(user)
    token = bviews._make_token(user.pk, PAID, BUSINESS, 13115)
    assert bviews._read_token(token) is not None  # the token itself is valid
    client = login(user)
    before = db_state(user)
    with ls_edge() as edge:
        resp = client.post(CHANGE_URL, {"plan": "business", "token": token})
    assert edge.call_count == 0
    assert_paused_redirect(resp)
    assert db_state(user) == before
    assert_message_shown(client)


@override_settings(**OFF)
def test_off_change_plan_unit_zero_ls_calls_no_plan_change_request():
    """Defence in depth: _change_plan itself refuses, even if a future caller
    forgets the view-level guard."""
    user = make_user("ks_unit", sub_id="sub_ks")
    plan = AccountPlan.objects.get(user=user)
    token = bviews._make_token(user.pk, PAID, BUSINESS, 13115)
    request = RequestFactory().post(CHANGE_URL, {"plan": "business", "token": token})
    SessionMiddleware(lambda _request: None).process_request(request)
    request._messages = FallbackStorage(request)
    request.user = user
    before = db_state(user)
    with ls_edge() as edge:
        resp = bviews._change_plan(request, plan, "business", BUSINESS, token=token)
    assert edge.call_count == 0
    assert_paused_redirect(resp)
    assert db_state(user) == before
    assert [str(m) for m in request._messages] == [PAUSED_MESSAGE]


@override_settings(ENABLE_BILLING=True, BILLING_CHANGES_ENABLED=None)
def test_invalid_value_that_slipped_past_e008_fails_closed():
    user = make_user("ks_none", sub_id="sub_ks")
    with ls_edge() as edge:
        resp = login(user).post(CHECKOUT_URL, {"plan": "business"})
    assert edge.call_count == 0
    assert_paused_redirect(resp)


@override_settings(**OFF)
def test_off_paused_message_has_no_internal_wording():
    lowered = PAUSED_MESSAGE.lower()
    for word in ("kill", "switch", "billing_changes", "lemon", "env", "flag"):
        assert word not in lowered
    assert PAUSED_MESSAGE == (
        "Plan changes are paused for maintenance. Your current plan keeps working. "
        "Email sales@mailsocket.app if you need help."
    )


# --- 2. On (default): the same requests DO reach Lemon Squeezy ----------------


def test_default_is_on():
    assert dj_settings.BILLING_CHANGES_ENABLED is True


@override_settings(**ON)
def test_on_checkout_new_subscriber_reaches_ls_checkout():
    user = make_user("ks_on_new", plan=AccountPlan.Plan.FREE, sub_id=None, price_id=None)
    routes = {
        ("POST", "/checkouts"): {"data": {"attributes": {"url": "https://mailsocket.lemonsqueezy.com/c/1"}}}
    }
    with ls_edge(routes) as edge:
        resp = login(user).post(CHECKOUT_URL, {"plan": "scale"})
    assert edge.call_count == 1
    assert resp.status_code == 303
    assert resp["Location"] == "https://mailsocket.lemonsqueezy.com/c/1"


@override_settings(**ON)
def test_on_confirm_get_reaches_ls_and_renders_quote():
    user = make_user("ks_on_get", sub_id="sub_ks")
    routes = {
        ("GET", "/subscriptions/"): sub_payload(PAID),
        ("GET", f"/variants/{PAID}"): variant_payload(1200),
        ("GET", f"/variants/{BUSINESS}"): variant_payload(14900),
    }
    with ls_edge(routes) as edge:
        resp = login(user).get(CHANGE_URL, {"plan": "business"})
    assert edge.call_count == 3
    assert resp.status_code == 200
    assert PAUSED_MESSAGE.encode() not in resp.content


@override_settings(**ON)
def test_on_confirm_post_valid_token_patches_ls_and_records_request():
    # BL-22: billing-lifecycle scenario (skill billing-lifecycle)
    user = make_user("ks_on_post", sub_id="sub_ks")
    token = bviews._make_token(user.pk, PAID, BUSINESS, 13115)
    routes = {("GET", "/subscriptions/"): sub_payload(PAID), ("PATCH", "/subscriptions/"): {"data": {}}}
    with ls_edge(routes) as edge:
        resp = login(user).post(CHANGE_URL, {"plan": "business", "token": token})
    methods = [c.args[0].get_method() for c in edge.call_args_list]
    assert methods == ["GET", "PATCH"]
    assert resp.status_code == 303
    assert PlanChangeRequest.objects.filter(user=user, status=PlanChangeRequest.Status.SUBMITTED).count() == 1


# --- 3. Off: webhooks and the portal keep working -------------------------------


def _payment_refund_scenario(tag):
    """payment_success then a full refund, through the real webhook."""
    client = Client()
    user = paid_user(f"ks_pay_{tag}", sub_id=f"sub_pay_{tag}")
    inv = f"inv_{tag}"
    r1 = post_event(client, payment_body(user, inv, webhook_id=f"wh_pay_{tag}"))
    r2 = post_event(client, refund_body(user, inv, webhook_id=f"wh_ref_{tag}", refunded_amount=1200))
    p = AccountPlan.objects.get(user=user)
    row = BillingPayment.objects.get(ls_invoice_id=inv)
    return (
        r1.status_code,
        r2.status_code,
        BillingEvent.objects.filter(user=user).count(),
        (row.amount_cents, row.refunded_amount_cents, row.refunded, row.reason),
        (p.plan, p.status),
    )


def _cancel_scenario(tag):
    client = Client()
    user = paid_user(f"ks_can_{tag}", sub_id=f"sub_can_{tag}", synced_at=timezone.now() - timedelta(days=1))
    ends_at = (timezone.now() + timedelta(days=10)).replace(microsecond=0)
    body = event_body(
        "subscription_cancelled",
        user_pk=user.pk,
        status="cancelled",
        ends_at=ends_at.isoformat(),
        updated_at=timezone.now().isoformat(),
        sub_id=f"sub_can_{tag}",
        webhook_id=f"wh_can_{tag}",
    )
    resp = post_event(client, body)
    p = AccountPlan.objects.get(user=user)
    return (
        resp.status_code,
        p.plan,
        p.status,
        p.current_period_end == ends_at,
        p.stripe_subscription_id is not None,
    )


def test_off_webhook_payment_and_refund_apply_exactly_as_when_on():
    with override_settings(**ON):
        on = _payment_refund_scenario("on")
    with override_settings(**OFF):
        off = _payment_refund_scenario("off")
    assert off == on
    assert off == (200, 200, 2, (1200, 1200, True, BillingPayment.Reason.RENEWAL), ("paid", "active"))


def test_off_webhook_cancelled_applies_exactly_as_when_on():
    with override_settings(**ON):
        on = _cancel_scenario("on")
    with override_settings(**OFF):
        off = _cancel_scenario("off")
    assert off == on
    assert off == (200, "paid", "active", True, True)


@override_settings(**OFF)
def test_off_webhook_never_calls_ls_and_is_not_redirected():
    user = paid_user("ks_wh_edge", sub_id="sub_wh_edge")
    with ls_edge() as edge:
        resp = post_event(Client(), payment_body(user, "inv_wh_edge", webhook_id="wh_edge"))
    assert edge.call_count == 0
    assert resp.status_code == 200
    assert BillingPayment.objects.filter(ls_invoice_id="inv_wh_edge").count() == 1


@override_settings(**OFF)
def test_off_portal_still_redirects_to_ls_customer_portal():
    user = make_user("ks_portal", sub_id="sub_ks")
    client = login(user)
    page = client.get(BILLING_URL).content.decode()
    assert PORTAL_URL in page and "Open billing portal" in page
    with ls_edge({("GET", "/subscriptions/sub_ks"): sub_payload(PAID)}) as edge:
        resp = client.post(PORTAL_URL)
    assert edge.call_count == 1
    assert resp.status_code == 303
    assert resp["Location"] == "https://mailsocket.lemonsqueezy.com/billing/x"


@override_settings(**OFF)
def test_off_account_erasure_still_cancels_ls_subscription_once_and_deletes_account():
    """EXEMPT by policy: erasure only stops future charges. With the switch
    off it still DELETEs the live LS subscription exactly once (real
    _ls_delete, HTTP edge patched) and then erases the account."""
    from apps.accounts.models import Account
    from apps.dashboard.tests.test_settings import _delete_payload
    from apps.dashboard.tests.test_settings import login as settings_login
    from apps.dashboard.tests.test_settings import make_user as settings_user

    user = settings_user("ks_erase", plan=AccountPlan.Plan.PAID, sub_id="sub_erase")
    user_pk = user.pk
    client = settings_login(user)
    with ls_edge({("DELETE", "/subscriptions/sub_erase"): {}}) as edge:
        resp = client.post(reverse("dashboard:settings-delete"), _delete_payload(user))
    calls = [(c.args[0].get_method(), c.args[0].full_url) for c in edge.call_args_list]
    assert calls == [("DELETE", f"{bviews.LS_API_BASE}/subscriptions/sub_erase")]
    assert resp.status_code == 200
    assert PAUSED_MESSAGE.encode() not in resp.content
    assert not get_user_model().objects.filter(pk=user_pk).exists()
    assert not Account.objects.filter(pk=user_pk).exists()
    assert not AccountPlan.objects.filter(user_id=user_pk).exists()


# --- 4. UI ---------------------------------------------------------------------


def _upgrade_section(page):
    return page.split("Upgrade your plan", 1)[1].split("</section>", 1)[0]


@override_settings(**OFF)
def test_off_billing_page_upgrade_buttons_disabled_no_forms_with_note():
    for name, sub in (("ks_ui_sub", "sub_ks_ui"), ("ks_ui_free", None)):
        plan = AccountPlan.Plan.PAID if sub else AccountPlan.Plan.FREE
        user = make_user(name, plan=plan, sub_id=sub, price_id=PAID if sub else None)
        section = _upgrade_section(login(user).get(BILLING_URL).content.decode())
        assert CHECKOUT_URL not in section and CHANGE_URL not in section
        assert "<form" not in section
        assert 'disabled aria-disabled="true">Upgrade to Scale</button>' in section
        assert PAUSED_MESSAGE in section


@override_settings(**ON)
def test_on_billing_page_upgrade_forms_unchanged():
    user = make_user("ks_ui_on", sub_id="sub_ks_on")
    section = _upgrade_section(login(user).get(BILLING_URL).content.decode())
    assert f'action="{CHANGE_URL}"' in section
    assert "disabled" not in section
    assert PAUSED_MESSAGE not in section


def _tier_head(page, key):
    import re

    return re.search(rf'<th[^>]*data-tier="{key}"[^>]*>(.*?)</th>', page, re.S).group(1)


def test_pricing_anonymous_byte_identical_on_and_off():
    with override_settings(**ON):
        on = Client().get(PRICING_URL)
    with override_settings(**OFF):
        off = Client().get(PRICING_URL)
    assert on.status_code == off.status_code == 200
    assert off.content == on.content
    assert PAUSED_MESSAGE.encode() not in off.content


def _assert_private_per_cookie(resp):
    cc = {part.strip() for part in resp["Cache-Control"].split(",")}
    assert {"private", "no-cache"} <= cc, resp["Cache-Control"]
    assert "public" not in cc and not any(p.startswith(("max-age", "s-maxage")) for p in cc)
    assert "cookie" in {v.strip().lower() for v in resp["Vary"].split(",")}, resp["Vary"]


@pytest.mark.parametrize("flags", [ON, OFF], ids=["on", "off"])
def test_pricing_cache_headers_private_no_cache_vary_cookie_anonymous_and_signed_in(flags):
    with override_settings(**flags):
        anon = Client().get(PRICING_URL)
        signed_in = login(make_user(f"ks_cache_{str(flags['BILLING_CHANGES_ENABLED']).lower()}")).get(
            PRICING_URL
        )
    for resp in (anon, signed_in):
        assert resp.status_code == 200
        _assert_private_per_cookie(resp)


@override_settings(**OFF)
def test_off_pricing_signed_in_paid_ctas_disabled_free_and_enterprise_unchanged():
    page = login(make_user("ks_pricing")).get(PRICING_URL).content.decode()
    anon = Client().get(PRICING_URL).content.decode()
    for key in ("paid", "scale", "business"):
        head = _tier_head(page, key)
        assert "<a " not in head, key
        assert 'disabled aria-disabled="true">Start free</button>' in head, key
        assert PAUSED_MESSAGE in head, key
    for key in ("free", "enterprise"):
        assert _tier_head(page, key) == _tier_head(anon, key), key


@override_settings(**ON)
def test_on_pricing_signed_in_ctas_still_links():
    page = login(make_user("ks_pricing_on")).get(PRICING_URL).content.decode()
    assert "disabled" not in _tier_head(page, "scale")
    assert f'href="{reverse("accounts:register")}?next=/app/billing/"' in _tier_head(page, "scale")


def _staff_client():
    staff = get_user_model().objects.create_user("ks_staff", password="x", is_staff=True)
    c = Client()
    c.force_login(staff)
    return c


def test_fleet_banner_only_when_paused():
    client = _staff_client()
    with override_settings(**OFF):
        off = client.get("/app/fleet/").content.decode()
    with override_settings(**ON):
        on = client.get("/app/fleet/").content.decode()
    assert "Billing changes paused" in off
    assert 'class="flash flash-error billing-paused-banner" role="alert"' in off
    assert "Billing changes paused" not in on


def _no_network(monkeypatch):
    import socket
    import urllib.request

    def no_net(*_args, **_kwargs):
        raise OSError("no network in tests")

    monkeypatch.setattr(socket, "create_connection", no_net)
    monkeypatch.setattr(urllib.request, "urlopen", no_net)


@pytest.mark.parametrize("enabled", [True, False])
def test_status_probe_logs_one_line_only_when_paused(monkeypatch, enabled):
    _no_network(monkeypatch)
    out = StringIO()
    with override_settings(BILLING_CHANGES_ENABLED=enabled):
        call_command("status_probe", stdout=out)
    lines = [ln for ln in out.getvalue().splitlines() if "billing_changes" in ln]
    assert lines == (
        [] if enabled else ["status_probe billing_changes=PAUSED (BILLING_CHANGES_ENABLED is off)"]
    )


# --- 5. mailsocket.E008 ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [("true", True), ("1", True), ("false", False), ("0", False)],
)
def test_strict_bool_env_valid_values(monkeypatch, raw, expected):
    monkeypatch.setenv("BILLING_CHANGES_ENABLED", raw)
    assert strict_bool_env("BILLING_CHANGES_ENABLED", True) == (expected, raw)


def test_strict_bool_env_unset_defaults_true(monkeypatch):
    monkeypatch.delenv("BILLING_CHANGES_ENABLED", raising=False)
    assert strict_bool_env("BILLING_CHANGES_ENABLED", True) == (True, None)


@pytest.mark.parametrize(
    "raw", ["True", "FALSE", "yes", "no", "on", "off", "", " true", "false ", "2", "flase"]
)
def test_strict_bool_env_invalid_values_are_none(monkeypatch, raw):
    monkeypatch.setenv("BILLING_CHANGES_ENABLED", raw)
    assert strict_bool_env("BILLING_CHANGES_ENABLED", True) == (None, raw)


def test_e008_clean_for_true_false_and_flags_none():
    assert billing_kill_switch_errors(SimpleNamespace(BILLING_CHANGES_ENABLED=True)) == []
    assert billing_kill_switch_errors(SimpleNamespace(BILLING_CHANGES_ENABLED=False)) == []
    errors = billing_kill_switch_errors(
        SimpleNamespace(BILLING_CHANGES_ENABLED=None, BILLING_CHANGES_ENABLED_RAW="yes")
    )
    assert [e.id for e in errors] == ["mailsocket.E008"]
    assert "'yes'" in errors[0].msg


@override_settings(BILLING_CHANGES_ENABLED=None, BILLING_CHANGES_ENABLED_RAW="flase")
def test_e008_registered_manage_check_fails():
    from django.core.management.base import SystemCheckError

    with pytest.raises(SystemCheckError, match="mailsocket.E008"):
        call_command("check", "--fail-level", "ERROR")


@pytest.mark.parametrize("raw, ok", [("yes", False), ("false", True), (None, True)])
def test_e008_real_process_refuses_to_start(raw, ok):
    """End to end: a fresh `manage.py check --fail-level ERROR` (what the
    container entrypoint runs before gunicorn) with the env var set."""
    env = dict(os.environ, DJANGO_SETTINGS_MODULE="config.settings.testing")
    env.pop("BILLING_CHANGES_ENABLED", None)
    if raw is not None:
        env["BILLING_CHANGES_ENABLED"] = raw
    proc = subprocess.run(
        [sys.executable, "manage.py", "check", "--fail-level", "ERROR"],
        cwd=Path(dj_settings.BASE_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if ok:
        assert proc.returncode == 0, proc.stderr
    else:
        assert proc.returncode != 0
        assert "mailsocket.E008" in proc.stderr
