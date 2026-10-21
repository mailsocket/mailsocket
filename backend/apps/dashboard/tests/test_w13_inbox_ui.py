"Public API."
import hashlib
import re

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AccountPlan, TeamMembership
from apps.domains.models import Domain
from apps.inboxes.models import Inbox, Message
from apps.outbound.models import OutboundEmail
from apps.outbound.tests.test_outbound import eligible_domain, make_user

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _enable_outbound(settings):
    settings.ENABLE_OUTBOUND = True


def login(user):
    client = Client()
    client.force_login(user)
    return client


def make_message(inbox, marker, *, otp=None, subject=None, header_from=None, read_at=None, provider_message_id=""):
    raw = f"Subject: {subject or marker}\r\n\r\nbody {marker}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    return Message.objects.create(
        inbox=inbox,
        dedupe_key=digest + provider_message_id,
        provider_message_id=provider_message_id,
        envelope_from="sender@example.test",
        envelope_recipient=inbox.address,
        header_from=header_from or f"Sender <sender-{marker}@example.test>",
        header_to=[inbox.address],
        header_cc=[],
        subject=subject or marker,
        text_body=f"text {marker}",
        html_body=f"<p>{marker}</p>",
        otp=otp,
        otp_confidence=0.9 if otp else None,
        raw_bytes=raw,
        raw_sha256=digest,
        size_bytes=len(raw),
        ingest_status=Message.IngestStatus.PARSED,
        read_at=read_at,
    )


def _subjects(response):
    return {message.subject for message in response.context["messages"]}


# --- rendered-id helpers (post-gate: read the HTML, not context) -----------

_DETAIL_HREF_RE = re.compile(rb'/app/messages/([A-Za-z0-9_\-]+)/"')


def _rendered_message_ids(response):
    """Every message-detail href's public_id present in the rendered list/
    group HTML. This is what a human/browser actually sees — a view that
    returns 200 with an empty/wrong queryset but a passing context dict
    cannot fool this oracle."""
    return {m.decode() for m in _DETAIL_HREF_RE.findall(response.content)}


def _rendered_group_counts(response, group_by):
    """Parse the rendered grouped table rows: {key: count} straight out of
    dashboard/partials/message_groups.html's <td>key</td><td>count</td>."""
    html = response.content.decode()
    rows = re.findall(r"<tr><td>(.*?)</td><td>(\d+)</td><td>", html)
    return {key: int(count) for key, count in rows}


# --- (1) unread filter --------------------------------------------------


def test_unread_filter_read_true_false_absent_and_cursor_replay_422():
    user = make_user("w13_unread")
    inbox = Inbox.objects.create(user=user, routing_key="w13unread")
    make_message(inbox, "read-one", read_at=timezone.now())
    unread_msg = make_message(inbox, "unread-one")

    client = login(user)

    resp = client.get(reverse("dashboard:messages"), {"read": "false"})
    assert _subjects(resp) == {"unread-one"}

    resp = client.get(reverse("dashboard:messages"), {"read": "true"})
    assert _subjects(resp) == {"read-one"}

    resp = client.get(reverse("dashboard:messages"))
    assert _subjects(resp) == {"read-one", "unread-one"}

    # Mint a cursor under read=false (force PAGE_SIZE=1 window via has_more
    # by creating enough rows is unnecessary here — decode requires the
    # SAME filter signature; replaying under a different `read` 422s).
    from apps.dashboard.views import _encode_cursor

    fake_request = type("R", (), {"session": {}})()
    fake_request.user = user
    cursor = _encode_cursor(fake_request, unread_msg, "inbox:all|domain:all|read=false")
    resp = client.get(reverse("dashboard:messages"), {"read": "true", "cursor": cursor})
    assert resp.status_code == 422
    resp = client.get(reverse("dashboard:messages"), {"cursor": cursor})
    assert resp.status_code == 422


# --- (2) group-by sender scoped: RENDERED ids + cursor ABSENT ---------------


def test_group_by_sender_scoped_no_cursor_stale_cursor_inert():
    alice = make_user("w13_grp_sender_a")
    bob = make_user("w13_grp_sender_b")
    alice_inbox = Inbox.objects.create(user=alice, routing_key="w13gsa")
    bob_inbox = Inbox.objects.create(user=bob, routing_key="w13gsb")

    a1 = make_message(alice_inbox, "a1", header_from="Same Sender <same@sender.test>")
    a2 = make_message(alice_inbox, "a2", header_from="Same Sender <same@sender.test>")
    b1 = make_message(bob_inbox, "b1", header_from="Same Sender <same@sender.test>")

    client = login(alice)
    resp = client.get(reverse("dashboard:messages"), {"group_by": "sender", "view": "grouped"})
    assert resp.status_code == 200
    groups = resp.context["groups"]
    assert len(groups) == 1
    assert groups[0]["count"] == 2

    # H4: read RENDERED ids — the grouped template renders only the LATEST
    # message per group (message_groups.html's "Latest" column), so the
    # rendered set is a proper subset of Alice's own ids — never empty,
    # never containing Bob's foreign id.
    rendered_ids = _rendered_message_ids(resp)
    assert rendered_ids, "expected at least one rendered message id in the grouped view"
    assert rendered_ids.issubset({a1.public_id, a2.public_id})
    assert b1.public_id not in rendered_ids

    # Per-group rendered count must be correct too.
    group_counts = _rendered_group_counts(resp, "sender")
    assert list(group_counts.values()) == [2]

    # cursor= must be ABSENT from the grouped response entirely (not "A or
    # B" — the literal substring must not appear anywhere).
    assert b"cursor=" not in resp.content
    assert resp.context.get("next_cursor") is None

    # A stale list cursor pasted into the grouped URL is inert (never
    # decoded, never errors, and the RENDERED set is unchanged).
    resp2 = client.get(
        reverse("dashboard:messages"),
        {"group_by": "sender", "view": "grouped", "cursor": "garbage-not-a-real-cursor"},
    )
    assert resp2.status_code == 200
    assert resp2.context["groups"][0]["count"] == 2
    assert _rendered_message_ids(resp2) == rendered_ids
    assert b"cursor=" not in resp2.content


# --- (3) group-by subject scoped + >200 cap notice -----------------------


def test_group_by_subject_scoped_and_over_cap_notice():
    alice = make_user("w13_grp_subject_a")
    bob = make_user("w13_grp_subject_b")
    inbox = Inbox.objects.create(user=alice, routing_key="w13gsubj")
    bob_inbox = Inbox.objects.create(user=bob, routing_key="w13gsubjb")

    dup_msgs = [make_message(inbox, f"dup-{i}", subject="Same Subject") for i in range(3)]
    other_msg = make_message(inbox, "other", subject="Different Subject")
    # H5: foreign-owned (tenant B) decoy with the SAME subject.
    decoy = make_message(bob_inbox, "decoy", subject="Same Subject")

    client = login(alice)
    resp = client.get(reverse("dashboard:messages"), {"group_by": "subject", "view": "grouped"})
    groups = {g["key"]: g["count"] for g in resp.context["groups"]}
    assert groups == {"Same Subject": 3, "Different Subject": 1}
    assert resp.context["truncated"] is False

    # Rendered ids: the grouped template shows only the LATEST message per
    # group, so the rendered set is a subset of Alice's own ids (one per
    # group: "Same Subject" and "Different Subject") — never Bob's decoy.
    rendered_ids = _rendered_message_ids(resp)
    alice_ids = {m.public_id for m in dup_msgs} | {other_msg.public_id}
    assert rendered_ids, "expected at least one rendered message id in the grouped view"
    assert rendered_ids.issubset(alice_ids)
    assert decoy.public_id not in rendered_ids

    # cursor absent + stale-list-cursor inert.
    assert b"cursor=" not in resp.content
    resp_stale = client.get(
        reverse("dashboard:messages"),
        {"group_by": "subject", "view": "grouped", "cursor": "garbage-not-a-real-cursor"},
    )
    assert resp_stale.status_code == 200
    assert _rendered_message_ids(resp_stale) == rendered_ids

    from apps.dashboard.views import GROUPED_LIMIT

    for i in range(GROUPED_LIMIT + 5):
        make_message(inbox, f"bulk-{i}", subject=f"Bulk {i}")
    resp2 = client.get(reverse("dashboard:messages"), {"group_by": "subject", "view": "grouped"})
    assert resp2.context["truncated"] is True
    assert b"newest 200" in resp2.content


# --- (4) search scoped: RENDERED ids, anti-collapse on duplicate subjects --


def test_search_q_scoped_bound_to_cursor_and_truncated():
    alice = make_user("w13_search_a")
    bob = make_user("w13_search_b")
    alice_inbox = Inbox.objects.create(user=alice, routing_key="w13sa")
    bob_inbox = Inbox.objects.create(user=bob, routing_key="w13sb")

    subj_match = make_message(alice_inbox, "subj-match", subject="find-me-otp code")
    from_match = make_message(alice_inbox, "from-match", header_from="Sender <find-me-otp@bank.test>")
    make_message(alice_inbox, "no-match", subject="unrelated")
    # H6: Bob has a message with the SAME matching subject as Alice's — if
    # the view ever collapsed cross-tenant ids into a subject set, this
    # would make Bob's query "pass" against Alice's id too.
    bob_match = make_message(bob_inbox, "bob-match", subject="find-me-otp code")

    client = login(alice)
    resp = client.get(reverse("dashboard:messages"), {"q": "find-me-otp"})
    alice_rendered = _rendered_message_ids(resp)
    assert alice_rendered == {subj_match.public_id, from_match.public_id}
    assert bob_match.public_id not in alice_rendered

    bob_client = login(bob)
    resp_bob = bob_client.get(reverse("dashboard:messages"), {"q": "find-me-otp"})
    bob_rendered = _rendered_message_ids(resp_bob)
    # ONLY Bob's own id, and NOT Alice's duplicate-subject id — a set-based
    # oracle over {"find-me-otp code"} could not tell these apart, rendered
    # ids can.
    assert bob_rendered == {bob_match.public_id}
    assert subj_match.public_id not in bob_rendered
    assert from_match.public_id not in bob_rendered

    # q bound into the cursor signature: replay under a different q -> 422.
    from apps.dashboard.views import _encode_cursor

    fake_request = type("R", (), {})()
    fake_request.user = alice
    msg = Message.objects.filter(inbox=alice_inbox, subject="find-me-otp code").get()
    cursor = _encode_cursor(fake_request, msg, "inbox:all|domain:all|q=find-me-otp")
    resp422 = client.get(reverse("dashboard:messages"), {"q": "different", "cursor": cursor})
    assert resp422.status_code == 422

    # q > 200 chars truncated, not a 500.
    long_q = "x" * 500
    resp_long = client.get(reverse("dashboard:messages"), {"q": long_q})
    assert resp_long.status_code == 200
    assert resp_long.context["filters"]["q"] == "x" * 200


# --- (5) domain filter scoped: RENDERED ids ---------------------------------


def test_domain_filter_scoped_shared_and_foreign_404():
    alice = make_user("w13_domain_a")
    bob = make_user("w13_domain_b")
    domain_a = eligible_domain(alice, "w13-domain-a.example.com")
    domain_b = eligible_domain(bob, "w13-domain-b.example.com")

    byod_inbox = Inbox.objects.create(user=alice, routing_key="w13da", domain=domain_a)
    shared_inbox = Inbox.objects.create(user=alice, routing_key="w13dashared")

    byod_msg = make_message(byod_inbox, "byod-msg", subject="byod")
    shared_msg = make_message(shared_inbox, "shared-msg", subject="shared")

    client = login(alice)

    resp = client.get(reverse("dashboard:messages"), {"domain_id": domain_a.public_id})
    rendered = _rendered_message_ids(resp)
    assert byod_msg.public_id in rendered
    assert shared_msg.public_id not in rendered

    resp_shared = client.get(reverse("dashboard:messages"), {"domain_id": "shared"})
    rendered_shared = _rendered_message_ids(resp_shared)
    assert shared_msg.public_id in rendered_shared
    assert byod_msg.public_id not in rendered_shared

    # qafix2 #14: B's domain id under A's session is ignored exactly like an
    # unknown id (filter falls back to "all"; no 404 page, no oracle).
    resp_foreign = client.get(reverse("dashboard:messages"), {"domain_id": domain_b.public_id})
    resp_unknown = client.get(reverse("dashboard:messages"), {"domain_id": "domain_doesnotexist"})
    assert resp_foreign.status_code == resp_unknown.status_code == 200
    assert _rendered_message_ids(resp_foreign) == _rendered_message_ids(resp_unknown)
    assert domain_b.name.encode() not in resp_foreign.content


# --- (6) outbound UI present-only-when-eligible -----------------------------


def test_outbound_ui_present_only_when_eligible():
    owner = make_user("w13_ui_owner", plan=AccountPlan.Plan.BUSINESS)
    domain = eligible_domain(owner, "w13-ui-eligible.example.com")
    sendable_inbox = Inbox.objects.create(user=owner, routing_key="w13uisend", domain=domain)
    shared_inbox = Inbox.objects.create(user=owner, routing_key="w13uishared")
    message = make_message(sendable_inbox, "m1")

    client = login(owner)
    resp = client.get(reverse("dashboard:messages"))
    assert resp.status_code == 200
    assert resp.context["outbound_ui"] is True
    assert b"Compose" in resp.content

    resp_detail = client.get(reverse("dashboard:message-detail", kwargs={"public_id": message.public_id}))
    assert b"Reply" in resp_detail.content
    assert b"Forward" in resp_detail.content

    resp_compose = client.get(reverse("dashboard:compose"))
    assert resp_compose.status_code == 200
    inbox_ids = {inbox.public_id for inbox in resp_compose.context["sendable_inboxes"]}
    assert inbox_ids == {sendable_inbox.public_id}
    assert shared_inbox.public_id not in resp_compose.content.decode()

    # H8: compose inbox-select eligibility decoys. Two custom-domain inboxes
    # that EACH independently fail one leg of the owned+verified+DKIM-verified
    # invariant (_inbox_is_sendable) must not appear in the rendered <select>,
    # while the fully-eligible custom-domain inbox above (sendable_inbox) DOES
    # -- asserted by rendered option value (not context alone), mirroring the
    # existing shared-domain (domain__isnull=True) decoy (shared_inbox).
    unverified_domain_status = eligible_domain(owner, "w13-ui-unverified-status.example.com")
    unverified_domain_status.status = Domain.Status.PENDING_DNS
    unverified_domain_status.save(update_fields=["status"])
    unverified_status_inbox = Inbox.objects.create(
        user=owner, routing_key="w13uiunverifiedstatus", domain=unverified_domain_status
    )

    unverified_domain_dkim = eligible_domain(owner, "w13-ui-unverified-dkim.example.com")
    unverified_domain_dkim.dkim_status = Domain.DkimStatus.PENDING_DNS
    unverified_domain_dkim.save(update_fields=["dkim_status"])
    unverified_dkim_inbox = Inbox.objects.create(
        user=owner, routing_key="w13uiunverifieddkim", domain=unverified_domain_dkim
    )

    resp_compose2 = client.get(reverse("dashboard:compose"))
    assert resp_compose2.status_code == 200
    inbox_ids2 = {inbox.public_id for inbox in resp_compose2.context["sendable_inboxes"]}
    assert inbox_ids2 == {sendable_inbox.public_id}

    compose_html = resp_compose2.content.decode()
    assert f'value="{sendable_inbox.public_id}"' in compose_html
    assert f'value="{shared_inbox.public_id}"' not in compose_html
    assert f'value="{unverified_status_inbox.public_id}"' not in compose_html
    assert f'value="{unverified_dkim_inbox.public_id}"' not in compose_html

    # H7: for EACH ineligible category, Compose/Reply/Forward markup ALL
    # absent AND the message list still renders (continuity).
    for plan in (AccountPlan.Plan.FREE, AccountPlan.Plan.PAID, AccountPlan.Plan.SCALE):
        AccountPlan.objects.filter(user=owner).update(plan=plan)
        resp_tier = client.get(reverse("dashboard:messages"))
        assert resp_tier.context["outbound_ui"] is False
        assert b"Compose" not in resp_tier.content
        assert resp_tier.status_code == 200
        assert _subjects(resp_tier) == {"m1"}

        resp_tier_detail = client.get(reverse("dashboard:message-detail", kwargs={"public_id": message.public_id}))
        assert resp_tier_detail.status_code == 200
        assert b"Reply" not in resp_tier_detail.content
        assert b"Forward" not in resp_tier_detail.content
    AccountPlan.objects.filter(user=owner).update(plan=AccountPlan.Plan.BUSINESS)

    # agent -> absent, list still renders.
    from apps.accounts.services import AGENT_ACCOUNT_DOMAIN

    agent = make_user("w13_ui_agent", plan=AccountPlan.Plan.BUSINESS, email=f"w13uiagent@{AGENT_ACCOUNT_DOMAIN}")
    agent_inbox = Inbox.objects.create(user=agent, routing_key="w13uiagentinbox")
    make_message(agent_inbox, "agent-m1")
    agent_client = login(agent)
    resp_agent = agent_client.get(reverse("dashboard:messages"))
    assert resp_agent.status_code == 200
    assert resp_agent.context["outbound_ui"] is False
    assert b"Compose" not in resp_agent.content
    assert _subjects(resp_agent) == {"agent-m1"}

    # H7: demo -> absent, DISTINCT case from agent (real get_demo_user()
    # path, not a hand-rolled email fixture).
    from apps.accounts.services import get_demo_user

    demo_user = get_demo_user()
    AccountPlan.objects.filter(user=demo_user).update(plan=AccountPlan.Plan.BUSINESS)
    demo_inbox = Inbox.objects.create(user=demo_user, routing_key="w13uidemoinbox")
    make_message(demo_inbox, "demo-m1")
    demo_client = login(demo_user)
    resp_demo = demo_client.get(reverse("dashboard:messages"))
    assert resp_demo.status_code == 200
    assert resp_demo.context["outbound_ui"] is False
    assert b"Compose" not in resp_demo.content
    assert _subjects(resp_demo) == {"demo-m1"}

    
    # list still renders.
    from django.test import override_settings

    member = make_user("w13_ui_member")
    TeamMembership.objects.create(account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER)
    with override_settings(ENABLE_TEAMS=True):
        member_client = login(member)
        resp_member = member_client.get(reverse("dashboard:messages"))
        assert resp_member.status_code == 200
        assert resp_member.context["outbound_ui"] is False
        assert b"Compose" not in resp_member.content
        assert _subjects(resp_member) == {"m1"}


# --- (7) isolation cross-tenant 404, WITH positive control ------------------


def test_isolation_cross_tenant_404_every_path():
    alice = make_user("w13_iso_a")
    bob = make_user("w13_iso_b")
    domain_a = eligible_domain(alice, "w13-iso-a.example.com")
    domain_b = eligible_domain(bob, "w13-iso-b.example.com")

    # H1 positive control: Alice's OWN inbox/domain/message, so an
    # all-404-everything stub implementation cannot pass this test.
    alice_inbox = Inbox.objects.create(user=alice, routing_key="w13isoa", domain=domain_a)
    alice_message = make_message(alice_inbox, "alice-own", header_from="External <ext@external.example.com>", subject="own")

    bob_inbox = Inbox.objects.create(user=bob, routing_key="w13isob", domain=domain_b)
    bob_message = make_message(bob_inbox, "bob-secret")

    client = login(alice)

    # --- positive control: Alice's OWN ids => 200 on every path. ---
    assert client.get(reverse("dashboard:message-detail", kwargs={"public_id": alice_message.public_id})).status_code == 200
    assert client.get(reverse("dashboard:message-panel", kwargs={"public_id": alice_message.public_id})).status_code == 200
    assert client.get(reverse("dashboard:message-html", kwargs={"public_id": alice_message.public_id})).status_code == 200
    resp_mark = client.post(reverse("dashboard:message-mark-read", kwargs={"public_id": alice_message.public_id}))
    assert resp_mark.status_code == 302
    resp_reply = client.post(
        reverse("dashboard:message-reply", kwargs={"public_id": alice_message.public_id}), {"body_text": "hi"}
    )
    assert resp_reply.status_code == 302
    resp_forward = client.post(
        reverse("dashboard:message-forward", kwargs={"public_id": alice_message.public_id}),
        {"to": "x@example.test", "body_text": "hi"},
    )
    assert resp_forward.status_code == 302
    assert OutboundEmail.objects.filter(user=alice).count() == 2

    # --- negative: B's foreign id => 404 on every path. ---
    assert client.get(reverse("dashboard:message-detail", kwargs={"public_id": bob_message.public_id})).status_code == 404
    assert client.get(reverse("dashboard:message-panel", kwargs={"public_id": bob_message.public_id})).status_code == 404
    assert client.get(reverse("dashboard:message-html", kwargs={"public_id": bob_message.public_id})).status_code == 404
    assert client.post(reverse("dashboard:message-mark-read", kwargs={"public_id": bob_message.public_id})).status_code == 404
    assert client.post(reverse("dashboard:message-mark-unread", kwargs={"public_id": bob_message.public_id})).status_code == 404
    assert client.post(
        reverse("dashboard:message-reply", kwargs={"public_id": bob_message.public_id}), {"body_text": "hi"}
    ).status_code == 404
    assert client.post(
        reverse("dashboard:message-forward", kwargs={"public_id": bob_message.public_id}),
        {"to": "x@example.test", "body_text": "hi"},
    ).status_code == 404

    assert OutboundEmail.objects.filter(user=bob).count() == 0


# --- (8) flag-off read-only identity, BYTE-COMPARED against a baseline -----


def test_flag_off_read_only_identity(settings):
    user = make_user("w13_flagoff")
    inbox = Inbox.objects.create(user=user, routing_key="w13flagoff")
    message = make_message(inbox, "m1")

    settings.ENABLE_OUTBOUND = False
    client = login(user)

    for name, kwargs in [
        ("dashboard:compose", {}),
        ("dashboard:message-reply", {"public_id": message.public_id}),
        ("dashboard:message-forward", {"public_id": message.public_id}),
    ]:
        resp = client.post(reverse(name, kwargs=kwargs), {})
        assert resp.status_code == 404
    assert client.get(reverse("dashboard:compose")).status_code == 404

    resp_list = client.get(reverse("dashboard:messages"))
    resp_detail = client.get(reverse("dashboard:message-detail", kwargs={"public_id": message.public_id}))
    assert resp_list.context["outbound_ui"] is False
    assert b"Compose" not in resp_list.content
    assert b"Reply" not in resp_detail.content
    assert b"Forward" not in resp_detail.content

    assert OutboundEmail.objects.count() == 0

    # H9: BYTE-COMPARE the rendered list AND detail HTML between
    
    # baseline render captured from a FRESH request under the exact same
    # flag-off setting. A csrftoken cookie/hidden-input value is the only
    # thing Django ever varies between two otherwise-identical GETs (mark-
    # read-on-open already fired above on resp_detail, so re-fetch both
    # once more so both sides observe the identical read_at state) — if a
    # CSRF token differs, we strip/normalize it and compare the rest, and
    # say so below.
    baseline_list = client.get(reverse("dashboard:messages"))
    baseline_detail = client.get(reverse("dashboard:message-detail", kwargs={"public_id": message.public_id}))
    repeat_list = client.get(reverse("dashboard:messages"))
    repeat_detail = client.get(reverse("dashboard:message-detail", kwargs={"public_id": message.public_id}))

    csrf_re = re.compile(rb'name="csrfmiddlewaretoken" value="[^"]*"')

    def _normalize(content):
        return csrf_re.sub(b'name="csrfmiddlewaretoken" value="STRIPPED"', content)

    list_a, list_b = _normalize(baseline_list.content), _normalize(repeat_list.content)
    detail_a, detail_b = _normalize(baseline_detail.content), _normalize(repeat_detail.content)
    assert list_a == list_b, "flag-off list render is not byte-identical across requests (modulo CSRF token)"
    assert detail_a == detail_b, "flag-off detail render is not byte-identical across requests (modulo CSRF token)"

    # Routes still 404, and no write occurred anywhere along the way.
    assert client.get(reverse("dashboard:compose")).status_code == 404
    assert OutboundEmail.objects.count() == 0


# --- (9) cursor bound to enlarged signature ---------------------------------


def test_cursor_bound_to_enlarged_signature_and_older_link_roundtrips_params():
    alice = make_user("w13_cursor_a")
    bob = make_user("w13_cursor_b")
    domain_a = eligible_domain(alice, "w13-cursor-a.example.com")
    inbox = Inbox.objects.create(user=alice, routing_key="w13cur", domain=domain_a)

    # PAGE_SIZE is 25; create enough unread otp-matching rows to force a
    # next_cursor under the exact filter combo.
    from apps.dashboard.views import PAGE_SIZE

    for i in range(PAGE_SIZE + 3):
        make_message(inbox, f"otp-msg-{i}", subject=f"otp code {i}")

    client = login(alice)
    params = {"inbox": inbox.public_id, "domain_id": domain_a.public_id, "read": "false", "q": "otp"}
    resp = client.get(reverse("dashboard:messages"), params)
    assert resp.status_code == 200
    next_cursor = resp.context["next_cursor"]
    assert next_cursor is not None

    # M2: PARSE the rendered "Older messages" link out of response.content
    # and FOLLOW it, rather than hand-constructing page 2.
    older_link_match = re.search(rb'class="button more" href="\?([^"]+)"', resp.content)
    assert older_link_match is not None, "no rendered 'Older messages' link found"
    from urllib.parse import parse_qs, unquote

    older_qs = unquote(older_link_match.group(1).decode()).replace("&amp;", "&")
    older_params = {k: v[0] for k, v in parse_qs(older_qs).items()}

    # The rendered link must carry every param with its EXACT value.
    for key, value in params.items():
        assert older_params.get(key) == value, f"{key} mismatch in rendered older-link: {older_params}"
    assert older_params.get("cursor") == next_cursor

    resp2 = client.get(reverse("dashboard:messages"), older_params)
    assert resp2.status_code == 200

    # Any one param changed -> 422 (mutate the FOLLOWED params, plus an
    # explicit inbox-change mutation).
    for changed_key, changed_value in [("read", "true"), ("q", "different"), ("domain_id", "shared")]:
        mutated = dict(older_params)
        mutated[changed_key] = changed_value
        resp_bad = client.get(reverse("dashboard:messages"), mutated)
        assert resp_bad.status_code == 422

    # M2: inbox-change mutation => 422 too.
    other_inbox = Inbox.objects.create(user=alice, routing_key="w13curother")
    mutated_inbox = dict(older_params)
    mutated_inbox["inbox"] = other_inbox.public_id
    resp_bad_inbox = client.get(reverse("dashboard:messages"), mutated_inbox)
    assert resp_bad_inbox.status_code == 422

    # Other tenant replay -> 422 (qafix2 #14: Alice's inbox/domain ids are
    # ignored under Bob's tenant, so the filter signature and the embedded
    # user pk no longer match and the cursor is refused) — still never a
    # silent wrong page.
    bob_client = login(bob)
    resp_bob = bob_client.get(reverse("dashboard:messages"), older_params)
    assert resp_bob.status_code == 422

    # M3: isolate the user-pk binding itself: mint a cursor under params
    # that resolve identically for both tenants (no inbox/domain), replay
    # as Bob -> 422 (decode fails on the embedded user pk mismatch, not a
    # 404). Unconditional — no `if bare_cursor` guard, so this oracle
    # ALWAYS runs and a regression that stops minting a cursor here fails
    # loudly instead of being silently skipped.
    bare_params = {"read": "false", "q": "otp"}
    resp_bare = client.get(reverse("dashboard:messages"), bare_params)
    bare_cursor = resp_bare.context["next_cursor"]
    assert bare_cursor is not None, "expected a next_cursor under the bare (no inbox/domain) filter combo"
    resp_bare_bob = bob_client.get(
        reverse("dashboard:messages"), {**bare_params, "cursor": bare_cursor}
    )
    assert resp_bare_bob.status_code == 422


# --- (10) reply/forward/compose choke point, ALL THREE paths spied ---------


def test_dashboard_send_paths_end_in_choke_point(monkeypatch):
    owner = make_user("w13_send_owner", plan=AccountPlan.Plan.BUSINESS)
    domain = eligible_domain(owner, "w13-send.example.com")
    inbox = Inbox.objects.create(user=owner, routing_key="w13send", domain=domain)
    message = make_message(
        inbox, "orig", header_from="External <ext@external.example.com>", subject="hello"
    )

    calls = []
    from apps.outbound import services as outbound_services

    real_enqueue = outbound_services.enqueue_outbound_email

    def spy(**kwargs):
        calls.append(kwargs)
        return real_enqueue(**kwargs)

    monkeypatch.setattr(outbound_services, "enqueue_outbound_email", spy)
    # reply_to_message/forward_message/compose_from_inbox call
    # enqueue_outbound_email via a `from ... import enqueue_outbound_email`
    # inside services.py's own module namespace, so patching the module
    # attribute is sufficient (same module).

    client = login(owner)

    def _assert_choke_point(call, *, expected_from=None):
        expected_from = expected_from or inbox.address
        assert call["from_address"] == expected_from
        assert call["user"] == owner
        assert call["created_by"] == owner

    # H2 (1/3): REPLY.
    resp = client.post(
        reverse("dashboard:message-reply", kwargs={"public_id": message.public_id}),
        {"body_text": "my reply"},
    )
    assert resp.status_code == 302
    assert len(calls) == 1
    _assert_choke_point(calls[0])

    # H2 (2/3): FORWARD, with a form-supplied from_address-shaped field
    # ("from_address") that must be IGNORED — the choke point must always
    # use inbox.address, never anything form-supplied.
    resp_forward = client.post(
        reverse("dashboard:message-forward", kwargs={"public_id": message.public_id}),
        {"to": "dest@example.test", "body_text": "fwd body", "from_address": "attacker@evil.example.com"},
    )
    assert resp_forward.status_code == 302
    assert len(calls) == 2
    _assert_choke_point(calls[1])

    # H2 (3/3): COMPOSE, same form-supplied from_address-shaped field ignored.
    resp_compose = client.post(
        reverse("dashboard:compose"),
        {
            "inbox": inbox.public_id,
            "to": "dest2@example.test",
            "subject": "hi",
            "body_text": "compose body",
            "from_address": "attacker@evil.example.com",
        },
    )
    assert resp_compose.status_code == 302
    assert len(calls) == 3
    _assert_choke_point(calls[2])

    # H3: CRLF oracle, non-vacuous. VERIFIED production behavior (confirmed
    # by direct probing before writing this assertion, since it differs
    # from a naive reading of "CRLF must always 4xx"): BODY fields are
    # never SMTP header fields, so has_control_chars_any/sanitize_body
    # deliberately NORMALIZE \r\n rather than reject it (see the
    # has_control_chars_any docstring/comment in apps/outbound/services.py
    # and sanitize_body in apps/outbound/reply_forward.py) — a body-only
    # CRLF injection on reply/forward is accepted (302) with the row
    # written, but the PERSISTED body must contain ZERO raw CRLF (proof the
    # sanitizer actually ran, not merely that the response wasn't a 500).
    # SUBJECT, by contrast, IS a header field (validate_plain_text_field ->
    # has_control_chars, strict, no normalization) — compose is the only
    # send path with a user-supplied subject (reply/forward build theirs
    # server-side via build_subject), so compose-subject-CRLF is the one
    # real "clean 4xx AND zero new row" oracle available on this codebase;
    # asserting a 4xx for reply/forward BODY CRLF would be a FALSE
    # assertion against verified, by-design behavior — it is called out
    # here rather than silently faked into passing.
    before_count = OutboundEmail.objects.count()

    # (a) reply, CRLF in body: accepted (not a header field) but the
    # PERSISTED row must carry ZERO raw CRLF — proves sanitize_body ran.
    resp_crlf_reply_body = client.post(
        reverse("dashboard:message-reply", kwargs={"public_id": message.public_id}),
        {"body_text": "line1\r\nInjected-Header: evil"},
    )
    assert resp_crlf_reply_body.status_code == 302
    reply_row = OutboundEmail.objects.order_by("-id").first()
    assert reply_row is not None and OutboundEmail.objects.count() == before_count + 1
    assert "\r" not in reply_row.body_text, "raw CRLF leaked into a persisted OutboundEmail body"

    # (b) forward, CRLF in body: same shape — accepted, sanitized on write.
    before_forward = OutboundEmail.objects.count()
    resp_crlf_forward_body = client.post(
        reverse("dashboard:message-forward", kwargs={"public_id": message.public_id}),
        {"to": "dest@example.test", "body_text": "line1\r\nInjected-Header: evil"},
    )
    assert resp_crlf_forward_body.status_code == 302
    forward_row = OutboundEmail.objects.order_by("-id").first()
    assert forward_row is not None and OutboundEmail.objects.count() == before_forward + 1
    assert "\r" not in forward_row.body_text, "raw CRLF leaked into a persisted OutboundEmail body"

    # (c) compose, CRLF in SUBJECT — the genuine "clean 4xx AND
    # OutboundEmail.objects.count() UNCHANGED" oracle: subject IS a header
    # field, validate_plain_text_field/has_control_chars rejects any \r or
    # \n unconditionally, no normalization branch exists for it.
    before_subject = OutboundEmail.objects.count()
    resp_crlf_compose_subject = client.post(
        reverse("dashboard:compose"),
        {
            "inbox": inbox.public_id,
            "to": "dest2@example.test",
            "subject": "evil\r\nInjected-Header: evil",
            "body_text": "clean body",
        },
    )
    assert resp_crlf_compose_subject.status_code == 422, (
        "compose subject-CRLF must be rejected — subject is a real header field"
    )
    assert OutboundEmail.objects.count() == before_subject, "subject-CRLF must never write a row"

    # (d) compose, CRLF in body — added case for parity: accepted like (a)/
    # (b) (body is not a header field), but since compose_from_inbox does
    # NOT call sanitize_body (unlike reply_to_message/forward_message), the
    # RAW \r\n IS persisted as-is here. This is flagged, not silently
    # asserted as clean: has_control_chars_any tolerates \r\n specifically
    # (it is replaced to \n before the control-char scan), so this send is
    # accepted at 302 with the raw bytes stored. Documented as a real,
    # verified asymmetry between compose and reply/forward — a production
    # concern to raise separately, NOT something this test-only patch can
    # silently paper over with a false "sanitized" assertion.
    before_compose_body = OutboundEmail.objects.count()
    resp_crlf_compose_body = client.post(
        reverse("dashboard:compose"),
        {
            "inbox": inbox.public_id,
            "to": "dest2@example.test",
            "subject": "clean subject",
            "body_text": "line1\r\nInjected-Header: evil",
        },
    )
    assert resp_crlf_compose_body.status_code == 302
    assert OutboundEmail.objects.count() == before_compose_body + 1
    compose_body_row = OutboundEmail.objects.order_by("-id").first()
    # Verified-true assertion of ACTUAL behavior (not the desired one): the
    # raw CRLF survives into the persisted body on the compose path only.
    assert "\r\n" in compose_body_row.body_text, (
        "compose body sanitization behavior changed — update this test's "
        "documented asymmetry with reply/forward if compose_from_inbox now "
        "also calls sanitize_body"
    )

    # M4: Quota exceeded -> flash, not 500, AND OutboundEmail count UNCHANGED
    # (no-write oracle) — assert BEFORE and AFTER the quota-exceeded POST.

    before_quota_count = OutboundEmail.objects.count()
    monkeypatch.setattr(
        "apps.outbound.services.can_send_outbound", lambda user, count: (False, "outbound_quota_exceeded")
    )
    resp_quota = client.post(
        reverse("dashboard:message-reply", kwargs={"public_id": message.public_id}),
        {"body_text": "another reply"},
    )
    assert resp_quota.status_code == 422
    assert b"quota" in resp_quota.content.lower()
    assert OutboundEmail.objects.count() == before_quota_count


# --- (11) mark-read ----------------------------------------------------------


def test_mark_read_post_toggles_and_get_detail_idempotent():
    user = make_user("w13_markread", plan=AccountPlan.Plan.FREE)
    inbox = Inbox.objects.create(user=user, routing_key="w13mark")
    message = make_message(inbox, "m1")
    assert message.read_at is None

    client = login(user)

    # Free-plan dashboard toggle allowed (documented divergence from API 402).
    resp = client.post(reverse("dashboard:message-mark-read", kwargs={"public_id": message.public_id}))
    assert resp.status_code == 302
    message.refresh_from_db()
    assert message.read_at is not None

    resp_unread = client.post(reverse("dashboard:message-mark-unread", kwargs={"public_id": message.public_id}))
    assert resp_unread.status_code == 302
    message.refresh_from_db()
    assert message.read_at is None

    # GET detail sets read_at idempotently (Q1).
    client.get(reverse("dashboard:message-detail", kwargs={"public_id": message.public_id}))
    message.refresh_from_db()
    assert message.read_at is not None
    second_read_at = message.read_at

    client.get(reverse("dashboard:message-detail", kwargs={"public_id": message.public_id}))
    message.refresh_from_db()
    assert message.read_at == second_read_at


# --- (12) open-redirect fix: mark-read/unread `next` host validation --------


def test_mark_read_next_param_open_redirect_fix():
    user = make_user("w13_openredirect", plan=AccountPlan.Plan.FREE)
    inbox = Inbox.objects.create(user=user, routing_key="w13redirinbox")
    message = make_message(inbox, "m1")

    client = login(user)

    # Attacker-controlled absolute external `next` must NOT be honored —
    # django.shortcuts.redirect() performs no host validation on its own, so
    # this is the exact P2 open-redirect regression check: the response must
    # 302 to the LOCAL messages page, never to the external host/scheme.
    resp_evil = client.post(
        reverse("dashboard:message-mark-read", kwargs={"public_id": message.public_id}),
        {"next": "https://evil.example/steal"},
    )
    assert resp_evil.status_code == 302
    assert resp_evil.url == reverse("dashboard:messages")
    assert "evil.example" not in resp_evil.url

    # Same check for mark-unread.
    resp_evil_unread = client.post(
        reverse("dashboard:message-mark-unread", kwargs={"public_id": message.public_id}),
        {"next": "https://evil.example/steal"},
    )
    assert resp_evil_unread.status_code == 302
    assert resp_evil_unread.url == reverse("dashboard:messages")
    assert "evil.example" not in resp_evil_unread.url

    # A safe, same-host relative `next` IS honored.
    resp_safe = client.post(
        reverse("dashboard:message-mark-read", kwargs={"public_id": message.public_id}),
        {"next": "/app/messages/"},
    )
    assert resp_safe.status_code == 302
    assert resp_safe.url == "/app/messages/"


@pytest.mark.parametrize(
    "evil",
    [
        "http://testserver/app/messages/", "//testserver/app/messages/", "/\\evil.example", "/\t/evil.example",
        "javascript:alert(1)",
        
        "/%2F%2Fevil.example", "/%255Cevil.example", "/\uff0fevil.example", "/..//evil.example",
    ],
)
def test_mark_read_next_must_be_a_relative_path(evil):
    "Public API."
    user = make_user("f1_markread_next", plan=AccountPlan.Plan.FREE)
    inbox = Inbox.objects.create(user=user, routing_key="f1markreadnext")
    message = make_message(inbox, "m1")
    client = login(user)
    for name in ("dashboard:message-mark-read", "dashboard:message-mark-unread"):
        resp = client.post(reverse(name, kwargs={"public_id": message.public_id}), {"next": evil})
        assert resp.status_code == 302
        assert resp.url == reverse("dashboard:messages")
