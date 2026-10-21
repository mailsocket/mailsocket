"Public API."
import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.entitlements import can_create_inbox
from apps.accounts.models import Account, AccountPlan, TeamMembership
from apps.accounts.multi_account_services import create_owned_account
from apps.accounts.tenancy import SESSION_KEY
from apps.billing import views as views_module
from apps.billing.models import BillingEvent

pytestmark = pytest.mark.django_db(transaction=True)

WEBHOOK_URL = "/billing/webhook/ls/"
SECRET = "test-ls-webhook-secret"


def make_user(name, *, plan=AccountPlan.Plan.FREE, email=None):
    user = get_user_model().objects.create_user(
        username=name, email=email or f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=email or f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.filter(user=user).delete()
    AccountPlan.objects.create(user=user, plan=plan)
    return user


def login(user):
    client = Client()
    client.force_login(user)
    return client


def sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def event_body(event_name, *, user_pk=None, custom_data=None, sub_id="sub_1", variant_id="2176466",
                status="active", updated_at=None, created_at=None, renews_at=None, ends_at=None,
                customer_id="cust_1", webhook_id="wh_1", extra_meta=None, extra_attrs=None):
    meta = {"event_name": event_name}
    if webhook_id is not None:
        meta["webhook_id"] = webhook_id
    if custom_data is not None:
        meta["custom_data"] = custom_data
    elif user_pk is not None:
        meta["custom_data"] = views_module.checkout_custom(user_pk)  
    if extra_meta:
        meta.update(extra_meta)

    attributes = {
        "status": status,
        "variant_id": variant_id,
        "customer_id": customer_id,
    }
    if updated_at is not None:
        attributes["updated_at"] = updated_at
    if created_at is not None:
        attributes["created_at"] = created_at
    if renews_at is not None:
        attributes["renews_at"] = renews_at
    if ends_at is not None:
        attributes["ends_at"] = ends_at
    if extra_attrs:
        attributes.update(extra_attrs)

    payload = {"meta": meta, "data": {"id": sub_id, "attributes": attributes}}
    return json.dumps(payload).encode("utf-8")


def post_event(client, body, *, sig=None):
    return client.post(
        WEBHOOK_URL,
        data=body,
        content_type="application/json",
        HTTP_X_SIGNATURE=sig if sig is not None else sign(body),
    )


# --- 1-5: signature / malformed / oversized --------------------------------


def _plan_snapshot(user):
    """Full byte-comparable snapshot of the money-critical AccountPlan fields."""
    plan = AccountPlan.objects.get(user=user)
    return (
        plan.plan,
        plan.status,
        plan.stripe_customer_id,
        plan.stripe_subscription_id,
        plan.stripe_price_id,
        plan.current_period_start,
        plan.current_period_end,
        plan.billing_synced_at,
    )


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_1_missing_signature_401_zero_db_no_event_row():  # BL-15
    user = make_user("sig1")
    before = _plan_snapshot(user)
    client = Client()
    body = event_body("subscription_created", user_pk=user.pk)
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    with CaptureQueriesContext(connection) as queries:
        resp = client.post(WEBHOOK_URL, data=body, content_type="application/json")
    assert resp.status_code == 401
    assert BillingEvent.objects.count() == 0
    assert _plan_snapshot(user) == before
    assert not any(q["sql"].strip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries.captured_queries)


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_2_wrong_signature_401():  # BL-15
    user = make_user("sig2")
    before = _plan_snapshot(user)
    client = Client()
    body = event_body("subscription_created", user_pk=user.pk)
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    with CaptureQueriesContext(connection) as queries:
        resp = client.post(
            WEBHOOK_URL, data=body, content_type="application/json", HTTP_X_SIGNATURE="0" * 64
        )
    assert resp.status_code == 401
    assert BillingEvent.objects.count() == 0
    assert _plan_snapshot(user) == before
    assert not any(q["sql"].strip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries.captured_queries)


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_3_valid_signature_but_mutated_byte_401():
    user = make_user("sig3")
    before = _plan_snapshot(user)
    client = Client()
    body = event_body("subscription_created", user_pk=user.pk)
    sig = sign(body)
    mutated = bytearray(body)
    mutated[-1] ^= 0xFF
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    with CaptureQueriesContext(connection) as queries:
        resp = client.post(
            WEBHOOK_URL, data=bytes(mutated), content_type="application/json", HTTP_X_SIGNATURE=sig
        )
    assert resp.status_code == 401
    assert BillingEvent.objects.count() == 0
    assert _plan_snapshot(user) == before
    assert not any(q["sql"].strip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries.captured_queries)


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_1b_non_ascii_signature_byte_401_not_500_zero_db():
    """P2-1: X-Signature containing a raw 0x80-0xFF byte must 401, not 500."""
    user = make_user("sig1b")
    before = _plan_snapshot(user)
    client = Client()
    body = event_body("subscription_created", user_pk=user.pk)
    non_ascii_sig = "deadbeef\udcff" * 8  # surrogate-escaped 0xFF byte, non-ASCII
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    with CaptureQueriesContext(connection) as queries:
        resp = client.post(
            WEBHOOK_URL,
            data=body,
            content_type="application/json",
            HTTP_X_SIGNATURE=non_ascii_sig,
        )
    assert resp.status_code == 401
    assert BillingEvent.objects.count() == 0
    assert _plan_snapshot(user) == before
    assert not any(q["sql"].strip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries.captured_queries)


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_4_valid_sig_malformed_json_400_no_plan_change():
    user = make_user("sig4")
    plan_before = AccountPlan.objects.get(user=user).plan
    client = Client()
    body = b"{not json"
    resp = post_event(client, body)
    assert resp.status_code == 400
    assert AccountPlan.objects.get(user=user).plan == plan_before


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET, LS_WEBHOOK_MAX_BYTES=100)
def test_5_oversized_body_400_before_verify():
    client = Client()
    body = b"x" * 500
    with patch("apps.billing.views.hmac.compare_digest") as mock_digest, \
            patch("apps.billing.views.json.loads") as mock_loads:
        resp = client.post(
            WEBHOOK_URL,
            data=body,
            content_type="application/json",
            HTTP_X_SIGNATURE="deadbeef",
            HTTP_CONTENT_LENGTH="500",
        )
    assert resp.status_code == 400
    assert BillingEvent.objects.count() == 0
    mock_digest.assert_not_called()
    mock_loads.assert_not_called()


# --- 6-9: dedupe / ordering --------------------------------------------------


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_6_duplicate_webhook_id_second_is_200_duplicate_plan_mutated_once():  # BL-16
    user = make_user("dedupe6")
    client = Client()
    now = timezone.now().isoformat()
    body = event_body(
        "subscription_created", user_pk=user.pk, status="active", updated_at=now, webhook_id="wh_dup"
    )
    with patch("apps.billing.views._apply_ls_event", wraps=views_module._apply_ls_event) as spy:
        resp1 = post_event(client, body)
        resp2 = post_event(client, body)
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp2.json().get("duplicate") is True
    assert AccountPlan.objects.get(user=user).plan == AccountPlan.Plan.PAID
    assert BillingEvent.objects.filter(event_key="wh_dup").count() == 1
    assert spy.call_count == 1


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_7_no_webhook_id_hash_fallback_dedupes_exact_replay():
    user = make_user("dedupe7")
    plan_before = AccountPlan.objects.get(user=user).plan
    assert plan_before == AccountPlan.Plan.FREE
    client = Client()
    now = timezone.now().isoformat()
    body = event_body(
        "subscription_created", user_pk=user.pk, status="active", updated_at=now, webhook_id=None
    )
    with patch("apps.billing.views._apply_ls_event", wraps=views_module._apply_ls_event) as spy:
        resp1 = post_event(client, body)
        resp2 = post_event(client, body)
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp2.json().get("duplicate") is True
    assert BillingEvent.objects.count() == 1
    # First delivery actually mutated the plan free->paid, and the replay
    # left the exact same single mutation (not a second application).
    assert AccountPlan.objects.get(user=user).plan == AccountPlan.Plan.PAID
    assert spy.call_count == 1


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_8_out_of_order_older_active_does_not_regress_state():  # BL-16
    user = make_user("order8")
    t2 = timezone.now()
    t1 = t2 - timedelta(hours=1)

    # SEED: paid + active, synced at T2, with a real subscription/period state.
    AccountPlan.objects.filter(user=user).update(
        plan=AccountPlan.Plan.PAID,
        status=AccountPlan.Status.ACTIVE,
        stripe_subscription_id="sub_order8",
        stripe_price_id="2176466",
        stripe_customer_id="cust_order8",
        current_period_start=t2 - timedelta(days=1),
        current_period_end=t2 + timedelta(days=29),
        billing_synced_at=t2,
    )
    seeded = _plan_snapshot(user)

    client = Client()
    stale_active_body = event_body(
        "subscription_updated", user_pk=user.pk, status="active", updated_at=t1.isoformat(),
        sub_id="sub_order8", webhook_id="wh_stale_active",
    )
    resp = post_event(client, stale_active_body)
    assert resp.status_code == 200
    # Stale (older) delivery must be a total no-op: every field stays at
    # the T2 seeded state (stale-skip guard must not have been bypassed).
    assert _plan_snapshot(user) == seeded


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_9_crash_after_billingevent_insert_rolls_back_both():
    user = make_user("crash9")
    client = Client()
    body = event_body("subscription_created", user_pk=user.pk, webhook_id="wh_crash")
    pre_event_snapshot = _plan_snapshot(user)

    def mutate_then_raise(event_name, payload):
        # Prove the rollback covers a REAL mutation, not a no-op raise.
        AccountPlan.objects.filter(user=user).update(
            plan=AccountPlan.Plan.PAID, status=AccountPlan.Status.ACTIVE
        )
        raise RuntimeError("boom")

    with patch("apps.billing.views._apply_ls_event", side_effect=mutate_then_raise):
        with pytest.raises(RuntimeError):
            post_event(client, body)

    assert BillingEvent.objects.filter(event_key="wh_crash").count() == 0
    assert _plan_snapshot(user) == pre_event_snapshot

    # Retry (LS redelivery) succeeds cleanly.
    resp = post_event(client, body)
    assert resp.status_code == 200
    assert AccountPlan.objects.get(user=user).plan == AccountPlan.Plan.PAID
    assert BillingEvent.objects.filter(event_key="wh_crash").count() == 1


# --- 10-18: status transitions -----------------------------------------------


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_10_created_active_variant_paid_sets_plan_ids_periods():
    user = make_user("t10")
    plan_id_before = AccountPlan.objects.get(user=user).pk
    client = Client()
    now = timezone.now()
    body = event_body(
        "subscription_created",
        user_pk=user.pk,
        variant_id="2176466",
        status="active",
        created_at=now.isoformat(),
        renews_at=(now + timedelta(days=30)).isoformat(),
        updated_at=now.isoformat(),
        sub_id="sub_10",
        customer_id="cust_10",
    )
    resp = post_event(client, body)
    assert resp.status_code == 200
    plan = AccountPlan.objects.get(user=user)
    assert plan.plan == AccountPlan.Plan.PAID
    assert plan.status == AccountPlan.Status.ACTIVE
    assert plan.stripe_subscription_id == "sub_10"
    assert plan.stripe_customer_id == "cust_10"
    assert plan.stripe_price_id == "2176466"
    assert plan.current_period_start is not None
    assert plan.current_period_end is not None
    from django.utils.dateparse import parse_datetime
    assert plan.current_period_end == parse_datetime((now + timedelta(days=30)).isoformat())
    assert plan.current_period_start == views_module.billing_period_start(plan.current_period_end, "month", 1)
    # Complete resulting tuple: exact billing_synced_at == the event's
    # updated_at (would catch removing the synced_at assignment at
    # views.py:203-206), and the AccountPlan row's identity preserved
    # (same pk, not replaced by a new row).
    assert plan.billing_synced_at == parse_datetime(now.isoformat())
    assert plan.pk == plan_id_before
    # Downstream effect: an active paid plan can create an inbox.
    import django.db.transaction as dj_transaction
    with dj_transaction.atomic():
        assert can_create_inbox(user) is True


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_11_created_active_variant_scale_sets_plan_scale():
    user = make_user("t11")
    plan_id_before = AccountPlan.objects.get(user=user).pk
    client = Client()
    now = timezone.now()
    renews_at = now + timedelta(days=30)
    body = event_body(
        "subscription_created", user_pk=user.pk, variant_id="2176478", status="active",
        created_at=now.isoformat(), renews_at=renews_at.isoformat(),
        updated_at=now.isoformat(), sub_id="sub_11", customer_id="cust_11",
    )
    resp = post_event(client, body)
    assert resp.status_code == 200
    plan = AccountPlan.objects.get(user=user)
    assert plan.plan == AccountPlan.Plan.SCALE
    assert plan.status == AccountPlan.Status.ACTIVE
    assert plan.stripe_subscription_id == "sub_11"
    assert plan.stripe_customer_id == "cust_11"
    assert plan.stripe_price_id == "2176478"
    from django.utils.dateparse import parse_datetime
    # Complete resulting tuple: exact billing_synced_at == the event's
    
    # renews_at - 1 month, current_period_end == renews_at), plus row identity.
    assert plan.billing_synced_at == parse_datetime(now.isoformat())
    assert plan.current_period_start == views_module.billing_period_start(renews_at, "month", 1)
    assert plan.current_period_end == parse_datetime(renews_at.isoformat())
    assert plan.pk == plan_id_before
    import django.db.transaction as dj_transaction
    with dj_transaction.atomic():
        assert can_create_inbox(user) is True


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_12_payment_failed_past_due_then_cannot_create_inbox():  # BL-08
    user = make_user("t12")
    client = Client()
    now = timezone.now().isoformat()
    created_at = timezone.now().isoformat()
    renews_at = (timezone.now() + timedelta(days=30)).isoformat()
    created_body = event_body(
        "subscription_created", user_pk=user.pk, status="active", updated_at=now,
        created_at=created_at, renews_at=renews_at,
        sub_id="sub_12", customer_id="cust_12", webhook_id="wh12a",
    )
    post_event(client, created_body)
    seeded = AccountPlan.objects.get(user=user)
    from django.utils.dateparse import parse_datetime
    
    period_start = views_module.billing_period_start(seeded.current_period_end, "month", 1)
    assert seeded.current_period_start == period_start
    assert seeded.current_period_end == parse_datetime(renews_at)
    seeded_ids_and_periods = (
        seeded.plan,
        seeded.stripe_subscription_id,
        seeded.stripe_customer_id,
        seeded.stripe_price_id,
        seeded.current_period_start,
        seeded.current_period_end,
    )
    plan_id_before = seeded.pk

    later = (timezone.now() + timedelta(minutes=1)).isoformat()
    failed_body = event_body(
        "subscription_payment_failed", user_pk=user.pk, status="past_due", updated_at=later,
        sub_id="sub_12", webhook_id="wh12b",
    )
    resp = post_event(client, failed_body)
    assert resp.status_code == 200
    plan = AccountPlan.objects.get(user=user)
    assert plan.status == AccountPlan.Status.PAST_DUE
    # Guard against a no-op subscription-create leaving FREE/PAST_DUE and
    # still passing: the plan MUST stay paid, and every id/period from the
    # created event must be intact (not cleared by the failed-payment path).
    assert plan.plan == AccountPlan.Plan.PAID
    assert (
        plan.plan,
        plan.stripe_subscription_id,
        plan.stripe_customer_id,
        plan.stripe_price_id,
        plan.current_period_start,
        plan.current_period_end,
    ) == seeded_ids_and_periods
    from django.utils.dateparse import parse_datetime
    # Explicit non-vacuous re-check: the exact period from the creation
    # event survives the payment-failure webhook unchanged.
    assert plan.current_period_start == seeded.current_period_start  # non-None, pinned above
    assert plan.current_period_end == parse_datetime(renews_at)
    assert plan.billing_synced_at == parse_datetime(later)
    assert plan.pk == plan_id_before
    import django.db.transaction as dj_transaction
    with dj_transaction.atomic():
        assert can_create_inbox(user) is False


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_13_payment_success_after_past_due_active_period_refreshed():
    user = make_user("t13")
    now = timezone.now()
    # SEED: paid + PAST_DUE with a real subscription id + period, as a genuine
    # renewal scenario (not a bare status flip).
    AccountPlan.objects.filter(user=user).update(
        plan=AccountPlan.Plan.PAID,
        status=AccountPlan.Status.PAST_DUE,
        stripe_subscription_id="sub_13",
        stripe_customer_id="cust_13",
        stripe_price_id="2176466",
        current_period_start=now - timedelta(days=31),
        current_period_end=now - timedelta(days=1),
        billing_synced_at=now - timedelta(days=1),
    )
    client = Client()
    renews_at = now + timedelta(days=30)
    body = event_body(
        "subscription_payment_success",
        user_pk=user.pk,
        sub_id="sub_13",
        renews_at=renews_at.isoformat(),
        updated_at=now.isoformat(),
    )
    resp = post_event(client, body)
    assert resp.status_code == 200
    plan = AccountPlan.objects.get(user=user)
    # Plan stays paid, status flips to ACTIVE, period end == exact renews_at.
    assert plan.plan == AccountPlan.Plan.PAID
    assert plan.status == AccountPlan.Status.ACTIVE
    from django.utils.dateparse import parse_datetime
    assert plan.current_period_end == parse_datetime(renews_at.isoformat())
    assert plan.stripe_subscription_id == "sub_13"
    # Downstream + full-tuple completeness: customer_id/price_id preserved
    # (payment_success never touches them), period start re-derived from the
    
    # and billing_synced_at advanced to the exact event updated_at.
    assert plan.stripe_customer_id == "cust_13"
    assert plan.stripe_price_id == "2176466"
    assert plan.current_period_start == views_module.billing_period_start(plan.current_period_end, "month", 1)
    assert plan.billing_synced_at == parse_datetime(now.isoformat())
    import django.db.transaction as dj_transaction
    with dj_transaction.atomic():
        assert can_create_inbox(user) is True


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_14_cancelled_keeps_plan_grace_then_expired_clears_ids():  # BL-05
    user = make_user("t14")
    client = Client()
    now = timezone.now()
    created_at = now.isoformat()
    created_body = event_body(
        "subscription_created",
        user_pk=user.pk,
        status="active",
        updated_at=now.isoformat(),
        created_at=created_at, renews_at=(now + timedelta(days=7)).isoformat(),
        sub_id="sub_14",
        customer_id="cust_14",
        webhook_id="wh14a",
    )
    post_event(client, created_body)
    seeded = AccountPlan.objects.get(user=user)
    seeded_period_start = seeded.current_period_start
    from django.utils.dateparse import parse_datetime
    # The creation event supplies a non-null renews_at, so this pins the seeded
    
    assert seeded_period_start == views_module.billing_period_start(seeded.current_period_end, "month", 1)
    assert seeded.billing_synced_at == parse_datetime(now.isoformat())

    ends_at = (now + timedelta(days=10)).isoformat()
    t2 = (now + timedelta(minutes=1)).isoformat()
    cancelled_body = event_body(
        "subscription_cancelled",
        user_pk=user.pk,
        status="cancelled",
        ends_at=ends_at,
        updated_at=t2,
        sub_id="sub_14",
        webhook_id="wh14b",
    )
    resp = post_event(client, cancelled_body)
    assert resp.status_code == 200
    plan = AccountPlan.objects.get(user=user)
    assert plan.plan == AccountPlan.Plan.PAID
    assert plan.status == AccountPlan.Status.ACTIVE
    assert plan.current_period_end == parse_datetime(ends_at)
    assert plan.stripe_subscription_id == "sub_14"
    # customer_id retained across the cancel transition, period start
    # untouched, and billing_synced_at advanced to the cancel event's
    # exact updated_at.
    assert plan.stripe_customer_id == "cust_14"
    assert plan.current_period_start == seeded_period_start
    assert plan.billing_synced_at == parse_datetime(t2)
    # Downstream during grace: still allowed to create an inbox.
    import django.db.transaction as dj_transaction
    with dj_transaction.atomic():
        assert can_create_inbox(user) is True

    t3 = (now + timedelta(minutes=2)).isoformat()
    expired_body = event_body(
        "subscription_expired", user_pk=user.pk, updated_at=t3, sub_id="sub_14", webhook_id="wh14c"
    )
    resp2 = post_event(client, expired_body)
    assert resp2.status_code == 200
    plan.refresh_from_db()
    assert plan.plan == AccountPlan.Plan.FREE
    assert plan.status == AccountPlan.Status.ACTIVE
    assert plan.stripe_subscription_id is None
    assert plan.stripe_price_id is None
    # customer_id retention across BOTH steps (expiry never clears the
    # catalog customer id), current_period_end preserved after expiry (the
    # grace value stays, not nulled), period start still untouched, and
    # billing_synced_at advanced to the expiry event's exact updated_at.
    assert plan.stripe_customer_id == "cust_14"
    assert plan.current_period_end == parse_datetime(ends_at)
    assert plan.current_period_start == seeded_period_start
    assert plan.billing_synced_at == parse_datetime(t3)
    # Downstream after expiry: FREE-plan quota now governs, with the correct
    # inbox limit for that plan (not the paid plan's).
    from apps.accounts.entitlements import plan_limits
    with dj_transaction.atomic():
        free_inboxes_allowed = plan_limits(AccountPlan.Plan.FREE)["inboxes"]
        assert can_create_inbox(user) == (0 < free_inboxes_allowed)


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_15_paused_past_due_unpause_active():
    user = make_user("t15")
    now = timezone.now()
    # SEED: paid + ACTIVE with a real subscription id.
    AccountPlan.objects.filter(user=user).update(
        plan=AccountPlan.Plan.PAID,
        status=AccountPlan.Status.ACTIVE,
        stripe_subscription_id="sub_15",
        stripe_customer_id="cust_15",
        stripe_price_id="2176466",
        billing_synced_at=now,
    )
    client = Client()

    t2 = (now + timedelta(minutes=1)).isoformat()
    paused_body = event_body(
        "subscription_paused", user_pk=user.pk, status="paused", updated_at=t2,
        sub_id="sub_15", webhook_id="wh15b",
    )
    resp = post_event(client, paused_body)
    assert resp.status_code == 200
    plan = AccountPlan.objects.get(user=user)
    # Must NOT drop to FREE — plan stays paid, only status changes.
    assert plan.plan == AccountPlan.Plan.PAID
    assert plan.status == AccountPlan.Status.PAST_DUE

    t3 = (now + timedelta(minutes=2)).isoformat()
    resume_body = event_body(
        "subscription_updated", user_pk=user.pk, status="active", updated_at=t3,
        sub_id="sub_15", webhook_id="wh15c",
    )
    resp2 = post_event(client, resume_body)
    assert resp2.status_code == 200
    plan.refresh_from_db()
    # Resume: must NOT drop to FREE — plan stays paid, status back to ACTIVE.
    assert plan.plan == AccountPlan.Plan.PAID
    assert plan.status == AccountPlan.Status.ACTIVE


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_16_refund_never_changes_plan():
    """refund-webhook-2026-10: a refund is a money fact only. It used to
    drop the account to FREE and clear the subscription id (orphaning a live
    LS subscription). Plan state now follows subscription events only — full
    coverage in test_billing_refund.py."""
    user = make_user("t16")
    now = timezone.now()
    AccountPlan.objects.filter(user=user).update(
        plan=AccountPlan.Plan.PAID,
        status=AccountPlan.Status.ACTIVE,
        stripe_subscription_id="sub_16",
        stripe_customer_id="cust_16",
        stripe_price_id="2176466",
        billing_synced_at=now,
    )
    before = _plan_snapshot(user)
    client = Client()

    t2 = (now + timedelta(minutes=1)).isoformat()
    refund_body = event_body(
        "subscription_payment_refunded", user_pk=user.pk, updated_at=t2, sub_id="sub_16", webhook_id="wh16b",
        extra_attrs={"total": 1200, "refunded_amount": 1200, "refunded": True, "subscription_id": 16},
    )
    resp = post_event(client, refund_body)
    assert resp.status_code == 200
    assert _plan_snapshot(user) == before


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_17_unknown_variant_id_200_no_op_error_logged():  # BL-17
    user = make_user("t17")
    client = Client()
    now = timezone.now().isoformat()
    body = event_body(
        "subscription_created", user_pk=user.pk, variant_id="9999999", status="active", updated_at=now
    )
    resp = post_event(client, body)
    assert resp.status_code == 200
    assert AccountPlan.objects.get(user=user).plan == AccountPlan.Plan.FREE


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_18_garbage_missing_user_pk_200_no_op_never_500():
    user = make_user("t18")
    before = _plan_snapshot(user)
    client = Client()

    # Missing user_pk: distinct webhook_id so it doesn't dedupe-exit.
    body = event_body(
        "subscription_created", custom_data={}, status="active", webhook_id="wh18_missing"
    )
    resp = post_event(client, body)
    assert resp.status_code == 200
    assert resp.json().get("duplicate") is not True

    # Non-int user_pk: separate distinct webhook_id.
    body2 = event_body(
        "subscription_created", custom_data={"user_pk": "not-an-int"}, status="active",
        webhook_id="wh18_nonint",
    )
    resp2 = post_event(client, body2)
    assert resp2.status_code == 200
    assert resp2.json().get("duplicate") is not True

    # Neither event mutated any plan (they carry no valid target user).
    assert _plan_snapshot(user) == before
    assert BillingEvent.objects.filter(event_key__in=["wh18_missing", "wh18_nonint"]).count() == 2


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_18b_non_dict_attributes_data_custom_data_200_noop_plan_untouched():
    """P2-2 isinstance-dict guards (views.py: meta_custom_data ~140-143,
    _apply_ls_event ~162 for `data`, ~166 for `attributes`) never execute
    under the shared event_body() builder — it always nests well-typed
    dicts. Build raw malformed payloads by hand here so each guard is
    actually exercised, and prove each is a clean 200 no-op that leaves
    the target AccountPlan byte-unchanged. Three distinct webhook_ids so
    no event dedupe-exits before reaching the guard under test."""
    user = make_user("t18b")
    before = _plan_snapshot(user)
    client = Client()

    # (a) attributes is a non-empty list, not a dict -> the ~166 guard in
    # _apply_ls_event must catch it before any attributes.get(...) call.
    # (An EMPTY list is falsy and gets normalized away by `or {}` before
    # the isinstance check ever matters — it must be non-empty to force
    # the isinstance branch to actually execute.)
    body_a = json.dumps(
        {
            "meta": {
                "event_name": "subscription_created",
                "webhook_id": "wh18b_list_attrs",
                "custom_data": {"user_pk": str(user.pk)},
            },
            "data": {"id": "sub_18b_a", "attributes": ["not", "a", "dict"]},
        }
    ).encode("utf-8")
    resp_a = post_event(client, body_a)
    assert resp_a.status_code == 200
    assert resp_a.json().get("duplicate") is not True
    assert _plan_snapshot(user) == before

    # (b) data is a string, not a dict -> the ~162 guard in _apply_ls_event
    # must catch it before data.get("attributes") is ever called.
    body_b = json.dumps(
        {
            "meta": {
                "event_name": "subscription_created",
                "webhook_id": "wh18b_str_data",
                "custom_data": {"user_pk": str(user.pk)},
            },
            "data": "x",
        }
    ).encode("utf-8")
    resp_b = post_event(client, body_b)
    assert resp_b.status_code == 200
    assert resp_b.json().get("duplicate") is not True
    assert _plan_snapshot(user) == before

    # (c) meta.custom_data is a string, not a dict -> the ~140-143 guard in
    # meta_custom_data() must catch it before custom_data.get(...) is called.
    body_c = json.dumps(
        {
            "meta": {
                "event_name": "subscription_created",
                "webhook_id": "wh18b_str_custom_data",
                "custom_data": "x",
            },
            "data": {"id": "sub_18b_c", "attributes": {"status": "active"}},
        }
    ).encode("utf-8")
    resp_c = post_event(client, body_c)
    assert resp_c.status_code == 200
    assert resp_c.json().get("duplicate") is not True
    assert _plan_snapshot(user) == before

    # All three recorded distinct BillingEvent rows (no dedupe interference)
    # and none of them touched the plan.
    assert (
        BillingEvent.objects.filter(
            event_key__in=["wh18b_list_attrs", "wh18b_str_data", "wh18b_str_custom_data"]
        ).count()
        == 3
    )
    assert _plan_snapshot(user) == before


# --- 19-21: collision proofs -------------------------------------------------


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_19_two_users_same_variant_both_rows_persist():
    user_a = make_user("t19a")
    user_b = make_user("t19b")
    client = Client()
    now = timezone.now().isoformat()
    body_a = event_body(
        "subscription_created", user_pk=user_a.pk, variant_id="2176466", status="active",
        updated_at=now, sub_id="sub_19a", webhook_id="wh19a",
    )
    body_b = event_body(
        "subscription_created", user_pk=user_b.pk, variant_id="2176466", status="active",
        updated_at=now, sub_id="sub_19b", webhook_id="wh19b",
    )
    resp_a = post_event(client, body_a)
    resp_b = post_event(client, body_b)
    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    plan_a = AccountPlan.objects.get(user=user_a)
    plan_b = AccountPlan.objects.get(user=user_b)
    assert plan_a.stripe_price_id == plan_b.stripe_price_id == "2176466"


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_20_same_customer_id_two_accountplan_rows_persist():
    user_a = make_user("t20a")
    user_b = make_user("t20b")
    client = Client()
    now = timezone.now().isoformat()
    body_a = event_body(
        "subscription_created", user_pk=user_a.pk, customer_id="cust_shared", status="active",
        updated_at=now, sub_id="sub_20a", webhook_id="wh20a",
    )
    body_b = event_body(
        "subscription_created", user_pk=user_b.pk, customer_id="cust_shared", status="active",
        updated_at=now, sub_id="sub_20b", webhook_id="wh20b",
    )
    resp_a = post_event(client, body_a)
    resp_b = post_event(client, body_b)
    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    plan_a = AccountPlan.objects.get(user=user_a)
    plan_b = AccountPlan.objects.get(user=user_b)
    assert plan_a.stripe_customer_id == plan_b.stripe_customer_id == "cust_shared"


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_21_duplicate_subscription_id_across_two_users_integrity_error():
    user_a = make_user("t21a")
    user_b = make_user("t21b")
    AccountPlan.objects.filter(user=user_a).update(stripe_subscription_id="sub_dupe")
    from django.db import IntegrityError

    with pytest.raises(IntegrityError):
        AccountPlan.objects.filter(user=user_b).update(stripe_subscription_id="sub_dupe")


# --- 22-25: checkout ---------------------------------------------------------


@override_settings(ENABLE_BILLING=True)
def test_22_client_posted_bad_plan_400_no_ls_call():
    user = make_user("chk22", plan=AccountPlan.Plan.FREE)
    client = login(user)
    with patch("apps.billing.views._ls_post") as mock_post:
        # "enterprise" is contact-sales only (never purchasable); "business"
        # became purchasable 2026-09-30 (test_billing_business.py).
        resp = client.post(reverse("dashboard:billing-checkout"), {"plan": "enterprise"})
        assert resp.status_code == 400
        mock_post.assert_not_called()

        resp_free = client.post(reverse("dashboard:billing-checkout"), {"plan": "free"})
        assert resp_free.status_code == 400
        mock_post.assert_not_called()

        resp2 = client.post(reverse("dashboard:billing-checkout"), {"plan": "<script>"})
        assert resp2.status_code == 400
        mock_post.assert_not_called()


@override_settings(ENABLE_BILLING=True)
def test_23_outbound_user_pk_is_tenant_pk_string_never_client_supplied():
    user = make_user("chk23", plan=AccountPlan.Plan.FREE)
    client = login(user)
    captured = {}

    def fake_post(path, body, **kwargs):
        captured["body"] = body
        return {"data": {"attributes": {"url": "https://checkout.example/x"}}}

    with patch("apps.billing.views._ls_post", side_effect=fake_post):
        resp = client.post(
            reverse("dashboard:billing-checkout"),
            {"plan": "paid", "user_pk": "999999", "variant": "evil"},
        )
    assert resp.status_code == 303
    custom = captured["body"]["data"]["attributes"]["checkout_data"]["custom"]
    assert custom["user_pk"] == str(user.pk)
    assert isinstance(custom["user_pk"], str)


@override_settings(ENABLE_BILLING=True)
def test_24_demo_agent_403_member_require_owner_false_403():
    from apps.accounts.services import AGENT_ACCOUNT_DOMAIN

    agent_user = make_user("chk24agent", email=f"agent@{AGENT_ACCOUNT_DOMAIN}")
    client = login(agent_user)
    resp = client.post(reverse("dashboard:billing-checkout"), {"plan": "paid"})
    assert resp.status_code == 403

    owner = make_user("chk24owner", plan=AccountPlan.Plan.BUSINESS)
    member = make_user("chk24member", plan=AccountPlan.Plan.FREE)
    with override_settings(ENABLE_TEAMS=True):
        TeamMembership.objects.create(
            account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER
        )
        member_client = login(member)
        resp2 = member_client.post(reverse("dashboard:billing-checkout"), {"plan": "paid"})
        assert resp2.status_code == 403


@override_settings(ENABLE_BILLING=True, ENABLE_MULTI_ACCOUNT=True)
def test_25_multi_account_switched_session_user_pk_is_workspace_tenant_pk():
    owner = make_user("chk25owner", plan=AccountPlan.Plan.PAID)
    link = create_owned_account(owner, "Client A")
    assert link.account_user_id != owner.pk, "workspace tenant pk must differ from owner pk"
    client = login(owner)
    session = client.session
    session[SESSION_KEY] = link.account_user_id
    session.save()

    captured = {}

    def fake_post(path, body, **kwargs):
        captured["body"] = body
        return {"data": {"attributes": {"url": "https://checkout.example/x"}}}

    with patch("apps.billing.views._ls_post", side_effect=fake_post):
        resp = client.post(reverse("dashboard:billing-checkout"), {"plan": "paid"})
    assert resp.status_code == 303
    custom = captured["body"]["data"]["attributes"]["checkout_data"]["custom"]
    assert custom["user_pk"] == str(link.account_user_id)
    # Explicit: the outbound user_pk equals the WORKSPACE tenant pk, and is
    
    assert custom["user_pk"] != str(owner.pk)


# --- HOTFIX P0 (checkout-form-action): CSP form-action <-> redirect Location --


def _csp_form_action_origins(header):
    for part in header.split(";"):
        tokens = part.split()
        if tokens and tokens[0] == "form-action":
            return set(tokens[1:])
    return set()


@override_settings(ENABLE_BILLING=True)
def test_checkout_redirect_location_origin_is_inside_csp_form_action():
    """The checkout POST's 303 Location and the response's own CSP
    form-action must never drift: parse both and assert containment, so a
    future change to one without the other fails loudly instead of quietly
    reintroducing the blocked-checkout bug."""
    from urllib.parse import urlsplit

    user = make_user("cspchk", plan=AccountPlan.Plan.FREE)
    client = login(user)

    def fake_post(path, body, **kwargs):
        return {"data": {"attributes": {"url": "https://mailsocket.lemonsqueezy.com/checkout/custom/abc"}}}

    with patch("apps.billing.views._ls_post", side_effect=fake_post):
        resp = client.post(reverse("dashboard:billing-checkout"), {"plan": "paid"})

    assert resp.status_code == 303
    location = resp["Location"]
    location_origin = f"{urlsplit(location).scheme}://{urlsplit(location).netloc}"

    allowed = _csp_form_action_origins(resp["Content-Security-Policy"])
    assert "'self'" in allowed
    assert location_origin in allowed, (
        f"checkout redirect origin {location_origin!r} is not in the CSP "
        f"form-action allowlist {allowed!r}"
    )


# --- 26-28: flag-off identity -------------------------------------------------


def test_26_flag_off_checkout_billing_portal_webhook_all_404():
    user = make_user("flagoff26", plan=AccountPlan.Plan.PAID)
    client = login(user)
    assert client.get(reverse("dashboard:billing-page")).status_code == 404
    assert client.post(reverse("dashboard:billing-checkout"), {"plan": "paid"}).status_code == 404
    assert client.post(reverse("dashboard:billing-portal")).status_code == 404

    anon = Client()
    body = event_body("subscription_created", user_pk=user.pk)
    resp = anon.post(
        WEBHOOK_URL,
        data=body,
        content_type="application/json",
        HTTP_X_SIGNATURE=sign(body),
    )
    assert resp.status_code == 404


def test_27_production_settings_flag_off_no_ls_env_imports_clean():
    """Deploy-incident regression mirror of the OUTBOUND_DKIM_MASTER_KEY
    incident (config/settings/production.py:21-31): proves the ENABLE_BILLING
    gate is the ONLY reason a flag-off import succeeds without LS env vars."""
    import importlib
    import os
    import sys

    base_required_env = {
        "SECRET_KEY": "test-secret-key",
        "ALLOWED_HOSTS": "example.com",
        "INGEST_SHARED_SECRET": "test-ingest-shared-secret",
        "AUTH_RATE_LIMIT_SECRET": "test-auth-rate-limit-secret",
        "API_KEY_PEPPER": "test-api-key-pepper",
        "WEBHOOK_SIGNING_MASTER_KEY": "test-webhook-signing-master-key",
        "DB_NAME": "test_db",
        "DB_USERNAME": "test_user",
        "DB_PASS": "test_pass",
        "DB_HOST": "localhost",
        "DB_PORT": "5432",
    }
    production_module = "config.settings.production"
    base_module = "config.settings.base"

    def reimport(env):
        old_environ = os.environ.copy()
        os.environ.clear()
        os.environ.update(env)
        for mod_name in (production_module, base_module):
            sys.modules.pop(mod_name, None)
        try:
            return importlib.import_module(production_module)
        finally:
            os.environ.clear()
            os.environ.update(old_environ)
            for mod_name in (production_module, base_module):
                sys.modules.pop(mod_name, None)

    # (a) flag-ON + missing LS_API_KEY/LS_WEBHOOK_SECRET -> ImproperlyConfigured.
    from django.core.exceptions import ImproperlyConfigured
    with pytest.raises(ImproperlyConfigured):
        reimport({**base_required_env, "ENABLE_BILLING": "true"})

    # (b) flag-ON + secrets present -> imports clean.
    settings_on = reimport({
        **base_required_env,
        "ENABLE_BILLING": "true",
        "LS_API_KEY": "test-ls-api-key",
        "LS_WEBHOOK_SECRET": "test-ls-webhook-secret",
    })
    assert settings_on.ENABLE_BILLING is True
    assert settings_on.LS_API_KEY == "test-ls-api-key"
    assert settings_on.LS_WEBHOOK_SECRET == "test-ls-webhook-secret"

    # (c) flag-OFF + no LS env -> clean (proves "only because flag off").
    settings_off = reimport({**base_required_env, "ENABLE_BILLING": "false"})
    assert settings_off.ENABLE_BILLING is False
    assert settings_off.LS_API_KEY == ""
    assert settings_off.LS_WEBHOOK_SECRET == "development-only-ls-webhook-secret"


def test_28_pricing_scale_available_false_byte_identical_flag_off():
    from apps.dashboard.views import _pricing_context

    # Explicit COMMITTED baseline for the entire flag-off pricing structure —
    # every tier, every field, written independently (not derived from
    # _pricing_context() itself). A silent price/copy/limit change on any
    # tier fails this test.
    expected_pricing_flag_off = {
        "tiers": [
            {
                "key": "free",
                "name": "Free",
                "price": "$0",
                "available": True,
                "badge": "Available now",
                
                "inboxes_display": "3 inboxes",
                "messages_display": "1,000 / month",
                "verify_display": "1,500 / hour",
                "retention_display": "1 day",
                "concurrent_waits": 2,
                "webhooks_display": "Not included",
                "domains_display": "Not included",
                "extra": None,
                "cta_text": "Start free",
                "cta_url_name": "accounts:register",
                "cta_mailto": None,
                "cta_next": None,
            },
            {
                "key": "paid",
                "name": "Pro",
                "price": "$12/mo",
                "available": True,
                "badge": "Available now",
                "inboxes_display": "10 inboxes",
                "messages_display": "10,000 / month",
                "verify_display": "1,500 / hour",
                "retention_display": "7 days",
                "concurrent_waits": 10,
                # Trust pass A (#24): one webhook footnote on /pricing/ instead of per-cell detail.
                "webhooks_display": "Included",
                "domains_display": "Not included",
                "extra": None,
                "cta_text": "Start free",
                "cta_url_name": "accounts:register",
                "cta_mailto": None,
                "cta_next": None,  # ENABLE_BILLING off here: no billing page to return to
            },
            {
                "key": "scale",
                "name": "Scale",
                "price": "$49/mo",
                "available": False,
                "badge": "Coming soon",
                "inboxes_display": "50 inboxes",
                "messages_display": "50,000 / month",
                # QA #8: Scale is an enforced tier -> the real hourly limit.
                "verify_display": "1,500 / hour",
                "retention_display": "30 days",
                "concurrent_waits": 20,
                "webhooks_display": "Included",
                
                "domains_display": "3 domains",
                "extra": None,
                "cta_text": "Contact us",
                "cta_url_name": None,
                "cta_mailto": "sales@mailsocket.app",
                "cta_next": None,
            },
            {
                "key": "business",
                "name": "Business",
                "price": "$149/mo",
                "available": False,
                "badge": "Coming soon",
                "inboxes_display": "200 inboxes",
                "messages_display": "250,000 / month",
                # Real enforced hourly limit (no planned quota; full SMTP
                # verify is off in prod).
                "verify_display": "1,500 / hour",
                "retention_display": "90 days",
                # QA #8: from BILLING_PLAN_LIMITS["business"] (enforced cap).
                "concurrent_waits": 40,
                "webhooks_display": "Included",
                
                "domains_display": "10 domains",
                "extra": "Team seats, catch-all",
                "cta_text": "Contact us",
                "cta_url_name": None,
                "cta_mailto": "sales@mailsocket.app",
                "cta_next": None,
            },
            {
                "key": "enterprise",
                "name": "Enterprise",
                # Contact sales, price "Custom", no SSO promise; provisioned
                # manually as plan=business (operations notes).
                "price": "Custom",
                "available": False,
                "badge": "Contact sales",
                "inboxes_display": "As Business",
                "messages_display": "As Business",
                "verify_display": "1,500 / hour",
                "retention_display": "As Business",
                "concurrent_waits": "As Business",
                "webhooks_display": "Included",
                "domains_display": "As Business",
                "extra": "Everything in Business, custom contract terms",
                "cta_text": "Contact sales",
                "cta_url_name": None,
                "cta_mailto": "sales@mailsocket.app",
                "cta_next": None,
            },
        ],
    }

    with override_settings(ENABLE_BILLING=False):
        ctx = _pricing_context()

    # Assert the ENTIRE flag-off structure against the independently
    # committed literal — not impl-vs-impl. json.dumps(sort_keys=True) byte
    # equality as the primary proof, dict equality as a belt-and-suspenders
    # check.
    assert json.dumps(ctx, sort_keys=True) == json.dumps(expected_pricing_flag_off, sort_keys=True)
    assert ctx == expected_pricing_flag_off

    scale = next(tier for tier in ctx["tiers"] if tier["key"] == "scale")
    assert scale["available"] is False
    assert scale["badge"] == "Coming soon"
    assert scale["cta_text"] == "Contact us"
    assert scale["cta_url_name"] is None
    assert scale["cta_mailto"] == "sales@mailsocket.app"

    business = next(tier for tier in ctx["tiers"] if tier["key"] == "business")
    assert business["available"] is False
    assert business["badge"] == "Coming soon"
    assert business["cta_mailto"] == "sales@mailsocket.app"

    # Second assertion: flag-off vs flag-on — only the self-serve billing-gated
    # tiers (scale, business) flip their availability-related fields; every
    # other tier is byte-identical.
    gated = ("scale", "business")
    other_keys = [tier["key"] for tier in ctx["tiers"] if tier["key"] not in gated]
    assert other_keys == ["free", "paid", "enterprise"]
    with override_settings(ENABLE_BILLING=True):
        ctx_on = _pricing_context()
    # ux3 N2: Pro's cta_next follows the billing page like Scale/Business
    # (None when billing is off: there is no billing page to land on).
    # Every other field of the non-gated tiers is byte-identical.
    def _without_next(tier):
        return {k: v for k, v in tier.items() if k != "cta_next"} if tier["key"] == "paid" else tier

    non_gated_off = [_without_next(t) for t in ctx["tiers"] if t["key"] not in gated]
    non_gated_on = [_without_next(t) for t in ctx_on["tiers"] if t["key"] not in gated]
    assert non_gated_off == non_gated_on, "non-gated tiers must be byte-identical flag-off vs flag-on"
    paid_off = next(t for t in ctx["tiers"] if t["key"] == "paid")
    paid_on = next(t for t in ctx_on["tiers"] if t["key"] == "paid")
    assert paid_off["cta_next"] is None
    assert paid_on["cta_next"] == reverse("dashboard:billing-page")
    flip = {"available", "badge", "cta_text", "cta_url_name", "cta_mailto", "cta_next"}
    for key in gated:
        off = next(t for t in ctx["tiers"] if t["key"] == key)
        on = next(t for t in ctx_on["tiers"] if t["key"] == key)
        assert {k: v for k, v in off.items() if k not in flip} == {k: v for k, v in on.items() if k not in flip}
        assert on["available"] is True
        assert on["badge"] == "Available now"
        assert on["cta_text"] == "Start free"
        assert on["cta_url_name"] == "accounts:register"
        assert on["cta_mailto"] is None
        assert on["cta_next"] == reverse("dashboard:billing-page")


# --- 29: portal ---------------------------------------------------------------


@override_settings(ENABLE_BILLING=True)
def test_29_portal_no_sub_id_button_absent_with_id_redirect_not_persisted():
    user = make_user("portal29", plan=AccountPlan.Plan.PAID)
    client = login(user)

    # Case A: no subscription id -> the "Manage billing" portal button/form
    # must be ABSENT (positive assertion, not an OR with something else).
    resp = client.get(reverse("dashboard:billing-page"))
    assert resp.status_code == 200
    assert b"Manage billing" not in resp.content
    assert reverse("dashboard:billing-portal").encode() not in resp.content

    resp_no_sub = client.post(reverse("dashboard:billing-portal"))
    assert resp_no_sub.status_code == 404

    # Case B: with a subscription id -> button present, and POST redirects
    # (303) to the mocked LS customer-portal URL.
    AccountPlan.objects.filter(user=user).update(stripe_subscription_id="sub_29")
    plan_before = AccountPlan.objects.get(user=user)
    fields_before = (
        plan_before.stripe_customer_id,
        plan_before.stripe_subscription_id,
        plan_before.stripe_price_id,
    )

    resp_with_sub = client.get(reverse("dashboard:billing-page"))
    assert b"Manage billing" in resp_with_sub.content

    with patch(
        "apps.billing.views._ls_get",
        return_value={"data": {"attributes": {"urls": {"customer_portal": "https://portal.example/x"}}}},
    ):
        resp2 = client.post(reverse("dashboard:billing-portal"))
    assert resp2.status_code == 303
    assert resp2.url == "https://portal.example/x"

    # URL not persisted anywhere on the row — re-read from DB, fields
    
    # fields that were captured before the portal call, not an unrelated one).
    plan_after = AccountPlan.objects.get(user=user)
    fields_after = (
        plan_after.stripe_customer_id,
        plan_after.stripe_subscription_id,
        plan_after.stripe_price_id,
    )
    assert fields_after == fields_before
    assert "portal.example" not in (plan_after.stripe_customer_id or "")
    assert "portal.example" not in (plan_after.stripe_subscription_id or "")
    assert plan_after.stripe_subscription_id == "sub_29"
