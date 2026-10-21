"Public API."

import re
from urllib.parse import urlsplit

import pytest
from django.test import Client
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.services import create_api_key
from apps.dashboard import demo
from apps.dashboard.tests.test_uxfix_r2 import _client, _code_block, _run_snippet, make_user
from apps.inboxes.models import Inbox

TRY = reverse("dashboard:try-it")





@pytest.mark.django_db
@pytest.mark.parametrize("url_name", ["dashboard:root", "dashboard:compare-mailpit"])
def test_try_links_carry_the_validated_ref(url_name):
    page = Client().get(reverse(url_name), {"ref": "hn"}).content.decode()
    assert f'href="{TRY}?ref=hn"' in page
    assert f'href="{TRY}"' not in page.split('class="site-footer"', 1)[0]
    plain = Client().get(reverse(url_name)).content.decode()
    assert f'href="{TRY}?ref=' not in plain
    assert f'href="{TRY}"' in plain


@pytest.mark.django_db
def test_try_link_never_echoes_an_invalid_ref():
    page = Client().get(reverse("dashboard:root"), {"ref": "<script>"}).content.decode()
    assert "?ref=" not in page
    assert page.count(f'href="{TRY}"') >= 2





@pytest.mark.django_db
def test_try_page_has_a_start_free_next_step_inside_the_live_card():
    page = Client().get(TRY, {"ref": "mcp"}).content.decode()
    card = page.split('id="try-inbox"', 1)[1].split('id="equivalent"', 1)[0]
    assert 'id="try-next"' in card
    assert '<a class="button" href="/register/?ref=mcp">Start free</a>' in card
    # the footnote link says "Start free" like every other public signup link
    assert ">sign up</a>" not in page





@pytest.mark.django_db(transaction=True)
def test_snippet_sends_wait_since_back_so_a_code_between_calls_is_printed():
    """Behavioural, against the real /wait view: the first call ends with 204;
    the code lands AFTER that response and BEFORE the next call starts. The
    second call must still return it (since = the first call's cutoff)."""
    user = make_user("act-gap")
    inbox = Inbox.objects.create(user=user, routing_key="act_gap_box", label="gap")
    page = _client(user).post(reverse("dashboard:api-key-onboarding"), HTTP_HX_REQUEST="true")
    python = _code_block(page.content.decode(), "snippet-python")
    assert python.startswith("# pip install requests\n")

    calls, delivered = [], []

    def requests_get(url, params=None, headers=None, timeout=None):
        calls.append(dict(params))
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=headers["Authorization"])
        resp = api.get(urlsplit(url).path, {**params, "timeout": 1})  # keep the test fast
        if resp.status_code == 204 and not delivered:
            delivered.append(demo.deliver_sample_code(inbox).message)  # lands in the gap
        if len(calls) > 3:
            raise AssertionError("the code landing between two calls was never returned")
        return type(
            "Resp",
            (),
            {
                "status_code": resp.status_code,
                "headers": resp.headers,
                "json": staticmethod(resp.json),
                "raise_for_status": staticmethod(lambda: None),
            },
        )

    printed = _run_snippet(python, requests_get=requests_get)
    delivered[0].refresh_from_db()
    assert printed[-1] == delivered[0].otp
    assert "since" not in calls[0]
    assert calls[1]["since"]  # the server's Mailsocket-Wait-Since from call 1





@pytest.mark.django_db
def test_onboarding_card_lists_run_then_send_steps():
    user = make_user("act-steps")
    Inbox.objects.create(user=user, routing_key="act_steps_box")
    page = _client(user).get(reverse("dashboard:overview")).content.decode()
    card = page.split('id="onboarding-card"', 1)[1].split("</section>", 1)[0]
    steps = re.findall(r"<li>(.*?)</li>", card.split('class="onboarding-steps"', 1)[1].split("</ol>", 1)[0])
    assert len(steps) == 3
    assert "API key" in steps[0] and "Run the snippet" in steps[1]
    assert "<strong>Send a test code</strong>" in steps[2] and "prints the code" in steps[2]


@pytest.mark.django_db
@pytest.mark.parametrize("state", ["onboarding", "overview"])
def test_dashboard_snippet_links_playwright_sdk_and_mcp(state):
    user = make_user(f"act-links-{state}")
    Inbox.objects.create(user=user, routing_key=f"act_links_{state}")
    client = _client(user)
    if state == "onboarding":
        page = client.post(reverse("dashboard:api-key-onboarding"), HTTP_HX_REQUEST="true").content.decode()
    else:
        create_api_key(user, "k")
        page = client.get(reverse("dashboard:overview")).content.decode()
    guide = reverse("dashboard:guide", kwargs={"slug": "playwright"})
    docs = reverse("dashboard:docs")
    for href in (guide, f"{docs}#sdks", f"{docs}#mcp"):
        assert f'href="{href}"' in page, href
    assert Client().get(guide).status_code == 200
    docs_page = Client().get(docs).content.decode()
    assert 'id="sdks"' in docs_page and 'id="mcp"' in docs_page


@pytest.mark.django_db
def test_inboxes_page_snippet_has_no_guide_links():
    """The per-inbox Connect snippets stay compact (links only on Overview)."""
    user = make_user("act-inboxes")
    Inbox.objects.create(user=user, routing_key="act_inboxes_box")
    page = _client(user).get(reverse("dashboard:inboxes")).content.decode()
    assert "Using Playwright, an SDK or an AI agent?" not in page
