"Public API."
import pytest
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan
from apps.accounts.services import create_api_key
from apps.domains.models import Domain
from apps.outbound.models import BounceEvent, OutboundEmail, SuppressionEntry
from apps.outbound.verp import build_verp_localpart

pytestmark = pytest.mark.django_db(transaction=True)

SECRET = "test-ingest-secret"
BOUNCE_DOMAIN = "bounces.inboxpipe.net"


def _hard_dsn(localpart: str, final_recipient: str | None = None) -> bytes:
    extra = (
        f"Final-Recipient: rfc822; {final_recipient}\r\n".encode() if final_recipient else b""
    )
    return (
        b"From: MAILER-DAEMON@mx.example.test\r\n"
        + f"To: {localpart}@{BOUNCE_DOMAIN}\r\n".encode()
        + b"Subject: Undelivered Mail Returned to Sender\r\n"
        b"Content-Type: multipart/report; report-type=delivery-status\r\n"
        b"\r\n"
        b"--boundary\r\n"
        b"Content-Type: message/delivery-status\r\n"
        b"\r\n" + extra +
        b"Action: failed\r\n"
        b"Status: 5.1.1\r\n"
        b"Diagnostic-Code: smtp; 550 5.1.1 No such user\r\n"
        b"--boundary--\r\n"
    )


def _soft_dsn(localpart: str) -> bytes:
    return (
        b"From: MAILER-DAEMON@mx.example.test\r\n"
        + f"To: {localpart}@{BOUNCE_DOMAIN}\r\n".encode()
        + b"Subject: Delayed Mail\r\n"
        b"Content-Type: multipart/report; report-type=delivery-status\r\n"
        b"\r\n"
        b"--boundary\r\n"
        b"Content-Type: message/delivery-status\r\n"
        b"\r\n"
        b"Action: delayed\r\n"
        b"Status: 4.2.2\r\n"
        b"--boundary--\r\n"
    )


def _post_ingest(client, raw, recipient, **overrides):
    headers = {
        "HTTP_X_MAILSOCKET_INGEST_SECRET": SECRET,
        "HTTP_X_ENVELOPE_TO": recipient,
        "HTTP_X_ENVELOPE_FROM": "",
        "REMOTE_ADDR": "172.30.0.9",
    }
    headers.update(overrides)
    return client.post(
        "/internal/ingest", data=raw, content_type="application/octet-stream", **headers
    )


def make_business_user(name):
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user, plan=AccountPlan.Plan.BUSINESS)
    return user


def eligible_domain(user, name):
    domain = Domain.objects.create(user=user, name=name)
    domain.status = Domain.Status.VERIFIED
    domain.dkim_status = Domain.DkimStatus.VERIFIED
    domain.dkim_selector = "msout1"
    domain.dkim_public_key_txt = "v=DKIM1; k=rsa; p=fake"
    from apps.outbound.dkim_keys import encrypt_private_key, generate_keypair

    private_pem, _ = generate_keypair()
    domain.dkim_private_key_enc = encrypt_private_key(private_pem)
    domain.save()
    return domain


def auth_client(user):
    from rest_framework.test import APIClient

    _, token = create_api_key(user, "bounce-chain-test")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    OUTBOUND_BOUNCE_DOMAIN=BOUNCE_DOMAIN,
    ENABLE_OUTBOUND=True,
)
def test_full_bounce_chain_hard_dsn_to_bounced_to_suppressed_422(api_client):
    """DSN => sent->bounced => SuppressionEntry => subsequent enqueue 422 —
    fed through the REAL /internal/ingest endpoint end to end."""
    user = make_business_user("bounce_chain_hard")
    domain = eligible_domain(user, "bounce-chain-hard.example.com")
    outbound_row = OutboundEmail.objects.create(
        user=user,
        domain=domain,
        from_address=f"hello@{domain.name}",
        to=["deadmailbox@example.test"],
        status=OutboundEmail.Status.SENT,
        recipient_count=1,
    )
    OutboundEmail.objects.filter(pk=outbound_row.pk).update(public_id="obe_dsntest01")
    outbound_row.refresh_from_db()

    localpart = build_verp_localpart("obe_dsntest01")
    raw = _hard_dsn(localpart, final_recipient="deadmailbox@example.test")
    resp = _post_ingest(api_client, raw, f"{localpart}@{BOUNCE_DOMAIN}")
    assert resp.status_code == 201, resp.content

    outbound_row.refresh_from_db()
    assert outbound_row.status == OutboundEmail.Status.BOUNCED
    assert outbound_row.bounced_at is not None
    assert SuppressionEntry.objects.filter(email_normalized="deadmailbox@example.test").exists()

    event = BounceEvent.objects.get(outbound_public_id="obe_dsntest01")
    assert event.parse_status == BounceEvent.ParseStatus.HARD
    assert event.processed_at is not None
    assert bytes(event.raw_bytes) == raw

    client = auth_client(user)
    send_resp = client.post(
        "/api/v1/outbound_emails",
        {"from_address": f"hello@{domain.name}", "to": ["deadmailbox@example.test"]},
        format="json",
    )
    assert send_resp.status_code == 422
    assert send_resp.json()["error"]["code"] == "recipient_rejected"


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    OUTBOUND_BOUNCE_DOMAIN=BOUNCE_DOMAIN,
    ENABLE_OUTBOUND=True,
)
def test_soft_bounce_dsn_records_only_no_suppression_no_status_change(api_client):
    user = make_business_user("bounce_chain_soft")
    domain = eligible_domain(user, "bounce-chain-soft.example.com")
    outbound_row = OutboundEmail.objects.create(
        user=user,
        domain=domain,
        from_address=f"hello@{domain.name}",
        to=["stillthere@example.test"],
        status=OutboundEmail.Status.SENT,
        recipient_count=1,
    )
    OutboundEmail.objects.filter(pk=outbound_row.pk).update(public_id="obe_dsntest02")
    outbound_row.refresh_from_db()

    localpart = build_verp_localpart("obe_dsntest02")
    resp = _post_ingest(api_client, _soft_dsn(localpart), f"{localpart}@{BOUNCE_DOMAIN}")
    assert resp.status_code == 201, resp.content

    outbound_row.refresh_from_db()
    assert outbound_row.status == OutboundEmail.Status.SENT  # unchanged
    assert not SuppressionEntry.objects.filter(email_normalized="stillthere@example.test").exists()

    event = BounceEvent.objects.get(outbound_public_id="obe_dsntest02")
    assert event.parse_status == BounceEvent.ParseStatus.SOFT
    assert event.processed_at is not None


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    OUTBOUND_BOUNCE_DOMAIN=BOUNCE_DOMAIN,
    ENABLE_OUTBOUND=True,
)
def test_hard_bounce_on_unknown_public_id_is_safe_noop_never_creates_customer_inbox(api_client):
    """A foreign/unknown obe_ public_id (still MAC-valid under OUR key) must
    never 500 and must never fall through to resolve_inbox as an unknown
    recipient (which would 404 for a different reason and, worse, could
    match a real Inbox by accident)."""
    localpart = build_verp_localpart("obe_doesnotexist")
    resp = _post_ingest(api_client, _hard_dsn(localpart), f"{localpart}@{BOUNCE_DOMAIN}")
    assert resp.status_code == 201, resp.content
    assert OutboundEmail.objects.count() == 0
    assert SuppressionEntry.objects.count() == 0
    event = BounceEvent.objects.get()
    assert event.outbound_public_id == "obe_doesnotexist"
    # record_hard_bounce's no-op branch still ran (row not found -> return),
    # but the DSN classified fine, so this is HARD + processed, not left for
    # replay — the public_id itself, not the classification, was foreign.
    assert event.parse_status == BounceEvent.ParseStatus.HARD
    assert event.processed_at is not None


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    OUTBOUND_BOUNCE_DOMAIN=BOUNCE_DOMAIN,
    ENABLE_OUTBOUND=True,
)
def test_unparseable_bounce_localpart_left_for_replay_never_500_never_resolve_inbox(api_client):
    """A malformed obe_ localpart (fails BOUNCE_LOCALPART_RE) must be
    stored raw and left unprocessed — never routed through resolve_inbox."""
    resp = _post_ingest(
        api_client, _hard_dsn("obe_!!!not-a-valid-shape"), f"obe_!!!not-a-valid-shape@{BOUNCE_DOMAIN}"
    )
    assert resp.status_code == 201, resp.content
    event = BounceEvent.objects.get()
    assert event.parse_status == BounceEvent.ParseStatus.UNPARSED
    assert event.processed_at is None
    assert event.outbound_public_id == ""
    assert OutboundEmail.objects.count() == 0


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    OUTBOUND_BOUNCE_DOMAIN=BOUNCE_DOMAIN,
    ENABLE_OUTBOUND=True,
)
def test_bounce_body_not_a_dsn_left_unprocessed_for_replay(api_client):
    localpart = build_verp_localpart("obe_notadsn12345")
    not_a_dsn = b"From: someone@example.test\r\nSubject: hi\r\n\r\njust a normal email\r\n"
    resp = _post_ingest(api_client, not_a_dsn, f"{localpart}@{BOUNCE_DOMAIN}")
    assert resp.status_code == 201, resp.content
    event = BounceEvent.objects.get()
    assert event.outbound_public_id == "obe_notadsn12345"
    assert event.parse_status == BounceEvent.ParseStatus.UNPARSED
    assert event.processed_at is None


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    OUTBOUND_BOUNCE_DOMAIN=BOUNCE_DOMAIN,
    ENABLE_OUTBOUND=True,
)
def test_hard_bounce_reaches_auto_pause_in_prod_path(api_client):
    "Public API."
    user = make_business_user("bounce_chain_autopause")
    domain = eligible_domain(user, "bounce-chain-autopause.example.com")
    for _ in range(39):
        OutboundEmail.objects.create(
            user=user, domain=domain, from_address=f"hello@{domain.name}",
            to=["x@example.com"], status=OutboundEmail.Status.SENT, recipient_count=1,
        )
    for _ in range(10):
        OutboundEmail.objects.create(
            user=user, domain=domain, from_address=f"hello@{domain.name}",
            to=["x@example.com"], status=OutboundEmail.Status.BOUNCED, recipient_count=1,
        )
    tipping_row = OutboundEmail.objects.create(
        user=user, domain=domain, from_address=f"hello@{domain.name}",
        to=["tipping@example.test"], status=OutboundEmail.Status.SENT, recipient_count=1,
    )
    OutboundEmail.objects.filter(pk=tipping_row.pk).update(public_id="obe_tipping0001")
    tipping_row.refresh_from_db()

    localpart = build_verp_localpart("obe_tipping0001")
    resp = _post_ingest(
        api_client,
        _hard_dsn(localpart, final_recipient="tipping@example.test"),
        f"{localpart}@{BOUNCE_DOMAIN}",
    )
    assert resp.status_code == 201, resp.content

    tipping_row.refresh_from_db()
    assert tipping_row.status == OutboundEmail.Status.BOUNCED
    account = Account.objects.get(user=user)
    assert account.outbound_paused_at is not None  # auto-pause fired via ingest, in prod code path


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    OUTBOUND_BOUNCE_DOMAIN=BOUNCE_DOMAIN,
    ENABLE_OUTBOUND=False,
)
def test_flag_off_bounce_recognition_is_inert_and_bit_identical(api_client):
    """ENABLE_OUTBOUND off => _is_bounce_recipient must return False
    unconditionally, so an obe_ localpart on OUTBOUND_BOUNCE_DOMAIN is
    treated as an ordinary (unroutable, in this test unconfigured)
    recipient and falls through to resolve_inbox's normal 404 — no
    BounceEvent row is ever created. This proves the CODE FIX 0 wiring is
    genuinely dead weight with the flag off, not merely unreachable by
    deploy-domain absence (see test_outbound_flag_off.py's equivalent
    store_message()-level test for the direct-call proof; the pre-existing
    ingest suite's own bit-identical pass is the parity proof, run
    separately in the full suite)."""
    from django.conf import settings

    assert settings.ENABLE_OUTBOUND is False
    localpart = build_verp_localpart("obe_dsntest01")
    resp = _post_ingest(api_client, _hard_dsn(localpart), f"{localpart}@{BOUNCE_DOMAIN}")
    assert resp.status_code == 404
    assert BounceEvent.objects.count() == 0


# --- D2 Part A/B new tests () -------------------


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    OUTBOUND_BOUNCE_DOMAIN=BOUNCE_DOMAIN,
    ENABLE_OUTBOUND=True,
)
def test_forged_bounce_without_mac_is_inert(api_client):
    """Legacy MAC-less obe_<id>@bounce shape (no ``.``) fails
    BOUNCE_LOCALPART_RE entirely -> UNPARSED, public_id blank, row untouched."""
    user = make_business_user("d2_no_mac")
    domain = eligible_domain(user, "d2-no-mac.example.com")
    outbound_row = OutboundEmail.objects.create(
        user=user, domain=domain, from_address=f"hello@{domain.name}",
        to=["victim@example.test"], status=OutboundEmail.Status.SENT, recipient_count=1,
    )
    OutboundEmail.objects.filter(pk=outbound_row.pk).update(public_id="obe_realid12345")
    outbound_row.refresh_from_db()

    resp = _post_ingest(
        api_client, _hard_dsn("obe_realid12345"), f"obe_realid12345@{BOUNCE_DOMAIN}"
    )
    assert resp.status_code == 201, resp.content

    event = BounceEvent.objects.get()
    assert event.parse_status == BounceEvent.ParseStatus.UNPARSED
    assert event.outbound_public_id == ""
    assert event.processed_at is None

    outbound_row.refresh_from_db()
    assert outbound_row.status == OutboundEmail.Status.SENT
    assert SuppressionEntry.objects.count() == 0


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    OUTBOUND_BOUNCE_DOMAIN=BOUNCE_DOMAIN,
    ENABLE_OUTBOUND=True,
)
def test_bounce_with_wrong_mac_is_inert(api_client):
    user = make_business_user("d2_wrong_mac")
    domain = eligible_domain(user, "d2-wrong-mac.example.com")
    outbound_row = OutboundEmail.objects.create(
        user=user, domain=domain, from_address=f"hello@{domain.name}",
        to=["victim2@example.test"], status=OutboundEmail.Status.SENT, recipient_count=1,
    )
    OutboundEmail.objects.filter(pk=outbound_row.pk).update(public_id="obe_realid22222")
    outbound_row.refresh_from_db()

    good = build_verp_localpart("obe_realid22222")
    public_id_part, mac_part = good.rsplit(".", 1)
    flipped_char = "0" if mac_part[0] != "0" else "1"
    bad = f"{public_id_part}.{flipped_char}{mac_part[1:]}"

    resp = _post_ingest(api_client, _hard_dsn(bad), f"{bad}@{BOUNCE_DOMAIN}")
    assert resp.status_code == 201, resp.content

    event = BounceEvent.objects.get()
    assert event.parse_status == BounceEvent.ParseStatus.UNPARSED
    assert event.outbound_public_id == ""

    outbound_row.refresh_from_db()
    assert outbound_row.status == OutboundEmail.Status.SENT
    assert SuppressionEntry.objects.count() == 0


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    OUTBOUND_BOUNCE_DOMAIN=BOUNCE_DOMAIN,
    ENABLE_OUTBOUND=True,
)
def test_bounce_mac_from_other_key_is_inert(api_client):
    with override_settings(OUTBOUND_DKIM_MASTER_KEY="a-completely-different-key"):
        localpart = build_verp_localpart("obe_otherkeyid1")

    resp = _post_ingest(api_client, _hard_dsn(localpart), f"{localpart}@{BOUNCE_DOMAIN}")
    assert resp.status_code == 201, resp.content
    event = BounceEvent.objects.get()
    assert event.parse_status == BounceEvent.ParseStatus.UNPARSED
    assert event.outbound_public_id == ""


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    OUTBOUND_BOUNCE_DOMAIN=BOUNCE_DOMAIN,
    ENABLE_OUTBOUND=True,
)
def test_valid_mac_hard_bounce_processed_including_uppercase(api_client):
    user = make_business_user("d2_valid_mac")
    domain = eligible_domain(user, "d2-valid-mac.example.com")
    outbound_row = OutboundEmail.objects.create(
        user=user, domain=domain, from_address=f"hello@{domain.name}",
        to=["uppermac@example.test"], status=OutboundEmail.Status.SENT, recipient_count=1,
    )
    OutboundEmail.objects.filter(pk=outbound_row.pk).update(public_id="obe_uppermacid1")
    outbound_row.refresh_from_db()

    localpart = build_verp_localpart("obe_uppermacid1")
    public_id_part, mac_part = localpart.rsplit(".", 1)
    upper_localpart = f"{public_id_part}.{mac_part.upper()}"

    resp = _post_ingest(
        api_client,
        _hard_dsn(upper_localpart, final_recipient="uppermac@example.test"),
        f"{upper_localpart}@{BOUNCE_DOMAIN}",
    )
    assert resp.status_code == 201, resp.content

    outbound_row.refresh_from_db()
    assert outbound_row.status == OutboundEmail.Status.BOUNCED
    assert SuppressionEntry.objects.filter(email_normalized="uppermac@example.test").exists()


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    OUTBOUND_BOUNCE_DOMAIN=BOUNCE_DOMAIN,
    ENABLE_OUTBOUND=True,
)
def test_multi_recipient_bounce_does_not_globally_suppress(api_client):
    user = make_business_user("d2_multi_rcpt")
    domain = eligible_domain(user, "d2-multi-rcpt.example.com")
    outbound_row = OutboundEmail.objects.create(
        user=user, domain=domain, from_address=f"hello@{domain.name}",
        to=["one@example.test", "two@example.test"], cc=["three@example.test"],
        status=OutboundEmail.Status.SENT, recipient_count=3,
    )
    OutboundEmail.objects.filter(pk=outbound_row.pk).update(public_id="obe_multircpt01")
    outbound_row.refresh_from_db()

    localpart = build_verp_localpart("obe_multircpt01")
    resp = _post_ingest(
        api_client,
        _hard_dsn(localpart, final_recipient="one@example.test"),
        f"{localpart}@{BOUNCE_DOMAIN}",
    )
    assert resp.status_code == 201, resp.content

    outbound_row.refresh_from_db()
    assert outbound_row.status == OutboundEmail.Status.BOUNCED
    assert SuppressionEntry.objects.count() == 0


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    OUTBOUND_BOUNCE_DOMAIN=BOUNCE_DOMAIN,
    ENABLE_OUTBOUND=True,
)
def test_single_rcpt_final_recipient_mismatch_no_suppress(api_client):
    user = make_business_user("d2_mismatch")
    domain = eligible_domain(user, "d2-mismatch.example.com")
    outbound_row = OutboundEmail.objects.create(
        user=user, domain=domain, from_address=f"hello@{domain.name}",
        to=["real@example.test"], status=OutboundEmail.Status.SENT, recipient_count=1,
    )
    OutboundEmail.objects.filter(pk=outbound_row.pk).update(public_id="obe_mismatch001")
    outbound_row.refresh_from_db()

    localpart = build_verp_localpart("obe_mismatch001")
    resp = _post_ingest(
        api_client,
        _hard_dsn(localpart, final_recipient="other@x.test"),
        f"{localpart}@{BOUNCE_DOMAIN}",
    )
    assert resp.status_code == 201, resp.content

    outbound_row.refresh_from_db()
    assert outbound_row.status == OutboundEmail.Status.BOUNCED
    assert SuppressionEntry.objects.count() == 0


@override_settings(
    INGEST_SHARED_SECRET=SECRET,
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    OUTBOUND_BOUNCE_DOMAIN=BOUNCE_DOMAIN,
    ENABLE_OUTBOUND=True,
)
def test_malformed_mac_localparts_never_500(api_client):
    for bad_localpart in (
        "obe_x.",
        "obe_x.zz",
        "obe_" + "x" * 60 + ".0" * 10,
        "obe_nothex.notahexmacc0",
    ):
        BounceEvent.objects.all().delete()
        resp = _post_ingest(api_client, _hard_dsn(bad_localpart), f"{bad_localpart}@{BOUNCE_DOMAIN}")
        assert resp.status_code == 201, (bad_localpart, resp.content)
        event = BounceEvent.objects.get()
        assert event.parse_status == BounceEvent.ParseStatus.UNPARSED
