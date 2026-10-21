"Public API."

import builtins
import html
import re
import secrets
import types
from datetime import timedelta
from urllib.parse import urlsplit

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, AccountPlan, RateLimitBucket
from apps.accounts.services import _bucket_hash, create_api_key
from apps.dashboard import demo
from apps.dashboard import views as dashboard_views
from apps.inboxes.models import Inbox, Message
from apps.inboxes.services import seed_welcome_message

# ux3 N6: first line the snippet prints, before any wait call.
WAITING_LINE = 'Waiting up to 60 s for a code to {address}… (click "Send a test code" now)'
TEST_CODE_TIP = "Puts a sample code straight into this inbox (does not go through the mail server)."


def make_user(name, plan=AccountPlan.Plan.PAID):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user, plan=plan)
    return user


def _client(user):
    client = Client()
    client.force_login(user)
    return client


def _code_block(page, element_id):
    match = re.search(rf'<code id="{re.escape(element_id)}">(.*?)</code>', page, re.S)
    assert match, element_id
    return html.unescape(match.group(1))


def _run_snippet(python, *, requests_get, time_module=None):
    """exec the copied Python exactly as rendered, with `requests` (and
    optionally `time`) supplied by the test. Returns the printed lines."""
    printed = []
    fakes = {"requests": types.SimpleNamespace(get=requests_get)}
    if time_module is not None:
        fakes["time"] = time_module
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name in fakes:
            return fakes[name]
        return real_import(name, *args, **kwargs)

    env = {
        "__builtins__": dict(builtins.__dict__, __import__=fake_import, print=lambda *a: printed.append(" ".join(map(str, a)))),
        "__name__": "__snippet__",
    }
    exec(compile(python, "<dashboard-snippet>", "exec"), env)  # noqa: S102 — our own template
    return printed


def _fixed_codes(monkeypatch, *codes):
    """deliver_sample_code draws its OTP from demo.secrets.randbelow; pin the
    sequence so the old and new test codes are guaranteed distinct."""
    seq = iter(codes)
    monkeypatch.setattr(
        demo,
        "secrets",
        types.SimpleNamespace(randbelow=lambda _n: next(seq), token_urlsafe=secrets.token_urlsafe),
    )





@pytest.mark.django_db(transaction=True)
def test_snippet_returns_the_new_code_not_the_welcome_or_an_older_test_code(monkeypatch):
    """Behavioural: render the onboarding snippet, then run the copied Python
    against the real /wait view (Django test client). The welcome OTP (60 s
    old) and an older test code (30 s old) are already in the inbox, i.e.
    inside the old 120 s back-window; a new code arrives during the wait.
    The snippet must print the NEW code."""
    from apps.api import views as api_views

    user = make_user("r2-snippet")
    inbox = Inbox.objects.create(user=user, routing_key="r2_snippet_box", label="r2")
    welcome = seed_welcome_message(inbox)
    _fixed_codes(monkeypatch, 111111, 222222)
    older = demo.deliver_sample_code(inbox).message
    now = timezone.now()
    Message.objects.filter(pk=welcome.pk).update(received_at=now - timedelta(seconds=60))
    Message.objects.filter(pk=older.pk).update(received_at=now - timedelta(seconds=30))
    welcome.refresh_from_db()
    older.refresh_from_db()
    assert welcome.otp and older.otp == "111111" and welcome.otp != "111111"

    page = _client(user).post(reverse("dashboard:api-key-onboarding"), HTTP_HX_REQUEST="true")
    python = _code_block(page.content.decode(), "snippet-python")

    # The new code lands while the snippet's first wait call is blocked
    # server-side (the view hands its DB connection back between polls).
    arrived = []
    real_idle = api_views._release_db_connection_while_idle

    def deliver_mid_wait():
        if not arrived:
            arrived.append(demo.deliver_sample_code(inbox).message)
        real_idle()

    monkeypatch.setattr(api_views, "_release_db_connection_while_idle", deliver_mid_wait)

    base = settings.PUBLIC_BASE_URL.rstrip("/")
    calls = []

    def requests_get(url, params=None, headers=None, timeout=None):
        assert url.startswith(base)
        calls.append(dict(params))
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=headers["Authorization"])
        resp = api.get(urlsplit(url).path, params)
        return types.SimpleNamespace(
            status_code=resp.status_code,
            headers=resp.headers,
            json=lambda: resp.json(),
            raise_for_status=lambda: (_ for _ in ()).throw(AssertionError(resp.status_code)),
        )

    printed = _run_snippet(python, requests_get=requests_get)
    assert arrived
    arrived[0].refresh_from_db()
    assert arrived[0].otp == "222222"
    assert printed == [WAITING_LINE.format(address=inbox.address), "222222"]
    assert welcome.otp not in printed and older.otp not in printed
    # Exactly the API default: no since sent, require=otp.
    assert calls and all("since" not in c and c["require"] == "otp" for c in calls)


# --- 2. P2: total runtime bounded by the ~60 s deadline -----------------------


@pytest.mark.django_db
def test_snippet_total_runtime_never_exceeds_deadline(monkeypatch):
    monkeypatch.setenv("MAILSOCKET_API_KEY", "ms_live_test")
    user = make_user("r2-budget")
    inbox = Inbox.objects.create(user=user, routing_key="r2_budget_box", label="r2")
    create_api_key(user, "k")
    page = _client(user).get(reverse("dashboard:overview")).content.decode()
    python = _code_block(page, "snippet-python")

    clock = {"t": 1000.0}
    fake_time = types.SimpleNamespace(monotonic=lambda: clock["t"], time=lambda: clock["t"])
    calls = []

    def requests_get(url, params=None, headers=None, timeout=None):
        calls.append((clock["t"], params["timeout"], timeout))
        clock["t"] += params["timeout"] + 0.3  # server blocks the full wait, plus latency
        return types.SimpleNamespace(status_code=204, headers={}, json=dict, raise_for_status=lambda: None)

    printed = _run_snippet(python, requests_get=requests_get, time_module=fake_time)
    start, deadline = 1000.0, 1060.0
    assert len(calls) >= 3
    for started_at, wait, client_timeout in calls:
        assert 1 <= wait <= 25
        assert wait <= max(1, deadline - started_at)
        assert client_timeout == wait + 10
    # Worst case: the last server wait ends by ~deadline (+1 s floor, + latency);
    # the client timeout caps a hung call at +10 s beyond that.
    last_start, last_wait, last_client = calls[-1]
    assert last_start + last_wait <= deadline + 1
    assert last_start + last_client <= deadline + 1 + 10
    assert clock["t"] - start <= 62
    assert printed == [
        WAITING_LINE.format(address=inbox.address),
        f"No code yet. Run this again, then send an email with a code to {inbox.address}."
    ]


@pytest.mark.django_db
def test_snippet_copy_says_run_first_then_send():
    user = make_user("r2-copy")
    inbox = Inbox.objects.create(user=user, routing_key="r2_copy_box", label="r2")
    create_api_key(user, "k")
    page = _client(user).get(reverse("dashboard:overview")).content.decode()
    expected = (
        "Run this, then click <strong>Send a test code</strong> (or send any email "
        f"with a code to <code>{inbox.address}</code>)."
    )
    assert expected in page
    inboxes = _client(user).get(reverse("dashboard:inboxes")).content.decode()
    assert expected in inboxes
    # Overview order: snippet first, then the button.
    action = reverse("dashboard:inbox-test-code", args=[inbox.public_id])
    assert page.index('id="snippet-python"') < page.index(f'action="{action}"')


# --- 3. P2: the onboarding card never navigates away from the one-time key ----


@pytest.mark.django_db
def test_onboarding_card_test_code_is_flash_only_and_keeps_the_key():
    user = make_user("r2-onboard")
    inbox = Inbox.objects.create(user=user, routing_key="r2_onboard_box", label="r2")
    card = _client(user).post(
        reverse("dashboard:api-key-onboarding"), HTTP_HX_REQUEST="true"
    ).content.decode()
    action = reverse("dashboard:inbox-test-code", args=[inbox.public_id])
    form = re.search(r'<form class="inline-form test-code-form".*?</form>', card, re.S).group(0)
    assert f'hx-post="{action}"' in form
    assert 'hx-target="#onboarding-test-code-flash"' in form
    assert 'name="return_to" value="onboarding"' in form
    assert "data-disable-on-submit" not in form
    assert "Copy your key first" in form  # no-JS fallback note next to the button
    assert 'id="onboarding-test-code-flash"' in card
    assert "<script" not in card and "style=" not in card  # CSP

    client = _client(user)
    ok = client.post(action, {"return_to": "onboarding"}, HTTP_HX_REQUEST="true")
    assert ok.status_code == 200
    body = ok.content.decode()
    assert "Test code sent — it appears in a few seconds." in body
    assert "inbox-rows" not in body and "<table" not in body  # flash only, nothing else swapped
    assert Message.objects.filter(inbox=inbox).count() == 1

    # An error is also a flash-only partial the htmx error guard may swap.
    inbox.is_enabled = False
    inbox.save(update_fields=["is_enabled"])
    refused = client.post(action, {"return_to": "onboarding"}, HTTP_HX_REQUEST="true")
    assert refused.status_code == 409 and refused.headers.get("HX-Partial")
    assert "flash-error" in refused.content.decode()

    # Without JS it is a plain POST back to the Overview (allowlisted target).
    inbox.is_enabled = True
    inbox.save(update_fields=["is_enabled"])
    plain = client.post(action, {"return_to": "onboarding"})
    assert plain.status_code == 302 and plain.url == reverse("dashboard:overview")


# --- 6. P2: inbox bucket first, then account bucket ---------------------------


@pytest.mark.django_db
def test_inbox_refusal_does_not_burn_account_budget():
    user = make_user("r2-buckets")
    inbox = Inbox.objects.create(user=user, routing_key="r2_bucket_box", label="r2")
    client = _client(user)
    url = reverse("dashboard:inbox-test-code", args=[inbox.public_id])
    limit = dashboard_views.TEST_CODE_INBOX_RATE_LIMIT
    statuses = [client.post(url, HTTP_HX_REQUEST="true").status_code for _ in range(limit + 3)]
    assert statuses == [200] * limit + [429] * 3
    account_bucket = RateLimitBucket.objects.get(
        bucket_hash=_bucket_hash(dashboard_views.TEST_CODE_ACCOUNT_RATE_NAME, str(user.pk))
    )
    assert account_bucket.count == limit  # refused clicks charged nothing account-wide


# --- 8. Info: tooltip on every test-code button -------------------------------


@pytest.mark.django_db
def test_test_code_buttons_carry_the_not_via_mail_server_tooltip():
    user = make_user("r2-tip")
    Inbox.objects.create(user=user, routing_key="r2_tip_box", label="r2")
    client = _client(user)
    escaped = f'title="{TEST_CODE_TIP}"'
    onboarding = client.get(reverse("dashboard:overview")).content.decode()
    assert escaped in onboarding
    card = client.post(reverse("dashboard:api-key-onboarding"), HTTP_HX_REQUEST="true").content.decode()
    assert escaped in card
    assert escaped in client.get(reverse("dashboard:overview")).content.decode()
    assert escaped in client.get(reverse("dashboard:inboxes")).content.decode()


# --- 9.  P3: CSRF failure path guard rejects any backslash -------------


@pytest.mark.parametrize("path", ["/\\evil.example", "/login/\\x", "/a\\"])
def test_csrf_failure_path_with_backslash_is_dropped(path):
    from config.views import csrf_failure

    response = csrf_failure(RequestFactory().post(path))
    page = response.content.decode()
    assert response.status_code == 403
    assert 'href="/">Reload the page</a>' in page
    assert "evil" not in page and "\\" not in page
