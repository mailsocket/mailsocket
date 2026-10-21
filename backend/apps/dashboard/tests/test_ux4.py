"Public API."

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.test import Client
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.models import AccountPlan
from apps.accounts.services import VERIFY_ACCOUNT_RATE_LIMIT, create_api_key
from apps.dashboard.tests.test_ux3 import _client, _media_block, make_user
from apps.inboxes.models import Inbox

pytestmark = pytest.mark.django_db

STATIC = Path(settings.BASE_DIR) / "apps/dashboard/static/dashboard"
CSS = STATIC / "app.css"
TEMPLATES = Path(settings.BASE_DIR) / "apps/dashboard/templates/dashboard"


def _css():
    return CSS.read_text()


# --- R3-4: inbox actions fit at 1280 ----------------------------------------------


def _inbox_row(user, routing_key="ux4_r34_box"):
    inbox = Inbox.objects.create(user=user, routing_key=routing_key, label="R3-4")
    page = _client(user).get(reverse("dashboard:inboxes")).content.decode()
    row = page.split('<table class="data-table inboxes-table">', 1)[1].split("</tr>", 2)[1]
    return inbox, row


def test_disable_and_delete_live_in_a_more_menu():
    inbox, row = _inbox_row(make_user("ux4-r34"))
    actions = row.split('<td class="actions">', 1)[1]
    more = actions.split('<details class="row-more">', 1)[1].split("</details>", 1)[0]
    assert f'<summary aria-label="More actions for {inbox.address}">More</summary>' in more
    assert reverse("dashboard:inbox-disable", args=[inbox.public_id]) in more
    assert reverse("dashboard:inbox-delete", args=[inbox.public_id]) in more
    assert '<button class="danger">Delete</button>' in more
    # Connect and the test code stay one click away, OUTSIDE the menu.
    before = actions.split('<details class="row-more">', 1)[0]
    assert "<summary>Connect</summary>" in before and ">Send a test code</button>" in before
    assert "inbox-disable" not in before and "inbox-delete" not in before
    # Delete keeps its confirm; no JS added for the menu.
    assert "hx-confirm=" in more and "<script" not in row and "style=" not in row


def test_disabled_inbox_keeps_enable_in_the_menu():
    user = make_user("ux4-r34-off")
    inbox = Inbox.objects.create(user=user, routing_key="ux4_r34_off", is_enabled=False)
    page = _client(user).get(reverse("dashboard:inboxes")).content.decode()
    more = page.split('<details class="row-more">', 1)[1].split("</details>", 1)[0]
    assert reverse("dashboard:inbox-enable", args=[inbox.public_id]) in more


def test_more_menu_css_is_no_js_and_fits_1280():
    css = _css()
    assert ".row-more>summary{" in css and ".row-more>summary::-webkit-details-marker{display:none}" in css
    assert ".row-more-body{display:grid;" in css
    wide = _media_block(css, "min-width:1280px")
    # Popover >= 1280 so opening it never widens the Actions column.
    assert ".inboxes-table .row-more-body{position:absolute;right:calc(100% + 6px);" in wide
    # The table's min widths fit the 1280 card (measured: 942px wrap, 0 overflow).
    narrow = _media_block(css, "min-width:1280px) and (max-width:1365px")
    assert ".inboxes-table .col-label,.inboxes-table td.col-label{min-width:190px}" in narrow
    assert ".inboxes-table .col-address,.inboxes-table td.col-address{min-width:262px}" in narrow




APP_JS = STATIC / "app.js"

_ROW_MORE_HARNESS = r"""
const fs = require("fs");
const docListeners = { capture: {}, bubble: {} };
class Element {
  constructor(tag, cls, parent) {
    this.tag = tag; this.cls = cls || ""; this.parent = parent || null; this.children = [];
    this.open = false; if (parent) parent.children.push(this);
  }
  matches(sel) {
    return sel.split(",").some((one) => {
      const m = one.trim().match(/^([a-z]+)?(?:\.([a-z-]+))?(\[open\])?$/);
      if (!m) return false;  // other app.js selectors ([data-copy-target], ...) match nothing here
      return (!m[1] || m[1] === this.tag) && (!m[2] || this.cls.split(" ").includes(m[2])) && (!m[3] || this.open);
    });
  }
  closest(sel) { for (let n = this; n; n = n.parent) { if (n.matches(sel)) return n; } return null; }
  contains(other) { for (let n = other; n; n = n.parent) { if (n === this) return true; } return false; }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  querySelectorAll(sel) { return this.all().filter((n) => n.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  focus() { document.activeElement = this; }
}
global.Element = Element;
const root = new Element("html");
const body = new Element("body", "", root);
function row(i) {
  const details = new Element("details", i === 99 ? "connect" : "row-more", body);
  details.summary = new Element("summary", "", details);
  details.menuBody = new Element("div", "row-more-body", details);
  details.button = new Element("button", "danger", details.menuBody);
  return details;
}
const menus = [row(0), row(1), row(2)];
const connect = row(99);
const outside = new Element("p", "", body);
global.window = { addEventListener() {}, setTimeout };
global.navigator = {};
global.CSS = { escape: (s) => s };
global.document = {
  readyState: "complete", activeElement: body,
  body: { addEventListener() {} },
  addEventListener(name, fn, capture) { (docListeners[capture ? "capture" : "bubble"][name] ||= []).push(fn); },
  getElementById() { return null; },
  // only the menu selectors reach the fake tree; app.js's other start-up
  // lookups (copy buttons, sidebar nav) find nothing here
  querySelector(sel) { return sel.includes("details") ? root.querySelector(sel) : null; },
  querySelectorAll(sel) { return sel.includes("details") ? root.querySelectorAll(sel) : []; },
};
eval(fs.readFileSync(process.argv[2], "utf8"));
function fire(name, detail, phase) { (docListeners[phase || "bubble"][name] || []).forEach((f) => f(detail)); }
function click(target) { fire("click", { target }); }
function setOpen(d, v) { d.open = v; }  // the browser's own summary toggle
const openIdx = () => menus.map((m, i) => (m.open ? i : -1)).filter((i) => i >= 0);
const out = {};
// one at a time: opening menu 1 closes menu 0
setOpen(menus[0], true); click(menus[0].summary);
setOpen(menus[1], true); click(menus[1].summary);
out.oneAtATime = openIdx();
// a click inside the open menu keeps it open
click(menus[1].menuBody); out.insideKeeps = openIdx();
// a click outside closes it
click(outside); out.afterOutside = openIdx();
// Escape closes it and returns focus to its summary
setOpen(menus[2], true); menus[2].button.focus();
fire("keydown", { key: "Escape" });
out.afterEsc = openIdx(); out.focusOnSummary = document.activeElement === menus[2].summary;
// Connect still closes on Escape
connect.open = true; fire("keydown", { key: "Escape" }); out.connectClosed = !connect.open;
// other keys do nothing
setOpen(menus[0], true); fire("keydown", { key: "Enter" }); out.enterKeeps = openIdx();
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_more_menu_closes_on_escape_outside_click_and_one_at_a_time(tmp_path):
    harness = tmp_path / "row_more.js"
    harness.write_text(_ROW_MORE_HARNESS)
    result = subprocess.run(["node", str(harness), str(APP_JS)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out == {
        "oneAtATime": [1],
        "insideKeeps": [1],
        "afterOutside": [],
        "afterEsc": [],
        "focusOnSummary": True,
        "connectClosed": True,
        "enterKeeps": [0],
    }, out


def test_more_menu_js_is_delegated_and_csp_safe():
    js = APP_JS.read_text()
    assert 'document.querySelectorAll("details.connect[open], details.row-more[open]")' in js
    assert 'event.target.closest("details.row-more")' in js
    # one document-level listener: rows swapped in by htmx are covered too
    assert 'document.querySelectorAll("details.row-more[open]").forEach' in js
    row = (TEMPLATES / "partials/inbox_rows.html").read_text()
    assert "<script" not in row and "onclick" not in row and "ontoggle" not in row


def test_more_menu_popover_opens_beside_its_summary_not_over_the_next_row():
    "Public API."
    wide = _media_block(_css(), "min-width:1280px")
    rule = wide.split(".inboxes-table .row-more-body{", 1)[1].split("}", 1)[0]
    for decl in ("position:absolute", "right:calc(100% + 6px)", "top:50%", "transform:translateY(-50%)",
                 "grid-auto-flow:column", "z-index:20"):
        assert decl in rule, decl
    assert "top:calc(100%" not in rule  # never below the summary (over the next row)


# --- R3-5 / R3-6: mobile header ---------------------------------------------------


def test_single_column_layout_does_not_stretch_the_header():
    tablet = _media_block(_css(), "max-width:800px")
    assert ".layout{grid-template-rows:auto 1fr}" in tablet


def test_phone_header_one_row_with_right_aligned_report_and_sign_out():
    phone = _media_block(_css(), "max-width:640px")
    assert ".sidebar{display:grid;grid-template-columns:minmax(0,1fr) auto auto;" in phone
    assert "padding:10px 16px}" in phone  # one 16px gutter (cards use 16px too)
    assert ".sidebar>.brand{grid-column:1;grid-row:1;" in phone
    assert ".sidebar>.app-nav-report{grid-column:2;grid-row:1;" in phone
    assert ".sidebar>form:last-child{grid-column:3;grid-row:1;" in phone
    assert ".sidebar>.app-nav-mobile,.sidebar>.account-switcher{grid-column:1/-1}" in phone
    assert ".content{padding:20px 16px 32px}" in _media_block(_css(), "max-width:800px")


def test_header_markup_order_still_brand_report_sign_out():
    page = _client(make_user("ux4-r36")).get(reverse("dashboard:overview")).content.decode()
    sidebar = page.split('<aside class="sidebar">', 1)[1].split("</aside>", 1)[0]
    assert sidebar.index('class="brand"') < sidebar.index('class="app-nav-report"') < sidebar.index('action="/logout/"')
    assert sidebar.rstrip().endswith("</form>")  # Sign out is still the last child


# --- R3-7: empty states outside the table -------------------------------------------


@pytest.mark.parametrize(
    ("url_name", "text"),
    [
        ("dashboard:domains", "No custom domains yet."),
        ("dashboard:inboxes", "No inboxes yet."),
        ("dashboard:messages", "No messages yet."),
        ("dashboard:api-keys", "No API keys yet."),
        ("dashboard:overview", "No messages yet."),
    ],
)
def test_empty_lists_render_an_empty_state_card_not_a_table(url_name, text, settings):
    settings.ENABLE_BYOD = True
    user = make_user(f"ux4-r37-{url_name.split(':')[1]}")
    response = _client(user).get(reverse(url_name))
    assert response.status_code == 200
    page = response.content.decode()
    assert f'<p class="empty-state">{text}' in page
    assert 'class="empty"' not in page and "colspan" not in page


def test_domains_empty_state_keeps_the_upgrade_link(settings):
    settings.ENABLE_BYOD = True
    page = _client(make_user("ux4-r37-up")).get(reverse("dashboard:domains")).content.decode()
    card = page.split('<p class="empty-state">', 1)[1].split("</p>", 1)[0]
    assert "No custom domains yet." in card and ">Upgrade to Scale or Business</a> to add one." in card
    assert "domains-table" not in page


def test_message_groups_and_webhook_deliveries_empty_states():
    user = make_user("ux4-r37-groups", AccountPlan.Plan.PAID)
    page = _client(user).get(reverse("dashboard:messages") + "?group_by=sender&view=grouped").content.decode()
    assert "groups-table" not in page
    assert '<p class="empty-state">No messages yet.</p>' in page and "colspan" not in page
    for name in ("message_groups.html", "webhook_delivery_rows.html", "message_rows.html", "inbox_rows.html", "api_key_rows.html"):
        source = (TEMPLATES / "partials" / name).read_text()
        assert "{% empty %}" not in source and 'class="empty-state"' in source, name


def test_tables_still_render_when_there_are_rows():
    user = make_user("ux4-r37-rows")
    Inbox.objects.create(user=user, routing_key="ux4_r37_rows")
    create_api_key(user, "rows")
    client = _client(user)
    assert 'class="data-table inboxes-table"' in client.get(reverse("dashboard:inboxes")).content.decode()
    assert 'class="data-table keys-table"' in client.get(reverse("dashboard:api-keys")).content.decode()


def test_empty_state_css():
    assert ".empty-state{margin:0;padding:18px 8px;text-align:center;" in _css()


def test_no_app_template_renders_an_empty_row_inside_a_table():
    for path in TEMPLATES.rglob("*.html"):
        source = path.read_text()
        assert not re.search(r"\{% empty %\}\s*<tr>", source), path.name


# --- R3-9: reset submit ------------------------------------------------------------


def test_reset_form_submit_says_set_new_password():
    from apps.accounts.tests.test_ux3_auth import _verified

    user = _verified("ux4-r39@example.com")
    url = reverse(
        "accounts:password-reset-confirm",
        kwargs={"uidb64": urlsafe_base64_encode(force_bytes(user.pk)), "token": default_token_generator.make_token(user)},
    )
    page = Client().get(url).content.decode()
    assert '<button type="submit" class="button auth-submit">Set new password</button>' in page
    assert ">Continue</button>" not in page


# --- R3-11a: no tagline on auth pages, brand mark kept --------------------------------


@pytest.mark.parametrize(
    "url_name", ["accounts:login", "accounts:register", "accounts:password-reset", "accounts:resend-verification"]
)
def test_auth_pages_drop_the_tagline_keep_the_brand(url_name):
    page = Client().get(reverse(url_name)).content.decode()
    assert "Get the OTP, not the email" not in page and "auth-tag" not in page
    assert '<div class="auth-brand">' in page and 'class="brand-mark"' in page


def test_auth_confirmation_and_check_email_pages_drop_the_tagline():
    for page in (
        Client().post(reverse("accounts:register"), {"email": "ux4-tag@example.com"}).content.decode(),
        Client().post(reverse("accounts:password-reset"), {"email": "ux4-tag@example.com"}).content.decode(),
    ):
        assert "Get the OTP, not the email" not in page and 'class="brand-mark"' in page
    for name in ("form.html", "confirmation.html", "check_email.html"):
        source = (Path(settings.BASE_DIR) / "apps/accounts/templates/accounts" / name).read_text()
        assert "auth-tag" not in source, name


def test_landing_keeps_the_tagline():
    assert "<title>mailsocket · Get the OTP, not the email</title>" in Client().get("/").content.decode()


# --- R3-10: pricing -------------------------------------------------------------------


def _pricing():
    return Client().get(reverse("dashboard:pricing")).content.decode()


@pytest.mark.parametrize("billing", [True, False])
def test_paid_tiers_keep_start_free_with_an_upgrade_subline(billing, settings):
    settings.ENABLE_BILLING = billing
    page = _pricing()
    heads = dict(re.findall(r'<th class="pricing-compare-col[^"]*" data-tier="(\w+)">(.*?)</th>', page, re.S))
    for key, name in (("paid", "Pro"), ("scale", "Scale"), ("business", "Business")):
        head = heads[key]
        if billing:
            assert ">Start free</a>" in head, key
            assert f'<span class="pricing-compare-then">then upgrade to {AccountPlan.DISPLAY_NAMES[key]} in Billing</span>' in head
            assert AccountPlan.DISPLAY_NAMES[key] == name
            assert head.index(">Start free</a>") < head.index("pricing-compare-then")
        else:
            # Billing off: there is no Billing page to upgrade in, so no sub-line
            # (Pro still says Start free; Scale/Business say Contact us).
            assert "pricing-compare-then" not in head, key
    assert "pricing-compare-then" not in heads["free"] and "pricing-compare-then" not in heads["enterprise"]


def test_pricing_phone_columns_fit_free_and_pro_at_375():
    phone = _media_block(_css(), "max-width:640px")
    assert ".pricing-compare-feature-col{width:88px;min-width:88px}" in phone
    assert ".pricing-compare-table{width:calc(88px + 5 * 120px)}" in phone
    # 88 label + 120 Free + 120 Pro + the page gutters fit in 375 (measured: Pro ends at x=348).
    assert 20 + 88 + 120 + 120 <= 375 - 20


def test_footer_and_footnote_links_are_24px_tap_targets():
    css = _css()
    assert ".site-footer .footer-col a{display:flex;align-items:center;min-height:24px}" in css
    assert ".pricing-compare-table sup a{display:inline-flex;align-items:center;justify-content:center;min-width:24px;min-height:24px}" in css
    page = _pricing()
    assert '<sup><a href="#webhooks-note" aria-label="About webhooks">1</a></sup>' in page


# --- R3-11c: verify row moved to Good to know --------------------------------------------


def test_email_verify_row_is_a_note_not_a_table_row():
    page = _pricing()
    table = page.split('<table class="docs-table pricing-compare-table">', 1)[1].split("</table>", 1)[0]
    assert "Email verify" not in table and "/ hour" not in table
    notes = page.split('id="notes"', 1)[1].split("</section>", 1)[0]
    assert (
        f'<li id="verify-note">Email verification (<code>/verify</code>) is limited to '
        f"{VERIFY_ACCOUNT_RATE_LIMIT:,} / hour per account on every plan.</li>"
    ) in notes


def test_verify_note_follows_the_setting(monkeypatch):
    from apps.dashboard import views as dashboard_views

    monkeypatch.setattr(dashboard_views, "VERIFY_ACCOUNT_RATE_LIMIT", 4321)
    assert "limited to 4,321 / hour per account on every plan" in _pricing()


# --- R3-11d: otp_confidence is called a heuristic score in prose ------------------------


def test_otp_confidence_is_called_a_heuristic_score():
    docs = Client().get(reverse("dashboard:docs")).content.decode()
    row = docs.split('<tr id="min-confidence">', 1)[1].split("</tr>", 1)[0]
    assert "<code>otp_confidence</code> is a heuristic score, not a model prediction" in row
    landing = Client().get("/").content.decode()
    assert "<code>otp_confidence</code> is a heuristic score: 0.95" in landing
    faq = Client().get(reverse("dashboard:faq")).content.decode()
    assert "give each a heuristic score between 0 and 1 (<code>otp_confidence</code>" in faq
    # The API field itself is unchanged.
    assert '"otp_confidence"' in landing


# --- CSP: nothing inline added ------------------------------------------------------------


def test_changed_pages_have_no_inline_style_or_script(settings):
    settings.ENABLE_BYOD = True
    user = make_user("ux4-csp")
    Inbox.objects.create(user=user, routing_key="ux4_csp_box")
    client = _client(user)
    for name in ("dashboard:inboxes", "dashboard:domains", "dashboard:api-keys", "dashboard:messages"):
        page = client.get(reverse(name)).content.decode()
        assert "style=" not in page and "<style" not in page, name
        assert page.count("<script") == page.count('<script defer src="'), name
    for page in (_pricing(), Client().get(reverse("accounts:login")).content.decode()):
        assert "style=" not in page and "<style" not in page
