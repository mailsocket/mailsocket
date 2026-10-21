"""Public reference/pricing/guide pages (sprint A2/A3/A5).

Every route here is anonymous, read-only and CSP-safe. These tests assert:

- each route returns 200 to a logged-out client (no redirect to /login/),
- correct Content-Type (HTML, and text/yaml for the raw spec),
- the pricing page renders the real settings numbers,
- the error reference lists every code in ERROR_MESSAGES,
- the API reference names all six endpoints and the wait params,
- the raw openapi.yaml route actually serves valid YAML.
"""

import copy

import yaml
from django.test import Client, override_settings
from django.urls import reverse

from apps.api.errors import MESSAGES

EXPECTED_PATHS = {
    "/inboxes",
    "/inboxes/{id}",
    "/inboxes/{id}/messages",
    "/inboxes/{id}/messages/latest",
    "/inboxes/{id}/messages/wait",
    "/messages/{id}",
}

# F-C: the full inventory, including the Group C routes (agent self-register,
# verify, SSE stream) that previously appeared in prose elsewhere on the site
# but were missing from API_ENDPOINTS itself — the one list /docs/api/ and
# /llms.txt both render from and that is claimed to mirror OpenAPI. Paired
# with method so a tightened check can assert the exact heading, not just a
# loose path substring (which can't tell GET /x from POST /x apart).
ALL_ENDPOINTS_EXPECTED = {
    ("POST", "/agents/register"),
    ("POST", "/verify"),
    ("POST", "/verify/batch"),
    ("GET", "/inboxes"),
    ("POST", "/inboxes"),
    ("GET", "/inboxes/{id}"),
    ("DELETE", "/inboxes/{id}"),
    ("GET", "/inboxes/{id}/messages"),
    ("GET", "/inboxes/{id}/messages/latest"),
    ("GET", "/inboxes/{id}/messages/wait"),
    ("GET", "/inboxes/{id}/stream"),
    ("GET", "/messages/{id}"),
    ("GET", "/public/emails/{email}/messages"),
}


def _get(url):
    return Client().get(url)


def _assert_no_inline_script(content):
    """CSP guard: public pages may load only EXTERNAL scripts (<script src=...>),
    never inline JS. Inline <script> or <script> without a src= is forbidden
    (would need 'unsafe-inline' and is an XSS surface)."""
    import re
    for tag in re.findall(r"<script\b[^>]*>", content):
        assert "src=" in tag, f"inline <script> not allowed on public page: {tag}"


def test_api_reference_is_public_html():
    response = _get(reverse("dashboard:api-reference"))
    assert response.status_code == 200
    assert response.status_code != 302
    assert response.headers["Content-Type"].startswith("text/html")
    content = response.content.decode()
    _assert_no_inline_script(content)
    assert "|safe" not in content


def test_api_reference_names_all_six_endpoints_and_wait_params():
    content = _get(reverse("dashboard:api-reference")).content.decode()
    for path in EXPECTED_PATHS:
        assert path in content
    # wait params, with types + defaults + constraints
    for fragment in ("timeout", "since", "require", "min_confidence", "otp | link | any", "[1, 25]"):
        assert fragment in content
    # auth + pagination + rate-limit explainers
    assert "Bearer" in content
    assert "ms_live_" in content
    assert "next_cursor" in content
    assert "has_more" in content
    assert "X-RateLimit-Limit" in content
    assert "X-RateLimit-Remaining" in content
    assert "X-RateLimit-Reset" in content
    assert "Retry-After" in content


# F-C: the endpoint INVENTORY (apps.dashboard.views.API_ENDPOINTS), not just
# scattered prose, must include every Group C route. Assert the exact
# method+path heading api_reference.html renders, not a loose substring —
# a substring check on "/verify" alone can't distinguish a real inventory
# entry from an incidental mention (e.g. inside a curl example) and is what
# let the three routes go missing from API_ENDPOINTS while still "mentioned"
# on the page.
def test_api_reference_inventory_has_exact_method_and_path_for_every_endpoint():
    content = _get(reverse("dashboard:api-reference")).content.decode()
    for method, path in ALL_ENDPOINTS_EXPECTED:
        heading = f'method-{method.lower()}">{method}</span> <code>/api/v1{path}</code>'
        assert heading in content, f"api reference missing exact heading for {method} {path}"


def test_error_reference_is_public_and_lists_every_code():
    response = _get(reverse("dashboard:error-reference"))
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/html")
    content = response.content.decode()
    # Trust pass A (#7): every code a SHIPPED endpoint can emit; the
    # unreleased-outbound codes are deliberately left out of public docs.
    from apps.dashboard.views import UNRELEASED_ERROR_CODES
    for code in set(MESSAGES) - UNRELEASED_ERROR_CODES:
        assert code in content
    # the canonical error envelope body shape is rendered (JSON, auto-escaped)
    assert "&quot;code&quot;" in content
    assert "&quot;message&quot;" in content
    assert "&quot;fields&quot;" in content
    # the two distinct 429 wait subcodes
    assert "too_many_wait_requests" in content
    assert "wait_capacity" in content
    _assert_no_inline_script(content)


OPENAPI_EXPECTED_PATHS = EXPECTED_PATHS | {
    "/agents/register",
    "/verify",
    "/verify/batch",
    "/inboxes/{id}/stream",
    "/inboxes/{id}/webhook/test",
    "/inboxes/{id}/test-code",
    "/messages/batch",
    "/domains",
    "/domains/{id}",
    "/domains/{id}/verify",
    "/domains/{id}/catch_all",
    "/public/emails/{email}/messages",
}


def test_openapi_yaml_route_serves_valid_yaml():
    response = _get(reverse("dashboard:openapi-yaml"))
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/yaml")
    spec = yaml.safe_load(response.content)
    assert spec is not None
    assert spec["openapi"].startswith("3.1")
    assert set(spec["paths"].keys()) == OPENAPI_EXPECTED_PATHS
    # it really is the shipped contract: bearer scheme + error enum present
    assert "bearerAuth" in spec["components"]["securitySchemes"]
    codes = set(spec["components"]["schemas"]["Error"]["properties"]["error"]["properties"]["code"]["enum"])
    from apps.dashboard.views import UNRELEASED_ERROR_CODES
    assert set(MESSAGES) - UNRELEASED_ERROR_CODES <= codes


def test_pricing_is_public_html():
    response = _get(reverse("dashboard:pricing"))
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/html")
    _assert_no_inline_script(response.content.decode())


def _pricing_tiers(content):
    """Split the rendered pricing table into per-tier HTML blocks keyed by
    tier key (data-tier="..."), so assertions can be scoped to a single
    tier's cells instead of searching the whole page (which can't tell a
    value/badge on the wrong tier from one on the right tier)."""
    import re

    blocks = {}
    for key in ("free", "paid", "scale", "business", "enterprise"):
        cells = re.findall(rf'data-tier="{key}"[^>]*>(.*?)</t[hd]>', content, re.DOTALL)
        assert cells, f"pricing table missing cells for tier {key}"
        blocks[key] = "\n".join(cells)
    return blocks


def test_pricing_renders_real_settings_numbers(settings):
    # BL-18: billing-lifecycle scenario (skill billing-lifecycle)
    # R-B: unique sentinel values per settings-backed field so a value from
    # the wrong tier/field leaking into the page (or a stale default
    # matching by coincidence) would be caught, and each is asserted WITHIN
    # its own tier's table cells, not just "somewhere on the page".
    settings.BILLING_PLAN_LIMITS = {
        "free": {"inboxes": 3, "messages_per_month": 4321, "webhooks": False},
        "paid": {"inboxes": 77, "messages_per_month": 98765, "webhooks": True},
        "scale": {
            "inboxes": 55,
            "messages_per_month": 55555,
            "webhooks": True,
            "wait_concurrency": 46,
        },
        # QA #8: Business figures are read from settings too (not literals).
        "business": {
            "inboxes": 222,
            "messages_per_month": 333333,
            "webhooks": True,
            "wait_concurrency": 47,
        },
    }
    settings.RETENTION_DAYS = {"free": 13, "paid": 29, "scale": 61, "business": 93}
    content = _get(reverse("dashboard:pricing")).content.decode()
    tiers = _pricing_tiers(content)

    free_cells = tiers["free"]
    assert "3 inbox" in free_cells
    assert "4,321 / month" in free_cells
    assert "13 day" in free_cells
    assert "Not included" in free_cells

    pro_cells = tiers["paid"]
    assert "77 inboxes" in pro_cells
    assert "98,765 / month" in pro_cells
    assert "29 days" in pro_cells
    assert "Included" in pro_cells
    
    # "Not included" cell, never on webhooks.
    assert pro_cells.count("Not included") == 1

    # T12: Scale row must also come from settings, not a hard-coded literal —
    # the exact regression this drift-guard exists to catch (spec T12).
    
    # values, not the production defaults (8, 30) — proving they're read
    # from settings rather than hard-coded to match the defaults by
    # coincidence.
    scale_cells = tiers["scale"]
    assert "55 inboxes" in scale_cells
    assert "55,555 / month" in scale_cells
    assert "61 days" in scale_cells
    assert "46" in scale_cells

    business_cells = tiers["business"]
    assert "222 inboxes" in business_cells
    assert "333,333 / month" in business_cells
    assert "93 days" in business_cells
    assert "47" in business_cells

    # Sentinels must not leak across tiers (would catch a swapped free/pro
    # context value).
    assert "4,321" not in pro_cells
    assert "98,765" not in free_cells
    assert "3 inbox" not in pro_cells
    assert "77 inboxes" not in free_cells
    assert "55,555" not in pro_cells and "55,555" not in free_cells

    # no checkout/payment
    assert "Stripe" not in content
    assert "checkout" not in content.lower()


def test_pricing_tracks_an_override(settings):
    # Deepcopy override: mutating the shared dict in place leaked 250 into
    # later tests (test_plan_changes free defaults) in some orders.
    limits = copy.deepcopy(settings.BILLING_PLAN_LIMITS)
    limits["free"]["messages_per_month"] = 250
    settings.BILLING_PLAN_LIMITS = limits
    content = _get(reverse("dashboard:pricing")).content.decode()
    assert "250" in content


def test_pricing_renders_all_five_tiers_with_correct_availability():
    # R-C: scoped per data-tier column so a swapped price/badge/CTA
    # between tiers fails instead of passing on a global count/substring.
    content = _get(reverse("dashboard:pricing")).content.decode()
    tiers = _pricing_tiers(content)
    assert set(tiers) == {"free", "paid", "scale", "business", "enterprise"}

    # Trust pass A (#24): available tiers render no badge (the CTA says it);
    # only unavailable tiers keep "Coming soon" / "Contact sales".
    expected = {
        "free": ("$0", None, "Start free"),
        "paid": ("$12/mo", None, "Start free"),
        "scale": ("$49/mo", "Coming soon", "Contact us"),
        "business": ("$149/mo", "Coming soon", "Contact us"),
        "enterprise": ("Custom", "Contact sales", "Contact sales"),
    }
    for key, (price, badge, cta) in expected.items():
        cells = tiers[key]
        assert price in cells, f"{key} missing price {price}"
        if badge:
            assert badge in cells, f"{key} missing badge {badge}"
        assert cta in cells, f"{key} missing CTA {cta}"
        # Exactly one availability badge per tier's header cell — the other
        # badge string must NOT also be present (guards against a
        # stray/duplicate badge).
        for other_badge in {"Available now", "Coming soon", "Contact sales"} - {badge}:
            assert other_badge not in cells, f"{key} unexpectedly has {other_badge}"

    # Overall totals still hold (belt-and-braces on top of the per-tier check).
    assert content.count("Available now") == 0
    assert content.count("Coming soon") == 2
    assert content.count("Contact us") == 2
    # Enterprise: no fixed price, no SSO promise.
    assert "$399" not in content
    assert "SSO" not in content


@override_settings(ENABLE_BILLING=True)
def test_pricing_billing_live_note_present_when_billing_enabled():
    "Public API."
    content = _get(reverse("dashboard:pricing")).content.decode()
    assert "Billing is live" in content
    assert "Billing is not yet live for self-serve purchase" not in content


@override_settings(ENABLE_BILLING=False)
def test_pricing_billing_live_note_absent_when_billing_disabled():
    "Public API."
    content = _get(reverse("dashboard:pricing")).content.decode()
    assert "Billing is live" not in content
    assert "Billing is not yet live for self-serve purchase" in content


def test_pricing_does_not_falsely_claim_message_count_is_enforced():
    """P-B honesty fix: only inbox count and byte size are hard-enforced.
    The monthly MESSAGE count is metered/monitored, not hard-blocked, so the
    page must never claim the message allowance itself is 'enforced'."""
    content = _get(reverse("dashboard:pricing")).content.decode()
    assert "monthly message allowance is metered, not blocked" in content
    assert "the message limit is enforced by the API" not in content.lower()
    assert "messages_per_month\":" not in content


def test_pricing_verify_row_matches_real_enforced_limit(settings):
    from apps.accounts.services import VERIFY_ACCOUNT_RATE_LIMIT

    content = _get(reverse("dashboard:pricing")).content.decode()
    assert f"{VERIFY_ACCOUNT_RATE_LIMIT:,} / hour" in content


def test_guides_are_public_and_contain_the_wait_flow():
    for slug in ("pytest", "cypress", "puppeteer", "github-actions"):
        response = _get(reverse("dashboard:guide", kwargs={"slug": slug}))
        assert response.status_code == 200
        assert response.headers["Content-Type"].startswith("text/html")
        content = response.content.decode()
        assert "messages/wait" in content
        assert "MAILSOCKET_API_KEY" in content
        _assert_no_inline_script(content)


def test_playwright_guide_is_public_and_uses_the_sdk():
    response = _get(reverse("dashboard:guide", kwargs={"slug": "playwright"}))
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/html")
    content = response.content.decode()
    assert "waitForOtp" in content
    assert "MailsocketClient" in content
    assert "MAILSOCKET_API_KEY" in content
    assert "examples/playwright/otp-signup.spec.ts" in content
    assert "Prefer the raw REST calls?" in content
    assert "github.com/mailsocket/mailsocket-sdk/tree/main/examples/playwright" in content
    assert "calls the REST API directly" not in content
    _assert_no_inline_script(content)


def test_other_guides_cross_link_to_the_playwright_guide():
    playwright_url = reverse("dashboard:guide", kwargs={"slug": "playwright"})
    for slug in ("pytest", "cypress", "puppeteer", "github-actions"):
        response = _get(reverse("dashboard:guide", kwargs={"slug": slug}))
        content = response.content.decode()
        assert playwright_url in content, f"{slug} guide does not link to the Playwright guide"


def test_guides_index_lists_the_playwright_guide():
    content = _get(reverse("dashboard:guides")).content.decode()
    assert reverse("dashboard:guide", kwargs={"slug": "playwright"}) in content
    assert "Playwright" in content


def test_guide_unknown_slug_is_404():
    response = _get(reverse("dashboard:guide", kwargs={"slug": "does-not-exist"}))
    assert response.status_code == 404


def test_faq_and_changelog_are_public():
    for name in ("dashboard:faq", "dashboard:changelog"):
        response = _get(reverse(name))
        assert response.status_code == 200
        assert response.headers["Content-Type"].startswith("text/html")


def test_faq_contains_retention_and_magic_link_truths(settings):
    content = _get(reverse("dashboard:faq")).content.decode()
    assert str(settings.RETENTION_DAYS["free"]) in content
    assert str(settings.RETENTION_DAYS["paid"]) in content
    assert "extract" in content
    assert "never click" in content or "never follow" in content
    assert "no" in content.lower()  # SMS / attachments answers exist


def test_none_of_the_new_pages_require_auth():
    for name in (
        "dashboard:api-reference",
        "dashboard:error-reference",
        "dashboard:pricing",
        "dashboard:faq",
        "dashboard:changelog",
    ):
        response = _get(reverse(name))
        assert response.status_code == 200
        assert response.status_code != 302


def test_root_is_host_aware(settings, db):
    """Root '/' behaves differently on the dashboard host vs marketing host.

    Regression for the "open dashboard" bug: dash.* root re-rendered the
    marketing landing for anonymous visitors instead of sending them to login.
    """
    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from apps.accounts.models import Account, AccountPlan

    settings.ALLOWED_HOSTS = list(settings.ALLOWED_HOSTS) + ["dash.testserver"]
    settings.DASHBOARD_HOST = "dash.testserver"

    c = Client()
    # marketing host, anonymous -> landing (200)
    assert c.get("/", HTTP_HOST="testserver").status_code == 200
    # dashboard host, anonymous -> redirect to login
    r = c.get("/", HTTP_HOST="dash.testserver")
    assert r.status_code == 302 and "/login/" in r["Location"]

    # logged in -> always the app overview, on either host
    U = get_user_model()
    u = U.objects.create_user(username="hostaware", email="hostaware@example.test", password="password")
    Account.objects.create(user=u, email_normalized="hostaware@example.test", email_verified_at=timezone.now())
    AccountPlan.objects.create(user=u, plan=AccountPlan.Plan.FREE)
    c2 = Client()
    c2.force_login(u)
    for host in ("testserver", "dash.testserver"):
        r = c2.get("/", HTTP_HOST=host)
        assert r.status_code == 302 and "/app/" in r["Location"]


def test_docs_pages_load_copy_button_script_externally():
    """Docs/reference pages ship the copy-button JS as an EXTERNAL (CSP-safe)
    script so code blocks get copy buttons without inline JS."""
    for name in ("api-reference", "error-reference", "docs", "pricing"):
        response = _get(reverse(f"dashboard:{name}"))
        assert response.status_code == 200
        content = response.content.decode()
        assert 'src="' in content and "dashboard/app.min.js" in content, name
        _assert_no_inline_script(content)


def test_custom_error_pages_are_branded():
    """404/403/500 render the branded terminal-theme templates, not Django's
    default gray pages. 500 must be fully self-contained (no external CSS)."""
    from django.template.loader import render_to_string
    html404 = render_to_string("404.html")
    assert "Page not found" in html404 and "error-page" in html404
    assert "dashboard/app.min.css" in html404  
    html403 = render_to_string("403.html")
    assert "Access forbidden" in html403 and "/login/" in html403
    html500 = render_to_string("500.html")
    assert "Something went wrong" in html500
    # 500 is self-contained: inline <style>, no external stylesheet dependency.
    assert "<style>" in html500 and "app.css" not in html500


def test_llms_txt_is_public_plaintext_and_lists_endpoints():
    """/llms.txt is the AI-agent front door: public, text/plain, and generated
    from the SAME endpoint list as /docs/api/ so it can't drift."""
    response = _get(reverse("dashboard:llms-txt"))
    assert response.status_code == 200
    assert response.status_code != 302
    assert response["Content-Type"].startswith("text/plain")
    body = response.content.decode()
    # Must carry the auth contract + the base URL + every documented path.
    assert "Authorization: Bearer" in body
    assert "/api/v1" in body
    for path in EXPECTED_PATHS:
        assert path in body, f"llms.txt missing endpoint path {path}"
    # SDK install lines so an agent can self-serve.
    assert "pip install mailsocket" in body
    assert "npm i mailsocket-sdk" in body
    # F-D (sprint C): the constructor line must match the real TS API
    # (positional apiKey, not an options-object) — regression guard for doc bug (b).
    assert "MailsocketClient({ apiKey })" not in body
    # Remote MCP (no install): hosted URL + OAuth-not-yet sentence for an
    # agent reading llms.txt to self-serve without guessing.
    assert "https://mcp.mailsocket.app/mcp" in body
    assert "Claude.ai" in body and "Smithery" in body
    assert "not available yet" in body


def test_docs_page_has_sdks_section():
    """/docs/ carries a #sdks section with install lines for all 3 packages,
    with no inline script (CSP guard). Install commands live in their own
    code block, separate from runnable source, and each language sample is
    copy-pasteable end to end (creates an inbox before waiting on it, no
    undefined placeholder variables like a bare `inbox_id`/`inboxId`)."""
    response = _get(reverse("dashboard:docs"))
    assert response.status_code == 200
    content = response.content.decode()
    assert 'id="sdks"' in content
    assert "pip install mailsocket" in content
    assert "npm i mailsocket-sdk" in content
    assert "mailsocket-mcp" in content
    _assert_no_inline_script(content)
    # Real constructors/calls, not placeholder pseudo-code.
    assert "mailsocket <span class=\"code-command\">import</span> Client" in content
    assert "client.create_inbox()" in content
    # ux4 R3-3: the SDK block passes a start cutoff taken before the trigger.
    assert 'client.wait_for_otp(inbox[<span class="code-string">"id"</span>], since=started).otp' in content
    assert 'new</span> MailsocketClient(<span class="code-string">"ms_live_...' in content
    assert 'createInbox();' in content and 'client = <span class="code-command">new</span> MailsocketClient' in content
    assert 'client.waitForOtp(inbox.id, { since: started });' in content
    # Regression guard: undefined placeholders must not appear anywhere in
    # the #sdks section's runnable code (they broke copy-paste, see gate
    
    assert "wait_for_otp(inbox_id)" not in content
    assert "waitForOtp(inboxId)" not in content


def test_landing_page_mentions_mcp():
    """The landing page points visitors at the MCP server alongside the SDKs."""
    response = _get(reverse("dashboard:root"))
    assert response.status_code == 200
    assert "mailsocket-mcp" in response.content.decode()


def test_docs_page_has_remote_mcp_block():
    """/docs/ #sdks carries the remote (no-install) MCP block: the hosted URL,
    the bearer-header auth note, client snippets for Claude Code/Cursor/VS Code,
    and the OAuth-not-yet-available sentence for Claude.ai/Desktop connectors/
    Smithery — CSP-safe (no inline script)."""
    response = _get(reverse("dashboard:docs"))
    assert response.status_code == 200
    content = response.content.decode()
    assert "https://mcp.mailsocket.app/mcp" in content
    assert "Authorization: Bearer" in content
    assert "claude mcp add --transport http mailsocket" in content
    assert '<span class="code-key">"url"</span>: <span class="code-string">"https://mcp.mailsocket.app/mcp"</span>' in content
    assert "Claude.ai" in content and "Smithery" in content and "OAuth" in content
    assert "not available yet" in content
    _assert_no_inline_script(content)


# --- B3/B4: /llms.txt must list the Group C endpoints and use the real -------
# --- {"data": ...} response envelope (not the stale {"message": ...} one). ---


def test_llms_txt_lists_group_c_endpoints_and_register_first_flow():
    """B3: /llms.txt must mention all 4 Group C endpoints and make agent
    self-registration the first/step-zero onboarding path, so an agent reading
    the doc doesn't conclude email verification is mandatory.

    F-C: tightened from loose substring checks to the exact `### METHOD path`
    heading llms_txt() renders per apps.dashboard.views.API_ENDPOINTS entry,
    so a route present only in prose (not the inventory) still fails here."""
    body = _get(reverse("dashboard:llms-txt")).content.decode()
    assert "### POST /api/v1/agents/register" in body
    assert "### POST /api/v1/verify" in body
    assert "### GET /api/v1/inboxes/{id}/stream" in body
    assert "routing_key" in body
    # register-first: the zero-auth path must appear before the first
    # Authorization-required call in the quick-start flow.
    register_pos = body.index("agents/register")
    first_bearer_pos = body.index("Bearer $MAILS")
    assert register_pos < first_bearer_pos


def test_llms_txt_response_envelope_matches_the_real_api():
    """B4: /llms.txt previously showed the wait response as
    {"message": {"otp": ...}} but the real API (and the docs) return
    {"data": {...}}. Code generated from the stale example broke on first call."""
    body = _get(reverse("dashboard:llms-txt")).content.decode()
    assert '"data":' in body
    assert '"message": {' not in body


# --- D4 (): billing refund copy -----------------


def test_terms_has_refund_section():
    body = _get(reverse("dashboard:legal-terms")).content.decode()
    assert "Payments, cancellation and refunds" in body
    assert "14 days" in body
    assert "Lemon Squeezy" in body
    assert "mailto:sales@mailsocket.app" in body


def test_billing_refund_copy_matches_terms(settings, db):
    "Public API."
    settings.ENABLE_BILLING = True
    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from apps.accounts.models import Account, AccountPlan

    user = get_user_model().objects.create_user(
        username="d4_refund_copy", email="d4_refund_copy@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized="d4_refund_copy@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user)
    client = Client()
    client.force_login(user)
    billing_body = client.get(reverse("dashboard:billing-page")).content.decode()
    terms_body = client.get(reverse("dashboard:legal-terms")).content.decode()

    for body in (billing_body, terms_body):
        assert "mailto:sales@mailsocket.app" in body
        assert "within 14 days of your first payment" in body
