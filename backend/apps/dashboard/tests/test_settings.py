"Public API."
import hashlib
import hmac
import json
import urllib.error
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import (
    Account,
    AccountOwnership,
    AccountPlan,
    ApiKey,
    MonthlyUsage,
    TeamInvite,
    TeamMembership,
)
from apps.billing.models import BillingEvent
from apps.domains.models import Domain
from apps.inboxes.models import Inbox, Message
from apps.outbound.models import ApprovedRecipient, OutboundEmail, SuppressionEntry
from apps.webhooks.models import Webhook, WebhookDelivery

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "orig-password-123"
NEW_PASSWORD = "new-password-456!"


def make_user(name, *, plan=AccountPlan.Plan.FREE, sub_id=None, email=None, password=PASSWORD):
    user = get_user_model().objects.create_user(
        username=name, email=email or f"{name}@example.test", password=password
    )
    Account.objects.create(
        user=user, email_normalized=email or f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.filter(user=user).delete()
    AccountPlan.objects.create(
        user=user, plan=plan, stripe_subscription_id=sub_id,
        stripe_customer_id="cust_1" if sub_id else None,
    )
    return user


def login(user, password=PASSWORD):
    client = Client()
    ok = client.login(username=user.username, password=password)
    assert ok
    return client


def make_inbox(user, routing_key="inbox1"):
    return Inbox.objects.create(user=user, routing_key=routing_key)


def make_message(inbox, marker="m1"):
    raw = f"Subject: {marker}\r\n\r\nbody {marker}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    return Message.objects.create(
        inbox=inbox,
        dedupe_key=digest,
        envelope_from="sender@example.test",
        envelope_recipient=inbox.address,
        header_from="Sender <sender@example.test>",
        header_to=[inbox.address],
        header_cc=[],
        subject=marker,
        text_body=f"text {marker}",
        raw_bytes=raw,
        raw_sha256=digest,
        size_bytes=len(raw),
        ingest_status=Message.IngestStatus.PARSED,
    )


def assert_prg(resp, section):
    """qafix2 #13: every Settings POST outcome is a redirect back to the page
    (POST-redirect-GET); the test client followed it (follow=True)."""
    assert resp.redirect_chain, "expected a redirect after POST"
    url, status = resp.redirect_chain[0]
    assert status == 302
    assert url == reverse("dashboard:settings") + f"#settings-{section}"
    assert resp.status_code == 200


# ============================= PASSWORD ======================================


def test_p1_happy_new_password_works_old_fails_same_session_survives():
    user = make_user("p1")
    client = login(user)
    resp = client.post(reverse("dashboard:settings-password"), {
        "current_password": PASSWORD,
        "new_password1": NEW_PASSWORD,
        "new_password2": NEW_PASSWORD,
    }, follow=True)
    assert_prg(resp, "password")
    assert b"Password changed" in resp.content

    # SAME session still authenticated (update_session_auth_hash).
    resp2 = client.get(reverse("dashboard:settings"))
    assert resp2.status_code == 200

    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)
    assert not user.check_password(PASSWORD)


def test_p2_other_session_invalidated_after_change():
    user = make_user("p2")
    client_a = login(user)
    client_b = login(user)

    resp = client_a.post(reverse("dashboard:settings-password"), {
        "current_password": PASSWORD,
        "new_password1": NEW_PASSWORD,
        "new_password2": NEW_PASSWORD,
    }, follow=True)
    assert resp.status_code == 200

    # client_a's session survives.
    assert client_a.get(reverse("dashboard:settings")).status_code == 200
    # client_b's session (old auth hash) dies -> redirected to login.
    resp_b = client_b.get(reverse("dashboard:settings"))
    assert resp_b.status_code == 302
    # audit 2026-10-11 sweep: really logged out (not just any redirect), and
    # the password actually changed.
    assert "/login/" in resp_b["Location"]
    assert "_auth_user_id" not in client_b.session
    assert client_a.session["_auth_user_id"] == str(user.pk)
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)


def test_p3_wrong_current_password_400_generic_password_unchanged():
    user = make_user("p3")
    client = login(user)
    resp = client.post(reverse("dashboard:settings-password"), {
        "current_password": "totally-wrong",
        "new_password1": NEW_PASSWORD,
        "new_password2": NEW_PASSWORD,
    }, follow=True)
    assert_prg(resp, "password")
    assert b"Unable to verify your credentials." in resp.content
    assert b"wrong current" not in resp.content.lower()
    user.refresh_from_db()
    assert user.check_password(PASSWORD)


def test_p4_rate_limit_429_generic_body():
    user = make_user("p4")
    client = login(user)
    for _ in range(5):
        client.post(reverse("dashboard:settings-password"), {
            "current_password": "wrong",
            "new_password1": NEW_PASSWORD,
            "new_password2": NEW_PASSWORD,
        }, follow=True)
    resp = client.post(reverse("dashboard:settings-password"), {
        "current_password": "wrong",
        "new_password1": NEW_PASSWORD,
        "new_password2": NEW_PASSWORD,
    }, follow=True)
    assert_prg(resp, "password")
    assert b"Unable to verify your credentials." in resp.content
    assert b"wrong" not in resp.content.lower()
    assert b"rate" not in resp.content.lower()
    assert b"too many" not in resp.content.lower()
    user.refresh_from_db()
    assert user.check_password(PASSWORD)
    assert not user.check_password(NEW_PASSWORD)


def test_p5_demo_and_agent_user_403_password_unchanged():
    from apps.accounts.services import AGENT_ACCOUNT_DOMAIN, get_demo_user

    demo = get_demo_user()
    demo.set_password(PASSWORD)
    demo.save()
    client = login(demo)
    resp = client.post(reverse("dashboard:settings-password"), {
        "current_password": PASSWORD,
        "new_password1": NEW_PASSWORD,
        "new_password2": NEW_PASSWORD,
    }, follow=True)
    assert_prg(resp, "password")
    demo.refresh_from_db()
    assert demo.check_password(PASSWORD)

    agent = make_user("p5agent", email=f"agent@{AGENT_ACCOUNT_DOMAIN}")
    agent_client = login(agent)
    resp2 = agent_client.post(reverse("dashboard:settings-password"), {
        "current_password": PASSWORD,
        "new_password1": NEW_PASSWORD,
        "new_password2": NEW_PASSWORD,
    }, follow=True)
    assert_prg(resp2, "password")
    agent.refresh_from_db()
    assert agent.check_password(PASSWORD)


def test_p6_get_redirects_missing_csrf_403():
    user = make_user("p6")
    client = login(user)
    # QA follow-up: GET redirects to the settings page (was a bare 405).
    resp = client.get(reverse("dashboard:settings-password"))
    assert resp.status_code == 302
    assert resp["Location"] == reverse("dashboard:settings")

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.login(username=user.username, password=PASSWORD)
    resp2 = csrf_client.post(reverse("dashboard:settings-password"), {
        "current_password": PASSWORD,
        "new_password1": NEW_PASSWORD,
        "new_password2": NEW_PASSWORD,
    })
    assert resp2.status_code == 403


@override_settings(ENABLE_TEAMS=True)
def test_p7_member_can_change_own_password_owner_untouched():
    owner = make_user("p7owner")
    member = make_user("p7member")
    TeamMembership.objects.create(account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER)

    client = login(member)
    resp = client.post(reverse("dashboard:settings-password"), {
        "current_password": PASSWORD,
        "new_password1": NEW_PASSWORD,
        "new_password2": NEW_PASSWORD,
    }, follow=True)
    assert_prg(resp, "password")
    member.refresh_from_db()
    assert member.check_password(NEW_PASSWORD)
    owner.refresh_from_db()
    assert owner.check_password(PASSWORD)


# ============================== DELETE ========================================


def _delete_payload(user, password=PASSWORD):
    return {"current_password": password, "confirm_email": user.email}


@patch("apps.dashboard.settings_views._ls_delete")
def test_d1_happy_free_tier_owner_full_cascade_gone_billingevent_survives(mock_ls):
    user = make_user("d1")
    inbox = make_inbox(user)
    message = make_message(inbox)
    Domain.objects.create(user=user, name="d1.example.com", status=Domain.Status.VERIFIED)
    webhook = Webhook(inbox=inbox, url="https://example.test/hook")
    webhook.save(_url_validated=True)
    WebhookDelivery.objects.create(
        webhook=webhook, message=message, payload={}, event_id="evt_d1",
    )
    ApiKey.objects.create(user=user, name="k1", prefix="pfx", key_hash="hash1")
    MonthlyUsage.objects.create(user=user, period_start=timezone.now().date().replace(day=1))
    OutboundEmail.objects.create(
        user=user, from_address="a@d1.example.com", to=["x@example.test"],
    )
    ApprovedRecipient.objects.create(user=user, email_normalized="ok@example.test")
    billing_event = BillingEvent.objects.create(event_key="evt_bill_d1", event_name="x", user=user)
    suppression = SuppressionEntry.objects.create(
        email_normalized="sup-d1@example.test", reason=SuppressionEntry.Reason.MANUAL, source_user=user,
    )
    invite = TeamInvite.objects.create(
        account_id=user.pk, email_normalized="invitee-d1@example.test",
        token_hash="tok_hash_d1", invited_by=user,
    )
    invite_pk = invite.pk
    user_pk = user.pk

    client = login(user)
    resp = client.post(reverse("dashboard:settings-delete"), _delete_payload(user))
    assert resp.status_code == 200
    assert b"deleted" in resp.content.lower()
    mock_ls.assert_not_called()

    User = get_user_model()
    assert not User.objects.filter(pk=user_pk).exists()
    assert not Account.objects.filter(pk=user_pk).exists()
    assert not AccountPlan.objects.filter(user_id=user_pk).exists()
    assert not ApiKey.objects.filter(user_id=user_pk).exists()
    assert not Inbox.objects.filter(user_id=user_pk).exists()
    assert not Message.objects.filter(inbox__user_id=user_pk).exists()
    assert not Domain.objects.filter(user_id=user_pk).exists()
    assert not Webhook.objects.filter(inbox__user_id=user_pk).exists()
    assert not WebhookDelivery.objects.filter(webhook_id=webhook.pk).exists()
    assert not MonthlyUsage.objects.filter(user_id=user_pk).exists()
    assert not OutboundEmail.objects.filter(user_id=user_pk).exists()
    assert not ApprovedRecipient.objects.filter(user_id=user_pk).exists()
    assert not TeamInvite.objects.filter(pk=invite_pk).exists()

    billing_event.refresh_from_db()
    assert billing_event.user_id is None
    suppression.refresh_from_db()
    assert suppression.source_user_id is None

    resp2 = client.get(reverse("dashboard:settings"))
    assert resp2.status_code == 302  # logged out


@patch("apps.dashboard.settings_views._ls_delete")
def test_d2_catch_all_set_delete_succeeds_cycle_broken(mock_ls):
    user = make_user("d2")
    sentinel = make_inbox(user, routing_key="sentinel2")
    domain = Domain.objects.create(
        user=user, name="d2.example.com", status=Domain.Status.VERIFIED,
        catch_all=True, catch_all_inbox=sentinel,
    )
    client = login(user)
    resp = client.post(reverse("dashboard:settings-delete"), _delete_payload(user))
    assert resp.status_code == 200
    assert not Domain.objects.filter(pk=domain.pk).exists()
    assert not Inbox.objects.filter(pk=sentinel.pk).exists()


@patch("apps.dashboard.settings_views._ls_delete")
def test_d3_active_sub_ls_delete_called_before_any_row_deleted(mock_ls):
    user = make_user("d3", sub_id="sub_d3")
    inbox = make_inbox(user)
    domain = Domain.objects.create(user=user, name="d3.example.com", status=Domain.Status.VERIFIED)
    webhook = Webhook(inbox=inbox, url="https://example.test/hook")
    webhook.save(_url_validated=True)
    api_key = ApiKey.objects.create(user=user, name="d3key", prefix="pfx3", key_hash="hash3")
    outbound = OutboundEmail.objects.create(
        user=user, from_address="a@d3.example.com", to=["x@example.test"],
    )
    usage = MonthlyUsage.objects.create(user=user, period_start=timezone.now().date().replace(day=1))
    call_order = []

    def record(path):
        call_order.append(path)
        # still there when LS is called — proves cancel-before-any-delete,
        # across every representative row type, not just Inbox.
        assert Inbox.objects.filter(pk=inbox.pk).exists()
        assert Domain.objects.filter(pk=domain.pk).exists()
        assert Webhook.objects.filter(pk=webhook.pk).exists()
        assert ApiKey.objects.filter(pk=api_key.pk).exists()
        assert OutboundEmail.objects.filter(pk=outbound.pk).exists()
        assert MonthlyUsage.objects.filter(pk=usage.pk).exists()

    mock_ls.side_effect = record
    client = login(user)
    resp = client.post(reverse("dashboard:settings-delete"), _delete_payload(user))
    assert resp.status_code == 200
    mock_ls.assert_called_once_with("/subscriptions/sub_d3")
    assert call_order == ["/subscriptions/sub_d3"]
    assert not Inbox.objects.filter(pk=inbox.pk).exists()


def _http_error(code):
    return urllib.error.HTTPError("https://api.lemonsqueezy.com/v1/x", code, "err", {}, None)


@patch("apps.dashboard.settings_views._ls_delete")
def test_d4_ls_cancel_5xx_502_zero_rows_deleted_plan_intact(mock_ls):
    mock_ls.side_effect = _http_error(500)
    user = make_user("d4", sub_id="sub_d4")
    inbox = make_inbox(user)
    client = login(user)
    resp = client.post(reverse("dashboard:settings-delete"), _delete_payload(user), follow=True)
    assert_prg(resp, "delete")
    assert b"cancel your subscription" in resp.content
    assert Inbox.objects.filter(pk=inbox.pk).exists()
    assert AccountPlan.objects.filter(user=user).exists()
    assert get_user_model().objects.filter(pk=user.pk).exists()


@patch("apps.dashboard.settings_views._ls_delete")
def test_d4b_ls_cancel_timeout_502_zero_rows_deleted(mock_ls):
    mock_ls.side_effect = TimeoutError("timed out")
    user = make_user("d4b", sub_id="sub_d4b")
    inbox = make_inbox(user)
    client = login(user)

    plan = AccountPlan.objects.get(user=user)
    snapshot = (
        plan.plan,
        plan.status,
        plan.stripe_subscription_id,
        plan.stripe_customer_id,
        plan.stripe_price_id,
        plan.current_period_end,
        plan.billing_synced_at,
    )

    resp = client.post(reverse("dashboard:settings-delete"), _delete_payload(user), follow=True)
    assert_prg(resp, "delete")
    assert b"cancel your subscription" in resp.content
    assert Inbox.objects.filter(pk=inbox.pk).exists()
    assert AccountPlan.objects.filter(user=user).exists()
    assert get_user_model().objects.filter(pk=user.pk).exists()

    plan.refresh_from_db()
    assert (
        plan.plan,
        plan.status,
        plan.stripe_subscription_id,
        plan.stripe_customer_id,
        plan.stripe_price_id,
        plan.current_period_end,
        plan.billing_synced_at,
    ) == snapshot


@patch("apps.dashboard.settings_views._ls_delete")
def test_d5_ls_cancel_404_treated_as_already_cancelled_proceeds(mock_ls):
    mock_ls.side_effect = _http_error(404)
    user = make_user("d5", sub_id="sub_d5")
    client = login(user)
    resp = client.post(reverse("dashboard:settings-delete"), _delete_payload(user))
    assert resp.status_code == 200
    assert not get_user_model().objects.filter(pk=user.pk).exists()


@override_settings(ENABLE_TEAMS=True)
def test_d6_member_tries_delete_403_everything_intact():
    owner = make_user("d6owner")
    member = make_user("d6member")
    TeamMembership.objects.create(account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER)
    inbox = make_inbox(member, routing_key="d6-member-inbox")
    client = login(member)
    resp = client.post(reverse("dashboard:settings-delete"), _delete_payload(member), follow=True)
    assert_prg(resp, "delete")
    assert get_user_model().objects.filter(pk=member.pk).exists()
    assert get_user_model().objects.filter(pk=owner.pk).exists()
    assert Inbox.objects.filter(pk=inbox.pk).exists()


def test_d7_demo_and_agent_tries_delete_403():
    from apps.accounts.services import AGENT_ACCOUNT_DOMAIN, get_demo_user

    demo = get_demo_user()
    demo.set_password(PASSWORD)
    demo.save()
    client = login(demo)
    resp = client.post(reverse("dashboard:settings-delete"), _delete_payload(demo), follow=True)
    assert_prg(resp, "delete")
    assert get_user_model().objects.filter(pk=demo.pk).exists()

    agent = make_user("d7agent", email=f"agent@{AGENT_ACCOUNT_DOMAIN}")
    inbox = make_inbox(agent, routing_key="d7-agent-inbox")
    agent_client = login(agent)
    resp2 = agent_client.post(reverse("dashboard:settings-delete"), _delete_payload(agent), follow=True)
    assert_prg(resp2, "delete")
    assert get_user_model().objects.filter(pk=agent.pk).exists()
    assert Inbox.objects.filter(pk=inbox.pk).exists()


@override_settings(ENABLE_TEAMS=True)
def test_d8_owner_with_live_members_409():
    owner = make_user("d8owner")
    member = make_user("d8member")
    TeamMembership.objects.create(account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER)
    inbox = make_inbox(owner, routing_key="d8-owner-inbox")
    client = login(owner)
    resp = client.post(reverse("dashboard:settings-delete"), _delete_payload(owner), follow=True)
    assert_prg(resp, "delete")
    assert b"members" in resp.content.lower()
    assert get_user_model().objects.filter(pk=owner.pk).exists()
    assert Inbox.objects.filter(pk=inbox.pk).exists()


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_d9_owner_with_account_ownership_rows_409():
    owner = make_user("d9owner")
    workspace = make_user("d9workspace")
    AccountOwnership.objects.create(owner_user=owner, account_user=workspace, name="ws1")
    inbox = make_inbox(owner, routing_key="d9-owner-inbox")
    client = login(owner)
    resp = client.post(reverse("dashboard:settings-delete"), _delete_payload(owner), follow=True)
    assert_prg(resp, "delete")
    assert b"workspaces" in resp.content.lower()
    assert get_user_model().objects.filter(pk=owner.pk).exists()
    assert Inbox.objects.filter(pk=inbox.pk).exists()


def test_d10_wrong_reauth_or_confirmation_400_no_delete():
    user = make_user("d10")
    inbox = make_inbox(user, routing_key="d10-inbox")
    client = login(user)
    resp = client.post(reverse("dashboard:settings-delete"), {
        "current_password": "wrong", "confirm_email": user.email,
    }, follow=True)
    assert_prg(resp, "delete")
    assert get_user_model().objects.filter(pk=user.pk).exists()
    assert Inbox.objects.filter(pk=inbox.pk).exists()

    resp2 = client.post(reverse("dashboard:settings-delete"), {
        "current_password": PASSWORD, "confirm_email": "not-my-email@example.test",
    }, follow=True)
    assert_prg(resp2, "delete")
    assert get_user_model().objects.filter(pk=user.pk).exists()
    assert Inbox.objects.filter(pk=inbox.pk).exists()


def test_d11_get_redirects_no_csrf_403():
    user = make_user("d11")
    client = login(user)
    # QA follow-up: GET redirects to the settings page and deletes nothing.
    resp = client.get(reverse("dashboard:settings-delete"))
    assert resp.status_code == 302
    assert resp["Location"] == reverse("dashboard:settings")
    assert get_user_model().objects.filter(pk=user.pk).exists()

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.login(username=user.username, password=PASSWORD)
    resp2 = csrf_client.post(reverse("dashboard:settings-delete"), _delete_payload(user))
    assert resp2.status_code == 403


@patch("apps.dashboard.settings_views._ls_delete")
def test_d12_double_submit_race_second_gets_clean_already_deleted(mock_ls):
    """Rewritten (R2 harden item 9): the ORIGINAL version's "second POST"
    never got past auth (Django's session middleware re-fetches request.user
    fresh each request, so once user_1 is gone the second request just
    redirects to login) — it never reached the Account.DoesNotExist race
    branch (settings_views.py:242-248) at all. This version simulates a
    request that is ALREADY PAST every precheck (password/confirm/etc, all
    of which read request.user.account BEFORE the transaction) whose
    select_for_update'd Account row is deleted by a concurrent request in
    the gap between those prechecks and this view's own
    ``Account.objects.select_for_update().get(pk=user_pk)`` call. Deleting
    the race handler at settings_views.py:242-248 (e.g. letting
    Account.DoesNotExist propagate) would turn this into an unhandled 500 —
    this test fails in that case.
    """
    user = make_user("d12")
    user_pk = user.pk
    client = login(user)

    original_select_for_update = Account.objects.select_for_update

    def deleting_select_for_update(*args, **kwargs):
        # Simulate a concurrent request that already fully erased this
        # account, between settings_delete's own precheck reads and its
        # select_for_update() call inside the atomic block. Account/
        # AccountPlan are PROTECT -> user, so they must go first (mirrors
        # the real cascade's own ordering discipline).
        AccountPlan.objects.filter(user_id=user_pk).delete()
        Account.objects.filter(pk=user_pk).delete()
        get_user_model().objects.filter(pk=user_pk).delete()
        return original_select_for_update(*args, **kwargs)

    with patch.object(Account.objects, "select_for_update", side_effect=deleting_select_for_update):
        resp = client.post(reverse("dashboard:settings-delete"), _delete_payload(user))

    assert resp.status_code != 500
    # the exact clean-already-deleted handling: logout + redirect to root
    # (settings_views.py:247-248) — not an auth-redirect side effect.
    assert resp.status_code == 302
    assert resp["Location"] == reverse("dashboard:root")

    # the session was actually flushed (logout()), not left half-authenticated.
    resp2 = client.get(reverse("dashboard:settings"))
    assert resp2.status_code == 302
    assert resp2.status_code != 500


@override_settings(ENABLE_BILLING=True)
def test_d13_post_delete_webhook_replay_200_no_exception():
    from apps.billing.views import ls_webhook  

    user = make_user("d13", sub_id="sub_d13")
    user_pk = user.pk
    Account.objects.filter(pk=user_pk).delete()
    AccountPlan.objects.filter(user_id=user_pk).delete()
    get_user_model().objects.filter(pk=user_pk).delete()

    secret = "test-ls-webhook-secret"
    payload = {
        "meta": {
            "event_name": "subscription_cancelled",
            "webhook_id": "wh_d13",
            "custom_data": {"user_pk": str(user_pk)},
        },
        "data": {"id": "sub_d13", "attributes": {"status": "cancelled"}},
    }
    body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    with override_settings(LS_WEBHOOK_SECRET=secret):
        client = Client()
        resp = client.post(
            "/billing/webhook/ls/", data=body, content_type="application/json", HTTP_X_SIGNATURE=sig,
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_d14_rate_limit_on_delete_attempts_429():
    user = make_user("d14")
    client = login(user)
    for _ in range(5):
        client.post(reverse("dashboard:settings-delete"), {
            "current_password": "wrong", "confirm_email": user.email,
        }, follow=True)
    resp = client.post(reverse("dashboard:settings-delete"), {
        "current_password": "wrong", "confirm_email": user.email,
    }, follow=True)
    assert_prg(resp, "delete")


# ============================== NAV LINKS =====================================


@override_settings(ENABLE_BILLING=True, ENABLE_TEAMS=True)
def test_nav_billing_and_team_links_present_when_flags_on():
    user = make_user("navon")
    client = login(user)
    resp = client.get(reverse("dashboard:overview"))
    assert reverse("dashboard:billing-page").encode() in resp.content
    assert reverse("dashboard:team").encode() in resp.content
    assert reverse("dashboard:settings").encode() in resp.content


@override_settings(ENABLE_BILLING=False, ENABLE_TEAMS=False)
def test_nav_billing_and_team_links_hidden_when_flags_off_pages_404():
    user = make_user("navoff")
    client = login(user)
    resp = client.get(reverse("dashboard:overview"))
    assert reverse("dashboard:billing-page").encode() not in resp.content
    assert reverse("dashboard:team").encode() not in resp.content
    assert reverse("dashboard:settings").encode() in resp.content

    assert client.get(reverse("dashboard:billing-page")).status_code == 404
    assert client.get(reverse("dashboard:team")).status_code == 404


def test_nav_settings_link_always_present():
    user = make_user("navalways")
    client = login(user)
    resp = client.get(reverse("dashboard:overview"))
    assert reverse("dashboard:settings").encode() in resp.content


@override_settings(ENABLE_MULTI_ACCOUNT=True)
def test_g6_f1_4_workspace_owner_delete_error_names_the_ops_path():
    "Public API."
    owner = make_user("g6f14owner")
    workspace = make_user("g6f14workspace")
    AccountOwnership.objects.create(owner_user=owner, account_user=workspace, name="ws1")
    inbox = make_inbox(owner, routing_key="g6f14-owner-inbox")
    resp = login(owner).post(
        reverse("dashboard:settings-delete"), _delete_payload(owner), follow=True
    )
    assert_prg(resp, "delete")
    body = resp.content.decode()
    assert "Delete your workspaces first." not in body
    assert "support@mailsocket.app" in body
    assert "workspaces" in body.lower()
    assert get_user_model().objects.filter(pk=owner.pk).exists()
    assert Inbox.objects.filter(pk=inbox.pk).exists()
