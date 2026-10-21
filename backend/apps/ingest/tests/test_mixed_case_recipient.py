"Public API."

import pytest
from django.test import override_settings

from apps.domains.models import Domain
from apps.inboxes.models import Inbox, Message
from apps.ingest.tests.test_stage_c_catch_all import make_catch_all_domain, make_domain, make_user
from apps.outbound.models import BounceEvent, OutboundEmail
from apps.outbound.verp import build_verp_localpart

pytestmark = pytest.mark.django_db(transaction=True)

SECRET = "test-ingest-secret"
SHARED = "in.inboxpipe.net"
BOUNCE_DOMAIN = "bounces.inboxpipe.net"
RAW = (
    b"From: Sender <sender@example.test>\r\n"
    b"To: visible@example.test\r\n"
    b"Message-ID: <mixed-case@example.test>\r\n"
    b"Subject: mixed case rcpt\r\n"
    b"\r\n"
    b"body\r\n"
)
INGEST = dict(INGEST_SHARED_SECRET=SECRET, INGEST_ALLOWED_CIDRS="172.30.0.0/24", MAIL_DOMAIN=SHARED)


def post(client, recipient, raw=RAW, envelope_from="bounce@example.test"):
    return client.post(
        "/internal/ingest",
        data=raw,
        content_type="application/octet-stream",
        HTTP_X_MAILSOCKET_INGEST_SECRET=SECRET,
        HTTP_X_ENVELOPE_TO=recipient,
        HTTP_X_ENVELOPE_FROM=envelope_from,
        REMOTE_ADDR="172.30.0.9",
    )


@override_settings(**INGEST)
@pytest.mark.parametrize(
    "recipient",
    [
        "8SJDBLCC@in.inboxpipe.net",  # the prod repro
        "8sjDbLcC@in.inboxpipe.net",
        "8sjdblcc@IN.InboxPipe.NET",  # domain case mix too
        "8SJDBLCC@IN.INBOXPIPE.NET",
    ],
)
def test_shared_domain_mixed_case_recipient_is_stored_in_existing_inbox(api_client, recipient):
    user = make_user("mc_shared")
    inbox = Inbox.objects.create(user=user, routing_key="8sjdblcc")

    response = post(api_client, recipient)

    assert response.status_code == 201, response.content
    message = Message.objects.get()
    assert message.inbox_id == inbox.pk
    assert bytes(message.raw_bytes) == RAW
    # Stored envelope recipient = lowercase canonical form.
    assert message.envelope_recipient == "8sjdblcc@in.inboxpipe.net"


@override_settings(**INGEST)
def test_shared_domain_mixed_case_unknown_recipient_still_404(api_client):
    make_user("mc_unknown")
    response = post(api_client, "NoSuchKey@in.inboxpipe.net")
    assert response.status_code == 404
    assert Message.objects.count() == 0


@override_settings(**INGEST)
def test_custom_domain_exact_inbox_mixed_case_is_stored(api_client):
    user = make_user("mc_custom_exact")
    domain = make_domain(user, "mc-exact.example.com")
    inbox = Inbox.objects.create(user=user, routing_key="storekey", domain=domain)

    response = post(api_client, "StoreKey@MC-Exact.Example.com")

    assert response.status_code == 201, response.content
    message = Message.objects.get()
    assert message.inbox_id == inbox.pk
    assert message.envelope_recipient == "storekey@mc-exact.example.com"


@override_settings(**INGEST)
@pytest.mark.parametrize("localpart", ["John.Doe", "INFO+Tag", "A"])
def test_custom_domain_catch_all_mixed_case_localpart_is_stored(api_client, localpart):
    user = make_user("mc_catchall")
    _, sentinel = make_catch_all_domain(user, "mc-catchall.example.com")

    response = post(api_client, f"{localpart}@mc-catchall.example.com")

    assert response.status_code == 201, response.content
    message = Message.objects.get()
    assert message.inbox_id == sentinel.pk
    assert message.envelope_recipient == f"{localpart.lower()}@mc-catchall.example.com"


@override_settings(**INGEST)
@pytest.mark.parametrize(
    "recipient",
    [
        "Bad Key@in.inboxpipe.net",  # still structurally invalid after folding
        "8SJ.DBLCC@in.inboxpipe.net",  # dot not allowed on the shared apex
        "8sjdblcc@in.inboxpipe.nét",  # non-ascii still refused
        "8SJDBLCC@in.inboxpipe.net\r\nX-Evil: yes",
    ],
)
def test_folding_does_not_widen_structural_checks(api_client, recipient):
    user = make_user("mc_struct")
    Inbox.objects.create(user=user, routing_key="8sjdblcc")
    assert post(api_client, recipient).status_code == 400
    assert Message.objects.count() == 0


def _hard_dsn(localpart):
    return (
        b"From: MAILER-DAEMON@mx.example.test\r\n"
        + f"To: {localpart}@{BOUNCE_DOMAIN}\r\n".encode()
        + b"Subject: Undelivered Mail Returned to Sender\r\n"
        b"Content-Type: multipart/report; report-type=delivery-status\r\n"
        b"\r\n"
        b"--boundary\r\n"
        b"Content-Type: message/delivery-status\r\n"
        b"\r\n"
        b"Final-Recipient: rfc822; gone@example.test\r\n"
        b"Action: failed\r\n"
        b"Status: 5.1.1\r\n"
        b"--boundary--\r\n"
    )


@override_settings(**INGEST, OUTBOUND_BOUNCE_DOMAIN=BOUNCE_DOMAIN, ENABLE_OUTBOUND=True)
def test_bounce_localpart_mixed_case_is_kept_verbatim_not_folded(api_client):
    """The VERP id is a case-sensitive token_urlsafe value + MAC. Folding it
    would change the public_id, so the MAC would no longer verify."""
    user = make_user("mc_bounce")
    domain = Domain.objects.create(user=user, name="mc-bounce.example.com")
    row = OutboundEmail.objects.create(
        user=user,
        domain=domain,
        from_address="hello@mc-bounce.example.com",
        to=["gone@example.test"],
        status=OutboundEmail.Status.SENT,
        recipient_count=1,
    )
    OutboundEmail.objects.filter(pk=row.pk).update(public_id="obe_MiXeDCaseId9")
    localpart = build_verp_localpart("obe_MiXeDCaseId9")

    response = post(
        api_client, f"{localpart}@Bounces.InboxPipe.net", raw=_hard_dsn(localpart), envelope_from=""
    )

    assert response.status_code == 201, response.content
    event = BounceEvent.objects.get()
    assert event.envelope_recipient == f"{localpart}@{BOUNCE_DOMAIN}"  # localpart verbatim, domain folded
    assert event.outbound_public_id == "obe_MiXeDCaseId9"  # MAC verified on the un-folded id
    assert event.parse_status == BounceEvent.ParseStatus.HARD
    row.refresh_from_db()
    assert row.status == OutboundEmail.Status.BOUNCED
    assert Message.objects.count() == 0
