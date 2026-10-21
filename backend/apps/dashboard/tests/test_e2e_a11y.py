"Public API."
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan
from apps.accounts.services import create_api_key
from apps.inboxes.models import Inbox

DASH = Path(django_settings.BASE_DIR) / "apps" / "dashboard"
APP_JS = (DASH / "static" / "dashboard" / "app.js").read_text()
APP_CSS = (DASH / "static" / "dashboard" / "app.css").read_text()

SCROLL_CLASSES = {"table-wrap", "docs-table-wrap", "landing-step-code"}


class RegionParser(HTMLParser):
    """Collects every element that may scroll horizontally."""

    def __init__(self):
        super().__init__()
        self.regions = []  # (tag, attrs)
        self._in_response_head = 0
        self._stack = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = set((a.get("class") or "").split())
        if tag == "pre" or classes & SCROLL_CLASSES or (tag == "code" and self._in_response_head):
            self.regions.append((tag, a))
        if tag == "div" and "landing-response-head" in classes:
            self._in_response_head += 1
            self._stack.append(True)
        elif tag not in ("br", "img", "input", "meta", "link"):
            self._stack.append(False)

    def handle_endtag(self, tag):
        if self._stack and self._stack.pop():
            self._in_response_head -= 1


def assert_regions_focusable(html, page):
    parser = RegionParser()
    parser.feed(html)
    assert parser.regions, f"{page}: expected scrollable regions"
    for tag, attrs in parser.regions:
        where = f"{page}: <{tag} class={attrs.get('class')!r} id={attrs.get('id')!r}>"
        assert attrs.get("tabindex") == "0", where
        assert attrs.get("role") == "region", where
        assert attrs.get("aria-label") in {"Code example", "Table"}, where
    return len(parser.regions)


@pytest.mark.parametrize("url_name", [
    "dashboard:root", "dashboard:docs", "dashboard:try-it", "dashboard:api-reference",
    "dashboard:pricing", "dashboard:security", "dashboard:error-reference",
])
def test_public_scrollable_regions_are_focusable_without_js(client, url_name, db):
    resp = client.get(reverse(url_name), follow=True)
    assert resp.status_code == 200
    assert_regions_focusable(resp.content.decode(), url_name)


def test_docs_has_every_pre_and_table_focusable(client, db):
    html = client.get(reverse("dashboard:docs")).content.decode()
    n = assert_regions_focusable(html, "docs")
    assert n >= 20  # 17 <pre> + 3 docs-table-wrap


def _user():
    user = get_user_model().objects.create_user(username="a11y", email="a11y@example.test", password="pw")
    Account.objects.create(user=user, email_normalized="a11y@example.test", email_verified_at=timezone.now())
    AccountPlan.objects.create(user=user)
    return user


@pytest.mark.django_db
def test_dashboard_snippets_and_tables_are_focusable():
    "Public API."
    user = _user()
    Inbox.objects.create(user=user, routing_key="a11ybox")
    create_api_key(user, "k")
    client = Client()
    client.force_login(user)
    for name in ("dashboard:overview", "dashboard:inboxes", "dashboard:api-keys"):
        html = client.get(reverse(name)).content.decode()
        assert_regions_focusable(html, name)
    overview = client.get(reverse("dashboard:overview")).content.decode()
    assert re.search(r'<div class="snippet">\s*<div class="snippet-label">curl</div>\s*<pre tabindex="0" role="region"', overview)


def test_app_js_fallback_makes_scroll_regions_focusable():
    assert "function makeScrollRegionsFocusable" in APP_JS
    for selector in ('"pre"', '".table-wrap"', '".docs-table-wrap"', '".landing-step-code"', '".landing-response-head code"'):
        assert selector in APP_JS
    assert 'el.setAttribute("tabindex", "0")' in APP_JS
    # runs on load AND after htmx swaps
    assert "enhance(document)" in APP_JS and "enhance(event.target)" in APP_JS


def test_focused_scroll_region_has_a_visible_ring():
    assert re.search(r"pre\[tabindex\]:focus-visible[^{]*\{outline:2px solid var\(--accent\)", APP_CSS)


def _rule(selector):
    m = re.search(re.escape(selector) + r"\{([^}]*)\}", APP_CSS)
    assert m, selector
    return m.group(1)


@pytest.mark.parametrize("selector", [
    ".usage-note a",                       
    ".landing-pricing-note a",
    ".landing-home .landing-step p a",
    ".landing-works-source",
    ".payment-list a",
    ".status-layout .status-json-link",
])
def test_in_text_links_are_underlined(selector):
    body = _rule(selector)
    assert "text-decoration:underline" in body and "text-decoration:none" not in body
    assert "text-underline-offset:3px" in body


@pytest.mark.django_db
def test_usage_see_plans_link_is_in_an_underlined_note(settings):
    user = _user()
    client = Client()
    client.force_login(user)
    html = client.get(reverse("dashboard:usage")).content.decode()
    assert re.search(r'<p class="usage-note">Need more\? <a href=', html)
