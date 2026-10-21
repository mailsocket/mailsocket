"Public API."

import json
import re

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from apps.dashboard.views import _pricing_context


def _get(url_name):
    return Client().get(reverse(url_name))


def _assert_no_inline_script_or_style(html):
    lower = html.lower()
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", lower, re.S)
    tags = re.findall(r"<script\b[^>]*>", lower)
    assert all(body.strip() == "" for body in scripts), tags
    assert all(" src=" in tag for tag in tags), tags
    assert "<style" not in lower
    assert not re.search(r"\sstyle=", lower)
    assert not re.search(r"\son[a-z]+=", lower)  # no inline event handlers


# --- privacy -------------------------------------------------------------------


def test_privacy_routes_requests_to_privacy_mailbox():
    html = _get("dashboard:legal-privacy").content.decode()
    assert "mailto:privacy@mailsocket.app" in html
    assert "until a separate" not in html
    assert "this monitored address also handles privacy requests" not in html
    # abuse@ remains, but only as the abuse channel
    main = html.split('<main class="legal-content">', 1)[1].split("</main>", 1)[0]
    assert "abuse reports to" in main


def test_privacy_names_subprocessors_consistent_with_security_page():
    privacy = _get("dashboard:legal-privacy").content.decode()
    security = _get("dashboard:security").content.decode()
    assert 'id="subprocessors"' in privacy
    for name in ("Hetzner Online GmbH", "Cloudflare", "Lemon Squeezy"):
        assert name in privacy, name
        assert name in security, name
    assert "Web Analytics" in privacy
    assert "sets no cookies" in privacy
    assert "merchant of record" in privacy
    assert reverse("dashboard:security") + "#subprocessors" in privacy


def test_privacy_is_honest_about_inbound_smtp_tls():
    privacy = _get("dashboard:legal-privacy").content.decode()
    assert "encryption in transit" in privacy
    assert "without requiring TLS" in privacy


def test_privacy_states_signup_only_attribution_and_no_tracking_cookie():
    privacy = _get("dashboard:legal-privacy").content.decode()
    assert (
        "When you sign up we record which site or campaign link brought you to the sign-up "
        "page (host name and campaign tag only) and store it with your account. "
        "We set no tracking or advertising cookies." in privacy
    )
    assert "first-party cookie" not in privacy
    assert "kept for 30 days" not in privacy
    assert "first page you landed on" not in privacy


# --- terms ---------------------------------------------------------------------


def test_terms_split_support_and_abuse_contacts():
    html = _get("dashboard:legal-terms").content.decode()
    main = html.split('<main class="legal-content">', 1)[1].split("</main>", 1)[0]
    assert "mailto:support@mailsocket.app" in main
    assert "mailto:abuse@inboxpipe.net" in main
    assert "Questions and abuse reports may be sent to" not in main


@pytest.mark.parametrize("url_name", ["dashboard:legal-terms", "dashboard:legal-privacy"])
def test_legal_pages_name_the_operator(url_name):
    html = _get(url_name).content.decode()
    assert "operated by Bytolab, a sole proprietorship registered in Vietnam" in html
    assert "OWNER" not in html


@pytest.mark.parametrize(
    "url_name", ["dashboard:root", "dashboard:legal-terms", "dashboard:legal-privacy"]
)
def test_public_pages_have_no_inline_script_or_style(url_name):
    response = _get(url_name)
    assert response.status_code == 200
    assert "'unsafe-inline'" not in response.headers["Content-Security-Policy"]
    _assert_no_inline_script_or_style(response.content.decode())


# --- landing -------------------------------------------------------------------


def _landing():
    return _get("dashboard:root").content.decode()


def test_landing_primary_cta_is_start_free_to_register():
    html = _landing()
    hero = html.split('class="landing-hero"', 1)[1].split("</section>", 1)[0]
    primary = re.findall(r'<a class="landing-button landing-button-primary" href="([^"]+)">([^<]+)</a>', hero)
    assert primary == [(reverse("accounts:register"), "Start free")]
    assert f'href="{reverse("dashboard:try-it")}">Try it without signup</a>' in hero
    assert hero.index("Start free") < hero.index("Try it without signup")
    assert "open dashboard" not in html


def test_landing_has_three_step_how_it_works():
    html = _landing()
    section = html.split('id="how-it-works"', 1)[1].split("</section>", 1)[0]
    steps = re.findall(r"<h3>([^<]+)</h3>", section)
    assert len(steps) == 3
    assert steps[0] == "Create an inbox"
    assert "wait()" in steps[2]
    assert settings.MAIL_DOMAIN in section
    assert "<img" not in section  # inline SVG only, no images


def test_landing_works_with_row_links_every_client():
    html = _landing()
    section = html.split('id="works-with"', 1)[1].split("</section>", 1)[0]
    for needle in (
        "pip install mailsocket",
        "npm i mailsocket-sdk",
        "pip install mailsocket-mcp",
        "https://mcp.mailsocket.app/mcp",
        "n8n-nodes-mailsocket",
        'href="https://github.com/mailsocket/mailsocket-sdk"',
        'href="https://pypi.org/project/mailsocket/"',
        'href="https://www.npmjs.com/package/mailsocket-sdk"',
        'href="https://pypi.org/project/mailsocket-mcp/"',
        'href="https://www.npmjs.com/package/n8n-nodes-mailsocket"',
        f'href="{reverse("dashboard:api-reference")}"',
        f'href="{reverse("dashboard:openapi-yaml")}"',
    ):
        assert needle in section, needle


def test_landing_scope_box_says_receive_only():
    html = _landing()
    section = html.split('id="scope"', 1)[1].split("</section>", 1)[0]
    assert "What it does not do" in section
    assert "Send email" in section
    assert "SMS" in section


def test_landing_pricing_teaser_comes_from_pricing_context():
    paid = [
        t for t in _pricing_context()["tiers"] if t["key"] == "paid"
    ][0]["price"].split("/", 1)[0]
    html = _landing()
    assert f"Free plan, paid plans from {paid}/month." in html
    assert f'href="{reverse("dashboard:pricing")}"' in html


def test_landing_pricing_teaser_follows_settings(monkeypatch):
    from apps.dashboard import views

    original = views._pricing_context

    def cheaper():
        ctx = original()
        for tier in ctx["tiers"]:
            if tier["key"] == "paid":
                tier["price"] = "$9/mo"
        return ctx

    monkeypatch.setattr(views, "_pricing_context", cheaper)
    assert "paid plans from $9/month" in _landing()


def test_landing_has_no_fake_social_proof():
    text = re.sub(r"<[^>]+>", " ", _landing()).lower()
    for phrase in ("testimonial", "trusted by", "customers", "developers use", "ms latency", "99.9"):
        assert phrase not in text, phrase


# --- landing r2 (de-template pass) -------------------------------------------


def _hero(html):
    return html.split('class="landing-hero"', 1)[1].split("</section>", 1)[0]


def test_landing_r2_hero_has_exactly_two_ctas_and_no_docs_link():
    hero = _hero(_landing())
    assert re.findall(r'class="landing-button[^"]*"[^>]*>([^<]+)</a>', hero) == [
        "Start free", "Try it without signup",
    ]
    assert "Read the docs" not in hero
    assert "No Gmail, no IMAP" not in _landing()
    assert "as an API" not in _landing()


def test_landing_r2_single_closing_cta_and_no_eyebrows():
    html = _landing()
    main = html.split("<main>", 1)[1].split("</main>", 1)[0]
    assert "landing-eyebrow" not in main
    assert main.count('class="landing-cta"') == 1
    cta = main.split('class="landing-cta"', 1)[1].split("</section>", 1)[0]
    # r4 adds the secondary try link to the single closing CTA
    assert re.findall(r'class="landing-button[^"]*"[^>]*>([^<]+)</a>', cta) == ["Start free", "Try it without signup"]
    assert main.count(f'href="{reverse("dashboard:pricing")}"') == 1  # the teaser only


def test_landing_r2_try_link_named_consistently_in_footer():
    html = _landing()
    footer = html.split('<footer class="site-footer">', 1)[1]
    assert f'href="{reverse("dashboard:try-it")}">Try it without signup</a>' in footer
    assert "Try it live" not in html


def test_landing_r2_sdk_sample_uses_real_sdk_shapes():
    html = _landing()
    card = html.split('class="landing-code-card"', 1)[1].split("</pre>", 1)[0]
    code = re.sub(r"<[^>]+>", "", card).replace("&quot;", '"')
    # r4: the signup step is the same Playwright lines as step 2 (no your_app placeholder)
    assert "your_app" not in code and "placeholder" not in code
    assert 'page.fill("#email", inbox["address"])' in code
    # list_inboxes() pages dicts; wait_for_otp() returns WaitResult(.otp, .confidence)
    assert 'client.wait_for_otp(inbox["id"], since=started)' in code
    assert "result.otp" in code and "result.confidence" in code


def test_landing_r2_response_block_uses_real_wait_fields():
    from apps.api.serializers import MessageSerializer

    # MessageSerializer.to_representation renames from_ -> from and drops has_html
    real = set(MessageSerializer().fields) - {"from_", "has_html"} | {"from"}
    hero = _hero(_landing())
    block = hero.split('class="landing-response"', 1)[1].split("</figure>", 1)[0]
    keys = set(re.findall(r'<span class="code-key">"([a-z_]+)"</span>', block)) - {"data"}
    assert {"otp", "otp_confidence", "magic_link", "subject", "from", "received_at"} <= keys
    assert keys <= real, keys - real
    shown = re.search(r"(\d+) of (\d+) fields shown", block)
    assert shown and int(shown.group(1)) == len(keys) and int(shown.group(2)) == len(real)


# --- landing r3 (polish pass) -------------------------------------------------


def _response_panels(html):
    block = _hero(html).split('class="landing-response"', 1)[1].split("</figure>", 1)[0]
    otp = block.split('class="landing-response-panel landing-response-otp"', 1)[1].split("</pre>", 1)[0]
    link = block.split('class="landing-response-panel landing-response-link"', 1)[1].split("</pre>", 1)[0]
    return block, otp, link


def _json_text(panel):
    text = re.sub(r"<[^>]+>", "", panel.split('<pre data-no-copy tabindex="0" role="region" aria-label="Code example"><code>', 1)[1]).replace("&lt;", "<").replace("&gt;", ">")
    return json.loads(text)["data"]


def test_landing_r3_hero_shows_an_otp_email_and_a_magic_link_email():
    """Copy promises "the code or the magic link": one real example of each,
    toggled by pure-CSS radio tabs (no JS)."""
    html = _landing()
    block, otp_panel, link_panel = _response_panels(html)
    otp, link = _json_text(otp_panel), _json_text(link_panel)
    assert otp["otp"] == "482193" and otp["magic_link"] is None
    assert link["magic_link"].startswith("https://") and link["otp"] is None
    assert link["otp_confidence"] == 0.0 and link["otp_context"] is None
    # the request line matches what each panel waits for
    assert "require=otp</code>" in otp_panel.split("<pre", 1)[0]
    assert "require=link</code>" in link_panel.split("<pre", 1)[0]
    # the same abridged field set in both, so "N of M fields shown" holds for each
    assert set(otp) == set(link)
    # radio tabs: OTP checked by default, labels point at the radios
    assert re.search(r'<input class="landing-tab-input" type="radio" name="hero-kind" id="hero-kind-otp" checked>', block)
    assert 'for="hero-kind-otp">OTP email</label>' in block
    assert 'for="hero-kind-link">Magic-link email</label>' in block
    assert "<script" not in block
    assert "the code or the magic link" in _hero(html)


def test_landing_r3_magic_link_example_is_what_the_parser_extracts():
    from apps.ingest.parser import extract_magic_link, extract_otp

    _block, _otp_panel, link_panel = _response_panels(_landing())
    link = _json_text(link_panel)
    body = f"Click to sign in:\r\n{link['magic_link']}\r\n"
    assert extract_magic_link(body, "")[0] == link["magic_link"]
    assert extract_otp(link["subject"], body, "") == (None, 0.0)


def test_landing_r3_otp_confidence_explainer_matches_parser_and_api():
    from apps.ingest.parser import extract_otp

    block, otp_panel, _link = _response_panels(_landing())
    note = block.split('class="landing-confidence"', 1)[1].split("</p>", 1)[0]
    text = re.sub(r"<[^>]+>", "", note).replace("&amp;", "&")
    # r4: one line + a docs link; parser semantics: keyword nearby -> 0.95, bare number -> 0.35
    assert "0.95" in text and "0.35" in text
    assert f'href="{reverse("dashboard:docs")}#min-confidence"' in note and "More in the docs" in text
    assert extract_otp("", "Your code is 482193", "") == ("482193", 0.95)
    assert extract_otp("", "Ref 482193", "") == ("482193", 0.35)
    assert extract_otp("", "no digits here", "") == (None, 0.0)
    assert _json_text(otp_panel)["otp_confidence"] == 0.95
    # the full explanation lives on /docs/ at that anchor, next to the real wait param
    docs = _get("dashboard:docs").content.decode()
    row = docs.split('<tr id="min-confidence">', 1)[1].split("</tr>", 1)[0]
    assert "<code>min_confidence</code>" in row and "40 characters" in row
    assert "<code>0.95</code>" in row and "<code>0.35</code>" in row


def test_landing_r3_free_plan_limits_come_from_pricing_context():
    free = [t for t in _pricing_context()["tiers"] if t["key"] == "free"][0]
    html = _landing()
    cta = html.split('class="landing-cta"', 1)[1].split("</section>", 1)[0]
    limits = cta.split('class="landing-limits"', 1)[1]
    assert "Free plan limits" in limits
    for value in (
        free["inboxes_display"], free["messages_display"], free["retention_display"],
        str(free["concurrent_waits"]), free["webhooks_display"],
    ):
        assert f">{value}</dd>" in limits, value


def test_landing_r3_free_plan_limits_follow_settings(settings):
    limits = {k: dict(v) for k, v in settings.BILLING_PLAN_LIMITS.items()}
    limits["free"].update(inboxes=3, messages_per_month=4321)
    settings.BILLING_PLAN_LIMITS = limits
    html = _landing()
    assert ">3 inboxes</dd>" in html
    assert ">4,321 / month</dd>" in html
    # /pricing/ renders the same numbers
    pricing = _get("dashboard:pricing").content.decode()
    assert "4,321 / month" in pricing


def test_landing_r3_works_with_link_labels_are_specific():
    html = _landing()
    section = html.split('id="works-with"', 1)[1].split("</section>", 1)[0]
    labels = re.findall(r"<a [^>]*>([^<]+)</a>", section)
    assert "Docs" not in labels and "Install guide" not in labels
    for label, href in (
        ("Python docs", reverse("dashboard:docs") + "#sdks"),
        ("npm package", "https://www.npmjs.com/package/mailsocket-sdk"),
        ("MCP setup", reverse("dashboard:docs") + "#mcp"),
        ("n8n node", "https://www.npmjs.com/package/n8n-nodes-mailsocket"),
        ("OpenAPI spec", reverse("dashboard:openapi-yaml")),
    ):
        assert f'href="{href}">{label}</a>' in section, label
    # the MCP setup anchor exists on /docs/
    assert 'id="mcp"' in _get("dashboard:docs").content.decode()


def test_landing_r3_sections_vary_layout():
    html = _landing()
    assert 'class="landing-works-grid"' in html  # full-width card grid
    assert 'class="landing-code-section landing-code-flip"' in html  # mirrored row
    css = (settings.BASE_DIR / "apps/dashboard/static/dashboard/app.css").read_text()
    # r4: the scope heading is left-aligned like every other section
    assert ".landing-home .landing-scope-section>h2{margin-bottom:24px;text-align:left}" in css


def test_landing_r3_closing_cta_headline_is_plain():
    html = _landing()
    cta = html.split('class="landing-cta"', 1)[1].split("</section>", 1)[0]
    assert '<h2 id="cta-title">Try it on your own signup test</h2>' in cta
    assert "Point one of your own" not in html


def test_footer_r3_sign_in_out_of_company_and_no_duplicate_try_link():
    html = _landing()
    footer = html.split('<footer class="site-footer">', 1)[1].split("</footer>", 1)[0]
    company = footer.split('aria-label="Company"', 1)[1].split("</nav>", 1)[0]
    assert "Sign in" not in company
    assert f'href="{reverse("accounts:login")}"' not in footer  # the nav already has it
    assert footer.count("Try it without signup") == 1
    header = html.split('<header class="site-header">', 1)[1].split("</header>", 1)[0]
    assert f'href="{reverse("accounts:login")}"' in header


# --- legal trailing-slash redirects ---------------------------------------------


@pytest.mark.parametrize("url_name", ["dashboard:legal-privacy", "dashboard:legal-terms", "dashboard:legal-aup"])
def test_legal_trailing_slash_redirects_permanently(url_name):
    canonical = reverse(url_name)
    assert not canonical.endswith("/")
    r = Client().get(canonical + "/")
    assert r.status_code == 301
    assert r["Location"] == canonical
    r = Client().get(canonical + "/?ref=x")
    assert r.status_code == 301 and r["Location"] == canonical + "?ref=x"
    assert Client().get(canonical + "/", follow=True).status_code == 200


# --- www -> apex ---------------------------------------------------------------


def test_www_redirects_permanently_to_apex(settings):
    settings.APEX_HOST = "mailsocket.app"
    # www host is deliberately NOT in ALLOWED_HOSTS: the redirect must still fire.
    r = Client().get("/pricing/?ref=hn", HTTP_HOST="www.mailsocket.app")
    assert r.status_code == 301
    assert r["Location"] == "https://mailsocket.app/pricing/?ref=hn"


def test_www_redirect_ignores_other_hosts(settings):
    settings.APEX_HOST = "mailsocket.app"
    assert Client().get("/", HTTP_HOST="testserver").status_code == 200
    # look-alike host is not redirected (and is rejected by ALLOWED_HOSTS)
    r = Client().get("/", HTTP_HOST="www.mailsocket.app.evil.test")
    assert r.status_code == 400


def test_www_redirect_disabled_when_apex_empty(settings):
    settings.APEX_HOST = ""
    r = Client().get("/", HTTP_HOST="www.mailsocket.app")
    assert r.status_code == 400  # DisallowedHost, no redirect





def _section(html, marker):
    return html.split(marker, 1)[1].split("</section>", 1)[0]


def test_landing_r4_step1_matches_free_inbox_limit_and_wait_since(settings):
    """No "fresh inbox per run" on a 1-inbox free plan; the reuse claim rests
    on the wait endpoint's since default (request start)."""
    from apps.api.views import _parse_wait_since

    free = [t for t in _pricing_context()["tiers"] if t["key"] == "free"][0]
    steps = _section(_landing(), 'id="how-it-works"')
    text = re.sub(r"<[^>]+>", "", steps)
    assert "fresh one per test run" not in text
    assert f"The free plan includes {free['inboxes_display']}" in text
    assert "reuse it on every run" in text and "Paid plans hold more inboxes" in text
    # server default: absent since -> None -> the view uses request start
    assert _parse_wait_since(None) is None
    assert "only mail that arrives after the call starts matches" in text
    # follows settings, never a literal
    limits = {k: dict(v) for k, v in settings.BILLING_PLAN_LIMITS.items()}
    limits["free"].update(inboxes=3)
    settings.BILLING_PLAN_LIMITS = limits
    assert "The free plan includes 3 inboxes" in re.sub(r"<[^>]+>", "", _section(_landing(), 'id="how-it-works"'))


def test_landing_r4_sdk_timeouts_reconciled_with_client():
    # read the worktree SDK source (an installed `mailsocket` may be another checkout)
    src = (settings.BASE_DIR.parent / "sdks/python/mailsocket/client.py").read_text()
    section = _section(_landing(), 'id="code-sample"')
    text = re.sub(r"<[^>]+>", "", section)
    assert "timeout=120" not in section
    assert "re-issues the 25-second long-poll" in text and "60 seconds by default" in text
    assert re.search(r"^WAIT_TIMEOUT_MAX = 25\b", src, re.M)
    otp_sig = src.split("def wait_for_otp(", 1)[1].split(") -> WaitResult", 1)[0]
    assert "timeout: float = 60.0" in otp_sig
    # The sample passes since=started explicitly, which is correct whatever the
    # SDK default is (0 in <=0.1.x, request start from 0.2.0).
    assert re.search(r"since=(0|None)", otp_sig)
    assert "since=started" in section
    assert "defaults to 0" not in text


def test_landing_r4_sdk_sample_reuses_step2_playwright_lines():
    html = _landing()
    step2 = re.sub(r"<[^>]+>", "", html.split("<h3>Type the address", 1)[1].split("</li>", 1)[0])
    card = re.sub(r"<[^>]+>", "", html.split('class="landing-code-card"', 1)[1].split("</pre>", 1)[0])
    for line in ('page.fill("#email", inbox["address"])', 'page.click("text=Create account")'):
        assert line in step2 and line in card, line
    assert "client.list_inboxes()[0]" in card  # Page supports indexing


def test_landing_r4_n8n_card_shows_install_path():
    card = _landing().split("<h3>n8n</h3>", 1)[1].split("</li>", 1)[0]
    assert "Settings &rarr; Community nodes &rarr; Install" in card
    assert '<code class="landing-works-chip">n8n-nodes-mailsocket</code>' in card


def test_landing_r4_closing_cta_has_try_link():
    cta = _section(_landing(), 'class="landing-cta"')
    assert re.findall(r'class="landing-button[^"]*"[^>]*>([^<]+)</a>', cta) == [
        "Start free", "Try it without signup",
    ]
    assert f'class="landing-button landing-button-secondary" href="{reverse("dashboard:try-it")}"' in cta


def test_landing_r4_css_mobile_code_scrolls_and_signin_is_sans():
    css = (settings.BASE_DIR / "apps/dashboard/static/dashboard/app.css").read_text()
    assert ".signin{\n  font-family:var(--sans);" in css
    mobile = css.split("@media(max-width:640px){\n  .landing-home .landing-shell", 1)[1].split("\n}\n", 1)[0]
    assert "white-space:pre;overflow-x:auto" in mobile
    assert "pre-wrap" not in mobile and "break-all" not in mobile
    assert "font-size:11" not in mobile
    assert "min-height:42px" in mobile  # tabs + footer links


def test_terms_governing_law_and_refund_wording():
    html = _get("dashboard:legal-terms").content.decode()
    assert "governed by the laws of Vietnam" in html
    assert "Refunds don't\nend your plan by themselves" in html or "Refunds don't end your plan by themselves" in re.sub(r"\s+", " ", html)
    assert "refunded\nsubscription ends immediately" not in html
