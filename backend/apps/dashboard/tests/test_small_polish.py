"Public API."

import hashlib
import re

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.template import Context, Template
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan
from apps.dashboard.templatetags.address_tags import wbr_breaks
from apps.domains.models import Domain
from apps.inboxes.models import Inbox, Message

pytestmark = pytest.mark.django_db(transaction=True)


def make_user(name):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user, plan=AccountPlan.Plan.BUSINESS)
    return user


def make_message(inbox, marker):
    raw = f"Subject: {marker}\r\n\r\nbody {marker}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    return Message.objects.create(
        inbox=inbox,
        dedupe_key=digest,
        envelope_from="sender@example.test",
        envelope_recipient=inbox.address,
        header_from=f"Sender <sender-{marker}@example.test>",
        header_to=[inbox.address],
        header_cc=[],
        subject=marker,
        text_body=f"text {marker}",
        html_body=f"<p>{marker}</p>",
        raw_bytes=raw,
        raw_sha256=digest,
        size_bytes=len(raw),
        ingest_status=Message.IngestStatus.PARSED,
    )


def seed(name, rows):
    """`rows` custom-domain inboxes, one message each (distinct Inbox instances per row)."""
    user = make_user(name)
    domain = Domain.objects.create(user=user, name=f"{name}.example.com")
    for i in range(rows):
        inbox = Inbox.objects.create(user=user, routing_key=f"{name}-box-{i}", label=f"box {i}", domain=domain)
        make_message(inbox, f"{name}-m{i}")
    client = Client()
    client.force_login(user)
    return client, domain


def count_queries(client, url, params=None):
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url, params or {})
    assert response.status_code == 200
    return len(ctx.captured_queries), response


def domain_lookups(ctx_queries):
    return [q["sql"] for q in ctx_queries if q["sql"].lstrip().startswith('SELECT') and 'FROM "domains_domain"' in q["sql"]]


@pytest.mark.parametrize(
    "url_name,params",
    [
        ("dashboard:messages", None),
        ("dashboard:messages", {"view": "grouped", "group_by": "sender"}),
        ("dashboard:overview", None),
        ("dashboard:recent-messages", None),
        ("dashboard:inboxes", None),
    ],
)
def test_query_count_does_not_grow_with_custom_domain_rows(url_name, params):
    small_client, small_domain = seed("npone-small", 2)
    big_client, big_domain = seed("npone-big", 8)
    url = reverse(url_name)

    small_count, small_resp = count_queries(small_client, url, params)
    big_count, big_resp = count_queries(big_client, url, params)

    assert big_count == small_count, (url_name, params, small_count, big_count)
    # Sanity: the custom domain is actually rendered where addresses render.
    if not params:
        assert f"@{big_domain.name}".encode() in big_resp.content


def test_message_detail_resolves_inbox_domain_in_the_same_query():
    client, domain = seed("npone-detail", 1)
    message = Message.objects.get(inbox__domain=domain)
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(reverse("dashboard:message-detail", args=[message.public_id]))
    assert response.status_code == 200
    # No standalone `FROM "domains_domain"` lookup (the domain arrives via the
    # message query's JOIN); then touching inbox.address costs zero queries.
    message_qs = [q["sql"] for q in ctx.captured_queries if 'FROM "inboxes_message"' in q["sql"]]
    assert any('"domains_domain"' in sql for sql in message_qs)
    assert domain_lookups(ctx.captured_queries) == []


def test_webhooks_page_does_no_per_row_domain_lookup():
    # Total count is NOT asserted here: the page also does one reverse
    # OneToOne `inbox.webhook` lookup per row (pre-existing, out of this
    
    small_client, _ = seed("npone-wh-small", 2)
    big_client, _ = seed("npone-wh-big", 8)
    for client in (small_client, big_client):
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(reverse("dashboard:webhooks"))
        assert response.status_code == 200
        assert domain_lookups(ctx.captured_queries) == []


# --- wbr_breaks filter -------------------------------------------------------


def test_wbr_breaks_inserts_wbr_after_separators_only():
    assert wbr_breaks("signup-flow_e2e.run") == "signup-<wbr>flow_<wbr>e2e.<wbr>run"
    assert wbr_breaks("plainkey") == "plainkey"
    assert wbr_breaks(None) == ""


def test_wbr_breaks_escapes_html_before_marking_safe():
    out = wbr_breaks('<script>alert("x")</script>-a')
    assert "<script>" not in out
    assert out == "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;-<wbr>a"


def test_wbr_breaks_in_template_is_not_double_escaped():
    rendered = Template("{% load address_tags %}{{ v|wbr_breaks }}").render(Context({"v": "a-b&c"}))
    assert rendered == "a-<wbr>b&amp;c"


def test_address_partial_keeps_plain_address_for_title_and_copy():
    rendered = Template(
        "{% include 'dashboard/partials/address.html' with a_local=k a_full=f a_id='addr-x' only %}"
    ).render(Context({"k": "long-key_v2", "f": "long-key_v2@in.example.test"}))
    assert 'title="long-key_v2@in.example.test"' in rendered
    assert '<span class="addr2-local">long-<wbr>key_<wbr>v2</span>' in rendered
    # Copy reads #addr-x textContent; stripping <wbr> must leave the exact address.
    code = rendered.split('id="addr-x">', 1)[1].split("</code>", 1)[0]
    assert re.sub(r"<[^>]+>", "", code) == "long-key_v2@in.example.test"
