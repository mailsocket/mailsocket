"""Comparison pages (/docs/compare/...): public, CSP-safe, sourced, drift-proof.

Every competitor fact must link to the competitor's own page; mailsocket's
numbers must come from settings (via _pricing_context), never literals.
"""

import copy
import re

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from apps.dashboard.compare import COMPARE_CHECKED, COMPARE_SOURCES, COMPARISONS
from apps.dashboard.views import _pricing_context

pytestmark = pytest.mark.django_db

APEX = "mailsocket.app"
DASH = "dash.mailsocket.app"
PAGES = ["dashboard:compare"] + [c["url_name"] for c in COMPARISONS.values()]
COMPETITOR_HOSTS = {
    "mailpit": ("github.com/axllent/mailpit",),
    "mailosaur": ("mailosaur.com", "api.npmjs.org"),
    "agentmail": ("agentmail.to", "pypistats.org"),
    "mailtrap": ("mailtrap.io", "api.npmjs.org"),
    "mailslurp": ("mailslurp.com", "api.npmjs.org"),
}
# Words that disparage a competitor or overclaim; none belong on these pages.
BANNED_WORDS = re.compile(
    r"\b(killer|kill|crush|destroy|terrible|awful|worst|bloated|outdated|legacy|clunky|"
    r"best-in-class|fastest|cheapest|only one)\b",
    re.I,
)


@pytest.fixture
def canon(settings):
    settings.CANONICAL_HOST_REDIRECTS = True
    settings.APEX_HOST = APEX
    settings.DASHBOARD_HOST = DASH
    settings.ATTRIBUTION_COOKIE_DOMAIN = APEX
    settings.ALLOWED_HOSTS = [APEX, DASH, "testserver"]
    return settings


def _page(url_name):
    response = Client().get(reverse(url_name))
    assert response.status_code == 200, url_name
    return response.content.decode()


def _main(html):
    return html.split('<main class="docs-content">', 1)[1].split("</main>", 1)[0]


def _visible(html):
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html)


def _rows(html):
    """(label, competitor cell, mailsocket cell) for each table body row."""
    body = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    rows = []
    for tr in re.findall(r"<tr>(.*?)</tr>", body, flags=re.S):
        cells = re.findall(r"<td>(.*?)</td>", tr, flags=re.S)
        assert len(cells) == 3, tr
        rows.append(tuple(cells))
    return rows


# --- served publicly on the apex, 301 from dash ---------------------------------


@pytest.mark.parametrize("url_name", PAGES)
def test_compare_pages_200_anonymous_on_apex(canon, url_name):
    response = Client().get(reverse(url_name), HTTP_HOST=APEX, secure=True)
    assert response.status_code == 200


def test_dash_compare_mailpit_301s_to_apex(canon):
    response = Client().get("/docs/compare/mailpit", HTTP_HOST=DASH, secure=True)
    assert response.status_code == 301
    assert response["Location"] == "https://" + APEX + "/docs/compare/mailpit"


def test_compare_paths_are_under_the_public_docs_prefix():
    from apps.accounts.middleware import PUBLIC_PATH_PREFIXES

    for url_name in PAGES:
        assert reverse(url_name).startswith("/docs/"), url_name
    assert "/docs/" in PUBLIC_PATH_PREFIXES


def test_unknown_compare_slug_404s():
    assert Client().get("/docs/compare/unknown").status_code == 404


# --- CSP / copy rules -------------------------------------------------------------


@pytest.mark.parametrize("url_name", PAGES)
def test_compare_pages_are_csp_safe(url_name):
    page = _page(url_name)
    for tag in re.findall(r"<script\b[^>]*>", page):
        assert "src=" in tag
    assert "<style" not in page
    assert " style=" not in page
    # no competitor logos or images, no CDN assets
    assert "<img" not in _main(page)
    assert re.findall(r'src="(https?://[^"]+)"', page) == []
    assert re.findall(r'<link rel="stylesheet" href="(https?://[^"]+)"', page) == []


@pytest.mark.parametrize("url_name", PAGES)
def test_compare_copy_has_no_em_dash_or_disparaging_words(url_name):
    text = _visible(_main(_page(url_name)))
    assert "\u2014" not in text and "&mdash;" not in text
    assert BANNED_WORDS.findall(text) == []


@pytest.mark.parametrize("slug", sorted(COMPARISONS))
def test_compare_page_states_date_footer_and_ctas(slug):
    page = _page(COMPARISONS[slug]["url_name"])
    main = _main(page)
    first_note = main.split('id="checked">', 1)[1].split("</p>", 1)[0]
    assert f"Checked {COMPARE_CHECKED}" in first_note
    assert "change" in first_note
    assert main.index('id="checked"') < main.index("<table")
    assert "Spotted something out of date? Email" in main
    assert 'href="mailto:support@mailsocket.app"' in main
    assert f'href="{reverse("dashboard:try-it")}"' in main
    assert f'href="{reverse("accounts:register")}"' in main
    assert 'id="choose-them"' in main and 'id="choose-mailsocket"' in main


def test_mailpit_page_has_honest_when_mailpit_is_better_section():
    main = _main(_page("dashboard:compare-mailpit"))
    assert "When Mailpit is the better choice" in main
    assert "MIT" in main and "free" in main.lower()


def test_compare_index_links_every_page():
    main = _main(_page("dashboard:compare"))
    for data in COMPARISONS.values():
        assert f'href="{reverse(data["url_name"])}"' in main
        assert data["title"] in main


def test_docs_nav_has_compare_link():
    page = _page("dashboard:compare-mailosaur")
    nav = page.split('<nav class="docs-nav"', 1)[1].split("</nav>", 1)[0]
    assert f'<a href="{reverse("dashboard:compare")}" aria-current="page">Compare</a>' in nav


# --- every competitor fact links to its source -------------------------------------


@pytest.mark.parametrize("slug", sorted(COMPARISONS))
def test_every_competitor_cell_links_to_the_competitors_own_page(slug):
    main = _main(_page(COMPARISONS[slug]["url_name"]))
    rows = _rows(main)
    assert rows
    for label, theirs, _ours in rows:
        links = re.findall(r'href="([^"]+)"', theirs)
        assert links, f"{slug}: no source for {label!r}"
        for link in links:
            assert link.startswith("https://"), link
            assert any(host in link for host in COMPETITOR_HOSTS[slug]), link
    for url in COMPARE_SOURCES[slug].values():
        assert f'href="{url}"' in main, url


@pytest.mark.parametrize("slug", ["mailosaur", "agentmail", "mailtrap", "mailslurp"])
def test_tables_show_where_the_competitor_is_better(slug):
    rows = _rows(_main(_page(COMPARISONS[slug]["url_name"])))
    they_win = [label for label, theirs, ours in rows if "✅" in theirs and "❌" in ours]
    assert they_win, slug
    labels = " ".join(label for label, _t, _o in rows)
    if slug == "mailosaur":
        assert "SMS testing" in they_win
        assert "How widely used" in labels
    elif slug == "agentmail":
        assert "WebSocket events" in they_win
        assert "Custom domains" in labels and "How widely used" in labels
    elif slug == "mailtrap":
        assert "Reply to inbound email" in they_win
        assert "Conversation threading" in they_win
    else:
        assert "SMS and phone numbers" in they_win
        assert "Device previews (Gmail, Outlook, mobile)" in they_win


# --- mailsocket numbers come from settings (drift guard) --------------------------


@pytest.mark.parametrize("slug", sorted(COMPARISONS))
def test_mailsocket_numbers_equal_pricing_context(slug):
    main = _main(_page(COMPARISONS[slug]["url_name"]))
    tiers = {t["key"]: t for t in _pricing_context()["tiers"]}
    free = tiers["free"]
    assert f'{free["inboxes_display"]}, {free["messages_display"]}' in main
    if slug != "mailpit":
        assert free["retention_display"] in main
        pro = tiers["paid"]
        assert f'{pro["name"]} {pro["price"]}: {pro["inboxes_display"]}, {pro["messages_display"]}' in main


def test_compare_pages_follow_a_plan_change(settings):
    limits = copy.deepcopy(settings.BILLING_PLAN_LIMITS)
    limits["free"]["inboxes"] = 7
    limits["free"]["messages_per_month"] = 4321
    limits["scale"]["custom_domains"] = 3
    settings.BILLING_PLAN_LIMITS = limits
    for slug in COMPARISONS:
        main = _main(_page(COMPARISONS[slug]["url_name"]))
        assert "7 inbox" in main and "4,321 / month" in main, slug
    agentmail = _main(_page("dashboard:compare-agentmail"))
    assert "From the Scale plan" in agentmail
    mailosaur = _main(_page("dashboard:compare-mailosaur"))
    assert "On the Scale plan" in mailosaur


def test_custom_domains_plan_matches_settings():
    first = next(
        key for key in ("free", "paid", "scale", "business")
        if settings.BILLING_PLAN_LIMITS[key]["custom_domains"] > 0
    )
    name = {t["key"]: t["name"] for t in _pricing_context()["tiers"]}[first]
    assert f"From the {name} plan" in _main(_page("dashboard:compare-agentmail"))


# --- discoverability -----------------------------------------------------------------


def test_sitemap_lists_compare_pages():
    body = Client().get("/sitemap.xml").content.decode()
    for url_name in PAGES:
        assert f"<loc>https://mailsocket.app{reverse(url_name)}</loc>" in body, url_name


def test_llms_txt_links_compare_index():
    body = Client().get(reverse("dashboard:llms-txt")).content.decode()
    assert "/docs/compare/" in body
