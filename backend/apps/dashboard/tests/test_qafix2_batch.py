"Public API."
import hashlib
import html
import re
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan, TeamInvite, TeamMembership
from apps.domains.models import Domain
from apps.inboxes.models import Inbox, Message
from apps.webhooks.models import Webhook, WebhookDelivery
from apps.webhooks.services import create_webhook
from apps.webhooks.transport import DeliveryResponse, DeliveryTransportError

pytestmark = pytest.mark.django_db(transaction=True)

DASH = Path(django_settings.BASE_DIR) / "apps" / "dashboard"
APP_JS = DASH / "static" / "dashboard" / "app.js"
APP_CSS = DASH / "static" / "dashboard" / "app.css"
PLAYGROUND_JS = DASH / "static" / "dashboard" / "playground.js"
TEMPLATES = DASH / "templates" / "dashboard"


def public_resolver(_hostname):
    return ["93.184.216.34"]


def make_user(name, *, plan=AccountPlan.Plan.BUSINESS):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user, plan=plan)
    return user


def login(user):
    client = Client()
    client.force_login(user)
    return client


def make_message(inbox, *, subject="s", text="", otp=None, magic_link=None, marker="m"):
    raw = f"Subject: {subject}\r\n\r\n{text}{marker}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    return Message.objects.create(
        inbox=inbox, dedupe_key=digest, envelope_from="s@example.test",
        envelope_recipient=inbox.address, header_from="s@example.test", header_to=[inbox.address],
        header_cc=[], subject=subject, text_body=text, raw_bytes=raw, raw_sha256=digest,
        size_bytes=len(raw), ingest_status=Message.IngestStatus.PARSED,
        detected_otp=otp, otp=otp, magic_link=magic_link, links=[magic_link] if magic_link else [],
    )


def full_page(resp):
    body = resp.content.decode()
    return "<html" in body and 'id="main-content"' in body


# --- #2 magic link shown in the dashboard -------------------------------------


MAGIC = "https://example.com/auth/magic?token=qa1003abcdef123&next=%2Fapp"


def test_2_detail_shows_magic_link_with_copy_and_safe_new_tab_link():
    user = make_user("qf2-magic")
    inbox = Inbox.objects.create(user=user, routing_key="qf2-magic-box")
    message = make_message(inbox, subject="Sign in", text="Sign in by clicking " + MAGIC, magic_link=MAGIC)
    body = login(user).get(reverse("dashboard:message-detail", args=[message.public_id])).content.decode()
    escaped = MAGIC.replace("&", "&amp;")
    assert f'<code id="magic-link-value" class="magic-link-url">{escaped}</code>' in body
    assert 'data-copy-target="magic-link-value"' in body
    anchor = re.search(r'<a [^>]*href="' + re.escape(escaped) + r'"[^>]*>', body).group(0)
    assert 'target="_blank"' in anchor and 'rel="noopener noreferrer nofollow"' in anchor
    # never auto-fetched: no img/iframe/link-prefetch/meta-refresh pointing at it
    assert not re.search(r'<(?:img|iframe|link|script)[^>]+' + re.escape(escaped), body)
    assert '"magic_link": "' + MAGIC + '"' in html.unescape(body)  # normalized JSON


def test_2_magic_link_markup_is_escaped():
    user = make_user("qf2-magic-xss")
    inbox = Inbox.objects.create(user=user, routing_key="qf2-magic-xss")
    evil = 'https://e.test/x?a="><script>alert(1)</script>'
    message = make_message(inbox, magic_link=evil)
    body = login(user).get(reverse("dashboard:message-detail", args=[message.public_id])).content.decode()
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_2_list_shows_link_indicator_when_no_otp():
    user = make_user("qf2-magic-list")
    inbox = Inbox.objects.create(user=user, routing_key="qf2-magic-list")
    make_message(inbox, magic_link=MAGIC, marker="a")
    make_message(inbox, otp="482193", marker="b")
    body = login(user).get(reverse("dashboard:messages")).content.decode()
    assert 'class="status link-pill"' in body
    assert '<code class="otp">482193</code>' in body
    assert MAGIC not in body  # the list never renders the token itself


# --- #3 domains: never a bare partial page -----------------------------------


@override_settings(ENABLE_BYOD=True)
@pytest.mark.parametrize("name", ["", "x" * 300, "not a domain"])
def test_3_domain_create_error_plain_post_redirects_to_full_page_with_flash(name):
    user = make_user(f"qf2-dom-{len(name)}")
    client = login(user)
    resp = client.post(reverse("dashboard:domain-create"), {"name": name}, follow=True)
    assert resp.redirect_chain == [(reverse("dashboard:domains"), 302)]
    assert full_page(resp)
    assert "<title>Domains · mailsocket</title>" in resp.content.decode()
    assert 'class="flash flash-error"' in resp.content.decode()
    assert Domain.objects.filter(user=user).count() == 0


@override_settings(ENABLE_BYOD=True)
def test_3_domain_create_duplicate_plain_post_full_page():
    owner = make_user("qf2-dom-dup-a")
    Domain.objects.create(user=owner, name="dup-qf2.example.com")
    client = login(make_user("qf2-dom-dup-b"))
    resp = client.post(reverse("dashboard:domain-create"), {"name": "dup-qf2.example.com"}, follow=True)
    assert full_page(resp)
    assert "already registered" in resp.content.decode()


@override_settings(ENABLE_BYOD=True)
def test_3_domain_verify_rate_limit_plain_post_full_page(monkeypatch):
    monkeypatch.setattr("apps.dashboard.domain_views.verify_domain", lambda dom, **kw: dom)
    user = make_user("qf2-dom-verify")
    domain = Domain.objects.create(user=user, name="verify-qf2.example.com")
    client = login(user)
    url = reverse("dashboard:domain-verify", args=[domain.public_id])
    client.post(url)
    resp = client.post(url, follow=True)
    assert resp.redirect_chain == [(reverse("dashboard:domains"), 302)]
    assert full_page(resp)
    assert "Checked too recently" in resp.content.decode()


@override_settings(ENABLE_BYOD=True)
def test_3_domain_htmx_error_is_marked_partial_and_template_targets_result_slot():
    user = make_user("qf2-dom-hx")
    resp = login(user).post(reverse("dashboard:domain-create"), {"name": "not a domain"}, HTTP_HX_REQUEST="true")
    assert resp.status_code == 422 and resp.headers.get("HX-Partial") == "flash"
    assert b"<html" not in resp.content
    source = (TEMPLATES / "domains.html").read_text()
    for url_name in ("domain-create", "domain-verify", "domain-delete"):
        tag = re.search(r"<form\b[^>]*dashboard:" + url_name + r"[^>]*>", source).group(0)
        assert 'hx-target="#domain-result"' in tag, tag
    assert 'id="domain-result"' in source


@override_settings(ENABLE_BYOD=True)
def test_3_and_11_domain_success_redirects_with_flash_and_delete_confirms():
    user = make_user("qf2-dom-ok")
    client = login(user)
    resp = client.post(reverse("dashboard:domain-create"), {"name": "ok-qf2.example.com"}, HTTP_HX_REQUEST="true")
    assert resp.status_code == 200 and resp.headers["HX-Redirect"] == reverse("dashboard:domains")
    domain = Domain.objects.get(user=user, name="ok-qf2.example.com")
    resp = client.post(reverse("dashboard:domain-delete", args=[domain.public_id]), follow=True)
    assert full_page(resp)
    assert "Domain ok-qf2.example.com deleted." in resp.content.decode()
    tag = re.search(r"<form\b[^>]*dashboard:domain-delete[^>]*>", (TEMPLATES / "domains.html").read_text()).group(0)
    assert "hx-confirm=" in tag


# --- #4 / #9 webhooks: never a bare partial, errors render only the error -----


def _hook(user, key):
    inbox = Inbox.objects.create(user=user, routing_key=key)
    webhook, _ = create_webhook(inbox, "https://receiver.example/hook", resolver=public_resolver)
    return inbox, webhook


def test_4_webhook_disable_plain_post_lands_on_full_page_with_flash():
    user = make_user("qf2-wh-dis")
    inbox, webhook = _hook(user, "qf2-wh-dis")
    resp = login(user).post(reverse("dashboard:webhook-disable", args=[inbox.public_id]), follow=True)
    assert resp.redirect_chain == [(reverse("dashboard:webhooks"), 302)]
    assert full_page(resp)
    body = resp.content.decode()
    assert "<title>Webhooks · mailsocket</title>" in body
    assert "Webhook disabled." in body
    webhook.refresh_from_db()
    assert webhook.is_enabled is False


def test_4_webhook_disable_form_is_htmx_with_result_target():
    rows = (TEMPLATES / "partials" / "webhook_rows.html").read_text()
    tag = re.search(r"<form\b[^>]*dashboard:webhook-disable[^>]*>", rows).group(0)
    assert "hx-post=" in tag and 'hx-target="#webhook-result"' in tag


def test_4_webhook_save_error_plain_post_full_page():
    user = make_user("qf2-wh-err")
    inbox = Inbox.objects.create(user=user, routing_key="qf2-wh-err")
    client = login(user)
    resp = client.post(reverse("dashboard:webhook-save", args=[inbox.public_id]), {"url": ""}, follow=True)
    assert full_page(resp)
    assert "Enter a valid HTTPS webhook URL." in resp.content.decode()
    resp = client.post(reverse("dashboard:webhook-save", args=[inbox.public_id]), {"url": "nope"}, follow=True)
    assert full_page(resp)
    assert "not an allowed public HTTPS target" in resp.content.decode()


def test_9_webhook_htmx_error_renders_only_the_error_not_the_card_list():
    user = make_user("qf2-wh-9")
    inbox = Inbox.objects.create(user=user, routing_key="qf2-wh-9")
    resp = login(user).post(
        reverse("dashboard:webhook-save", args=[inbox.public_id]), {"url": "http://x"}, HTTP_HX_REQUEST="true"
    )
    assert resp.status_code == 422 and resp.headers.get("HX-Partial") == "flash"
    body = resp.content.decode()
    assert "webhook-form" not in body and "webhook-card" not in body
    assert 'class="flash"' in body


def test_4_webhook_htmx_success_refreshes_cards_out_of_band():
    user = make_user("qf2-wh-oob")
    inbox, _ = _hook(user, "qf2-wh-oob")
    resp = login(user).post(reverse("dashboard:webhook-disable", args=[inbox.public_id]), HTTP_HX_REQUEST="true")
    body = resp.content.decode()
    assert resp.status_code == 200
    assert "Webhook disabled." in body
    assert '<div id="webhook-rows" hx-swap-oob="innerHTML">' in body
    assert body.count('class="webhook-form"') == 1


def test_4_webhook_create_plain_post_shows_secret_on_full_page_no_store(monkeypatch):
    user = make_user("qf2-wh-create")
    inbox = Inbox.objects.create(user=user, routing_key="qf2-wh-create")
    import apps.webhooks.services as services

    original = services.validate_webhook_target

    def public_target(url, resolver=None):
        return original(url, resolver=public_resolver)

    monkeypatch.setattr(services, "validate_webhook_target", public_target)
    resp = login(user).post(
        reverse("dashboard:webhook-save", args=[inbox.public_id]),
        {"url": "https://receiver.example/hook", "is_enabled": "on"},
    )
    assert resp.status_code == 200 and full_page(resp)
    assert resp["Cache-Control"] == "private, no-store"
    assert 'id="created-webhook-secret"' in resp.content.decode()


# --- #5 Send test + Delete webhook -------------------------------------------


def test_5_send_test_reuses_api_service_metering_and_transport(monkeypatch):
    from apps.accounts.entitlements import usage_summary

    user = make_user("qf2-wh-test")
    inbox, _ = _hook(user, "qf2-wh-test")
    calls = []

    def fake_transport(url, body, headers):
        calls.append((url, headers["X-Mailsocket-Event"]))
        return DeliveryResponse(204)

    # The SAME symbol the API view uses — proves there is no second transport.
    monkeypatch.setattr("apps.api.views.post_json_pinned", fake_transport)
    before = usage_summary(user)["webhook_attempts"]
    resp = login(user).post(reverse("dashboard:webhook-test", args=[inbox.public_id]), HTTP_HX_REQUEST="true")
    assert resp.status_code == 200
    assert "Test event delivered — your endpoint answered HTTP 204." in resp.content.decode()
    assert calls == [("https://receiver.example/hook", "webhook.test")]
    assert usage_summary(user)["webhook_attempts"] == before + 1
    assert WebhookDelivery.objects.count() == 0  # no retry row, like the API


def test_5_send_test_reports_non_2xx_and_unreachable_inline(monkeypatch):
    user = make_user("qf2-wh-test-bad")
    inbox, _ = _hook(user, "qf2-wh-test-bad")
    client = login(user)
    monkeypatch.setattr("apps.api.views.post_json_pinned", lambda *a, **k: DeliveryResponse(405))
    resp = client.post(reverse("dashboard:webhook-test", args=[inbox.public_id]), HTTP_HX_REQUEST="true")
    assert "answered HTTP 405" in resp.content.decode() and "flash-error" in resp.content.decode()

    def boom(*a, **k):
        raise DeliveryTransportError("timeout")

    monkeypatch.setattr("apps.api.views.post_json_pinned", boom)
    resp = client.post(reverse("dashboard:webhook-test", args=[inbox.public_id]), HTTP_HX_REQUEST="true")
    assert "could not be reached (timeout)" in resp.content.decode()


def test_5_send_test_shares_the_api_rate_limit_bucket(monkeypatch):
    user = make_user("qf2-wh-test-rl")
    inbox, _ = _hook(user, "qf2-wh-test-rl")
    monkeypatch.setattr("apps.api.views.post_json_pinned", lambda *a, **k: DeliveryResponse(200))
    client = login(user)
    for _ in range(10):
        assert client.post(reverse("dashboard:webhook-test", args=[inbox.public_id]), HTTP_HX_REQUEST="true").status_code == 200
    resp = client.post(reverse("dashboard:webhook-test", args=[inbox.public_id]), HTTP_HX_REQUEST="true")
    assert resp.status_code == 429 and resp.headers.get("HX-Partial") == "flash"


def test_5_send_test_and_delete_are_tenant_scoped():
    alice = make_user("qf2-wh-alice")
    bob = make_user("qf2-wh-bob")
    inbox, webhook = _hook(bob, "qf2-wh-bob")
    client = login(alice)
    assert client.post(reverse("dashboard:webhook-test", args=[inbox.public_id])).status_code == 404
    assert client.post(reverse("dashboard:webhook-delete", args=[inbox.public_id])).status_code == 404
    assert Webhook.objects.filter(pk=webhook.pk).exists()


def test_5_delete_webhook_removes_deliveries_atomically_and_confirms():
    user = make_user("qf2-wh-del")
    inbox, webhook = _hook(user, "qf2-wh-del")
    message = make_message(inbox)
    WebhookDelivery.objects.create(webhook=webhook, message=message, event_id="evt_qf2del", payload={})
    resp = login(user).post(reverse("dashboard:webhook-delete", args=[inbox.public_id]), follow=True)
    assert full_page(resp) and "Webhook deleted." in resp.content.decode()
    assert not Webhook.objects.filter(pk=webhook.pk).exists()
    assert not WebhookDelivery.objects.filter(webhook_id=webhook.pk).exists()
    assert Message.objects.filter(pk=message.pk).exists()  # mail itself is kept
    rows = (TEMPLATES / "partials" / "webhook_rows.html").read_text()
    tag = re.search(r"<form\b[^>]*dashboard:webhook-delete[^>]*>", rows).group(0)
    assert "hx-confirm=" in tag
    assert "dashboard:webhook-test" in rows


def test_5_delete_webhook_is_all_or_nothing(monkeypatch):
    """If the Webhook delete fails after the deliveries were deleted, the
    whole transaction rolls back (no half-deleted history)."""
    from django.db import DatabaseError
    from django.db.models import QuerySet

    from apps.webhooks import services

    user = make_user("qf2-wh-del-atomic")
    inbox, webhook = _hook(user, "qf2-wh-del-atomic")
    WebhookDelivery.objects.create(webhook=webhook, message=make_message(inbox), event_id="evt_qf2atom", payload={})
    real_delete = QuerySet.delete

    def failing_delete(self):
        if self.model is Webhook:
            raise DatabaseError("boom")
        return real_delete(self)

    monkeypatch.setattr(QuerySet, "delete", failing_delete)
    with pytest.raises(DatabaseError):
        services.delete_webhook(webhook)
    monkeypatch.undo()
    assert Webhook.objects.filter(pk=webhook.pk).exists()
    assert WebhookDelivery.objects.filter(webhook_id=webhook.pk).count() == 1


# --- #6 inbox counter + Create button refreshed after htmx swaps --------------


def test_6_inbox_create_and_delete_refresh_counter_and_button_oob():
    # Free limit comes from settings (3 since feat/plan); fill to the cap so the
    # last create must flip the button to disabled, then delete re-enables it.
    from apps.accounts.entitlements import plan_limits

    limit = plan_limits(AccountPlan.Plan.FREE)["inboxes"]
    assert limit >= 1
    user = make_user("qf2-inbox-oob", plan=AccountPlan.Plan.FREE)
    client = login(user)
    body = ""
    for n in range(1, limit + 1):
        body = client.post(reverse("dashboard:inbox-create"), {"label": f"x{n}"}, HTTP_HX_REQUEST="true").content.decode()
        assert f'<span id="inbox-usage-count" class="usage-count" hx-swap-oob="outerHTML">{n} of {limit} inboxes used.</span>' in body
        if n < limit:
            assert '<button id="inbox-create-button" class="button" hx-swap-oob="outerHTML">' in body
    assert re.search(r'<button id="inbox-create-button" class="button" disabled hx-swap-oob="outerHTML">', body)
    inbox = Inbox.objects.filter(user=user).first()
    body = client.post(reverse("dashboard:inbox-delete", args=[inbox.public_id]), HTTP_HX_REQUEST="true").content.decode()
    assert f">{limit - 1} of {limit} inboxes used.<" in body
    assert '<button id="inbox-create-button" class="button" hx-swap-oob="outerHTML">' in body
    page = client.get(reverse("dashboard:inboxes")).content.decode()
    assert 'id="inbox-usage-count"' in page and 'id="inbox-create-button"' in page
    assert "hx-swap-oob" not in page


# --- #7 billing plan cards wrap on phones ------------------------------------


def test_7_usage_grid_collapses_to_one_column_on_narrow_screens():
    css = APP_CSS.read_text()
    assert re.search(r"@media\(max-width:640px\)\{\.usage-grid\{grid-template-columns:minmax\(0,1fr\)\}\}", css)


# --- #8 seat count / #10 stale team error / #11 confirm on revoke -------------


@override_settings(ENABLE_TEAMS=True)
def test_8_team_page_shows_seats_counted_like_can_invite_member():
    owner = make_user("qf2-seats")
    member = make_user("qf2-seats-m")
    TeamMembership.objects.create(account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER)
    TeamInvite.objects.create(
        account_id=owner.pk, email_normalized="p@example.test", token_hash="h1" * 32,
        expires_at=timezone.now() + timedelta(days=7),
    )
    TeamInvite.objects.create(  # expired: not counted (same as can_invite_member)
        account_id=owner.pk, email_normalized="old@example.test", token_hash="h2" * 32,
        expires_at=timezone.now() - timedelta(days=1),
    )
    from apps.accounts.entitlements import plan_limits

    seats = plan_limits(AccountPlan.Plan.BUSINESS)["seats"]
    body = login(owner).get(reverse("dashboard:team")).content.decode()
    assert f"Seats: 3 of {seats} used" in body


@override_settings(ENABLE_TEAMS=True)
def test_10_team_success_clears_the_error_slot_and_11_revoke_confirms(monkeypatch):
    monkeypatch.setattr("apps.accounts.team_services._send_invite_mail", lambda **kw: None)
    owner = make_user("qf2-team10")
    resp = login(owner).post(reverse("dashboard:team-invite-create"), {"email": "ok@example.test"}, HTTP_HX_REQUEST="true")
    body = resp.content.decode()
    assert "Invite sent." in body
    assert '<section id="team-result" aria-live="polite" hx-swap-oob="innerHTML"></section>' in body
    rows = (TEMPLATES / "partials" / "team_rows.html").read_text()
    for url_name in ("team-invite-revoke", "team-member-revoke"):
        tag = re.search(r"<form\b[^>]*dashboard:" + url_name + r"[^>]*>", rows).group(0)
        assert "hx-confirm=" in tag, tag


# --- #12 billing shows current plan price ------------------------------------


@override_settings(ENABLE_BILLING=True)
def test_12_billing_shows_current_plan_price_from_pricing_context():
    from apps.dashboard.views import _pricing_context

    user = make_user("qf2-bill")
    price = next(t["price"] for t in _pricing_context()["tiers"] if t["key"] == "business")
    body = login(user).get(reverse("dashboard:billing-page")).content.decode()
    assert f'<span class="plan-price">· {price}</span>' in body


# --- #13 settings PRG + field-specific empty errors ---------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"current_password": "", "new_password1": "a", "new_password2": "a"}, "Enter your current password."),
        ({"current_password": "password", "new_password1": "", "new_password2": ""}, "Enter the new password in both fields."),
    ],
)
def test_13_settings_password_empty_fields_named_and_prg(payload, expected):
    user = make_user("qf2-pw-" + str(len(payload["current_password"])))
    resp = login(user).post(reverse("dashboard:settings-password"), payload, follow=True)
    assert resp.redirect_chain == [(reverse("dashboard:settings") + "#settings-password", 302)]
    assert expected in resp.content.decode()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"current_password": "", "confirm_email": "x@example.test"}, "Enter your current password."),
        ({"current_password": "password", "confirm_email": ""}, "Type your account email to confirm."),
    ],
)
def test_13_settings_delete_empty_fields_named_and_prg(payload, expected):
    user = make_user("qf2-del-" + str(len(payload["current_password"])))
    resp = login(user).post(reverse("dashboard:settings-delete"), payload, follow=True)
    assert resp.redirect_chain == [(reverse("dashboard:settings") + "#settings-delete", 302)]
    assert expected in resp.content.decode()
    assert get_user_model().objects.filter(pk=user.pk).exists()


def test_13_wrong_password_keeps_generic_message_and_reload_does_not_repost():
    user = make_user("qf2-pw-wrong")
    client = login(user)
    resp = client.post(
        reverse("dashboard:settings-password"),
        {"current_password": "nope", "new_password1": "Xx-12345678", "new_password2": "Xx-12345678"},
    )
    assert resp.status_code == 302  # PRG: the browser lands on a GET
    page = client.get(resp["Location"]).content.decode()
    assert "Unable to verify your credentials." in page
    # the message is consumed: a reload of the GET shows nothing stale
    assert "Unable to verify" not in client.get(reverse("dashboard:settings")).content.decode()


# --- #14 unknown filters are ignored, not a 404 -------------------------------


def test_14_unknown_or_foreign_filter_ids_look_identical_and_do_not_404():
    alice = make_user("qf2-filter-a")
    bob = make_user("qf2-filter-b")
    a_inbox = Inbox.objects.create(user=alice, routing_key="qf2-filter-a")
    b_inbox = Inbox.objects.create(user=bob, routing_key="qf2-filter-b")
    b_domain = Domain.objects.create(user=bob, name="filter-qf2.example.com")
    make_message(a_inbox, subject="alice mail")
    make_message(b_inbox, subject="bob mail")
    client = login(alice)
    url = reverse("dashboard:messages")
    bogus = client.get(url, {"inbox": "bogus"})
    foreign = client.get(url, {"inbox": b_inbox.public_id})
    assert bogus.status_code == foreign.status_code == 200
    assert "alice mail" in bogus.content.decode() and "bob mail" not in foreign.content.decode()
    strip = lambda r: re.sub(r'name="csrfmiddlewaretoken" value="[^"]+"', "", r.content.decode())  # noqa: E731
    assert strip(bogus).replace("bogus", "X") == strip(foreign).replace(b_inbox.public_id, "X")
    d_bogus = client.get(url, {"domain_id": "999999"})
    d_foreign = client.get(url, {"domain_id": b_domain.public_id})
    assert d_bogus.status_code == d_foreign.status_code == 200
    assert "filter-qf2.example.com" not in d_foreign.content.decode()


# --- #15/#16/#17 small JS -----------------------------------------------------


def test_15_17_app_js_esc_closes_connect_and_reveals_active_nav():
    js = APP_JS.read_text()
    # ux4 r2: the same handler also closes the inbox "More" menus.
    assert 'document.querySelectorAll("details.connect[open], details.row-more[open]")' in js
    assert 'event.key !== "Escape"' in js
    assert "revealActiveNavItem" in js and 'a[aria-current="page"]' in js


def test_16_playground_clears_previous_response_on_validation_error():
    js = PLAYGROUND_JS.read_text()
    for message in ("Enter an inbox id first", "Enter an API key"):
        idx = js.index(message)
        assert 'respOut.textContent = "";' in js[idx - 120:idx], message


# --- #18 OTP visible at 390px -------------------------------------------------


def test_18_otp_is_the_first_messages_column():
    rows = (TEMPLATES / "partials" / "message_rows.html").read_text()
    headers = re.findall(r'<th class="(col-[a-z]+)"', rows)
    assert headers[0] == "col-otp"


# --- #19 enabled pill contrast ------------------------------------------------


def _luminance(hex_color):
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def test_19_enabled_pill_contrast_is_at_least_4_5():
    css = APP_CSS.read_text()
    tokens = dict(re.findall(r"(--[a-z-]+):(#[0-9a-fA-F]{6})", css))
    rule = re.search(r"\n\.status\.on\{background:var\((--[a-z-]+)\);color:var\((--[a-z-]+)\)\}", css)
    bg, fg = tokens[rule.group(1)], tokens[rule.group(2)]
    hi, lo = sorted((_luminance(bg), _luminance(fg)), reverse=True)
    assert (hi + 0.05) / (lo + 0.05) >= 4.5


# --- #20 fleet is a 404 for customers -----------------------------------------


def test_20_fleet_404_for_non_staff_and_anonymous_staff_unchanged():
    user = make_user("qf2-fleet")
    resp = login(user).get(reverse("dashboard:fleet"))
    assert resp.status_code == 404 and "admin" not in resp.get("Location", "")
    assert Client().get(reverse("dashboard:fleet")).status_code == 404
    user.is_staff = True
    user.save()
    assert login(user).get(reverse("dashboard:fleet")).status_code == 200
