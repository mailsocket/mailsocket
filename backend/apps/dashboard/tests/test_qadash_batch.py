"""Regression tests for the dashboard QA batch (QA-DASH-FINDINGS.md).

Each test names the finding it guards and FAILS on the pre-fix code:

- #8  htmx 2.x swallowed 4xx form errors -> app.js responseHandling swaps the
      flash partial for 400/403/404/409/413/422/429; 5xx -> generic flash.
- #1  390px horizontal scroll -> grid column minmax(0,1fr) + min-width:0.
- #2  GET /logout/ -> "Sign out?" confirm page (POST+CSRF still required).
- #4  double-submit -> hx-disabled-elt on mutating forms + 5 s same-name
      API-key idempotency guard.
- #5  invalid expires_in_days -> expiry-specific error message.
- #6/#9 revoke key / delete inbox carry hx-confirm.
- #7  OTP in the messages list renders as <code class="otp">.
- #10 skip-to-content link + id on <main>.
"""
import json
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan, ApiKey
from apps.inboxes.models import Inbox, Message

DASH = Path(django_settings.BASE_DIR) / "apps" / "dashboard"
APP_JS = DASH / "static" / "dashboard" / "app.js"
APP_CSS = DASH / "static" / "dashboard" / "app.css"
TEMPLATES = DASH / "templates" / "dashboard"


def _make_user(name, *, plan=AccountPlan.Plan.FREE):
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


# --- #8 htmx 4xx responses are swapped ---------------------------------------


def test_app_js_configures_htmx_to_swap_client_error_partials():
    source = APP_JS.read_text()
    assert "htmx.config.responseHandling" in source
    match = re.search(r'SWAP_ERROR_CODES\s*=\s*"([^"]+)"', source)
    assert match, "app.js must declare the swapped 4xx code list"
    pattern = re.compile(match.group(1))
    for code in ("400", "403", "404", "409", "413", "422", "429"):
        assert pattern.search(code), code
    for code in ("401", "405", "410", "500", "502", "503"):
        assert not pattern.search(code), code
    # 5xx gets a generic flash instead of a silent console error.
    assert "htmx:beforeSwap" in source
    assert "Something went wrong" in source


_NODE_HARNESS = r"""
const fs = require("fs");
const listeners = {};
const created = [];
function el(id) {
  const node = {
    id, children: [], attrs: {}, className: "", textContent: "",
    setAttribute(k, v) { this.attrs[k] = v; },
    insertBefore(child) { this.children.unshift(child); created.push(child); },
    remove() { this.removed = true; delete nodes[this.id]; },
    closest() { return null; },
  };
  // The fallback flash must never be built from markup.
  Object.defineProperty(node, "innerHTML", {
    set() { throw new Error("innerHTML used"); }, get() { return ""; },
  });
  return node;
}
const main = el("main-content");
const nodes = { "main-content": main };
global.window = { htmx: { config: { responseHandling: "default" } }, setTimeout, addEventListener() {} };
global.navigator = {};
global.document = {
  readyState: "complete",
  body: { addEventListener(n, f) { (listeners[n] = listeners[n] || []).push(f); } },
  addEventListener() {},
  getElementById(id) { return nodes[id] || null; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement() {
    const e = el("");
    // the flash box registers itself in `nodes` once it gets its id
    return new Proxy(e, { set(t, k, v) { t[k] = v; if (k === "id") nodes[v] = t; return true; } });
  },
};
eval(fs.readFileSync(process.argv[2], "utf8"));
const rh = window.htmx.config.responseHandling;
function pick(status) {
  for (const r of rh) { if (new RegExp(r.code).test(String(status))) return r; }
  return { swap: false };
}
function fire(name, detail) { (listeners[name] || []).forEach((f) => f({ detail })); }
function flashText() { const b = nodes["htmx-error-flash"]; return b ? b.textContent : null; }
function swap(status, body, headers, elt) {
  headers = headers || {};
  const r = pick(status);
  const xhr = { status, getResponseHeader(h) {
    for (const k of Object.keys(headers)) { if (k.toLowerCase() === h.toLowerCase()) return headers[k]; }
    return null;
  } };
  const detail = { xhr, serverResponse: body, shouldSwap: !!r.swap, isError: !!r.error,
                   target: "orig", elt: elt || null, requestConfig: { elt: elt || null } };
  fire("htmx:beforeSwap", detail);
  return detail;
}
const out = {};
const MARK = { "HX-Partial": "flash" };
const partial = '<div class="flash" role="alert">That address is already taken.</div>';
for (const s of [409, 422, 429, 400, 403, 404, 413]) {
  out[s] = swap(s, partial, MARK).shouldSwap;
}
// Unmarked responses are never swapped, whatever they look like.
out.unmarked_fragment = swap(409, partial).shouldSwap;
out.unmarked_fragment_flash = flashText();
out.json422 = swap(422, '{"detail": "<b>nope</b>"}', { "Content-Type": "application/json" }).shouldSwap;
out.plain403 = swap(403, "Access denied by WAF <img src=x>", { "Content-Type": "text/plain" }).shouldSwap;
out.plain403_flash = flashText();
out.wrong_marker = swap(409, partial, { "HX-Partial": "something-else" }).shouldSwap;
const e500 = swap(500, "<!doctype html><html><body>err</body></html>", MARK);
out.s500 = e500.shouldSwap;
out.flash500 = flashText();
out.page404 = swap(404, "<!doctype html><html><body>Not found</body></html>").shouldSwap;
console.log(JSON.stringify(out));
"""


def _run_harness(tmp_path, harness_src):
    harness = tmp_path / "harness.js"
    harness.write_text(harness_src)
    result = subprocess.run(
        ["node", str(harness), str(APP_JS)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_app_js_beforeswap_behaviour_under_node(tmp_path):
    out = _run_harness(tmp_path, _NODE_HARNESS)
    # Marked partials (HX-Partial: flash) swap for the expected client errors.
    for code in ("409", "422", "429", "400", "403", "404", "413"):
        assert out[code] is True, code
    # r2 item 1: nothing without the server marker is ever injected — not a
    # bare HTML fragment from a proxy, not JSON, not plain text.
    assert out["unmarked_fragment"] is False
    assert out["json422"] is False
    assert out["plain403"] is False
    assert out["wrong_marker"] is False
    # ...they get the fixed static-text flash (no status/body echoed).
    assert out["unmarked_fragment_flash"] == "Something went wrong. Please try again."
    assert out["plain403_flash"] == "Something went wrong. Please try again."
    assert out["s500"] is False
    assert out["flash500"] == "Something went wrong. Please try again."
    assert out["page404"] is False


def test_app_js_requires_the_partial_header_and_uses_textcontent():
    source = APP_JS.read_text()
    assert 'PARTIAL_HEADER = "HX-Partial"' in source
    assert 'PARTIAL_HEADER_VALUE = "flash"' in source
    assert "getResponseHeader(PARTIAL_HEADER)" in source
    assert "marker !== PARTIAL_HEADER_VALUE" in source
    # The old body-sniffing heuristic is gone and the fallback never echoes the body.
    assert "<html" not in source
    assert "serverResponse" not in source
    flash_fn = source.split("function showErrorFlash", 1)[1].split("\n  }\n", 1)[0]
    assert "box.textContent = text" in flash_fn
    assert "innerHTML" not in flash_fn
    assert "showErrorFlash(GENERIC_ERROR, elt)" in source


_NODE_FLASH_OWNER_HARNESS = r"""
const fs = require("fs");
const listeners = {};
const nodes = {};
function el(id, attrs) {
  return {
    id, attrs: attrs || {}, className: "", textContent: "", isConnected: true,
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    setAttribute(k, v) { this.attrs[k] = v; },
    insertBefore() {}, closest() { return null; },
    remove() { this.isConnected = false; delete nodes[this.id]; },
  };
}
nodes["main-content"] = el("main-content");
global.window = { htmx: { config: {} }, setTimeout, addEventListener() {} };
global.navigator = {};
global.document = {
  readyState: "complete",
  body: { addEventListener(n, f) { (listeners[n] = listeners[n] || []).push(f); } },
  addEventListener() {},
  getElementById(id) { return nodes[id] || null; },
  querySelector() { return null; }, querySelectorAll() { return []; },
  createElement() {
    const e = el("");
    return new Proxy(e, { set(t, k, v) { t[k] = v; if (k === "id") nodes[v] = t; return true; } });
  },
};
eval(fs.readFileSync(process.argv[2], "utf8"));
function pick(status) {
  for (const r of window.htmx.config.responseHandling) { if (new RegExp(r.code).test(String(status))) return r; }
  return { swap: false };
}
function respond(elt, status, headers) {
  headers = headers || {};
  const detail = { elt, requestConfig: { elt }, shouldSwap: !!pick(status).swap, target: "t",
    xhr: { status, getResponseHeader(h) { return headers[h] || null; } } };
  listeners["htmx:beforeSwap"].forEach((f) => f({ detail }));
}
const flash = () => !!nodes["htmx-error-flash"];
const formA = el("form-a", { "hx-post": "/a" });
const formB = el("form-b", { "hx-post": "/b" });
const poll = el("recent-messages", { "hx-get": "/recent", "hx-trigger": "every 10s" });
const out = {};
respond(formA, 502);                       // generic flash owned by form A
out.shown = flash();
respond(poll, 200);                        // 10 s poll succeeds
out.after_poll = flash();
respond(formB, 200);                       // an unrelated form succeeds
out.after_other_form = flash();
respond(formA, 200);                       // the failing form now succeeds
out.after_owner_success = flash();
respond(formA, 403);                       // unmarked 4xx -> flash again
respond(formA, 409, { "HX-Partial": "flash" }); // server partial supersedes it
out.after_owner_partial = flash();
respond(formA, 500);
formA.isConnected = false;                 // owner swapped out of the page
respond(poll, 200);
out.orphan_after_poll = flash();           // still: polls never clear
respond(formB, 200);
out.orphan_after_user_action = flash();    // a user action clears an orphan
listeners["htmx:sendError"].forEach((f) => f({ detail: { elt: formB } }));
respond(poll, 200);
out.network_after_poll = flash();
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_error_flash_is_cleared_only_by_the_element_that_raised_it(tmp_path):
    "Public API."
    out = _run_harness(tmp_path, _NODE_FLASH_OWNER_HARNESS)
    assert out == {
        "shown": True,
        "after_poll": True,
        "after_other_form": True,
        "after_owner_success": False,
        "after_owner_partial": False,
        "orphan_after_poll": True,
        "orphan_after_user_action": False,
        "network_after_poll": True,
    }, out


def test_app_js_scopes_clear_error_flash_to_the_triggering_element():
    source = APP_JS.read_text()
    assert "errorFlashOwner" in source
    assert "function clearErrorFlashFor(elt)" in source
    # No unconditional clear left anywhere.
    assert "clearErrorFlash()" not in source


@pytest.mark.django_db
def test_server_marks_4xx_flash_and_list_partials_with_hx_partial_header():
    """r2 item 1: the positive signal app.js relies on is set by the server."""
    user = _make_user("qadash-hxpartial")
    client = _client(user)
    Inbox.objects.create(user=_make_user("qadash-hxpartial-owner"), routing_key="qadash-hxp-taken")
    # list partial (inbox_rows) — 409 and 422
    taken = client.post(
        reverse("dashboard:inbox-create"), {"routing_key": "qadash-hxp-taken"}, HTTP_HX_REQUEST="true"
    )
    assert taken.status_code == 409 and taken.headers.get("HX-Partial") == "flash"
    bad = client.post(
        reverse("dashboard:inbox-create"), {"routing_key": "-Bad Slug!"}, HTTP_HX_REQUEST="true"
    )
    assert bad.status_code == 422 and bad.headers.get("HX-Partial") == "flash"
    # bare flash partial — 422 (expiry) and 409 (duplicate name)
    exp = client.post(
        reverse("dashboard:api-key-create"), {"name": "x", "expires_in_days": "0"}, HTTP_HX_REQUEST="true"
    )
    assert exp.status_code == 422 and exp.headers.get("HX-Partial") == "flash"
    ok = client.post(reverse("dashboard:api-key-create"), {"name": "hxp"}, HTTP_HX_REQUEST="true")
    assert ok.status_code == 200
    assert "HX-Partial" not in ok.headers  # success responses are not marked
    dup = client.post(reverse("dashboard:api-key-create"), {"name": "hxp"}, HTTP_HX_REQUEST="true")
    assert dup.status_code == 409 and dup.headers.get("HX-Partial") == "flash"
    # webhook_rows list partial — 422
    inbox = Inbox.objects.create(user=user, routing_key="qadash-hxp-hook")
    hook = client.post(
        reverse("dashboard:webhook-save", args=[inbox.public_id]), {"url": "not a url"},
        HTTP_HX_REQUEST="true",
    )
    assert hook.status_code == 422 and hook.headers.get("HX-Partial") == "flash"
    # A 4xx we did NOT render as a partial (Django 404 page) is never marked.
    missing = client.post(
        reverse("dashboard:api-key-revoke", args=["key_doesnotexist"]), HTTP_HX_REQUEST="true"
    )
    assert missing.status_code == 404 and "HX-Partial" not in missing.headers


def test_every_dashboard_partial_render_goes_through_render_partial():
    """The HX-Partial marker is set centrally, so no view may bypass it."""
    for name in ("views.py", "account_views.py", "domain_views.py", "team_views.py"):
        source = (DASH / name).read_text()
        assert not re.search(r"(?<![_\w])render\(\s*request,\s*[\"']dashboard/partials/", source), name
    views = (DASH / "views.py").read_text()
    assert "response = render_partial(request, template, _webhook_page_context(" in views


def test_render_partial_marks_only_4xx():
    from django.test import RequestFactory

    from apps.dashboard.partials import render_partial

    request = RequestFactory().get("/")
    ok = render_partial(request, "dashboard/partials/flash.html", {"flash": "hi"})
    assert "HX-Partial" not in ok.headers
    err = render_partial(request, "dashboard/partials/flash.html", {"flash": "no"}, status=409)
    assert err.headers["HX-Partial"] == "flash"
    with pytest.raises(ValueError):
        render_partial(request, "dashboard/overview.html")


@pytest.mark.django_db
def test_inbox_create_409_and_422_return_the_flash_partial():
    user = _make_user("qadash-inbox-err")
    client = _client(user)
    Inbox.objects.create(user=_make_user("qadash-slug-owner"), routing_key="qadash-taken")
    taken = client.post(
        reverse("dashboard:inbox-create"), {"routing_key": "qadash-taken"}, HTTP_HX_REQUEST="true"
    )
    assert taken.status_code == 409
    assert b'class="flash"' in taken.content and b"already taken" in taken.content
    assert b"<html" not in taken.content
    bad = client.post(
        reverse("dashboard:inbox-create"), {"routing_key": "-Bad Slug!"}, HTTP_HX_REQUEST="true"
    )
    assert bad.status_code == 422
    assert b'class="flash"' in bad.content and b"<html" not in bad.content


def test_error_prone_htmx_forms_have_a_non_destructive_error_target():
    # A bare flash partial must not replace the team list / onboarding card.
    team = (TEMPLATES / "team.html").read_text()
    assert 'data-error-target="#team-result"' in team and 'id="team-result"' in team
    rows = (TEMPLATES / "partials" / "team_rows.html").read_text()
    assert rows.count('data-error-target="#team-result"') == 2
    overview = (TEMPLATES / "overview.html").read_text()
    assert 'data-error-target="#onboarding-error"' in overview and 'id="onboarding-error"' in overview


# --- #1 390px horizontal scroll ----------------------------------------------


def test_layout_grid_column_can_shrink_below_content_min_width():
    css = APP_CSS.read_text()
    assert ".layout{display:grid;grid-template-columns:220px minmax(0,1fr)" in css
    assert ".layout>.sidebar,.layout>.content{min-width:0}" in css
    assert ".table-wrap{overflow-x:auto" in css
    mobile = css.split("@media(max-width:800px){", 1)[1].split("}\n}", 1)[0]
    assert ".layout{grid-template-columns:minmax(0,1fr)}" in mobile
    # The plain `1fr` (== minmax(auto,1fr)) that caused the overflow is gone.
    assert ".layout{grid-template-columns:1fr}" not in mobile


@pytest.mark.django_db
def test_dashboard_tables_are_wrapped_in_a_scroll_container():
    user = _make_user("qadash-tables")
    # ux4 R3-7: an EMPTY list renders an empty-state card, not a table, so give
    # each page one row to render the table we are checking.
    from apps.accounts.services import create_api_key
    from apps.dashboard.demo import deliver_sample_code

    inbox = Inbox.objects.create(user=user, routing_key="qadash_tables_box")
    deliver_sample_code(inbox)
    create_api_key(user, "tables")
    client = _client(user)
    for name in ("dashboard:inboxes", "dashboard:messages", "dashboard:api-keys"):
        body = client.get(reverse(name)).content.decode()
        assert "<table" in body, name
        # every table sits inside a .table-wrap
        assert body.count("<table") == body.count('class="table-wrap"'), name


# --- #2 GET /logout/ ------------------------------------------------------------


@pytest.mark.django_db
def test_get_logout_shows_confirm_and_keeps_session():
    user = _make_user("qadash-logout")
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    response = client.get(reverse("accounts:logout"))
    assert response.status_code == 200
    body = response.content.decode()
    assert "Sign out?" in body
    assert 'method="post"' in body and "csrfmiddlewaretoken" in body
    assert "_auth_user_id" in client.session  # GET never logs out
    # POST without CSRF is still refused; nothing changes.
    assert client.post(reverse("accounts:logout")).status_code == 403
    assert "_auth_user_id" in client.session


@pytest.mark.django_db
def test_get_logout_anonymous_redirects_to_login():
    response = Client().get(reverse("accounts:logout"))
    assert response.status_code == 302
    assert response.url == django_settings.LOGIN_URL
    # Other verbs keep the 405.
    assert Client().put(reverse("accounts:logout")).status_code == 405


# --- #4 double-submit ---------------------------------------------------------


@pytest.mark.django_db
def test_api_key_same_name_within_5s_is_refused():
    user = _make_user("qadash-dup")
    client = _client(user)
    url = reverse("dashboard:api-key-create")
    first = client.post(url, {"name": "qa-dash-key"}, HTTP_HX_REQUEST="true")
    assert first.status_code == 200
    second = client.post(url, {"name": "qa-dash-key"}, HTTP_HX_REQUEST="true")
    assert second.status_code == 409
    assert b"A key with this name was just created" in second.content
    assert ApiKey.objects.filter(user=user, name="qa-dash-key").count() == 1
    # A different name is not affected.
    assert client.post(url, {"name": "other"}, HTTP_HX_REQUEST="true").status_code == 200
    # Outside the 5 s window the same name is allowed again.
    ApiKey.objects.filter(user=user, name="qa-dash-key").update(
        created_at=timezone.now() - timedelta(seconds=6)
    )
    assert client.post(url, {"name": "qa-dash-key"}, HTTP_HX_REQUEST="true").status_code == 200
    assert ApiKey.objects.filter(user=user, name="qa-dash-key").count() == 2


@pytest.mark.django_db
def test_api_key_duplicate_guard_is_per_tenant_and_does_not_burn_rate_limit():
    alice = _make_user("qadash-dup-alice")
    bob = _make_user("qadash-dup-bob")
    url = reverse("dashboard:api-key-create")
    assert _client(alice).post(url, {"name": "ci"}, HTTP_HX_REQUEST="true").status_code == 200
    assert _client(bob).post(url, {"name": "ci"}, HTTP_HX_REQUEST="true").status_code == 200
    # 9 more distinct creates still fit in alice's hourly budget of 10 even
    # after several refused duplicates (duplicates are not charged).
    client = _client(alice)
    for _ in range(5):
        assert client.post(url, {"name": "ci"}, HTTP_HX_REQUEST="true").status_code == 409
    for index in range(9):
        assert client.post(url, {"name": f"k{index}"}, HTTP_HX_REQUEST="true").status_code == 200


@pytest.mark.django_db
def test_api_key_dedupe_ignores_revoked_keys():
    "Public API."
    user = _make_user("qadash-dup-revoked")
    client = _client(user)
    url = reverse("dashboard:api-key-create")
    assert client.post(url, {"name": "redo"}, HTTP_HX_REQUEST="true").status_code == 200
    first = ApiKey.objects.get(user=user, name="redo")
    revoke = client.post(
        reverse("dashboard:api-key-revoke", args=[first.public_id]), HTTP_HX_REQUEST="true"
    )
    assert revoke.status_code == 200
    again = client.post(url, {"name": "redo"}, HTTP_HX_REQUEST="true")
    assert again.status_code == 200, again.content
    assert ApiKey.objects.filter(user=user, name="redo").count() == 2
    assert ApiKey.objects.filter(user=user, name="redo", revoked_at__isnull=True).count() == 1
    # A live duplicate is still refused.
    assert client.post(url, {"name": "redo"}, HTTP_HX_REQUEST="true").status_code == 409


def _race_same_name(users, name, monkeypatch):
    """r2 item 3: fire one Create-key POST per user at the same instant from
    separate threads (each thread gets its own DB connection) and return the
    status codes. The window between the dedupe check and the INSERT is
    widened deterministically by slowing the rate-limit charge (which runs
    between the two), so without the tenant row lock both requests pass the
    check and both mint a key."""
    import threading
    import time

    from django.db import connection

    from apps.dashboard import views as dashboard_views

    real_increment = dashboard_views.increment_rate_limit

    def slow_increment(*args, **kwargs):
        result = real_increment(*args, **kwargs)
        time.sleep(0.3)
        return result

    monkeypatch.setattr(dashboard_views, "increment_rate_limit", slow_increment)
    clients = [_client(user) for user in users]
    barrier = threading.Barrier(len(clients))
    statuses, errors = [], []

    def worker(client):
        try:
            barrier.wait(timeout=10)
            response = client.post(
                reverse("dashboard:api-key-create"), {"name": name}, HTTP_HX_REQUEST="true"
            )
            statuses.append(response.status_code)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(repr(exc))
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(c,)) for c in clients]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    return sorted(statuses)


@pytest.mark.django_db(transaction=True)
def test_api_key_dedupe_is_race_safe_under_real_concurrency(monkeypatch):
    user = _make_user("qadash-race")
    statuses = _race_same_name([user, user], "race-key", monkeypatch)
    assert statuses == [200, 409], statuses
    assert ApiKey.objects.filter(user=user, name="race-key").count() == 1


@pytest.mark.django_db(transaction=True)
def test_api_key_dedupe_race_owner_and_member_share_the_tenant(monkeypatch, settings):
    from apps.accounts.models import TeamMembership

    settings.ENABLE_TEAMS = True
    owner = _make_user("qadash-race-owner", plan=AccountPlan.Plan.BUSINESS)
    member = _make_user("qadash-race-member")
    TeamMembership.objects.create(account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER)
    statuses = _race_same_name([owner, member], "team-race-key", monkeypatch)
    assert statuses == [200, 409], statuses
    keys = ApiKey.objects.filter(name="team-race-key")
    assert keys.count() == 1
    assert keys.get().user_id == owner.pk  # FK'd to the tenant either way


_MUTATING_HTMX_FORMS = {
    "api_keys.html": ["dashboard:api-key-create"],
    "inboxes.html": ["dashboard:inbox-create"],
    "team.html": ["dashboard:team-invite-create"],
    "overview.html": ["dashboard:api-key-onboarding"],
    "partials/webhook_rows.html": [
        "dashboard:webhook-save", "dashboard:webhook-rotate", "dashboard:webhook-disable",
        "dashboard:webhook-test", "dashboard:webhook-delete",
    ],
    "domains.html": ["dashboard:domain-create", "dashboard:domain-verify", "dashboard:domain-delete"],
    "partials/api_key_rows.html": ["dashboard:api-key-revoke"],
    "partials/inbox_rows.html": ["dashboard:inbox-delete", "dashboard:inbox-rename"],
    "partials/team_rows.html": ["dashboard:team-member-revoke", "dashboard:team-invite-revoke"],
}


def test_every_htmx_post_form_disables_its_button_while_in_flight():
    for name in _MUTATING_HTMX_FORMS:
        source = (TEMPLATES / name).read_text()
        forms = re.findall(r"<form\b[^>]*>", source)
        htmx_forms = [f for f in forms if "hx-post=" in f]
        assert htmx_forms, name
        for tag in htmx_forms:
            assert 'hx-disabled-elt="find button"' in tag, (name, tag)
    # Every hx-post form anywhere in the dashboard templates is covered.
    for path in TEMPLATES.rglob("*.html"):
        for tag in re.findall(r"<form\b[^>]*>", path.read_text()):
            if "hx-post=" in tag:
                assert "hx-disabled-elt" in tag, (path.name, tag)


# r2 item 2: plain (non-htmx) POST forms that are TRULY idempotent and may
# therefore skip data-disable-on-submit. Keyed by (template, url name).
_IDEMPOTENT_PLAIN_POST_FORMS = {
    # Sign-out: a second POST just ends an already-ended session.
    ("base.html", "accounts:logout"),
}


def _plain_post_forms():
    """Yield (relative template path, url name, <form> tag) for every
    method=post form in the dashboard templates that is NOT an hx-post form."""
    for path in sorted(TEMPLATES.rglob("*.html")):
        rel = path.relative_to(TEMPLATES).as_posix()
        for tag in re.findall(r"<form\b[^>]*>", path.read_text()):
            if not re.search(r"""method=["']post["']""", tag, re.I):
                continue
            if "hx-post=" in tag:
                continue  # covered by hx-disabled-elt (test above)
            url = re.search(r"""\{%\s*url\s+'([^']+)'""", tag)
            yield rel, (url.group(1) if url else ""), tag


def test_every_plain_post_form_disables_on_submit_or_is_allowlisted():
    seen = []
    for rel, url_name, tag in _plain_post_forms():
        seen.append((rel, url_name))
        if (rel, url_name) in _IDEMPOTENT_PLAIN_POST_FORMS:
            continue
        assert "data-disable-on-submit" in tag, (rel, url_name, tag)
    
    for expected in (
        ("partials/message_detail_panel.html", "dashboard:message-reply"),
        ("partials/message_detail_panel.html", "dashboard:message-forward"),
        ("compose.html", "dashboard:compose"),
        ("settings.html", "dashboard:settings-password"),
        ("settings.html", "dashboard:settings-delete"),
        ("base.html", "dashboard:account-switch"),
        ("base.html", "dashboard:account-new"),
        ("partials/message_rows.html", "dashboard:message-mark-read"),
        ("partials/message_rows.html", "dashboard:message-mark-unread"),
        ("team_accept.html", "dashboard:team-accept-confirm"),
        ("billing.html", "dashboard:billing-checkout"),
        ("billing.html", "dashboard:billing-portal"),
    ):
        assert expected in seen, expected
    # Every allowlist entry is real (no stale exemptions).
    for entry in _IDEMPOTENT_PLAIN_POST_FORMS:
        assert entry in seen, entry
    assert "form[data-disable-on-submit]" in APP_JS.read_text()


@pytest.mark.django_db
def test_reply_and_forward_forms_render_with_the_guard():
    user = _make_user("qadash-reply-guard")
    inbox = Inbox.objects.create(user=user, routing_key="qadash-reply-guard-box")
    import hashlib

    raw = b"Subject: hi\r\n\r\nhello"
    digest = hashlib.sha256(raw).hexdigest()
    message = Message.objects.create(
        inbox=inbox, dedupe_key=digest, envelope_from="s@example.test",
        envelope_recipient=inbox.address, header_from="s@example.test", header_to=[inbox.address],
        header_cc=[], subject="hi", text_body="hello", raw_bytes=raw,
        raw_sha256=digest, size_bytes=len(raw), ingest_status=Message.IngestStatus.PARSED,
    )
    source = (TEMPLATES / "partials" / "message_detail_panel.html").read_text()
    from django.template import engines

    html = engines["django"].from_string(source).render(
        {"message": message, "outbound_ui": True, "normalized_json": "{}"}
    )
    reply = re.search(r"<form[^>]*/reply/[^>]*>", html).group(0)
    forward = re.search(r"<form[^>]*/forward/[^>]*>", html).group(0)
    assert "data-disable-on-submit" in reply and "data-disable-on-submit" in forward


# --- #5 expiry message ---------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["-5", "0", "999999", "3651", "abc"])
def test_api_key_invalid_expiry_reports_expiry_error(value):
    user = _make_user(f"qadash-exp-{value.strip('-')}")
    response = _client(user).post(
        reverse("dashboard:api-key-create"),
        {"name": "qa", "expires_in_days": value},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 422
    assert b"Expiry must be between 1 and 3650 days." in response.content
    assert b"Name is required" not in response.content
    assert not ApiKey.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_api_key_invalid_name_still_reports_name_error():
    user = _make_user("qadash-badname")
    response = _client(user).post(
        reverse("dashboard:api-key-create"), {"name": "x" * 81}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 422
    assert b"Name is required and must be at most 80 characters." in response.content


# --- #6/#9 confirm on destructive actions --------------------------------------


@pytest.mark.django_db
def test_revoke_key_and_delete_inbox_ask_for_confirmation():
    user = _make_user("qadash-confirm")
    client = _client(user)
    client.post(reverse("dashboard:api-key-create"), {"name": "to-revoke"}, HTTP_HX_REQUEST="true")
    Inbox.objects.create(user=user, routing_key="qadash-confirm-box")
    keys = client.get(reverse("dashboard:api-keys")).content.decode()
    revoke = re.search(r"<form[^>]*api-keys/[^\"]*/revoke/[^>]*>", keys).group(0)
    assert 'hx-confirm="Revoke the API key &quot;to-revoke&quot;?' in revoke
    inboxes = client.get(reverse("dashboard:inboxes")).content.decode()
    delete = re.search(r"<form[^>]*/delete/[^>]*>", inboxes).group(0)
    assert "hx-confirm=\"Delete the inbox qadash-confirm-box@" in delete
    assert "all of its messages" in delete


# --- #7 OTP styling in the list -------------------------------------------------


@pytest.mark.django_db
def test_messages_list_renders_otp_as_highlighted_code():
    import hashlib

    user = _make_user("qadash-otp")
    inbox = Inbox.objects.create(user=user, routing_key="qadash-otp-box")
    raw = b"Subject: code\r\n\r\nYour code is 583917"
    digest = hashlib.sha256(raw).hexdigest()
    message = Message.objects.create(
        inbox=inbox, dedupe_key=digest, envelope_from="s@example.test",
        envelope_recipient=inbox.address, header_from="s@example.test", header_to=[inbox.address],
        header_cc=[], subject="code", text_body="Your code is 583917", raw_bytes=raw,
        raw_sha256=digest, size_bytes=len(raw), ingest_status=Message.IngestStatus.PARSED,
        detected_otp="583917",
    )
    client = _client(user)
    listing = client.get(reverse("dashboard:messages")).content.decode()
    assert '<code class="otp">583917</code>' in listing
    detail = client.get(reverse("dashboard:message-detail", args=[message.public_id])).content.decode()
    assert '<code id="otp-value" class="otp">583917</code>' in detail
    assert "code.otp{" in APP_CSS.read_text()


# --- #10 skip link ---------------------------------------------------------------


@pytest.mark.django_db
def test_dashboard_has_skip_to_content_link_as_first_focusable():
    user = _make_user("qadash-skip")
    body = _client(user).get(reverse("dashboard:overview")).content.decode()
    after_body = body.split("<body>", 1)[1]
    first_focusable = re.search(r"<(a|button|input|select|textarea)\b[^>]*>", after_body).group(0)
    assert 'class="skip-link"' in first_focusable and 'href="#main-content"' in first_focusable
    assert "Skip to content" in after_body.split("</a>", 1)[0]
    assert '<main class="content" id="main-content"' in body
    css = APP_CSS.read_text()
    assert ".skip-link{" in css and ".skip-link:focus" in css
