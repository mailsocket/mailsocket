"Public API."
import hashlib

import pytest

from apps.accounts.models import Account, AccountPlan
from apps.domains.models import Domain
from apps.inboxes.models import Inbox, Message
from apps.outbound.models import ApprovedRecipient, OutboundEmail, SuppressionEntry
from apps.outbound.tests.test_outbound import (
    _bomb_transport,
    _error_without_request_id,
    auth_client,
    eligible_domain,
    make_user,
    member_auth_client,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _enable_outbound(settings):
    settings.ENABLE_OUTBOUND = True


def byod_inbox(user, domain_name, *, routing_key="w8inbox"):
    domain = eligible_domain(user, domain_name)
    return Inbox.objects.create(user=user, routing_key=routing_key, domain=domain), domain


def shared_inbox(user, *, routing_key="w8shared"):
    return Inbox.objects.create(user=user, routing_key=routing_key)


def make_message(
    inbox,
    *,
    raw_bytes,
    header_from="",
    header_to=None,
    header_cc=None,
    subject="",
    provider_message_id="",
    text_body="",
    html_body="",
):
    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    dedupe_key = hashlib.sha256((raw_sha256 + provider_message_id).encode()).hexdigest()
    # Postgres text columns reject embedded NUL bytes — the real ingest
    # parser already strips them (apps.ingest.parser._bounded replaces
    # \x00 with U+FFFD) before Message ever gets a text_body/html_body/
    # subject value, so tests that want a \x00-laden original apply the
    # same substitution here rather than storing a NUL literally.
    text_body = (text_body or "").replace("\x00", "\ufffd")
    html_body = (html_body or "").replace("\x00", "\ufffd")
    subject = (subject or "").replace("\x00", "\ufffd")
    return Message.objects.create(
        inbox=inbox,
        dedupe_key=dedupe_key,
        provider_message_id=provider_message_id,
        envelope_from="sender@external.example.com",
        envelope_recipient=inbox.address,
        header_from=header_from,
        header_to=header_to or [],
        header_cc=header_cc or [],
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        raw_bytes=raw_bytes,
        raw_sha256=raw_sha256,
        size_bytes=len(raw_bytes),
        ingest_status="parsed",
    )


def basic_raw_message(*, references="", reply_to=""):
    lines = [
        b"From: External Sender <sender@external.example.com>",
        b"To: dest@example.com",
    ]
    if reply_to:
        lines.append(f"Reply-To: {reply_to}".encode())
    lines.append(b"Subject: hello")
    lines.append(b"Message-ID: <orig-msg-1@external.example.com>")
    if references:
        lines.append(f"References: {references}".encode())
    return b"\r\n".join(lines) + b"\r\n\r\nbody\r\n"


# --- (1) reply from BYOD verified+DKIM -> 201, correct threading ------------


def test_reply_from_byod_inbox_201_correct_threading_and_dkim_signs_it():
    from apps.outbound.engine import claim_one, deliver_claim

    user = make_user("w8_reply_ok")
    inbox, domain = byod_inbox(user, "w8-reply-ok.example.com")
    message = make_message(
        inbox,
        raw_bytes=basic_raw_message(),
        header_from="External Sender <sender@external.example.com>",
        subject="hello",
        provider_message_id="<orig-msg-1@external.example.com>",
        text_body="original body",
    )
    client = auth_client(user)
    resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{message.public_id}/reply",
        {"body_text": "my reply"},
        format="json",
    )
    assert resp.status_code == 201, resp.json()
    data = resp.json()["data"]
    assert data["from_address"] == inbox.address
    assert data["to"] == ["sender@external.example.com"]
    assert data["subject"] == "Re: hello"

    row = OutboundEmail.objects.get(public_id=data["id"])
    assert row.in_reply_to == "<orig-msg-1@external.example.com>"
    assert row.references.endswith("<orig-msg-1@external.example.com>")
    assert row.inbox_id == inbox.pk

    calls = []
    pk, token = claim_one()
    result = deliver_claim(pk, token, transport=lambda **kw: calls.append(kw))
    assert result == "sent"
    assert len(calls) == 1
    raw_message = calls[0]["raw_message"]
    assert b"In-Reply-To: <orig-msg-1@external.example.com>" in raw_message
    assert b"References:" in raw_message
    assert b"DKIM-Signature" in raw_message


# --- (2) compose 201; forward 201 with quoted body + Fwd: -------------------


def test_compose_from_byod_inbox_201():
    user = make_user("w8_compose_ok")
    inbox, domain = byod_inbox(user, "w8-compose-ok.example.com")
    client = auth_client(user)
    resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/compose",
        {"to": ["dest@example.test"], "subject": "hi there", "body_text": "hello"},
        format="json",
    )
    assert resp.status_code == 201, resp.json()
    data = resp.json()["data"]
    assert data["from_address"] == inbox.address
    assert data["to"] == ["dest@example.test"]
    assert data["subject"] == "hi there"
    row = OutboundEmail.objects.get(public_id=data["id"])
    assert row.in_reply_to is None
    assert row.references is None


def test_forward_201_with_quoted_body_and_fwd_prefix():
    user = make_user("w8_forward_ok")
    inbox, domain = byod_inbox(user, "w8-forward-ok.example.com")
    message = make_message(
        inbox,
        raw_bytes=basic_raw_message(),
        header_from="External Sender <sender@external.example.com>",
        header_to=["dest@example.com"],
        subject="hello",
        provider_message_id="<orig-msg-1@external.example.com>",
        text_body="original body text",
    )
    client = auth_client(user)
    resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{message.public_id}/forward",
        {"to": ["forwardee@example.test"], "body_text": "fyi"},
        format="json",
    )
    assert resp.status_code == 201, resp.json()
    data = resp.json()["data"]
    assert data["subject"] == "Fwd: hello"
    assert data["to"] == ["forwardee@example.test"]
    row = OutboundEmail.objects.get(public_id=data["id"])
    assert "Forwarded message" in row.body_text
    assert "original body text" in row.body_text
    assert row.references == "<orig-msg-1@external.example.com>"
    assert row.in_reply_to is None





def test_shared_domain_reply_compose_forward_403_byte_identical_to_w5():
    user = make_user("w8_shared_403")
    inbox = shared_inbox(user)
    message = make_message(
        inbox,
        raw_bytes=basic_raw_message(),
        header_from="External Sender <sender@external.example.com>",
        subject="hello",
        provider_message_id="<orig-msg-1@external.example.com>",
    )
    client = auth_client(user)

    reply_resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{message.public_id}/reply", {}, format="json"
    )
    compose_resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/compose",
        {"to": ["dest@example.test"], "subject": "hi"},
        format="json",
    )
    forward_resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{message.public_id}/forward",
        {"to": ["dest@example.test"]},
        format="json",
    )
    for resp in (reply_resp, compose_resp, forward_resp):
        assert resp.status_code == 403, resp.json()
        assert resp.json()["error"]["code"] == "sending_domain_not_eligible"

    
    w5_resp = client.post(
        "/api/v1/outbound_emails",
        {"from_address": inbox.address, "to": ["dest@example.test"]},
        format="json",
    )
    assert w5_resp.status_code == 403
    bodies_without_rid = [
        _error_without_request_id(resp)
        for resp in (w5_resp, reply_resp, compose_resp, forward_resp)
    ]
    assert all(body == bodies_without_rid[0] for body in bodies_without_rid), bodies_without_rid


def test_byod_dkim_unverified_403_byte_identical_to_w5_with_positive_control():
    "Public API."
    user = make_user("w8_dkim_unverified")
    domain = Domain.objects.create(user=user, name="w8-dkim-unverified.example.com")
    domain.status = Domain.Status.VERIFIED
    domain.save(update_fields=["status"])
    inbox = Inbox.objects.create(user=user, routing_key="w8dkimuv", domain=domain)
    message = make_message(
        inbox,
        raw_bytes=basic_raw_message(),
        header_from="External Sender <sender@external.example.com>",
        subject="hello",
        provider_message_id="<orig-msg-1@external.example.com>",
    )
    client = auth_client(user)
    resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{message.public_id}/reply",
        {"body_text": "hi"},
        format="json",
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "sending_domain_not_eligible"

    
    # send (shared inbox.address as from_address is ineligible the same way).
    w5_resp = client.post(
        "/api/v1/outbound_emails",
        {"from_address": inbox.address, "to": ["dest@example.test"]},
        format="json",
    )
    assert w5_resp.status_code == 403
    assert w5_resp.json()["error"]["code"] == "sending_domain_not_eligible"
    assert _error_without_request_id(w5_resp) == _error_without_request_id(resp)

    # Positive control (separate user/inbox to avoid quota interplay): BYOD
    # verified+DKIM -> 201, proving the 403 above is load-bearing on DKIM
    # status and not an always-403 endpoint.
    ok_user = make_user("w8_dkim_verified_control")
    ok_inbox, ok_domain = byod_inbox(ok_user, "w8-dkim-verified-control.example.com")
    ok_message = make_message(
        ok_inbox,
        raw_bytes=basic_raw_message(),
        header_from="External Sender <sender@external.example.com>",
        subject="hello",
        provider_message_id="<orig-msg-1@external.example.com>",
    )
    ok_client = auth_client(ok_user)
    ok_resp = ok_client.post(
        f"/api/v1/inboxes/{ok_inbox.public_id}/messages/{ok_message.public_id}/reply",
        {"body_text": "hi"},
        format="json",
    )
    assert ok_resp.status_code == 201, ok_resp.json()


# --- (4) cross-inbox / cross-tenant / nonexistent -> uniform 404 -----------


def test_cross_inbox_cross_tenant_nonexistent_message_uniform_404():
    user_a = make_user("w8_404_a")
    user_b = make_user("w8_404_b")
    inbox_a1, _ = byod_inbox(user_a, "w8-404-a1.example.com", routing_key="w8404a1")
    inbox_a2, _ = byod_inbox(user_a, "w8-404-a2.example.com", routing_key="w8404a2")
    inbox_b, _ = byod_inbox(user_b, "w8-404-b.example.com", routing_key="w8404b")

    message_a2 = make_message(inbox_a2, raw_bytes=basic_raw_message())
    message_b = make_message(inbox_b, raw_bytes=basic_raw_message())

    client_a = auth_client(user_a)

    # cross-inbox: message belongs to inbox_a2, requested via inbox_a1.
    cross_inbox_resp = client_a.post(
        f"/api/v1/inboxes/{inbox_a1.public_id}/messages/{message_a2.public_id}/reply", {}, format="json"
    )
    # cross-tenant inbox_id.
    cross_tenant_inbox_resp = client_a.post(
        f"/api/v1/inboxes/{inbox_b.public_id}/messages/{message_b.public_id}/reply", {}, format="json"
    )
    # cross-tenant message under own inbox.
    cross_tenant_message_resp = client_a.post(
        f"/api/v1/inboxes/{inbox_a1.public_id}/messages/{message_b.public_id}/reply", {}, format="json"
    )
    # nonexistent.
    nonexistent_resp = client_a.post(
        f"/api/v1/inboxes/{inbox_a1.public_id}/messages/msg_doesnotexist000000000000/reply", {}, format="json"
    )

    responses = [cross_inbox_resp, cross_tenant_inbox_resp, cross_tenant_message_resp, nonexistent_resp]
    for resp in responses:
        assert resp.status_code == 404, resp.json()
    bodies = [_error_without_request_id(resp) for resp in responses]
    assert all(body == bodies[0] for body in bodies), bodies


# --- (5) non-business / demo / agent / member 403 ---------------------------


@pytest.mark.parametrize("plan", [AccountPlan.Plan.FREE, AccountPlan.Plan.PAID, AccountPlan.Plan.SCALE])
def test_non_business_tier_cannot_reply(plan):
    user = make_user(f"w8_tier_{plan}", plan=plan)
    inbox, _ = byod_inbox(user, f"w8-tier-{plan}.example.com")
    message = make_message(inbox, raw_bytes=basic_raw_message())
    client = auth_client(user)
    resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{message.public_id}/reply", {}, format="json"
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "outbound_entitlement_required"


def test_demo_user_cannot_reply_wall_fires_before_row_lookup():
    """M3: demo wall is wall #2 in _w8_gate_response, checked BEFORE
    _owned_inbox/_owned_message_for_inbox. Use a NONEXISTENT inbox_id and
    message_id — if the wall did not run first, this would 404 (NotFound
    from the lookup), not 403. Getting 403 proves the wall is load-bearing
    and runs first."""
    from apps.accounts.services import get_demo_user

    demo_user = get_demo_user()
    client = auth_client(demo_user)
    resp = client.post(
        "/api/v1/inboxes/inbox_doesnotexist000000000000/messages/msg_doesnotexist000000000000/reply",
        {},
        format="json",
    )
    assert resp.status_code == 403, resp.json()
    assert resp.json()["error"]["code"] == "outbound_entitlement_required"


def test_agent_user_cannot_reply_wall_fires_before_row_lookup():
    """M3: same proof as the demo case for the agent wall — nonexistent
    inbox/message still 403s, never 404s."""
    from apps.accounts.services import register_agent

    agent_user, api_key, token = register_agent("w8-agent-wall")
    from rest_framework.test import APIClient

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    resp = client.post(
        "/api/v1/inboxes/inbox_doesnotexist000000000000/messages/msg_doesnotexist000000000000/reply",
        {},
        format="json",
    )
    assert resp.status_code == 403, resp.json()
    assert resp.json()["error"]["code"] == "outbound_entitlement_required"


def test_member_minted_key_cannot_reply_wall_fires_before_row_lookup():
    "Public API."
    owner = make_user("w8_member_owner")
    member = make_user("w8_member_member")
    client = member_auth_client(owner, member)
    resp = client.post(
        "/api/v1/inboxes/inbox_doesnotexist000000000000/messages/msg_doesnotexist000000000000/reply",
        {},
        format="json",
    )
    assert resp.status_code == 403, resp.json()
    assert resp.json()["error"]["code"] == "account_forbidden"


# --- (6) flag off -> 404 all three -------------------------------------------


def test_flag_off_all_three_w8_endpoints_404(settings):
    settings.ENABLE_OUTBOUND = False
    user = make_user("w8_flag_off")
    inbox = Inbox.objects.create(user=user, routing_key="w8flagoff")
    client = auth_client(user)
    assert client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/msg_x/reply", {}, format="json"
    ).status_code == 404
    assert client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/msg_x/forward", {"to": ["a@example.test"]}, format="json"
    ).status_code == 404
    assert client.post(
        f"/api/v1/inboxes/{inbox.public_id}/compose", {"to": ["a@example.test"], "subject": "x"}, format="json"
    ).status_code == 404


def test_flag_off_404_fires_before_any_message_row_lookup(settings):
    """M3: flag-off and missing-row both yield 404, so status code alone
    cannot distinguish them (design: _outbound_disabled_response raises
    NotFound() BEFORE _owned_inbox/_owned_message_for_inbox ever run). Prove
    the lookup genuinely did not execute — using a VALID, EXISTING inbox and
    message (so a 404 here cannot be explained by a missing row) plus a
    query-count capture asserting zero queries against inboxes_message."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    settings.ENABLE_OUTBOUND = False
    user = make_user("w8_flag_off_lookup_proof")
    inbox, domain = byod_inbox(user, "w8-flag-off-lookup.example.com")
    message = make_message(inbox, raw_bytes=basic_raw_message())
    client = auth_client(user)

    with CaptureQueriesContext(connection) as ctx:
        resp = client.post(
            f"/api/v1/inboxes/{inbox.public_id}/messages/{message.public_id}/reply",
            {"body_text": "hi"},
            format="json",
        )
    assert resp.status_code == 404, resp.json()
    message_table = message._meta.db_table
    inbox_table = inbox._meta.db_table
    touched_tables = [q["sql"] for q in ctx.captured_queries]
    assert not any(message_table in sql for sql in touched_tables), touched_tables
    assert not any(inbox_table in sql for sql in touched_tables), touched_tables





def test_allowlist_on_reply_target_not_approved_422_then_approved_201_then_drain_revoke():
    from apps.accounts.services import normalize_email
    from apps.outbound.engine import claim_one, deliver_claim

    user = make_user("w8_allowlist")
    inbox, domain = byod_inbox(user, "w8-allowlist.example.com")
    message = make_message(
        inbox,
        raw_bytes=basic_raw_message(),
        header_from="External Sender <sender@external.example.com>",
        subject="hello",
        provider_message_id="<orig-msg-1@external.example.com>",
    )
    Account.objects.filter(pk=user.pk).update(outbound_allowlist_enabled=True)
    client = auth_client(user)

    not_approved_resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{message.public_id}/reply",
        {"body_text": "hi"},
        format="json",
    )
    assert not_approved_resp.status_code == 422
    assert not_approved_resp.json()["error"]["code"] == "recipient_not_approved"

    ApprovedRecipient.objects.create(
        user=user, email_normalized=normalize_email("sender@external.example.com"), created_by=user
    )
    approved_resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{message.public_id}/reply",
        {"body_text": "hi"},
        format="json",
    )
    assert approved_resp.status_code == 201, approved_resp.json()

    # Drain-time revoke.
    ApprovedRecipient.objects.filter(user=user).delete()
    pk, token = claim_one()
    result = deliver_claim(pk, token, transport=_bomb_transport)
    assert result == "rejected"
    row = OutboundEmail.objects.get(pk=pk)
    assert row.error_code == "recipient_not_approved"


# --- (8) suppressed reply target -> 422 ---------------------------------------


def test_suppressed_reply_target_422():
    from apps.outbound.services import suppress

    user = make_user("w8_suppressed")
    inbox, domain = byod_inbox(user, "w8-suppressed.example.com")
    message = make_message(
        inbox,
        raw_bytes=basic_raw_message(),
        header_from="External Sender <sender@external.example.com>",
        subject="hello",
        provider_message_id="<orig-msg-1@external.example.com>",
    )
    suppress("sender@external.example.com", reason=SuppressionEntry.Reason.MANUAL)
    client = auth_client(user)
    resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{message.public_id}/reply",
        {"body_text": "hi"},
        format="json",
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "recipient_rejected"


# --- (9) CRLF in original Subject -> sanitized, no injected header ----------
# (M6/LOW: NUL claim dropped here — see docstring below; NUL sanitization is
# still unit-tested directly against sanitize_body in group (13).)


def test_crlf_in_original_subject_sanitized_single_subject_line_no_injected_bcc():
    "Public API."
    user = make_user("w8_crlf_subject")
    inbox, domain = byod_inbox(user, "w8-crlf-subject.example.com")
    message = make_message(
        inbox,
        raw_bytes=basic_raw_message(),
        header_from="External Sender <sender@external.example.com>",
        subject="hello\r\nBcc: evil@example.com",
        provider_message_id="<orig-msg-1@external.example.com>",
    )
    client = auth_client(user)
    resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{message.public_id}/reply",
        {"body_text": "hi"},
        format="json",
    )
    assert resp.status_code == 201, resp.json()
    row = OutboundEmail.objects.get(public_id=resp.json()["data"]["id"])
    from apps.outbound.message_builder import build_raw_message

    raw, _ = build_raw_message(row)
    header_lines = raw.split(b"\r\n\r\n")[0].split(b"\r\n")
    subject_lines = [line for line in header_lines if line.startswith(b"Subject:")]
    bcc_lines = [line for line in header_lines if line.startswith(b"Bcc:")]
    assert len(subject_lines) == 1
    assert bcc_lines == []
    # The smuggled address is folded harmlessly into the single Subject
    # line's TEXT (not a header injection) — prove it via the count of
    # header lines matching "victim"-style To:/Bcc:/Cc: prefixes staying 0,
    # rather than asserting the substring is absent from the whole block.
    recipient_header_lines = [
        line for line in header_lines
        if line.startswith((b"To:", b"Cc:", b"Bcc:")) and b"evil@example.com" in line
    ]
    assert recipient_header_lines == []


def test_malformed_message_id_with_crlf_bcc_threading_omitted_and_no_header_injected_201():
    """M1: BUILD the raw outbound bytes via the same build_raw_message path
    the drain uses, and assert the header block contains NEITHER a Bcc:
    header NOR the victim address — proving the malformed provider_message_id
    (with an embedded CRLF+Bcc smuggling attempt) is actually omitted from
    the built message, not merely that the DB column is None."""
    user = make_user("w8_malformed_msgid")
    inbox, domain = byod_inbox(user, "w8-malformed-msgid.example.com")
    message = make_message(
        inbox,
        raw_bytes=basic_raw_message(),
        header_from="External Sender <sender@external.example.com>",
        subject="hello",
        provider_message_id="malformed value\r\nBcc: victim@example.com",
    )
    client = auth_client(user)
    resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{message.public_id}/reply",
        {"body_text": "hi"},
        format="json",
    )
    assert resp.status_code == 201, resp.json()
    row = OutboundEmail.objects.get(public_id=resp.json()["data"]["id"])
    assert row.in_reply_to is None

    from apps.outbound.message_builder import build_raw_message

    raw, _ = build_raw_message(row)
    header_block = raw.split(b"\r\n\r\n")[0]
    assert b"Bcc:" not in header_block
    assert b"victim@example.com" not in header_block
    assert b"In-Reply-To:" not in header_block


def test_hostile_display_name_from_parsed_addr_spec_only_no_extra_recipient():
    """M4: original From has a hostile display-name that itself looks like
    an email address / header injection attempt. Assert the reply target is
    ONLY the parsed addr-spec (the real sender), the display name is never
    treated as a recipient or copied anywhere, and the built raw bytes
    contain no extra recipient/header derived from the display name."""
    user = make_user("w8_hostile_display_name")
    inbox, domain = byod_inbox(user, "w8-hostile-display-name.example.com")
    hostile_from = '"Bcc: victim@evil.com" <real-sender@example.com>'
    message = make_message(
        inbox,
        raw_bytes=basic_raw_message(),
        header_from=hostile_from,
        subject="hello",
        provider_message_id="<orig-msg-1@external.example.com>",
    )
    client = auth_client(user)
    resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{message.public_id}/reply",
        {"body_text": "hi"},
        format="json",
    )
    assert resp.status_code == 201, resp.json()
    data = resp.json()["data"]
    assert data["to"] == ["real-sender@example.com"]
    assert "victim@evil.com" not in data["to"]

    row = OutboundEmail.objects.get(public_id=data["id"])
    assert row.to == ["real-sender@example.com"]

    from apps.outbound.message_builder import build_raw_message

    raw, _ = build_raw_message(row)
    header_block = raw.split(b"\r\n\r\n")[0]
    to_lines = [line for line in header_block.split(b"\r\n") if line.startswith(b"To:")]
    assert len(to_lines) == 1
    assert to_lines[0] == b"To: real-sender@example.com"
    assert b"Bcc:" not in header_block
    assert b"victim@evil.com" not in header_block


# --- (10) DIRECT enqueue_outbound_email gate belt independent of the view ---


def test_direct_enqueue_in_reply_to_crlf_raises_gate_belt():
    from apps.outbound.services import AddressFormatError, enqueue_outbound_email

    user = make_user("w8_direct_gate")
    inbox, domain = byod_inbox(user, "w8-direct-gate.example.com")
    with pytest.raises(AddressFormatError):
        enqueue_outbound_email(
            user=user,
            created_by=user,
            from_address=inbox.address,
            to=["dest@example.test"],
            in_reply_to="<a>\r\nBcc: x@y>",
            inbox=inbox,
        )
    assert OutboundEmail.objects.filter(user=user).count() == 0


# --- (11) reply_all excludes self + dedup + hostile 50-addr cap -> 422 -----


def test_reply_all_excludes_self_and_dedups():
    user = make_user("w8_reply_all")
    inbox, domain = byod_inbox(user, "w8-reply-all.example.com")
    message = make_message(
        inbox,
        raw_bytes=basic_raw_message(),
        header_from="External Sender <sender@external.example.com>",
        header_to=[inbox.address, "sender@external.example.com", "third@example.test"],
        header_cc=["third@example.test", "fourth@example.test"],
        subject="hello",
        provider_message_id="<orig-msg-1@external.example.com>",
    )
    client = auth_client(user)
    resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{message.public_id}/reply",
        {"body_text": "hi", "reply_all": True},
        format="json",
    )
    assert resp.status_code == 201, resp.json()
    to = resp.json()["data"]["to"]
    assert inbox.address not in [addr.lower() for addr in to]
    assert to.count("third@example.test") == 1
    assert "fourth@example.test" in to
    assert to[0] == "sender@external.example.com"


def test_reply_all_hostile_50_header_addresses_hits_total_recipient_cap_422():
    "Public API."
    from django.conf import settings as django_settings

    assert django_settings.OUTBOUND_MAX_RECIPIENTS == 10

    user = make_user("w8_reply_all_header_cap")
    inbox, domain = byod_inbox(user, "w8-reply-all-header-cap.example.com")
    many_to = [f"to{i}@example.test" for i in range(25)]
    many_cc = [f"cc{i}@example.test" for i in range(25)]
    message = make_message(
        inbox,
        raw_bytes=basic_raw_message(),
        header_from="External Sender <sender@external.example.com>",
        header_to=many_to,
        header_cc=many_cc,
        subject="hello",
        provider_message_id="<orig-msg-1@external.example.com>",
    )
    client = auth_client(user)
    resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{message.public_id}/reply",
        {"body_text": "hi", "reply_all": True},
        format="json",
    )
    # Reaches the gate's own total-recipient cap (reply_target + 50 header
    # addresses = 51, no `cc` field involved at all), NOT the serializer's
    # per-field `cc` max_length.
    assert resp.status_code == 422, resp.json()
    assert resp.json()["error"]["code"] == "validation_error"
    assert OutboundEmail.objects.filter(user=user).count() == 0


def test_reply_all_small_positive_control_201():
    """Positive control for the above: a SMALL reply_all (well under the
    cap) still passes, proving the 422 above is genuinely load-bearing on
    recipient COUNT, not an always-422 regression on reply_all itself."""
    user = make_user("w8_reply_all_small_control")
    inbox, domain = byod_inbox(user, "w8-reply-all-small-control.example.com")
    message = make_message(
        inbox,
        raw_bytes=basic_raw_message(),
        header_from="External Sender <sender@external.example.com>",
        header_to=[inbox.address, "sender@external.example.com", "third@example.test"],
        header_cc=["fourth@example.test"],
        subject="hello",
        provider_message_id="<orig-msg-1@external.example.com>",
    )
    client = auth_client(user)
    resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{message.public_id}/reply",
        {"body_text": "hi", "reply_all": True},
        format="json",
    )
    assert resp.status_code == 201, resp.json()
    to = resp.json()["data"]["to"]
    assert "third@example.test" in to
    assert "fourth@example.test" in to


def test_reply_all_hostile_50_cc_serializer_cap_422():
    """Original test kept: a caller-supplied `cc` field over 10 items trips
    the SERIALIZER's per-field limit before ever reaching the gate's own
    total-recipient cap. This is a distinct, narrower guard from the
    header_to/header_cc extraction cap proven above — both are load-bearing
    but at different layers."""
    user = make_user("w8_reply_all_cap")
    inbox, domain = byod_inbox(user, "w8-reply-all-cap.example.com")
    message = make_message(
        inbox,
        raw_bytes=basic_raw_message(),
        header_from="External Sender <sender@external.example.com>",
        subject="hello",
        provider_message_id="<orig-msg-1@external.example.com>",
    )
    client = auth_client(user)
    resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{message.public_id}/reply",
        {"body_text": "hi", "reply_all": True, "cc": [f"r{i}@example.test" for i in range(11)]},
        format="json",
    )
    # Serializer caps `cc` at 10 items -> validation_error before ever
    # reaching the gate's own >10-total-recipients 422.
    assert resp.status_code == 422


# --- (12) Re:/Fwd: dedup -----------------------------------------------------


def test_subject_dedup_re_re_fwd_and_fw():
    user = make_user("w8_subject_dedup")
    inbox, domain = byod_inbox(user, "w8-subject-dedup.example.com")
    reply_message = make_message(
        inbox,
        raw_bytes=basic_raw_message(),
        header_from="External Sender <sender@external.example.com>",
        subject="Re: Re: Fwd: x",
        provider_message_id="<dedup-1@external.example.com>",
    )
    forward_message = make_message(
        inbox,
        raw_bytes=basic_raw_message(),
        header_from="External Sender <sender@external.example.com>",
        header_to=["dest@example.com"],
        subject="FW: x",
        provider_message_id="<dedup-2@external.example.com>",
    )
    client = auth_client(user)
    reply_resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{reply_message.public_id}/reply",
        {"body_text": "hi"},
        format="json",
    )
    forward_resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{forward_message.public_id}/forward",
        {"to": ["dest@example.test"]},
        format="json",
    )
    assert reply_resp.status_code == 201, reply_resp.json()
    assert forward_resp.status_code == 201, forward_resp.json()
    assert reply_resp.json()["data"]["subject"] == "Re: x"
    assert forward_resp.json()["data"]["subject"] == "Fwd: x"


# --- (13) forward 300KB body -> 201 truncated (not 422); \x00 sanitized -----


def test_forward_300kb_body_truncated_201_and_nul_body_sanitized():
    user = make_user("w8_forward_big")
    inbox, domain = byod_inbox(user, "w8-forward-big.example.com")
    big_body = "x" * 300_000
    message = make_message(
        inbox,
        raw_bytes=basic_raw_message(),
        header_from="External Sender <sender@external.example.com>",
        header_to=["dest@example.com"],
        subject="big",
        provider_message_id="<orig-big@external.example.com>",
        text_body=big_body,
    )
    client = auth_client(user)
    resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{message.public_id}/forward",
        {"to": ["dest@example.test"], "body_text": "fyi"},
        format="json",
    )
    assert resp.status_code == 201, resp.json()
    row = OutboundEmail.objects.get(public_id=resp.json()["data"]["id"])
    assert len(row.body_text.encode("utf-8")) <= 262_144
    assert "\x00" not in row.body_text


def test_sanitize_body_strips_nul_and_normalizes_linesep():
    "Public API."
    from apps.outbound.reply_forward import sanitize_body

    assert sanitize_body("line one\x00line two") == "line one line two"
    assert sanitize_body("a\r\nb\rc") == "a\nb\nc"


# --- (14) quota reply burns recipient_count boundary ------------------------


def test_quota_reply_burns_recipient_count_boundary(monkeypatch):
    from apps.accounts import entitlements as entitlements_module

    business_limits = dict(entitlements_module.settings.BILLING_PLAN_LIMITS["business"])
    business_limits["outbound_per_hour"] = 1
    business_limits["outbound_per_day"] = 100
    monkeypatch.setitem(entitlements_module.settings.BILLING_PLAN_LIMITS, "business", business_limits)

    user = make_user("w8_quota_boundary")
    inbox, domain = byod_inbox(user, "w8-quota-boundary.example.com")
    message = make_message(
        inbox,
        raw_bytes=basic_raw_message(),
        header_from="External Sender <sender@external.example.com>",
        subject="hello",
        provider_message_id="<orig-quota@external.example.com>",
    )
    client = auth_client(user)
    resp1 = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{message.public_id}/reply",
        {"body_text": "hi"},
        format="json",
    )
    assert resp1.status_code == 201, resp1.json()

    resp2 = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/compose",
        {"to": ["dest@example.test"], "subject": "x"},
        format="json",
    )
    assert resp2.status_code == 403
    assert resp2.json()["error"]["code"] == "outbound_quota_exceeded"


# --- reply_target_invalid: nothing parseable --------------------------------


def test_reply_target_invalid_when_nothing_parseable():
    user = make_user("w8_reply_target_invalid")
    inbox, domain = byod_inbox(user, "w8-reply-target-invalid.example.com")
    raw = b"Subject: hello\r\nMessage-ID: <no-from@external.example.com>\r\n\r\nbody\r\n"
    message = make_message(
        inbox,
        raw_bytes=raw,
        header_from="",
        subject="hello",
        provider_message_id="<no-from@external.example.com>",
    )
    message.envelope_from = ""
    message.save(update_fields=["envelope_from"])
    client = auth_client(user)
    resp = client.post(
        f"/api/v1/inboxes/{inbox.public_id}/messages/{message.public_id}/reply",
        {"body_text": "hi"},
        format="json",
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "reply_target_invalid"
