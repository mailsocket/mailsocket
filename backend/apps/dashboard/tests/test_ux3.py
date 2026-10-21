"Public API."

import math
import re
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan
from apps.dashboard import views as dashboard_views
from apps.inboxes.models import Inbox

pytestmark = pytest.mark.django_db

STATIC = Path(settings.BASE_DIR) / "apps/dashboard/static/dashboard"
CSS = STATIC / "app.css"


def make_user(name, plan=AccountPlan.Plan.FREE):
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


def _media_block(css, query):
    """Concatenated bodies of every ``@media(<query>){...}`` block."""
    out = []
    for match in re.finditer(r"@media\s*\(" + re.escape(query) + r"\)\s*\{", css):
        depth, i = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        out.append(css[match.end():i - 1])
    return "\n".join(out)


# --- N8: test-code 429 says when to retry --------------------------------------


def test_dashboard_test_code_429_says_minutes_from_bucket_expiry():
    user = make_user("ux3-n8")
    inbox = Inbox.objects.create(user=user, routing_key="ux3_n8_box")
    client = _client(user)
    url = reverse("dashboard:inbox-test-code", args=[inbox.public_id])
    for _ in range(dashboard_views.TEST_CODE_INBOX_RATE_LIMIT):
        assert client.post(url, HTTP_HX_REQUEST="true").status_code == 200
    refused = client.post(url, HTTP_HX_REQUEST="true")
    assert refused.status_code == 429
    page = refused.content.decode()
    minutes = math.ceil(dashboard_views.TEST_CODE_RATE_WINDOW / 60)
    # Window just started: 60 min (59 if a second ticked over).
    assert re.search(r"You can send another test code in (59|60) min\.", page), minutes
    assert "Please wait and try again" not in page


@pytest.mark.parametrize(
    ("seconds", "minutes"), [(1, 1), (59, 1), (60, 1), (61, 2), (3599, 60), (3600, 60), (-5, 1)]
)
def test_retry_minutes_rounds_up_and_is_at_least_one(seconds, minutes):
    now = timezone.now()
    reset_at = now + timedelta(seconds=seconds)
    assert dashboard_views.retry_minutes_until(reset_at, now=now) == minutes


# --- N2: Pro CTA carries next=/app/billing/ ----------------------------------------


@pytest.mark.parametrize("key", ["paid", "scale", "business"])
def test_paid_tier_ctas_carry_billing_next(key, settings):
    settings.ENABLE_BILLING = True
    page = Client().get(reverse("dashboard:pricing")).content.decode()
    head = re.search(rf'<th[^>]*data-tier="{key}"[^>]*>(.*?)</th>', page, re.S).group(1)
    assert f'href="{reverse("accounts:register")}?next=/app/billing/"' in head, key
    assert ">Start free</a>" in head, key


def test_pro_cta_lands_signed_in_user_on_billing(settings):
    settings.ENABLE_BILLING = True
    user = make_user("ux3-n2")
    page = Client().get(reverse("dashboard:pricing")).content.decode()
    head = re.search(r'<th[^>]*data-tier="paid"[^>]*>(.*?)</th>', page, re.S).group(1)
    href = re.search(r'href="[^"]*?(/register/[^"]*)"', head).group(1).replace("&amp;", "&")
    response = _client(user).get(href)
    assert response.status_code == 302 and response.url == "/app/billing/"


# --- N3: exactly one copy control per dashboard snippet ---------------------------


def test_app_js_skips_auto_copy_when_inner_code_has_a_copy_control():
    js = (STATIC / "app.js").read_text()
    assert "function hasCopyControl(pre)" in js
    assert 'pre.querySelectorAll("[id]")' in js
    assert "if (hasCopyControl(pre)) return;" in js
    # The old pre-id-only check (which missed <code id> targets) is gone.
    assert '`[data-copy-target="${pre.id}"]`' not in js


def test_every_dashboard_snippet_has_one_explicit_copy_targeting_its_code():
    """Server half of N3: each snippet <pre><code id=X> has exactly one
    data-copy-target=X, which is what hasCopyControl() keys on."""
    user = make_user("ux3-n3")
    inbox = Inbox.objects.create(user=user, routing_key="ux3_n3_box")
    client = _client(user)
    from apps.accounts.services import create_api_key

    create_api_key(user, "k")
    for name in ("dashboard:overview", "dashboard:inboxes"):
        page = client.get(reverse(name)).content.decode()
        ids = re.findall(r'<pre tabindex="0" role="region" aria-label="Code example"><code id="([^"]+)">', page)
        assert ids, name
        for code_id in ids:
            assert page.count(f'data-copy-target="{code_id}"') == 1, (name, code_id)
    assert inbox


def test_public_docs_blocks_keep_their_auto_copy_button():
    """Docs code blocks have no explicit control, so the injected one stays."""
    page = Client().get(reverse("dashboard:docs")).content.decode()
    assert '<pre tabindex="0" role="region" aria-label="Code example"><code>' in page  # no id -> nothing targets it
    assert "data-copy-target" not in page.split('id="sixty-seconds"', 1)[1].split("</section>", 1)[0]


# --- N6: the Python snippet prints a first line ----------------------------------


def test_python_snippet_prints_waiting_line_before_first_call():
    user = make_user("ux3-n6")
    inbox = Inbox.objects.create(user=user, routing_key="ux3_n6_box")
    from apps.accounts.services import create_api_key

    create_api_key(user, "k")
    page = _client(user).get(reverse("dashboard:overview")).content.decode()
    import html

    code = html.unescape(re.search(r'<code id="snippet-python">(.*?)</code>', page, re.S).group(1))
    line = f'print("Waiting up to 60 s for a code to {inbox.address}… (click \\"Send a test code\\" now)")'
    assert line in code
    assert code.index(line) < code.index("requests.get(")


# --- N7: compact mobile nav -------------------------------------------------------


def test_mobile_nav_is_a_details_menu_with_current_page_and_report_near_sign_out():
    user = make_user("ux3-n7")
    page = _client(user).get(reverse("dashboard:inboxes")).content.decode()
    sidebar = page.split('<aside class="sidebar">', 1)[1].split("</aside>", 1)[0]
    # Desktop nav unchanged: every item incl. Report a problem.
    desktop = sidebar.split('<nav class="app-nav-desktop"', 1)[1].split("</nav>", 1)[0]
    assert 'aria-current="page">Inboxes</a>' in desktop
    assert ">Report a problem</a>" in desktop
    # Phone menu: <details>, no JS, current page named in the summary.
    mobile = sidebar.split('<details class="app-nav-mobile">', 1)[1].split("</details>", 1)[0]
    summary = mobile.split("<summary>", 1)[1].split("</summary>", 1)[0]
    assert '<span class="app-nav-menu">Menu</span>' in summary
    assert '<span class="app-nav-current">Inboxes</span>' in summary
    assert 'aria-current="page">Inboxes</a>' in mobile
    assert "Report a problem" not in mobile
    # Report a problem sits right before Sign out.
    tail = sidebar.split("</details>", 1)[1]
    assert re.search(
        r'<a class="app-nav-report" href="/app/feedback/">Report a problem</a>\s*\{?#?.*?<form method="post" action="/logout/">',
        tail,
        re.S,
    ) or tail.index("app-nav-report") < tail.index('action="/logout/"')
    assert "<script" not in sidebar and "style=" not in sidebar


@pytest.mark.parametrize(
    ("url_name", "label"),
    [("dashboard:overview", "Overview"), ("dashboard:api-keys", "API Keys"), ("dashboard:usage", "Usage"), ("dashboard:settings", "Settings")],
)
def test_mobile_menu_summary_names_the_current_page(url_name, label):
    page = _client(make_user(f"ux3-n7-{label.replace(' ', '').lower()}")).get(reverse(url_name)).content.decode()
    assert f'<span class="app-nav-current">{label}</span>' in page


def test_mobile_nav_css_desktop_unchanged():
    css = CSS.read_text()
    phone = _media_block(css, "max-width:640px")
    assert ".sidebar>.app-nav-desktop{display:none}" in phone
    assert ".sidebar>.app-nav-mobile{display:block" in phone
    assert ".sidebar>.app-nav-report{display:inline-flex" in phone
    # Outside the phone query both extras are hidden: desktop/tablet unchanged.
    assert ".app-nav-mobile,.app-nav-report{display:none}" in css
    tablet = _media_block(css, "max-width:800px")
    assert "app-nav-desktop" not in tablet


def test_feedback_page_menu_names_report_a_problem():
    page = _client(make_user("ux3-n7-fb")).get("/app/feedback/").content.decode()
    assert '<span class="app-nav-current">Report a problem</span>' in page


# --- N9: slash -> no-slash 301s -----------------------------------------------------


SLASH_REDIRECTS = [
    ("/status/", "/status"),
    ("/status.json/", "/status.json"),
    ("/security/", "/security"),
    ("/docs/faq/", "/docs/faq"),
    ("/docs/changelog/", "/docs/changelog"),
    ("/docs/versioning/", "/docs/versioning"),
    ("/legal/terms/", "/legal/terms"),
    ("/legal/privacy/", "/legal/privacy"),
    ("/legal/aup/", "/legal/aup"),
    ("/docs/compare/mailpit/", "/docs/compare/mailpit"),
    ("/docs/compare/mailosaur/", "/docs/compare/mailosaur"),
    ("/docs/compare/agentmail/", "/docs/compare/agentmail"),
    ("/docs/compare/mailtrap/", "/docs/compare/mailtrap"),
    ("/docs/compare/mailslurp/", "/docs/compare/mailslurp"),
]


@pytest.mark.parametrize(("slashed", "canonical"), SLASH_REDIRECTS)
def test_slashed_public_url_301s_to_canonical_and_keeps_query(slashed, canonical):
    client = Client()
    response = client.get(slashed + "?utm=x")
    assert response.status_code == 301
    assert response["Location"] == canonical + "?utm=x"
    assert client.get(canonical).status_code == 200


def test_every_public_no_slash_page_has_a_slash_redirect():
    """Guard: a new public no-slash page must get its slashed 301 too."""
    from django.urls import URLResolver, get_resolver

    def walk(resolver, prefix=""):
        for p in resolver.url_patterns:
            if isinstance(p, URLResolver):
                yield from walk(p, prefix + str(p.pattern))
            else:
                yield prefix + str(p.pattern)

    routes = set(walk(get_resolver()))
    machine = {".well-known/security.txt", "docs/openapi.yaml", "docs/try/inbox", "llms.txt", "robots.txt", "sitemap.xml"}
    skip_prefixes = ("api/", "internal", "admin", "billing", "healthz", "instant", "team/accept")
    pages = {
        r for r in routes
        if r and not r.endswith("/") and "<" not in r and r not in machine and not r.startswith(skip_prefixes)
    }
    assert {"status", "security", "docs/faq"} <= pages
    assert {r for r in pages if r + "/" not in routes} == set()


SLASH_REDIRECT_IDS = [slashed for slashed, _ in SLASH_REDIRECTS]


@pytest.mark.parametrize("slashed", SLASH_REDIRECT_IDS)
@pytest.mark.parametrize("method", ["post", "put", "patch", "delete", "options"])
def test_slashed_redirect_refuses_non_get_methods(slashed, method):
    """r2 P2-1: only GET/HEAD redirect; a POST used to be 301'd (and replayed
    by browsers as a GET)."""
    response = getattr(Client(), method)(slashed + "?a=1")
    assert response.status_code == 405
    assert "Location" not in response
    assert set(response["Allow"].replace(" ", "").split(",")) == {"GET", "HEAD"}


@pytest.mark.parametrize("slashed", SLASH_REDIRECT_IDS)
def test_slashed_redirect_head_still_301s(slashed):
    response = Client().head(slashed + "?a=1")
    assert response.status_code == 301
    assert response["Location"] == slashed.rstrip("/") + "?a=1"


def test_every_redirect_route_is_get_head_only():
    """Guard: no plain RedirectView (which answers every method) is routed
    in our public URLconfs (Django admin's own redirects are out of scope)."""
    from django.views.generic import RedirectView

    from apps.dashboard import urls as dashboard_urls
    from apps.dashboard.redirects import SlashRedirectView
    from apps.status import urls as status_urls

    redirects = [
        p for p in dashboard_urls.urlpatterns + status_urls.urlpatterns
        if isinstance(getattr(p.callback, "view_class", None), type)
        and issubclass(p.callback.view_class, RedirectView)
    ]
    assert len(redirects) >= len(SLASH_REDIRECTS)
    for p in redirects:
        assert issubclass(p.callback.view_class, SlashRedirectView), str(p.pattern)
        assert set(p.callback.view_class.http_method_names) == {"get", "head"}





def test_tap_target_rules_use_padding_and_min_height():
    css = CSS.read_text()
    ux3 = css.split("/* --- ux3 2026-10-08", 1)[1]
    assert re.search(r"\.auth-links a\{[^}]*min-height:24px[^}]*padding", ux3)
    assert re.search(r"\.landing-home \.landing-works-links a,\.landing-works-source\{[^}]*min-height:24px", ux3)
    assert re.search(r"\.content label\.checkbox\{[^}]*min-height:24px[^}]*padding:4px 6px", ux3)
    assert re.search(r"\.back\{[^}]*min-height:24px", ux3)
    phone = _media_block(ux3, "max-width:640px")
    assert re.search(r"\.auth-links a,\.back\{min-height:44px\}", phone)
    
    for rule in re.findall(r"(\.auth-links a\{[^}]*\}|\.back\{[^}]*\}|\.content label\.checkbox\{[^}]*\})", ux3):
        assert "font-size" not in rule, rule


def test_webhook_toggle_and_message_back_link_use_the_padded_classes():
    user = make_user("ux3-f18", AccountPlan.Plan.PAID)
    inbox = Inbox.objects.create(user=user, routing_key="ux3_f18_box")
    from apps.webhooks.services import create_webhook

    create_webhook(inbox, "https://receiver.example/hook", enabled=True, resolver=lambda _h: ["93.184.216.34"])
    page = _client(user).get(reverse("dashboard:webhooks")).content.decode()
    assert '<label class="checkbox"><input type="checkbox" name="is_enabled"' in page
    from apps.dashboard.demo import deliver_sample_code

    message = deliver_sample_code(inbox).message
    detail = _client(user).get(reverse("dashboard:message-detail", args=[message.public_id])).content.decode()
    assert '<a class="back" href="/app/messages/">← Messages</a>' in detail







# stays: no old second tagline anywhere on them.
@pytest.mark.parametrize("url_name", ["accounts:login", "accounts:register", "accounts:password-reset", "accounts:resend-verification"])
def test_auth_pages_use_landing_tagline(url_name):
    page = Client().get(reverse(url_name)).content.decode()
    assert "Programmable email inbox" not in page
    assert 'class="brand-mark"' in page


def test_auth_submit_labels():
    login = Client().get(reverse("accounts:login")).content.decode()
    assert '<button type="submit" class="button auth-submit">Sign in</button>' in login
    assert ">New here? Start free</a>" in login and "Create an account" not in login
    register = Client().get(reverse("accounts:register")).content.decode()
    assert '<button type="submit" class="button auth-submit">Start free</button>' in register
    reset = Client().get(reverse("accounts:password-reset")).content.decode()
    assert '<button type="submit" class="button auth-submit">Send reset link</button>' in reset
    for page in (login, register, reset):
        assert ">Continue</button>" not in page


@pytest.mark.parametrize(
    "url_name", ["dashboard:docs", "dashboard:compare-mailpit", "dashboard:root", "dashboard:pricing"]
)
def test_public_signup_links_say_start_free(url_name):
    page = Client().get(reverse(url_name)).content.decode()
    signup_texts = re.findall(r'<a[^>]*href="[^"]*/register/[^"]*"[^>]*>([^<]+)</a>', page)
    assert signup_texts, url_name
    assert set(signup_texts) == {"Start free"}, (url_name, signup_texts)





def test_try_provider_row_hidden_by_default_and_js_toggles_it():
    page = Client().get(reverse("dashboard:try-it")).content.decode()
    assert '<dt id="try-provider-label" hidden>Provider</dt><dd id="try-provider" hidden>' in page
    js = (STATIC / "try.js").read_text()
    assert "providerEl.hidden = !value;" in js
    assert "providerLabel.hidden = !value;" in js
    assert '|| "—"' not in js.split("setProvider(message.provider_hint)", 1)[0].rsplit("\n", 3)[-1]
    assert 'providerEl.textContent = message.provider_hint || "—"' not in js
    assert ".try-result [hidden]{display:none}" in CSS.read_text()
