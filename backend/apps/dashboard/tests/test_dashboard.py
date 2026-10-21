import hashlib
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan, ApiKey
from apps.accounts.services import (
    MAX_ACTIVE_API_KEYS_PER_USER,
    ApiKeyLimitExceeded,
    create_api_key,
)
from apps.inboxes.models import Inbox, Message
from apps.ingest.parser import parse_message

pytestmark = pytest.mark.django_db(transaction=True)


def make_user(name):
    user = get_user_model().objects.create_user(username=name, email=f"{name}@example.test", password="password")
    Account.objects.create(user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now())
    AccountPlan.objects.create(user=user, plan=AccountPlan.Plan.PAID)
    return user


def make_message(inbox, marker, **overrides):
    raw = f"Subject: {marker}\r\n\r\nbody {marker}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    values = {
        "inbox": inbox,
        "dedupe_key": digest,
        "envelope_from": "sender@example.test",
        "envelope_recipient": inbox.address,
        "header_from": "Header Sender <header@example.test>",
        "header_to": [inbox.address],
        "header_cc": ["copy@example.test"],
        "subject": marker,
        "text_body": f"text <script>{marker}</script>",
        "raw_bytes": raw,
        "raw_sha256": digest,
        "size_bytes": len(raw),
        "ingest_status": Message.IngestStatus.PARSED,
    }
    values.update(overrides)
    return Message.objects.create(**values)


def test_root_renders_landing_for_anonymous_and_redirects_authenticated_user():
    anonymous = Client()
    response = anonymous.get(reverse("dashboard:root"))
    assert response.status_code == 200
    
    assert b"<h1>Get the OTP, not the email</h1>" in response.content
    # pub-pages: primary CTA is "Start free" -> /register/ (was "$ open dashboard").
    assert b'href="/register/">Start free</a>' in response.content
    assert b"$ open dashboard" not in response.content
    assert b"messages/wait" in response.content
    assert anonymous.get(reverse("accounts:login")).status_code == 200

    user = make_user("root-user")
    authenticated = Client()
    authenticated.force_login(user)
    response = authenticated.get(reverse("dashboard:root"))
    assert response.status_code == 302
    assert response.url == reverse("dashboard:overview")


def test_docs_page_is_public_and_contains_wait_contract():
    import re as _re_mod

    anonymous = Client()
    response = anonymous.get(reverse("dashboard:docs"))
    # Public: no login redirect, renders 200.
    assert response.status_code == 200
    assert response.status_code != 302
    content = response.content.decode()
    assert "messages/wait" in content
    assert "require=otp" in content
    assert "Bearer" in content
    assert "timeout" in content
    assert "since=0" in content
    assert "too_many_wait_requests" in content
    assert "wait_capacity" in content
    # 3-step quickstart links to signup + dashboard.
    assert reverse("accounts:register") in content
    assert reverse("dashboard:overview") in content
    # Playwright example callout.
    assert "otp-signup.spec.ts" in content
    assert "MAILSOCKET_API_KEY" in content
    # Inert placeholder key (: $MAILSOCKET_API_KEY, never "***"), no
    # real key, no inline scripts.
    assert "Bearer $MAILSOCKET_API_KEY" in content
    assert "***" not in content
    assert _re_mod.search(r"ms_live_[A-Za-z0-9_-]{8,}", content) is None
    import re as _re
    for _tag in _re.findall(r"<script\b[^>]*>", content):
        assert "src=" in _tag, f"inline <script> not allowed on public page: {_tag}"


def test_docs_page_authorization_examples_render_as_valid_pasteable_markup():
    """B2 regression: the docs.html quickstart previously had corrupted
    `<code>`/`<span>` markup around the Authorization examples — a leaked
    `class="code-string">` fragment and a mangled `&lt;ke...de>` truncation —
    so a dev/AI copying the RENDERED text got invalid HTML/JS that wouldn't
    run. Assert the actual rendered text, not just template syntax."""
    content = Client().get(reverse("dashboard:docs")).content.decode()

    # No leaked/corrupted markup fragments in the visible text — these are the
    # exact byte patterns the regression shipped (a leaked HTML attribute
    # fragment glued onto "Authorization:", and a truncated placeholder).
    assert "Authorization: <span class=\"code-string\">" not in content
    assert "Authorization: <span class=\"code-key\">" in content or \
        '"Authorization"' in content
    assert "&lt;ke...de&gt;" not in content
    assert "&lt;ke...de>" not in content
    # B2 line-28 regression: the quickstart step-2 `<code>` example must be a
    # complete, valid, pasteable element — no mangled closing-tag fragment
    # and no truncated placeholder missing its closing `&gt;`.
    assert "...de>" not in content
    import re as _re2
    for _m in _re2.finditer(r"Bearer &lt;ke(?!y&gt;)", content):
        raise AssertionError(
            f"truncated 'Bearer &lt;ke' placeholder without closing &gt;: "
            f"{content[max(0, _m.start() - 20):_m.end() + 20]!r}"
        )
    assert "Authorization: Bearer &lt;key&gt;</code>" in content

    # The three canonical, paste-able Authorization examples are all present
    # verbatim: curl header, Python dict, and the Node/fetch object literal.
    # : each language reads the key from the environment its own way
    # (curl: $VAR, Python: os.environ, Node: process.env) — never "***".
    assert '-H <span class="code-string">"Authorization: Bearer $MAILSOCKET_API_KEY"</span>' in content
    assert (
        '<span class="code-key">"Authorization"</span>: '
        '<span class="code-string">f"Bearer {os.environ[\'MAILSOCKET_API_KEY\']}"</span>'
    ) in content
    # The Node/fetch header key must render as a real object key (wrapped in
    # its own <span>), not a bare leaked-fragment identifier.
    assert '<span class="code-key">Authorization</span>: <span class="code-string">`Bearer ${process.env.MAILSOCKET_API_KEY}`</span>' in content
    assert "***" not in content


@pytest.fixture
def tenants():
    alice = make_user("alice")
    bob = make_user("bob")
    alice_inbox = Inbox.objects.create(user=alice, routing_key="alice_box", label="Alice")
    bob_inbox = Inbox.objects.create(user=bob, routing_key="bob_box", label="Bob secret")
    alice_message = make_message(alice_inbox, "Alice message")
    bob_message = make_message(bob_inbox, "Bob secret message")
    return alice, bob, alice_inbox, bob_inbox, alice_message, bob_message


def test_case_11_anonymous_pages_redirect_and_mutations_require_csrf(tenants):
    _, _, alice_inbox, _, alice_message, _ = tenants
    anonymous = Client()
    for url in (
        "/app/",
        "/app/inboxes/",
        "/app/messages/",
        reverse("dashboard:message-detail", args=[alice_message.public_id]),
        reverse("dashboard:message-html", args=[alice_message.public_id]),
    ):
        response = anonymous.get(url)
        assert response.status_code == 302
        assert response.url.startswith("/login/")

    alice = tenants[0]
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(alice)
    for url in (
        reverse("dashboard:inbox-create"),
        reverse("dashboard:inbox-rename", args=[alice_inbox.public_id]),
        reverse("dashboard:inbox-disable", args=[alice_inbox.public_id]),
        reverse("dashboard:inbox-delete", args=[alice_inbox.public_id]),
    ):
        assert csrf_client.post(url, {"label": "x"}, HTTP_HX_REQUEST="true").status_code == 403


def test_cases_12_13_inbox_actions_are_owned_and_soft_delete_reserves_key(tenants):
    alice, _, alice_inbox, bob_inbox, _, _ = tenants
    client = Client()
    client.force_login(alice)

    response = client.post(reverse("dashboard:inbox-create"), {"label": "Generated"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    assert "inboxesChanged" in response.headers["HX-Trigger"]
    generated = Inbox.objects.get(user=alice, label="Generated")
    assert generated.routing_key and generated.routing_key != "Generated"

    response = client.post(reverse("dashboard:inbox-rename", args=[alice_inbox.public_id]), {"label": "Renamed"})
    assert response.status_code == 302
    alice_inbox.refresh_from_db()
    assert alice_inbox.label == "Renamed"

    for route in ("inbox-rename", "inbox-enable", "inbox-disable", "inbox-delete"):
        response = client.post(reverse(f"dashboard:{route}", args=[bob_inbox.public_id]), {"label": "stolen"})
        missing = client.post(reverse(f"dashboard:{route}", args=["inbox_missing"]), {"label": "stolen"})
        assert response.status_code == missing.status_code == 404
    bob_inbox.refresh_from_db()
    assert bob_inbox.label == "Bob secret"

    key = alice_inbox.routing_key
    client.post(reverse("dashboard:inbox-delete", args=[alice_inbox.public_id]))
    alice_inbox.refresh_from_db()
    assert alice_inbox.deleted_at is not None
    assert alice_inbox.is_enabled is False
    assert not Inbox.objects.filter(user=alice, deleted_at__isnull=True, pk=alice_inbox.pk).exists()
    with pytest.raises(IntegrityError), transaction.atomic():
        Inbox.objects.create(user=alice, routing_key=key)


def test_case_12_validation_returns_same_partial_with_422(tenants):
    alice, _, alice_inbox, _, _, _ = tenants
    client = Client()
    client.force_login(alice)
    response = client.post(
        reverse("dashboard:inbox-rename", args=[alice_inbox.public_id]),
        {"label": "x" * 121},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 422
    assert b'id="inbox-rows"' not in response.content
    assert b"Label must be at most 120 characters" in response.content


# --- S-C: web-form custom slug uses the SAME shared validator as the API -------


def test_web_form_custom_slug_creates_inbox_at_exact_address(tenants):
    alice, _, _, _, _, _ = tenants
    client = Client()
    client.force_login(alice)

    response = client.post(
        reverse("dashboard:inbox-create"),
        {"label": "Custom", "routing_key": "my-custom-slug"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert Inbox.objects.filter(user=alice, routing_key="my-custom-slug").exists()


def test_web_form_uppercase_slug_is_folded_to_lowercase(tenants):
    alice, _, _, _, _, _ = tenants
    client = Client()
    client.force_login(alice)

    response = client.post(
        reverse("dashboard:inbox-create"),
        {"label": "Folded", "routing_key": "MyCustomBox"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert Inbox.objects.filter(user=alice, routing_key="mycustombox").exists()
    assert not Inbox.objects.filter(routing_key="MyCustomBox").exists()


@pytest.mark.parametrize("reserved", ["postmaster", "abuse", "admin", "noreply", "hostmaster", "mailer-daemon"])
def test_web_form_reserved_slug_rejected_422_no_inbox_created(tenants, reserved):
    """Same reserved-slug set the API rejects (see
    apps.api.tests.test_group_c_hardening.test_custom_slug_rejects_reserved_localparts) —
    both surfaces call apps.inboxes.services.normalize_routing_key."""
    alice, _, _, _, _, _ = tenants
    client = Client()
    client.force_login(alice)

    response = client.post(
        reverse("dashboard:inbox-create"),
        {"label": "Reserved", "routing_key": reserved},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 422
    assert not Inbox.objects.filter(routing_key=reserved).exists()


def test_web_form_duplicate_custom_slug_is_409_not_randomized(tenants):
    alice, _, alice_inbox, _, _, _ = tenants
    client = Client()
    client.force_login(alice)

    total_before = Inbox.objects.filter(user=alice).count()

    response = client.post(
        reverse("dashboard:inbox-create"),
        {"label": "Dup", "routing_key": alice_inbox.routing_key},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 409
    assert b"already taken" in response.content
    # No new inbox silently minted under a random key for this attempt.
    assert Inbox.objects.filter(user=alice, routing_key=alice_inbox.routing_key).count() == 1
    # And the user's TOTAL inbox count is unchanged — proves no other
    # inbox (random key or otherwise) was silently minted either.
    assert Inbox.objects.filter(user=alice).count() == total_before


def test_web_form_empty_slug_still_yields_random_address(tenants):
    alice, _, _, _, _, _ = tenants
    client = Client()
    client.force_login(alice)

    response = client.post(
        reverse("dashboard:inbox-create"),
        {"label": "Random", "routing_key": ""},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    generated = Inbox.objects.get(user=alice, label="Random")
    assert generated.routing_key and generated.routing_key != ""


def test_cases_14_15_owner_scoping_foreign_filters_and_escaped_detail(tenants):
    alice, _, alice_inbox, bob_inbox, alice_message, bob_message = tenants
    client = Client()
    client.force_login(alice)

    overview = client.get(reverse("dashboard:overview"))
    listing = client.get(reverse("dashboard:messages"))
    assert b"Alice message" in overview.content and b"Bob secret" not in overview.content
    assert b"Alice message" in listing.content and b"Bob secret" not in listing.content
    # qafix2 #14: a foreign or unknown ?inbox= is ignored (not a 404 page);
    # foreign and missing behave identically and Bob's mail never appears.
    foreign = client.get(reverse("dashboard:messages"), {"inbox": bob_inbox.public_id})
    missing = client.get(reverse("dashboard:messages"), {"inbox": "inbox_missing"})
    assert foreign.status_code == missing.status_code == 200
    assert b"Bob secret" not in foreign.content and b"Alice message" in foreign.content

    for route in ("message-detail", "message-panel", "message-html"):
        foreign = client.get(reverse(f"dashboard:{route}", args=[bob_message.public_id]))
        missing = client.get(reverse(f"dashboard:{route}", args=["msg_missing"]))
        assert foreign.status_code == missing.status_code == 404

    detail = client.get(reverse("dashboard:message-detail", args=[alice_message.public_id]))
    assert b"Header Sender" in detail.content
    assert alice_inbox.address.encode() in detail.content
    assert b"copy@example.test" in detail.content
    assert b"&lt;script&gt;Alice message&lt;/script&gt;" in detail.content
    assert b"raw_bytes" not in detail.content and b"dedupe_key" not in detail.content
    assert f'/app/messages/{alice_message.public_id}/html/'.encode() in detail.content
    assert b'<iframe class="preview"' in detail.content and b" sandbox " in detail.content
    assert b"|safe" not in detail.content


def test_case_14_cursor_is_deterministic_for_identical_timestamps(tenants):
    alice, _, alice_inbox, _, _, _ = tenants
    stamp = timezone.now() - timedelta(hours=1)
    created = [make_message(alice_inbox, f"same-{index}") for index in range(30)]
    Message.objects.filter(pk__in=[message.pk for message in created]).update(received_at=stamp)
    expected = list(Message.objects.filter(pk__in=[message.pk for message in created]).order_by("-received_at", "-id").values_list("public_id", flat=True))

    client = Client()
    client.force_login(alice)
    first = client.get(reverse("dashboard:messages"))
    assert first.status_code == 200
    first_ids = [message.public_id for message in first.context["messages"] if message.public_id in expected]
    second = client.get(reverse("dashboard:messages"), {"cursor": first.context["next_cursor"]})
    second_ids = [message.public_id for message in second.context["messages"] if message.public_id in expected]
    assert first_ids + second_ids == expected
    assert len(set(first_ids + second_ids)) == 30


def test_cases_16_17_18_html_is_bounded_sanitized_and_preview_is_locked_down(tenants):
    alice, _, alice_inbox, _, _, _ = tenants
    attack = """<p><b>Benign</b></p><script>window.pwned=1</script>
    <img src="https://attacker.test/pixel" onerror="fetch('https://attacker.test')">
    <a href="javascript:alert(1)" onclick="alert(2)">bad</a>
    <a href="data:text/html,pwn">data</a><a href="https://example.test/ok">good</a>
    <form action="https://attacker.test"><input autofocus></form><iframe src="https://attacker.test"></iframe>
    <svg><script>alert(3)</script></svg><style>@import url(https://attacker.test/x);</style>
    <div style="background:url(https://attacker.test/y)">styled</div>"""
    raw = (
        b"MIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary=x\r\n\r\n"
        b"--x\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nOTP 482193\r\n"
        b"--x\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        + attack.encode()
        + b"\r\n--x--\r\n"
    )
    digest = hashlib.sha256(raw).hexdigest()
    message = Message.objects.create(
        inbox=alice_inbox,
        dedupe_key=digest,
        envelope_recipient=alice_inbox.address,
        raw_bytes=raw,
        raw_sha256=digest,
        size_bytes=len(raw),
    )
    message = parse_message(message.pk)
    stored = message.html_body
    assert "<b>Benign</b>" in stored and "styled" in stored
    for forbidden in ("<script", "onerror", "onclick", "javascript:", "data:text", "<form", "<input", "<iframe", "<svg", "<style", "attacker.test", "style="):
        assert forbidden not in stored.lower()
    assert 'href="https://example.test/ok"' in stored
    assert 'rel="noopener noreferrer nofollow"' in stored
    assert message.text_body.strip() == "OTP 482193"
    assert message.detected_otp == "482193"
    assert bytes(message.raw_bytes) == raw

    client = Client()
    client.force_login(alice)
    preview = client.get(reverse("dashboard:message-html", args=[message.public_id]))
    assert preview.status_code == 200
    assert preview.headers["Content-Type"] == "text/html; charset=utf-8"
    assert preview.headers["Content-Security-Policy"] == "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
    assert preview.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert stored.encode() in preview.content

    blank = make_message(alice_inbox, "Blank HTML", html_body="")
    blank_preview = client.get(reverse("dashboard:message-html", args=[blank.public_id]))
    assert b"No HTML body." in blank_preview.content
    assert blank.html_body == ""

    oversized = "<p>" + ("x" * 1000) + "</p>"
    oversized_raw = b"Content-Type: text/html; charset=utf-8\r\n\r\n" + oversized.encode()
    from django.test import override_settings
    with override_settings(MAX_PARSE_HTML_BYTES=100):
        parsed = parse_message(Message.objects.create(
            inbox=alice_inbox,
            dedupe_key=hashlib.sha256(oversized_raw).hexdigest(),
            envelope_recipient=alice_inbox.address,
            raw_bytes=oversized_raw,
            raw_sha256=hashlib.sha256(oversized_raw).hexdigest(),
            size_bytes=len(oversized_raw),
        ).pk)
    assert len(parsed.html_body.encode()) <= 107
    assert bytes(parsed.raw_bytes) == oversized_raw


def _api_key_client(user):
    client = Client()
    client.force_login(user)
    return client


def test_api_key_create_success_returns_plaintext_once_and_no_store():
    user = make_user("apikey-success")
    client = _api_key_client(user)
    response = client.post(
        reverse("dashboard:api-key-create"),
        {"name": "CI key"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    body = response.content.decode()
    assert "ms_live_" in body
    assert "Copy this key now" in body

    key = ApiKey.objects.get(user=user)
    assert key.name == "CI key"
    assert key.key_hash
    # The plaintext token is never persisted.
    token = body.split("ms_live_", 1)[1].split("<", 1)[0]
    token = f"ms_live_{token}"
    assert token not in key.__dict__.values()


def test_api_key_active_cap_refuses_21st_and_revoke_frees_a_slot():
    user = make_user("apikey-cap")
    client = _api_key_client(user)

    # Fill to the cap through the service (no rate limit there) so the cap is
    # what the view refuses, not the hourly create limit.
    for index in range(MAX_ACTIVE_API_KEYS_PER_USER):
        create_api_key(user, f"key-{index}")
    assert ApiKey.objects.filter(user=user, revoked_at__isnull=True).count() == MAX_ACTIVE_API_KEYS_PER_USER

    # 21st create is refused with 409 and mints no extra row.
    response = client.post(
        reverse("dashboard:api-key-create"),
        {"name": "overflow"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 409
    assert b"maximum of 20 active API keys" in response.content
    assert ApiKey.objects.filter(user=user, revoked_at__isnull=True).count() == MAX_ACTIVE_API_KEYS_PER_USER

    # Revoking one key frees a slot; the next create succeeds.
    victim = ApiKey.objects.filter(user=user, revoked_at__isnull=True).order_by("id").first()
    client.post(reverse("dashboard:api-key-revoke", args=[victim.public_id]), HTTP_HX_REQUEST="true")
    response = client.post(
        reverse("dashboard:api-key-create"),
        {"name": "after-revoke"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert ApiKey.objects.filter(user=user, revoked_at__isnull=True).count() == MAX_ACTIVE_API_KEYS_PER_USER
    assert ApiKey.objects.filter(user=user, name="after-revoke").exists()


def test_api_key_rate_limit_429_and_per_user_isolation():
    alice = make_user("apikey-rate-alice")
    bob = make_user("apikey-rate-bob")
    alice_client = _api_key_client(alice)

    for index in range(10):
        response = alice_client.post(
            reverse("dashboard:api-key-create"),
            {"name": f"alice-{index}"},
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
    response = alice_client.post(
        reverse("dashboard:api-key-create"),
        {"name": "alice-11th"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 429
    assert b"Too many API keys" in response.content
    assert ApiKey.objects.filter(user=alice).count() == 10

    # Bob is a different rate-limit subject and is unaffected.
    bob_client = _api_key_client(bob)
    response = bob_client.post(
        reverse("dashboard:api-key-create"),
        {"name": "bob-1"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert ApiKey.objects.filter(user=bob).count() == 1


def test_api_key_service_raises_distinct_cap_exception():
    user = make_user("apikey-service-cap")
    for _ in range(MAX_ACTIVE_API_KEYS_PER_USER):
        create_api_key(user, "bulk")
    with pytest.raises(ApiKeyLimitExceeded) as exc_info:
        create_api_key(user, "overflow")
    assert exc_info.value.limit == MAX_ACTIVE_API_KEYS_PER_USER
    assert ApiKey.objects.filter(user=user, revoked_at__isnull=True).count() == MAX_ACTIVE_API_KEYS_PER_USER


def test_onboarding_api_key_create_respects_cap_and_rate_limit():
    user = make_user("apikey-onboarding")
    client = _api_key_client(user)
    url = reverse("dashboard:api-key-onboarding")

    # Fill to the cap through the service (no rate limit), then the onboarding
    # path must refuse instead of bypassing the cap.
    for index in range(MAX_ACTIVE_API_KEYS_PER_USER):
        create_api_key(user, f"onboard-{index}")
    response = client.post(url, HTTP_HX_REQUEST="true")
    assert response.status_code == 409
    assert b"maximum of 20 active API keys" in response.content
    assert ApiKey.objects.filter(user=user, revoked_at__isnull=True).count() == MAX_ACTIVE_API_KEYS_PER_USER


def test_onboarding_api_key_create_rate_limited_no_500():
    user = make_user("apikey-onboarding-rate")
    client = _api_key_client(user)
    url = reverse("dashboard:api-key-onboarding")

    for _ in range(10):
        response = client.post(url, HTTP_HX_REQUEST="true")
        assert response.status_code == 200
    response = client.post(url, HTTP_HX_REQUEST="true")
    assert response.status_code == 429
    assert b"Too many API keys" in response.content
    assert ApiKey.objects.filter(user=user).count() == 10


@pytest.mark.django_db
def test_fleet_is_staff_only():
    "Public API."
    from django.contrib.auth import get_user_model
    from django.test import Client
    U = get_user_model()

    staff = U.objects.create_user("fleet_staff_t", password="x")
    staff.is_staff = True
    staff.save()
    c = Client()
    c.force_login(staff)
    r = c.get("/app/fleet/", HTTP_HOST="testserver", secure=True)
    assert r.status_code == 200
    body = r.content.decode()
    assert "Fleet" in body and "Customers" in body and "Top customers" in body

    normal = U.objects.create_user("fleet_normal_t", password="x")
    c2 = Client()
    c2.force_login(normal)
    r2 = c2.get("/app/fleet/", HTTP_HOST="testserver", secure=True)
    # qafix2 #20: plain 404, never a redirect revealing the admin login path.
    assert r2.status_code == 404
    assert "/admin/" not in r2.get("Location", "")

    r3 = Client().get("/app/fleet/", HTTP_HOST="testserver", secure=True)
    assert r3.status_code == 404


def test_api_keys_page_ships_playground_csp_safe():
    """The API-keys page carries the test-your-key playground (data-api-base +
    controls) and loads its JS EXTERNALLY (CSP-safe, no inline script)."""
    import re
    user = make_user("pg-user")
    client = _api_key_client(user)
    response = client.get(reverse("dashboard:api-keys"))
    assert response.status_code == 200
    body = response.content.decode()
    assert 'id="api-playground"' in body
    assert 'data-api-base=' in body
    assert 'id="pg-run"' in body
    assert 'id="pg-key"' in body
    # playground.js must be an external <script src>, and NO inline script.
    assert re.search(r'<script\b[^>]*\bsrc="[^"]*/dashboard/playground\.min\.js"', body)
    for tag in re.findall(r"<script\b[^>]*>", body):
        assert "src=" in tag, f"inline <script> on api-keys page: {tag}"
