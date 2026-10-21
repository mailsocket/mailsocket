"""ci-attrib r2: no inline style in anything served under the strict CSP.

The site CSP (apps.accounts.middleware._default_csp) has no style-src, so
style-src falls back to default-src 'self': a ``style="..."`` attribute or a
``<style>`` element in our HTML is refused by the browser (console error,
style silently dropped). The fix for every such case is a class in app.css,
never 'unsafe-inline'.

Three layers, so an inline style cannot come back:
  1. every template source file (pages AND htmx partials, which are swapped
     into a CSP page) is grepped for ``style=`` / ``<style``;
  2. the HTML-producing static JS is grepped for ways to create a style
     attribute or element (CSSOM ``el.style.x = ...`` is allowed by CSP and
     stays allowed);
  3. real rendered responses (public, signed-in app pages and the htmx
     partials the e2e journey swaps in) are checked, with the CSP header.

Allowed exceptions (and why):
  * 500.html: self-contained by contract (a 500 may fire when static is
    unavailable, see config.views.server_error). Its <style> is test-pinned
    and is the one known exception; it is listed here explicitly so any
    other template gaining a <style> fails.
"""
import re
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

BACKEND = Path(django_settings.BASE_DIR)
APPS = BACKEND / "apps"

# relative to backend/; the ONLY templates allowed to carry inline style.
ALLOWED_INLINE_STYLE_TEMPLATES = {
    "apps/dashboard/templates/500.html",
}

STYLE_ATTR = re.compile(r"""(?<![\w-])style\s*=""", re.IGNORECASE)
STYLE_ELEM = re.compile(r"<style\b", re.IGNORECASE)
# JS that would create inline style the CSP refuses (CSSOM property writes
# such as `ta.style.left = "-9999px"` are NOT refused and are fine).
JS_INLINE_STYLE = re.compile(
    r"""setAttribute\(\s*["']style["']"""
    r"""|\.style\.cssText\s*="""
    r"""|createElement\(\s*["']style["']"""
    r"""|style\s*=\s*\\?["']"""  # style="..." inside an HTML string
    r"""|<style\b""",
    re.IGNORECASE,
)


def _templates():
    return sorted(p for p in APPS.glob("*/templates/**/*") if p.is_file() and p.suffix in {".html", ".txt", ".xml", ".svg"})


def _own_static_js():
    return sorted(p for p in APPS.glob("*/static/**/*.js") if "vendor" not in p.parts)


def _offenders(text):
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if STYLE_ATTR.search(line) or STYLE_ELEM.search(line):
            out.append(f"{n}: {line.strip()[:160]}")
    return out


def test_template_inventory_is_not_empty():
    # guards the glob itself: an empty list would make the next test vacuous
    assert len(_templates()) >= 60
    assert len(_own_static_js()) >= 4


def test_allowlisted_templates_exist():
    for rel in ALLOWED_INLINE_STYLE_TEMPLATES:
        assert (BACKEND / rel).is_file(), rel


@pytest.mark.parametrize("path", _templates(), ids=lambda p: str(p.relative_to(APPS)))
def test_template_has_no_inline_style(path):
    rel = str(path.relative_to(BACKEND))
    if rel in ALLOWED_INLINE_STYLE_TEMPLATES:
        pytest.skip("allowed: self-contained error page (see module docstring)")
    offenders = _offenders(path.read_text())
    assert not offenders, (
        f"{rel} has inline style; the CSP (default-src 'self', no 'unsafe-inline') "
        "refuses it. Move it to a class in app.css.\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize("path", _own_static_js(), ids=lambda p: str(p.relative_to(APPS)))
def test_static_js_creates_no_inline_style(path):
    hits = [
        f"{n}: {line.strip()[:160]}"
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if JS_INLINE_STYLE.search(line)
    ]
    assert not hits, f"{path.relative_to(BACKEND)} creates inline style (CSP-refused):\n" + "\n".join(hits)


def test_detector_catches_the_shapes_it_guards():
    # the regexes must actually match what they are meant to forbid
    assert _offenders('<div style="color:red">')
    assert _offenders("<p STYLE='x'>")
    assert _offenders("<style>p{}</style>")
    assert _offenders('<svg><style>.a{}</style></svg>')
    assert not _offenders('<div data-style="x" class="style-box">')
    assert not _offenders("<link rel=stylesheet href=/static/app.css>")
    assert JS_INLINE_STYLE.search('el.setAttribute("style", "x")')
    assert JS_INLINE_STYLE.search("el.style.cssText = 'x'")
    assert JS_INLINE_STYLE.search('document.createElement("style")')
    assert JS_INLINE_STYLE.search("box.innerHTML = '<span style=\"x\">'")
    assert not JS_INLINE_STYLE.search('ta.style.left = "-9999px";')


# --- rendered responses ---------------------------------------------------------


def _assert_rendered_clean(response, where):
    html = response.content.decode()
    assert not STYLE_ELEM.search(html), f"{where}: <style> in rendered HTML"
    hits = [m.group(0) for m in re.finditer(r"<[a-z][^>]*(?<![\w-])style\s*=[^>]*>", html, re.IGNORECASE)]
    assert not hits, f"{where}: inline style attribute(s): {hits[:5]}"
    csp = response.headers.get("Content-Security-Policy", "")
    if csp:
        assert "unsafe-inline" not in csp, where


PUBLIC_URLS = [
    "dashboard:root", "dashboard:docs", "dashboard:try-it", "dashboard:api-reference",
    "dashboard:pricing", "dashboard:security", "dashboard:error-reference", "accounts:login",
]


@pytest.mark.django_db
@pytest.mark.parametrize("url_name", PUBLIC_URLS)
def test_public_page_rendered_has_no_inline_style(url_name):
    response = Client().get(reverse(url_name), follow=True)
    assert response.status_code == 200
    _assert_rendered_clean(response, url_name)


APP_URLS = [
    "dashboard:overview", "dashboard:api-keys", "dashboard:inboxes", "dashboard:messages",
    "dashboard:usage", "dashboard:webhooks", "dashboard:domains", "dashboard:team",
    "dashboard:settings", "dashboard:billing-page",
]


@pytest.fixture
def app_client(db):
    user = get_user_model().objects.create_user(username="nostyle", email="nostyle@example.test", password="pw")
    Account.objects.create(user=user, email_normalized="nostyle@example.test", email_verified_at=timezone.now())
    AccountPlan.objects.create(user=user, plan=AccountPlan.Plan.BUSINESS)
    Inbox.objects.create(user=user, routing_key="nostylebox")
    create_api_key(user, "k")
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.parametrize("url_name", APP_URLS)
def test_app_page_rendered_has_no_inline_style(app_client, settings, url_name):
    # team/domains/billing are feature-flagged (404 when off); the e2e server
    # (config.settings.e2e) runs them on, so render them the same way here.
    settings.ENABLE_TEAMS = True
    settings.ENABLE_BYOD = True
    settings.ENABLE_BILLING = True
    response = app_client.get(reverse(url_name))
    assert response.status_code == 200, url_name
    _assert_rendered_clean(response, url_name)


def test_htmx_partials_from_the_journey_have_no_inline_style(app_client):
    """The partials the e2e journey swaps into a CSP page (key created,
    inbox created): swapped markup is subject to the same CSP."""
    key = app_client.post(reverse("dashboard:api-key-create"), {"name": "nostyle"}, HTTP_HX_REQUEST="true")
    assert key.status_code == 200
    assert "ms_live_" in key.content.decode()
    _assert_rendered_clean(key, "api-key-create partial")
    inbox = app_client.post(reverse("dashboard:inbox-create"), {"label": "nostyle2"}, HTTP_HX_REQUEST="true")
    assert inbox.status_code in (200, 201), inbox.status_code
    _assert_rendered_clean(inbox, "inbox-create partial")
