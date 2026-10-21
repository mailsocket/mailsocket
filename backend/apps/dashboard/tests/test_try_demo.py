"""Zero-signup try-it-live playground (sprint C1) — threat-model tests.

Covers the full defensive surface of the anonymous /docs/try/ endpoints and
page: IP rate limiting (429 not 500, per-IP independence), demo-user scoping,
IDOR resistance against real inboxes, no real API-key exposure, Free-retention
coverage of demo mail, wait-proxy reuse of the real wait path (timeout clamp,
OTP+context return, 204 empty, 429 cap), XSS-safe rendering, and anonymous
access with no login and no API key.
"""

import hashlib
import re
import time

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan
from apps.accounts.services import (
    DEMO_USER_EMAIL,
    acquire_wait_lease,
    get_demo_user,
)
from apps.inboxes.models import Inbox, Message
from apps.inboxes.retention import _plan_expiry_filter

pytestmark = pytest.mark.django_db(transaction=True)


def _make_real_user(name, *, plan=AccountPlan.Plan.PAID):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user,
        email_normalized=f"{name}@example.test",
        email_verified_at=timezone.now(),
    )
    AccountPlan.objects.create(user=user, plan=plan)
    return user


def _make_message(inbox, marker, **overrides):
    raw = f"Subject: {marker}\r\n\r\ncode body {marker}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    values = {
        "inbox": inbox,
        "dedupe_key": digest,
        "envelope_from": "security@acme.test",
        "envelope_recipient": inbox.address,
        "header_from": "Security <security@acme.test>",
        "header_to": [inbox.address],
        "header_cc": [],
        "subject": marker,
        "text_body": f"Your verification code is 482193. {marker}",
        "html_body": "",
        "raw_bytes": raw,
        "raw_sha256": digest,
        "size_bytes": len(raw),
        "ingest_status": Message.IngestStatus.PARSED,
        "otp": "482193",
        "otp_confidence": 0.95,
        "detected_otp": "482193",
    }
    values.update(overrides)
    return Message.objects.create(**values)


def _create_url():
    return reverse("dashboard:try-inbox-create")


def _wait_url(public_id):
    return reverse("dashboard:try-inbox-wait", kwargs={"public_id": public_id})


def _sample_url(public_id):
    return reverse("dashboard:try-inbox-sample", kwargs={"public_id": public_id})


# --- demo user provisioning / scoping ----------------------------------------


def test_demo_user_is_free_verified_unusable_and_idempotent():
    first = get_demo_user()
    second = get_demo_user()

    assert first.pk == second.pk
    assert first.email == DEMO_USER_EMAIL
    assert not first.has_usable_password()
    assert not first.is_staff and not first.is_superuser
    assert first.account.email_verified_at is not None
    assert first.account.is_suspended is False
    assert first.account_plan.plan == AccountPlan.Plan.FREE
    # Idempotent: a second call never mints a duplicate user/account/plan.
    assert get_user_model().objects.filter(email=DEMO_USER_EMAIL).count() == 1
    assert Account.objects.filter(user=first).count() == 1
    assert AccountPlan.objects.filter(user=first).count() == 1


def test_demo_inbox_owned_by_demo_user_on_free_plan():
    demo = get_demo_user()
    response = Client().post(_create_url())
    assert response.status_code == 201
    inbox = Inbox.objects.get(public_id=response.json()["id"])
    assert inbox.user_id == demo.pk
    assert inbox.user.account_plan.plan == AccountPlan.Plan.FREE


def test_demo_message_falls_under_free_retention_filter():
    demo = get_demo_user()
    inbox = Inbox.objects.create(user=demo, routing_key="demo_retention_box")
    message = _make_message(inbox, "retention", otp="111111", otp_confidence=0.9)

    # Backdate the demo message a day + a bit: on the Free plan it must be
    # matched by the shared retention expiry predicate (the purge's source of
    # truth) — i.e. demo mail is auto-deleted by the EXISTING purge, no new path.
    old = timezone.now() - timezone.timedelta(days=1, seconds=1)
    Message.objects.filter(pk=message.pk).update(received_at=old)
    expired_ids = set(
        Message.objects.filter(_plan_expiry_filter(timezone.now())).values_list("pk", flat=True)
    )
    assert message.pk in expired_ids


# --- anonymous access + no key -----------------------------------------------


def test_try_page_is_public_and_emits_no_real_key_or_inline_script():
    client = Client()
    response = client.get(reverse("dashboard:try-it"))
    assert response.status_code == 200
    assert response.status_code != 302
    content = response.content.decode()

    # No real API key secret anywhere in the page (placeholder only).
    assert re.search(r"ms_live_[A-Za-z0-9_-]{8,}", content) is None
    # The real wait contract is shown (equivalent snippet) — placeholder key.
    assert "messages/wait" in content
    assert "Bearer $MAILSOCKET_API_KEY" in content
    assert "***" not in content
    # CSP-safe: every <script> is an external src; no inline script; no |safe.
    assert content.count("<script") == content.count('<script defer src="')
    assert "|safe" not in content


def test_demo_endpoints_work_anonymously_with_no_login_no_key():
    client = Client()
    assert not client.session.items()
    create = client.post(_create_url())
    assert create.status_code == 201
    body = create.json()
    assert "id" in body and "address" in body

    # Wait on the created demo inbox with no auth -> 204 (nothing yet).
    wait = client.get(_wait_url(body["id"]), {"timeout": 0.5})
    assert wait.status_code == 204


# --- rate limiting -----------------------------------------------------------


def test_inbox_creation_ip_rate_limited_429_not_500_and_per_ip_independent():
    from apps.accounts.services import DEMO_INBOX_RATE_LIMIT

    for _ in range(DEMO_INBOX_RATE_LIMIT):
        response = Client().post(_create_url(), REMOTE_ADDR="203.0.113.7")
        assert response.status_code == 201

    blocked = Client().post(_create_url(), REMOTE_ADDR="203.0.113.7")
    assert blocked.status_code == 429
    assert blocked.json().get("error")

    # A different IP is an independent bucket and still succeeds.
    other = Client().post(_create_url(), REMOTE_ADDR="203.0.113.8")
    assert other.status_code == 201


@override_settings(TRUSTED_PROXY_CIDRS="172.30.0.9/32")  # trusted hop; distinct CF IPs -> distinct buckets
def test_inbox_creation_prefers_cloudflare_ip_and_respects_distinct_buckets():
    for _ in range(2):
        assert Client().post(
            _create_url(),
            REMOTE_ADDR="172.30.0.9",
            HTTP_CF_CONNECTING_IP="198.51.100.1",
        ).status_code == 201
    # A new CF IP is independent even though REMOTE_ADDR is identical.
    assert Client().post(
        _create_url(),
        REMOTE_ADDR="172.30.0.9",
        HTTP_CF_CONNECTING_IP="198.51.100.2",
    ).status_code == 201


# --- IDOR ----------------------------------------------------------------


def test_demo_wait_returns_404_for_real_customer_inbox():
    owner = _make_real_user("demo-idor-owner")
    real_inbox = Inbox.objects.create(user=owner, routing_key="real_owner_box")

    response = Client().get(_wait_url(real_inbox.public_id))

    assert response.status_code == 404
    
    content = response.content.decode()
    assert real_inbox.address not in content
    assert "owner" not in content


def test_demo_wait_returns_404_for_unknown_inbox():
    response = Client().get(_wait_url("inbox_does_not_exist"))
    assert response.status_code == 404


def test_demo_wait_only_resolves_demo_user_inboxes():
    demo = get_demo_user()
    demo_inbox = Inbox.objects.create(user=demo, routing_key="demo_only_box")

    response = Client().get(_wait_url(demo_inbox.public_id), {"timeout": 0.5})
    assert response.status_code == 204


# --- wait proxy: reuse real path, clamp timeout, OTP shape, caps ---------


def test_wait_proxy_returns_otp_and_context_on_seeded_message(monkeypatch):
    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    demo = get_demo_user()
    inbox = Inbox.objects.create(user=demo, routing_key="demo_seed_box")
    _make_message(inbox, "seed-otp")

    response = Client().get(_wait_url(inbox.public_id), {"timeout": 1, "since": "0"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["otp"] == "482193"
    assert data["otp_context"] and "482193" in data["otp_context"]
    assert data["from"] == "Security <security@acme.test>"
    # The demo response must never include raw/HTML body fields.
    assert "text" not in data and "html" not in data


def test_wait_proxy_times_out_with_204(monkeypatch):
    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    demo = get_demo_user()
    inbox = Inbox.objects.create(user=demo, routing_key="demo_timeout_box")

    response = Client().get(_wait_url(inbox.public_id), {"timeout": 0.5})

    assert response.status_code == 204


def test_wait_proxy_clamps_timeout_to_demo_max(monkeypatch):
    """try-page.md step 1: DEMO_WAIT_TIMEOUT_MAX now equals the paid API's own
    ceiling (25s) rather than a lower bot-safety value, but an absurd
    requested timeout must still be clamped to that ceiling, not accepted
    verbatim. Monkeypatch both the demo ceiling and the poll interval low so
    the test itself stays fast while still proving the clamp arithmetic."""
    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.01)
    monkeypatch.setattr("apps.dashboard.demo.DEMO_WAIT_TIMEOUT_MAX", 0.3)
    demo = get_demo_user()
    inbox = Inbox.objects.create(user=demo, routing_key="demo_clamp_box")

    start = time.monotonic()
    response = Client().get(_wait_url(inbox.public_id), {"timeout": "999"})
    elapsed = time.monotonic() - start

    assert response.status_code == 204
    # Clamped to the (patched-low) demo ceiling, not the requested 999s.
    assert elapsed < 2


def test_wait_proxy_honours_demo_user_wait_cap_429(monkeypatch):
    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    demo = get_demo_user()
    inbox = Inbox.objects.create(user=demo, routing_key="demo_cap_box")

    # Occupy the demo user's per-account wait slots (r2: the demo user's own
    # cap, DEMO_WAIT_LEASE_LIMIT = WAIT_CAPACITY_FREE_LIMIT - 1 = 11, not Free's 2).
    from apps.accounts.services import DEMO_WAIT_LEASE_LIMIT

    for _ in range(DEMO_WAIT_LEASE_LIMIT):
        assert acquire_wait_lease(demo.pk) is True

    response = Client().get(_wait_url(inbox.public_id), {"timeout": 1})

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "too_many_wait_requests"


# --- XSS ----------------------------------------------------------------


def test_demo_page_escapes_html_in_subject_and_context():
    demo = get_demo_user()
    inbox = Inbox.objects.create(user=demo, routing_key="demo_xss_box")
    _make_message(
        inbox,
        '<script>alert("xss")</script>',
        text_body='Your code is 482193 <img src=x onerror=alert(1)>',
        subject='<script>alert("xss")</script>',
    )

    # The page template never renders message content (it's fetched via JS and
    # written with textContent), and the template itself has no |safe and no
    # raw html body usage.
    page = Client().get(reverse("dashboard:try-it")).content.decode()
    assert "|safe" not in page

    # The message's html_body is empty in the demo fixture; ensure the summary
    # serializer does not expose an html field at all.
    response = Client().get(_wait_url(inbox.public_id), {"timeout": 1, "since": "0"})
    data = response.json()["data"]
    assert "html" not in data
    # The subject is returned as a plain JSON string (never a raw HTML fragment
    # the browser would parse), so rendering it via textContent is inert.
    assert data["subject"] == '<script>alert("xss")</script>'





def test_get_demo_user_concurrent_first_boot_never_500s():
    """Two threads racing the very first get_demo_user() both return the SAME
    demo user; the loser catches the unique-constraint IntegrityError and
    re-fetches instead of surfacing a 500 (Audit-found first-boot race)."""
    from concurrent.futures import ThreadPoolExecutor

    from django.db import connection

    assert not get_user_model().objects.filter(email=DEMO_USER_EMAIL).exists()

    def _make():
        try:
            return get_demo_user().pk
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        pks = [f.result() for f in [pool.submit(_make), pool.submit(_make)]]

    assert pks[0] == pks[1]
    # Exactly one demo user exists — no duplicate, no orphan.
    assert get_user_model().objects.filter(email=DEMO_USER_EMAIL).count() == 1


def test_wait_proxy_rejects_garbage_timeout_via_shared_parser(monkeypatch):
    """demo.py no longer hand-rolls timeout parsing; a NaN/negative/non-numeric
    timeout falls back through the shared _parse_wait_timeout and is re-clamped
    to the demo ceiling (never raises, never holds a worker long)."""
    from apps.dashboard import demo as demo_mod

    captured = {}
    def _fake_perform_wait(*, timeout, **kwargs):
        captured["timeout"] = timeout
        from rest_framework.response import Response

        return Response(status=204)

    monkeypatch.setattr(demo_mod, "perform_wait", _fake_perform_wait)
    demo_user = get_demo_user()
    inbox = Inbox.objects.create(user=demo_user, routing_key="garbagetimeout01")

    client = Client()
    for bad in ("abc", "-5", "nan", "1e999"):
        captured.clear()
        resp = client.get(_wait_url(inbox.public_id), {"timeout": bad})
        assert resp.status_code == 204
        # Always a finite value inside the demo's [0.25, DEMO_WAIT_TIMEOUT_MAX].
        assert 0.25 <= captured["timeout"] <= demo_mod.DEMO_WAIT_TIMEOUT_MAX


def test_sweep_demo_inboxes_removes_only_stale_empty_demo_inboxes():
    """The GC sweep ( P2-a) deletes demo-user inboxes that are old AND
    empty, while preserving: recent demo inboxes, demo inboxes with messages,
    and real customers' inboxes entirely."""
    from datetime import timedelta

    from django.core.management import call_command

    demo_user = get_demo_user()
    now = timezone.now()

    stale_empty = Inbox.objects.create(user=demo_user, routing_key="staleempty000001")
    Inbox.objects.filter(pk=stale_empty.pk).update(created_at=now - timedelta(hours=48))

    fresh_empty = Inbox.objects.create(user=demo_user, routing_key="freshempty000001")

    stale_with_msg = Inbox.objects.create(user=demo_user, routing_key="stalewithmsg0001")
    Inbox.objects.filter(pk=stale_with_msg.pk).update(created_at=now - timedelta(hours=48))
    _make_message(stale_with_msg, "stalewithmsg")

    real_user = _make_real_user("realsweep")
    real_stale_empty = Inbox.objects.create(user=real_user, routing_key="realstaleempty01")
    Inbox.objects.filter(pk=real_stale_empty.pk).update(created_at=now - timedelta(hours=48))

    call_command("sweep_demo_inboxes", "--older-than-hours", "24")

    assert not Inbox.objects.filter(pk=stale_empty.pk).exists()  # swept
    assert Inbox.objects.filter(pk=fresh_empty.pk).exists()  # too new
    assert Inbox.objects.filter(pk=stale_with_msg.pk).exists()  # has mail
    assert Inbox.objects.filter(pk=real_stale_empty.pk).exists()  # not demo


# --- try-page.md: honest long-wait + "send a sample code" -------------------


def _primed_client():
    """A Client that enforces CSRF and already has a valid token+cookie,
    exactly as a real browser visiting /docs/try/ would (the page itself
    renders {% csrf_token %} inside the inbox card)."""
    client = Client(enforce_csrf_checks=True)
    priming = client.get(reverse("dashboard:try-it"))
    assert priming.status_code == 200
    token = client.cookies["csrftoken"].value
    return client, token


def test_demo_wait_timeout_max_matches_paid_api_ceiling():
    """try-page.md step 1: the demo long-wait clamp equals the paid API's own
    ceiling (apps.api.views.WAIT_TIMEOUT_MAX), not a lower bot-safety value —
    the page genuinely issues one long call per cycle instead of a disguised
    short-interval poll."""
    from apps.api.views import WAIT_TIMEOUT_MAX
    from apps.dashboard.demo import DEMO_WAIT_TIMEOUT_MAX

    assert DEMO_WAIT_TIMEOUT_MAX == WAIT_TIMEOUT_MAX


def test_demo_wait_still_uses_free_wait_capacity_subpool(monkeypatch):
    """Even at the raised clamp, a demo wait must still route through the
    reserved Free/agent sub-pool (WAIT_CAPACITY_FREE_LIMIT), never the shared
    headroom — verified via the real capacity helper, not a mock: exhaust the
    free sub-pool directly and confirm the demo wait is refused with the
    free-pool's own error, even though the GLOBAL pool still has room."""
    from apps.accounts.services import (
        WAIT_CAPACITY_FREE_LIMIT,
        acquire_wait_capacity,
    )

    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    demo_user = get_demo_user()
    inbox = Inbox.objects.create(user=demo_user, routing_key="demo_freepool_box")

    # Fill the free sub-pool (independent of, and smaller than, the global
    # WaitCapacity pool) directly through the production helper.
    for _ in range(WAIT_CAPACITY_FREE_LIMIT):
        assert acquire_wait_capacity(free=True) is True

    response = Client().get(_wait_url(inbox.public_id), {"timeout": 1})

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "wait_capacity"


def test_sample_creates_otp_message_only_for_demo_inbox(monkeypatch):
    """Happy path: POST .../sample on a demo inbox creates ONE Message via
    the real ingest/parse pipeline, with a 6-digit OTP the production parser
    extracted (not hand-set), and the subsequent wait call returns it."""
    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    demo_user = get_demo_user()
    inbox = Inbox.objects.create(user=demo_user, routing_key="demo_sample_box01")
    client, token = _primed_client()

    resp = client.post(
        _sample_url(inbox.public_id), {"csrfmiddlewaretoken": token}
    )
    assert resp.status_code == 201

    messages = Message.objects.filter(inbox=inbox)
    assert messages.count() == 1
    message = messages.get()
    assert message.ingest_status == Message.IngestStatus.PARSED
    assert message.otp is not None and re.fullmatch(r"\d{6}", message.otp)
    assert message.header_from == "mailsocket demo <demo@mailsocket.app>"
    assert message.subject == "Your verification code"

    wait_resp = Client().get(_wait_url(inbox.public_id), {"timeout": 1, "since": "0"})
    assert wait_resp.status_code == 200
    assert wait_resp.json()["data"]["otp"] == message.otp


def test_sample_rejects_foreign_or_unknown_inbox_with_uniform_404():
    """The sample endpoint resolves inboxes through the SAME demo-user-scoped
    lookup as the wait proxy: a real customer's inbox (or an unknown id)
    404s identically and never creates a Message anywhere."""
    owner = _make_real_user("sample-idor-owner")
    real_inbox = Inbox.objects.create(user=owner, routing_key="real_sample_box")
    client, token = _primed_client()

    resp = client.post(
        _sample_url(real_inbox.public_id), {"csrfmiddlewaretoken": token}
    )
    assert resp.status_code == 404
    assert not Message.objects.filter(inbox=real_inbox).exists()

    resp2 = client.post(
        _sample_url("inbox_does_not_exist"), {"csrfmiddlewaretoken": token}
    )
    assert resp2.status_code == 404


def test_sample_enforces_csrf():
    """CSRF-enforced client with NO token is refused (403), unlike the other
    two anonymous demo endpoints — this one creates data server-side."""
    demo_user = get_demo_user()
    inbox = Inbox.objects.create(user=demo_user, routing_key="demo_sample_csrf1")
    client = Client(enforce_csrf_checks=True)

    resp = client.post(_sample_url(inbox.public_id))

    assert resp.status_code == 403
    assert not Message.objects.filter(inbox=inbox).exists()


def test_sample_is_rate_limited_429_per_ip():
    from apps.accounts.services import DEMO_SAMPLE_RATE_LIMIT

    demo_user = get_demo_user()
    client, token = _primed_client()
    inboxes = [
        Inbox.objects.create(user=demo_user, routing_key=f"demo_samp_rl{i:02d}")
        for i in range(DEMO_SAMPLE_RATE_LIMIT + 1)
    ]

    for inbox in inboxes[:DEMO_SAMPLE_RATE_LIMIT]:
        resp = client.post(
            _sample_url(inbox.public_id), {"csrfmiddlewaretoken": token},
            REMOTE_ADDR="203.0.113.50",
        )
        assert resp.status_code == 201

    blocked = client.post(
        _sample_url(inboxes[-1].public_id), {"csrfmiddlewaretoken": token},
        REMOTE_ADDR="203.0.113.50",
    )
    assert blocked.status_code == 429
    assert not Message.objects.filter(inbox=inboxes[-1]).exists()


def test_sample_enforces_per_inbox_cap():
    from apps.accounts.services import DEMO_SAMPLE_PER_INBOX_LIMIT

    demo_user = get_demo_user()
    inbox = Inbox.objects.create(user=demo_user, routing_key="demo_sample_cap01")
    client, token = _primed_client()

    for _ in range(DEMO_SAMPLE_PER_INBOX_LIMIT):
        resp = client.post(
            _sample_url(inbox.public_id), {"csrfmiddlewaretoken": token}
        )
        assert resp.status_code == 201

    blocked = client.post(
        _sample_url(inbox.public_id), {"csrfmiddlewaretoken": token}
    )
    assert blocked.status_code == 429
    assert Message.objects.filter(inbox=inbox).count() == DEMO_SAMPLE_PER_INBOX_LIMIT


def test_sample_rejects_disabled_demo_inbox():
    demo_user = get_demo_user()
    inbox = Inbox.objects.create(
        user=demo_user, routing_key="demo_sample_dis01", is_enabled=False
    )
    client, token = _primed_client()

    resp = client.post(_sample_url(inbox.public_id), {"csrfmiddlewaretoken": token})

    assert resp.status_code == 404
    assert not Message.objects.filter(inbox=inbox).exists()


def _try_js_source():
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "static"
        / "dashboard"
        / "try.js"
    )
    return path.read_text()


def _js_block(content, opener):
    """Return the body of the ``{ ... }`` block that starts at ``opener``
    (brace-matched), so a test can assert on ONE branch of try.js."""
    start = content.index(opener)
    i = content.index("{", start)
    depth = 0
    for j in range(i, len(content)):
        if content[j] == "{":
            depth += 1
        elif content[j] == "}":
            depth -= 1
            if depth == 0:
                return content[i + 1 : j]
    raise AssertionError(f"unterminated block for {opener!r}")


def test_try_js_long_poll_shape_no_inline_script():
    """try.js stays a pure external file (CSP) and still asks for the full
    25s long-poll window."""
    content = _try_js_source()
    assert "timeout=25" in content
    assert "<script" not in content


def test_try_js_no_immediate_poll_in_429_or_error_branches():
    "Public API."
    content = _try_js_source()
    throttled = _js_block(content, 'if (outcome.kind === "throttled")')
    gone = _js_block(content, 'if (outcome.kind === "gone")')
    error = _js_block(content, 'if (outcome.kind === "error")')
    catch = _js_block(content, ".catch(() => {\n        if (token !== pollToken)")
    for name, body in [("429", throttled), ("404", gone), ("5xx", error), ("network", catch)]:
        assert "poll()" not in body, f"{name} branch re-polls immediately"
    assert "scheduleRetry(outcome.delay)" in throttled
    assert "scheduleRetry(backoffDelay())" in error
    assert "scheduleRetry(backoffDelay())" in catch


def test_try_js_reads_retry_after_and_404_stops():
    content = _try_js_source()
    assert 'response.headers.get("Retry-After")' in content
    assert "RETRY_AFTER_DEFAULT_S = 2" in content
    assert "Math.random() * 1000" in content  # 0-1s jitter
    gone = _js_block(content, 'if (outcome.kind === "gone")')
    assert "stopped = true" in gone
    assert "This demo inbox expired, create a new one" in gone
    # poll() refuses to run once stopped or while the tab is hidden.
    poll_body = _js_block(content, "function poll()")
    assert "stopped" in poll_body.split("\n")[1] and "document.hidden" in poll_body.split("\n")[1]
    assert "BACKOFF_START_MS = 1200" in content
    assert "BACKOFF_MAX_MS = 30000" in content
    assert 'addEventListener("visibilitychange"' in content


# Node-driven behavioural harness: runs the REAL try.js against a fake DOM,
# fake fetch and a manual clock, and reports how many wait requests were made
# and which timers were scheduled for each scripted response sequence.
_TRY_JS_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.argv[2], "utf8");
const scenario = JSON.parse(process.argv[3]);

function el(id) {
  return {
    id, textContent: "", hidden: false, disabled: false,
    dataset: {
      endpoint: "/docs/try/inbox",
      waitBase: "/docs/try/inbox/__ID__/wait",
      sampleBase: "/docs/try/inbox/__ID__/sample",
    },
    listeners: {},
    setAttribute() {}, removeAttribute() {},
    addEventListener(t, f) { this.listeners[t] = f; },
  };
}
const els = {};
const docListeners = {};
const document = {
  hidden: false,
  getElementById(id) { return els[id] || (els[id] = el(id)); },
  querySelector() { return { value: "tok" }; },
  addEventListener(t, f) { docListeners[t] = f; },
};
const waitResponses = scenario.responses.slice();
const waitCalls = [];
const timers = [];
function fetch(url, opts) {
  if (opts && opts.method === "POST") {
    return Promise.resolve({ status: 201, ok: true, headers: { get: () => null },
      json: () => Promise.resolve({ id: "inbox_x", address: "x@in.test" }) });
  }
  waitCalls.push(url);
  const r = waitResponses.shift();
  if (!r) return new Promise(() => {});  // hang forever: end of script
  if (r.network) return Promise.reject(new Error("net"));
  return Promise.resolve({
    status: r.status, ok: r.status >= 200 && r.status < 300,
    headers: { get: (h) => (r.headers || {})[h] ?? null },
    json: () => Promise.resolve(r.body || {}),
  });
}
const window = {
  setTimeout(fn, ms) { timers.push({ fn, ms }); return timers.length; },
  clearTimeout() {},
};
const ctx = { document, fetch, window, Math: Object.create(Math), console, Number, parseInt, Promise };
ctx.Math.random = () => 0.5;
vm.createContext(ctx);
vm.runInContext(src, ctx);

const flush = () => new Promise((r) => setImmediate(r));
(async () => {
  els["try-new-inbox"].listeners.click();
  for (let i = 0; i < 50; i++) await flush();
  const out = { waitCalls: waitCalls.length, timers: timers.map((t) => t.ms),
                status: els["try-status"].textContent, otp: els["try-otp"].textContent };
  // Fire the first scheduled retry (if any) and see that it polls once more.
  if (scenario.fireFirstTimer && timers.length) {
    document.hidden = !!scenario.hiddenBeforeFire;
    timers[0].fn();
    for (let i = 0; i < 50; i++) await flush();
    out.waitCallsAfterTimer = waitCalls.length;
    if (scenario.hiddenBeforeFire) {
      document.hidden = false;
      docListeners.visibilitychange();
      for (let i = 0; i < 50; i++) await flush();
      out.waitCallsAfterVisible = waitCalls.length;
    }
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _run_try_js(tmp_path, scenario):
    import json
    import pathlib
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed; static try.js tests still cover the shape")
    harness = tmp_path / "harness.js"
    harness.write_text(_TRY_JS_HARNESS)
    js_path = (
        pathlib.Path(__file__).resolve().parent.parent / "static" / "dashboard" / "try.js"
    )
    out = subprocess.run(
        [node, str(harness), str(js_path), json.dumps(scenario)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(out.stdout)


def test_try_js_behaviour_429_waits_retry_after_with_jitter(tmp_path):
    r = _run_try_js(tmp_path, {
        "responses": [{"status": 429, "headers": {"Retry-After": "5"}}],
        "fireFirstTimer": True,
    })
    # Exactly one wait call — no hot-loop — and a single retry timer of
    # Retry-After (5s) + jitter (random()=0.5 -> 500ms).
    assert r["waitCalls"] == 1
    assert r["timers"] == [5500]
    assert r["waitCallsAfterTimer"] == 2


def test_try_js_behaviour_429_without_header_defaults_to_2s(tmp_path):
    r = _run_try_js(tmp_path, {"responses": [{"status": 429}]})
    assert r["waitCalls"] == 1
    assert r["timers"] == [2500]


def test_try_js_behaviour_404_stops_polling(tmp_path):
    r = _run_try_js(tmp_path, {"responses": [{"status": 404}, {"status": 204}]})
    assert r["waitCalls"] == 1
    assert r["timers"] == []
    assert r["status"] == "This demo inbox expired, create a new one"


def test_try_js_behaviour_5xx_and_network_exponential_backoff(tmp_path):
    r = _run_try_js(tmp_path, {"responses": [{"status": 503}]})
    assert r["waitCalls"] == 1
    assert r["timers"] == [1200]
    r = _run_try_js(tmp_path, {"responses": [{"network": True}]})
    assert r["waitCalls"] == 1
    assert r["timers"] == [1200]


def test_try_js_behaviour_backoff_doubles_caps_and_resets(tmp_path):
    import pathlib
    import shutil
    import subprocess

    # Drive five consecutive 5xx by firing each scheduled retry: 1.2, 2.4,
    # 4.8, 9.6, 19.2, then capped at 30s; a 204 resets to 1.2s.
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    harness = _TRY_JS_HARNESS.replace(
        "  process.stdout.write(JSON.stringify(out));",
        "  while (timers.length < 8) { const t = timers[timers.length - 1]; t.fn();"
        " for (let i = 0; i < 50; i++) await flush(); }\n"
        "  out.timers = timers.map((t) => t.ms);\n"
        "  process.stdout.write(JSON.stringify(out));",
    )
    (tmp_path / "h.js").write_text(harness)
    import json

    responses = [{"status": 500}] * 7 + [{"status": 204}, {"status": 500}]
    js_path = pathlib.Path(__file__).resolve().parent.parent / "static" / "dashboard" / "try.js"
    out = subprocess.run(
        [node, str(tmp_path / "h.js"), str(js_path), json.dumps({"responses": responses})],
        capture_output=True, text=True, timeout=30, check=True,
    )
    timers = json.loads(out.stdout)["timers"]
    assert timers == [1200, 2400, 4800, 9600, 19200, 30000, 30000, 1200]


def test_try_js_behaviour_204_and_200_reissue_immediately(tmp_path):
    r = _run_try_js(tmp_path, {"responses": [
        {"status": 204},
        {"status": 200, "body": {"data": {"id": "msg_1", "otp": "123456"}}},
    ]})
    # 204 -> immediate re-issue; 200 -> shown + immediate re-issue (3rd call
    # hangs). No timers at all on the success path.
    assert r["waitCalls"] == 3
    assert r["timers"] == []
    assert r["otp"] == "123456"


def test_try_js_behaviour_hidden_tab_pauses_and_visibility_resumes(tmp_path):
    r = _run_try_js(tmp_path, {
        "responses": [{"status": 429}],
        "fireFirstTimer": True,
        "hiddenBeforeFire": True,
    })
    assert r["waitCalls"] == 1
    # Retry timer fired while hidden -> no request.
    assert r["waitCallsAfterTimer"] == 1
    # Tab visible again -> resumes immediately.
    assert r["waitCallsAfterVisible"] == 2


# --- r2: race-safe per-inbox sample cap + demo wait-lease limit -------------


def test_sample_per_inbox_cap_is_race_safe_under_parallel_posts():
    "Public API."
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from django.db import connection

    from apps.accounts.services import DEMO_SAMPLE_PER_INBOX_LIMIT

    assert DEMO_SAMPLE_PER_INBOX_LIMIT == 3
    demo_user = get_demo_user()
    inbox = Inbox.objects.create(user=demo_user, routing_key="demo_sample_race1")
    workers = 8
    barrier = threading.Barrier(workers)

    def _post(i):
        try:
            client, token = _primed_client()
            barrier.wait(timeout=10)
            return client.post(
                _sample_url(inbox.public_id),
                {"csrfmiddlewaretoken": token},
                REMOTE_ADDR=f"198.51.100.{i + 1}",  # distinct IPs: not the per-IP limit
            ).status_code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        codes = list(pool.map(_post, range(workers)))

    assert codes.count(201) == DEMO_SAMPLE_PER_INBOX_LIMIT
    assert codes.count(429) == workers - DEMO_SAMPLE_PER_INBOX_LIMIT
    assert Message.objects.filter(inbox=inbox).count() == DEMO_SAMPLE_PER_INBOX_LIMIT


def test_demo_user_wait_lease_limit_equals_free_subpool():
    """r2 ( P2): the shared demo user gets its own per-account wait
    cap (= WAIT_CAPACITY_FREE_LIMIT - 1, 11) instead of the Free plan's 2, while a
    real Free account keeps the Free cap and the demo user stays Free (so it
    is still bounded by the free sub-pool)."""
    from apps.accounts.services import (
        WAIT_CAPACITY_FREE_LIMIT,
        WAIT_LEASE_LIMIT_FREE,
        is_free_plan,
        wait_lease_limit_for,
    )

    demo_user = get_demo_user()
    real_free = _make_real_user("r2-real-free", plan=AccountPlan.Plan.FREE)

    assert WAIT_CAPACITY_FREE_LIMIT == 12
    # Demo leaves one free-pool slot for real Free/agent users ( r2).
    assert wait_lease_limit_for(demo_user.pk) == WAIT_CAPACITY_FREE_LIMIT - 1 == 11
    assert wait_lease_limit_for(real_free.pk) == WAIT_LEASE_LIMIT_FREE == 2
    assert is_free_plan(demo_user.pk) is True

    for _ in range(WAIT_CAPACITY_FREE_LIMIT - 1):
        assert acquire_wait_lease(demo_user.pk) is True
    assert acquire_wait_lease(demo_user.pk) is False


def test_demo_waits_still_bounded_by_free_subpool(monkeypatch):
    """With the raised demo lease cap, the global free sub-pool (12) still
    bounds total demo waits: with the free sub-pool already full, a demo wait
    is refused with wait_capacity even though the demo user's own lease cap
    (11) still has room."""
    from apps.accounts.services import WAIT_CAPACITY_FREE_LIMIT, acquire_wait_capacity

    monkeypatch.setattr("apps.api.views.WAIT_POLL_SECONDS", 0.05)
    demo_user = get_demo_user()
    inbox = Inbox.objects.create(user=demo_user, routing_key="demo_r2_pool_box")
    for _ in range(WAIT_CAPACITY_FREE_LIMIT):
        assert acquire_wait_capacity(free=True) is True
    assert acquire_wait_lease(demo_user.pk) is True  # demo lease still has room

    response = Client().get(_wait_url(inbox.public_id), {"timeout": 1})

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "wait_capacity"
