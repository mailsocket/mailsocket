"Public API."

import ast
import html
import re

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.entitlements import usage_summary
from apps.accounts.models import Account, AccountPlan, MonthlyUsage, RateLimitBucket
from apps.accounts.services import create_api_key
from apps.dashboard import views as dashboard_views
from apps.feedback.testing import strip_request_noise
from apps.inboxes.models import Inbox, Message
from apps.webhooks.models import WebhookDelivery
from apps.webhooks.services import create_webhook

pytestmark = pytest.mark.django_db


def make_user(name, plan=AccountPlan.Plan.PAID):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user, plan=plan)
    return user


@pytest.fixture
def tenants():
    alice = make_user("ux-alice")
    bob = make_user("ux-bob")
    alice_inbox = Inbox.objects.create(user=alice, routing_key="ux_alice_box", label="Alice")
    bob_inbox = Inbox.objects.create(user=bob, routing_key="ux_bob_box", label="Bob")
    return alice, bob, alice_inbox, bob_inbox


def _client(user):
    client = Client()
    client.force_login(user)
    return client


def _code_block(page, element_id):
    match = re.search(rf'<code id="{re.escape(element_id)}">(.*?)</code>', page, re.S)
    assert match, element_id
    return html.unescape(match.group(1))


# --- item 2: snippets ---------------------------------------------------------


def _assert_python_loop(python, inbox):
    ast.parse(python)  # valid Python as copied
    assert "while otp is None and time.monotonic() < deadline" in python
    assert "deadline = time.monotonic() + 60" in python
    assert "wait = max(1, min(25, int(deadline - time.monotonic())))" in python
    assert '"timeout": wait' in python and "timeout=wait + 10" in python
    
    # later calls send back the server's Mailsocket-Wait-Since (activation item 1).
    assert 'params = {"require": "otp", "timeout": wait}' in python
    assert 'since = resp.headers.get("Mailsocket-Wait-Since") or since' in python
    assert "resp.status_code != 204" in python  # 204 -> keep waiting
    assert "print(otp)" in python
    assert (
        f'print("No code yet. Run this again, then send an email with a code to {inbox.address}.")'
        in python
    )
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    assert f'"{base}/api/v1/inboxes/{inbox.public_id}/messages/wait"' in python


def test_overview_snippet_loops_and_uses_env_key(tenants):
    alice, _, inbox, _ = tenants
    create_api_key(alice, "k")
    page = _client(alice).get(reverse("dashboard:overview")).content.decode()
    python = _code_block(page, "snippet-python")
    curl = _code_block(page, "snippet-curl")
    _assert_python_loop(python, inbox)
    assert 'API_KEY = os.environ["MAILSOCKET_API_KEY"]' in python
    assert 'Bearer $MAILSOCKET_API_KEY"' in curl
    assert "timeout=25" in curl
    assert "export MAILSOCKET_API_KEY=&lt;your key&gt;" in page
    # No unusable placeholders.
    assert "ms_live_\u2026" not in page
    assert "***" not in page


def test_inboxes_connect_snippet_uses_shared_loop(tenants):
    alice, _, inbox, _ = tenants
    page = _client(alice).get(reverse("dashboard:inboxes")).content.decode()
    _assert_python_loop(_code_block(page, f"connect-python-{inbox.public_id}"), inbox)
    curl = _code_block(page, f"connect-curl-{inbox.public_id}")
    assert 'Bearer $MAILSOCKET_API_KEY"' in curl
    assert "ms_live_\u2026" not in page


def test_onboarding_snippet_has_real_key_and_same_loop(tenants):
    alice, _, inbox, _ = tenants
    response = _client(alice).post(reverse("dashboard:api-key-onboarding"), HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    page = response.content.decode()
    token = re.search(r'<code id="created-api-key">([^<]+)</code>', page).group(1)
    python = _code_block(page, "snippet-python")
    _assert_python_loop(python, inbox)
    assert f'API_KEY = "{token}"' in python
    assert f'Bearer {token}"' in _code_block(page, "snippet-curl")
    assert "Send a test code" in page


def test_snippet_api_base_is_the_docs_setting(tenants, settings):
    "Public API."
    settings.PUBLIC_BASE_URL = "https://api-base.example.test/"
    alice, _, inbox, _ = tenants
    create_api_key(alice, "k")
    overview = _client(alice).get(reverse("dashboard:overview")).content.decode()
    docs = Client().get(reverse("dashboard:docs")).content.decode()
    assert "https://api-base.example.test/api/v1/inboxes/" + inbox.public_id in overview
    assert "https://api-base.example.test/api/v1/" in docs
    assert "https://mailsocket.app/api/v1" not in overview


def test_public_docs_and_try_have_no_star_placeholders():
    for name in ("dashboard:docs", "dashboard:try-it"):
        page = Client().get(reverse(name)).content.decode()
        assert "***" not in page, name
        assert 'Bearer $MAILSOCKET_API_KEY"' in page, name


def test_llms_txt_uses_full_env_var_name():
    body = Client().get(reverse("dashboard:llms-txt")).content.decode()
    assert "MAILS...KEY" not in body
    assert body.count("Bearer $MAILSOCKET_API_KEY") == 5  # ux3: + test-code, r2: + latest (SINCE)


# --- item 3: Send a test code -------------------------------------------------


def _send(client, inbox, *, htmx=True, **extra):
    kwargs = {"HTTP_HX_REQUEST": "true"} if htmx else {}
    return client.post(reverse("dashboard:inbox-test-code", args=[inbox.public_id]), extra, **kwargs)


def test_send_test_code_creates_parsed_otp_message_and_flash(tenants):
    alice, _, inbox, _ = tenants
    client = _client(alice)
    response = _send(client, inbox)
    assert response.status_code == 200
    assert "Test code sent — it appears in a few seconds." in response.content.decode()
    message = Message.objects.get(inbox=inbox)
    assert message.ingest_status == Message.IngestStatus.PARSED
    assert re.fullmatch(r"\d{6}", message.detected_otp or "")
    assert message.envelope_from == dashboard_views.TEST_CODE_SENDER
    # It shows in Recent messages.
    recent = client.get(reverse("dashboard:recent-messages")).content.decode()
    assert message.detected_otp in recent


def test_send_test_code_plain_post_redirects_with_flash(tenants):
    alice, _, inbox, _ = tenants
    client = _client(alice)
    response = _send(client, inbox, htmx=False, return_to="overview")
    assert response.status_code == 302 and response.url == reverse("dashboard:overview")
    page = client.get(response.url).content.decode()
    assert "Test code sent — it appears in a few seconds." in page
    # Unknown return_to never becomes a redirect target.
    response = _send(client, inbox, htmx=False, return_to="https://evil.example/")
    assert response.url == reverse("dashboard:inboxes")


def test_send_test_code_counts_toward_usage(tenants):
    alice, _, inbox, _ = tenants
    before = usage_summary(alice)["messages_received"]
    _send(_client(alice), inbox)
    assert usage_summary(alice)["messages_received"] == before + 1


def test_send_test_code_fires_configured_webhook_like_real_mail(tenants):
    alice, _, inbox, _ = tenants
    create_webhook(
        inbox,
        "https://receiver.example/hook",
        enabled=True,
        resolver=lambda _host: ["93.184.216.34"],
    )
    _send(_client(alice), inbox)
    assert WebhookDelivery.objects.filter(webhook__inbox=inbox).count() == 1


def test_send_test_code_foreign_and_unknown_ids_are_identical_404(tenants):
    alice, _, _, bob_inbox = tenants
    client = _client(alice)
    foreign = client.post(reverse("dashboard:inbox-test-code", args=[bob_inbox.public_id]))
    unknown = client.post(reverse("dashboard:inbox-test-code", args=["inbox_doesnotexist000000"]))
    assert foreign.status_code == unknown.status_code == 404
    # Byte-identical apart from per-request noise (own request id + path).
    assert strip_request_noise(foreign.content) == strip_request_noise(unknown.content)
    assert not Message.objects.filter(inbox=bob_inbox).exists()
    assert not RateLimitBucket.objects.exists()  # nothing charged


def test_send_test_code_requires_login_and_csrf(tenants):
    alice, _, inbox, _ = tenants
    url = reverse("dashboard:inbox-test-code", args=[inbox.public_id])
    anon = Client().post(url)
    assert anon.status_code == 302 and anon.url.startswith("/login/")
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(alice)
    assert csrf_client.post(url).status_code == 403
    # GET never mutates.
    assert _client(alice).get(url).status_code == 302
    assert not Message.objects.filter(inbox=inbox).exists()


def test_send_test_code_refuses_disabled_and_deleted_inboxes(tenants):
    alice, _, inbox, _ = tenants
    client = _client(alice)
    inbox.is_enabled = False
    inbox.save(update_fields=["is_enabled"])
    response = _send(client, inbox)
    assert response.status_code == 409
    assert "This inbox is disabled." in response.content.decode()
    assert not RateLimitBucket.objects.exists()
    inbox.deleted_at = timezone.now()
    inbox.save(update_fields=["deleted_at"])
    assert _send(client, inbox).status_code == 404
    assert not Message.objects.filter(inbox=inbox).exists()


def test_send_test_code_rate_limited_per_inbox(tenants):
    alice, _, inbox, _ = tenants
    client = _client(alice)
    limit = dashboard_views.TEST_CODE_INBOX_RATE_LIMIT
    statuses = [_send(client, inbox).status_code for _ in range(limit + 1)]
    assert statuses[:limit] == [200] * limit and statuses[-1] == 429
    assert Message.objects.filter(inbox=inbox).count() == limit


def test_send_test_code_rate_limited_per_account(tenants, monkeypatch):
    alice, _, inbox, _ = tenants
    monkeypatch.setattr(dashboard_views, "TEST_CODE_ACCOUNT_RATE_LIMIT", 2)
    other = Inbox.objects.create(user=alice, routing_key="ux_alice_two", label="Two")
    client = _client(alice)
    assert _send(client, inbox).status_code == 200
    assert _send(client, other).status_code == 200
    third = _send(client, other)
    assert third.status_code == 429
    assert "Too many test codes requested." in third.content.decode()
    assert Message.objects.filter(inbox__user=alice).count() == 2


def test_send_test_code_buttons_rendered(tenants):
    alice, _, inbox, _ = tenants
    client = _client(alice)
    action = reverse("dashboard:inbox-test-code", args=[inbox.public_id])
    inboxes = client.get(reverse("dashboard:inboxes")).content.decode()
    assert f'action="{action}"' in inboxes and ">Send a test code</button>" in inboxes
    overview_onboarding = client.get(reverse("dashboard:overview")).content.decode()
    assert f'action="{action}"' in overview_onboarding
    assert "No mail yet? Use <strong>Send a test code</strong>" in overview_onboarding
    create_api_key(alice, "k")
    overview = client.get(reverse("dashboard:overview")).content.decode()
    assert f'action="{action}"' in overview
    # Disabled inbox: no button in the list.
    inbox.is_enabled = False
    inbox.save(update_fields=["is_enabled"])
    assert f'action="{action}"' not in client.get(reverse("dashboard:inboxes")).content.decode()


# --- item 13: Overview card copy ----------------------------------------------


def test_overview_messages_card_copy(tenants):
    alice, _, _, _ = tenants
    client = _client(alice)
    page = client.get(reverse("dashboard:overview")).content.decode()
    assert "<span>Messages this month</span>" in page
    assert "UTC-month messages" not in page
    MonthlyUsage.objects.create(
        user=alice, period_start=timezone.now().replace(day=1).date(), messages_received=10**7
    )
    page = client.get(reverse("dashboard:overview")).content.decode()
    usage_page = client.get(reverse("dashboard:usage")).content.decode()
    wording = "Soft limit exceeded — inbound mail is still accepted, but you are over this period&#x27;s message allowance."
    raw = "Soft limit exceeded — inbound mail is still accepted, but you are over this period's message allowance."
    assert raw in usage_page or wording in usage_page
    assert raw in page or wording in page
