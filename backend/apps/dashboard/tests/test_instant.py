"Public API."

import hashlib

import pytest
from django.contrib.auth import get_user_model
from django.core import signing
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan
from apps.accounts.services import (
    INSTANT_CLAIM_MAX_AGE,
    INSTANT_CLAIM_SALT,
    INSTANT_INBOX_MINT_RATE_LIMIT,
    INSTANT_USER_EMAIL,
    get_instant_user,
)
from apps.domains.models import Domain
from apps.inboxes.models import Inbox, Message

pytestmark = pytest.mark.django_db(transaction=True)


def _mint_url():
    return reverse("dashboard:instant-email")


def _messages_url(email):
    return reverse("dashboard:instant-messages", kwargs={"email": email})


def _mint(client=None):
    client = client or Client()
    response = client.get(_mint_url())
    assert response.status_code == 200
    body = response.content.decode()
    address_line = [ln for ln in body.splitlines() if ln.startswith("Address:")][0]
    address = address_line.split("`")[1]
    return client, response, address


def _make_message(inbox, marker, **overrides):
    raw = f"Subject: {marker}\r\n\r\ncode body {marker}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    values = {
        "inbox": inbox,
        "dedupe_key": digest,
        "envelope_from": "security@acme.test",
        "envelope_recipient": inbox.address,
        "header_from": "Security <security@acme.test>",
        "header_to": [inbox.address],
        "header_cc": [],
        "subject": marker,
        "text_body": f"Your verification code is 482193. {marker}",
        "html_body": "",
        "raw_bytes": raw,
        "raw_sha256": digest,
        "size_bytes": len(raw),
        "ingest_status": Message.IngestStatus.PARSED,
        "otp": "482193",
        "otp_confidence": 0.95,
        "detected_otp": "482193",
    }
    values.update(overrides)
    return Message.objects.create(**values)


def _make_real_user(name):
    User = get_user_model()
    user = User.objects.create_user(username=name, email=f"{name}@example.test", password="password")
    Account.objects.create(user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now())
    AccountPlan.objects.create(user=user, plan=AccountPlan.Plan.PAID)
    return user


# --- I1: cookie isolation ----------------------------------------------------


def test_i1_cookie_a_cannot_read_inbox_b():
    client_a, _, address_a = _mint()
    client_b, _, address_b = _mint()
    assert address_a != address_b

    resp = client_a.get(_messages_url(address_b))
    assert resp.status_code == 404


# --- I2: no cookie / no claim -------------------------------------------------


def test_i2_no_cookie_no_claim_is_404_even_for_real_instant_inbox():
    _, _, address = _mint()
    anon = Client()  # fresh client, no cookie jar carried over
    resp = anon.get(_messages_url(address))
    assert resp.status_code == 404


# --- I3: forged claim ----------------------------------------------------


def test_i3_forged_claim_is_404_not_500():
    _, _, address = _mint()
    anon = Client()
    resp = anon.get(_messages_url(address), {"claim": "not-a-real-signed-value"})
    assert resp.status_code == 404


def test_i3_tampered_payload_claim_is_404():
    instant_user = get_instant_user()
    inbox = Inbox.objects.create(user=instant_user, label="", routing_key="tamperedbox")
    real_claim = signing.dumps({"i": inbox.public_id}, salt=INSTANT_CLAIM_SALT)
    # Flip a character to corrupt the signature.
    forged = real_claim[:-1] + ("a" if real_claim[-1] != "a" else "b")
    anon = Client()
    resp = anon.get(_messages_url(inbox.address), {"claim": forged})
    assert resp.status_code == 404


# --- I4: expired claim ---------------------------------------------------


def test_i4_expired_claim_is_404(monkeypatch):
    instant_user = get_instant_user()
    inbox = Inbox.objects.create(user=instant_user, label="", routing_key="expiredbox")
    claim = signing.dumps({"i": inbox.public_id}, salt=INSTANT_CLAIM_SALT)

    import time as _time

    real_time = _time.time
    monkeypatch.setattr(_time, "time", lambda: real_time() + INSTANT_CLAIM_MAX_AGE + 3600)

    anon = Client()
    resp = anon.get(_messages_url(inbox.address), {"claim": claim})
    assert resp.status_code == 404


# --- I5: real customer inbox never resolves, even with a "matching" claim ----


def test_i5_real_customer_inbox_address_is_404():
    real_user = _make_real_user("i5_customer")
    real_inbox = Inbox.objects.create(user=real_user, label="", routing_key="realcustomerbox")
    # Attacker cannot legitimately mint a claim for someone else's inbox, but
    # even if they forge the exact public_id (e.g. guessed/leaked), the
    # instant-user scope on the resolve step still excludes it.
    forged_claim = signing.dumps({"i": real_inbox.public_id}, salt=INSTANT_CLAIM_SALT)
    anon = Client()
    resp = anon.get(_messages_url(real_inbox.address), {"claim": forged_claim})
    assert resp.status_code == 404


# --- I6: custom-domain address never resolves --------------------------------


@override_settings(ENABLE_BYOD=True)
def test_i6_custom_domain_address_is_404():
    instant_user = get_instant_user()
    domain = Domain.objects.create(user=instant_user, name="verified-byod.example.com")
    domain.status = Domain.Status.VERIFIED
    domain.save(update_fields=["status"])
    inbox = Inbox.objects.create(
        user=instant_user, label="", routing_key="byodbox", domain=domain
    )
    claim = signing.dumps({"i": inbox.public_id}, salt=INSTANT_CLAIM_SALT)
    anon = Client()
    resp = anon.get(_messages_url(inbox.address), {"claim": claim})
    assert resp.status_code == 404


# --- I7: mint rate limit ------------------------------------------------


def test_i7_sixth_mint_from_one_ip_in_an_hour_is_429():
    for _ in range(INSTANT_INBOX_MINT_RATE_LIMIT):
        client = Client()
        resp = client.get(_mint_url())
        assert resp.status_code == 200
    resp = Client().get(_mint_url())
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


# --- I8: idempotent refresh -----------------------------------------------


def test_i8_refresh_with_live_cookie_returns_same_address_no_new_inbox():
    client, _, address = _mint(Client())
    before = Inbox.objects.filter(user__email=INSTANT_USER_EMAIL).count()
    resp2 = client.get(_mint_url())
    assert resp2.status_code == 200
    body2 = resp2.content.decode()
    address_line = [ln for ln in body2.splitlines() if ln.startswith("Address:")][0]
    address2 = address_line.split("`")[1]
    assert address2 == address
    after = Inbox.objects.filter(user__email=INSTANT_USER_EMAIL).count()
    assert after == before


# --- I9: response headers -------------------------------------------------


def test_i9_headers_on_200():
    _, resp, _ = _mint()
    assert resp.headers["Content-Type"].startswith("text/markdown")
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Robots-Tag"] == "noindex"


def test_i9_headers_on_404():
    resp = Client().get(_messages_url("unknownbox@in.inboxpipe.net"))
    assert resp.status_code == 404
    assert resp.headers["Content-Type"].startswith("text/markdown")
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Robots-Tag"] == "noindex"


def test_i9_headers_on_429():
    for _ in range(INSTANT_INBOX_MINT_RATE_LIMIT):
        assert Client().get(_mint_url()).status_code == 200
    resp = Client().get(_mint_url())
    assert resp.status_code == 429
    assert resp.headers["Content-Type"].startswith("text/markdown")
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Robots-Tag"] == "noindex"


# --- I10: markdown-injection hardening ------------------------------------


def test_i10_backtick_subject_is_stripped_and_stays_in_a_code_span():
    client, _, address = _mint()
    routing_key = address.split("@")[0]
    inbox = Inbox.objects.get(routing_key=routing_key, domain__isnull=True)
    evil_subject = "``` [click](https://evil.example) ```"
    _make_message(inbox, evil_subject)

    resp = client.get(_messages_url(address))
    assert resp.status_code == 200
    body = resp.content.decode()
    # Backticks from the attacker-controlled subject must be stripped —
    # the ONLY backticks in the body come from our own code-span wrappers.
    assert "```" not in body
    # The (defanged) content is still present, just neutralized.
    assert "click" in body
    assert "evil.example" in body


# --- R4 cross-check: metadata never appears in the .md body -----------------


def test_metadata_never_appears_in_instant_md_body():
    client, _, address = _mint()
    routing_key = address.split("@")[0]
    inbox = Inbox.objects.get(routing_key=routing_key, domain__isnull=True)
    inbox.metadata = {"internal_secret": "should-never-leak"}
    inbox.save(update_fields=["metadata"])
    _make_message(inbox, "has metadata set on inbox")

    resp = client.get(_messages_url(address))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "internal_secret" not in body
    assert "should-never-leak" not in body


# --- R3 FIX 1 cross-check: otp_context/links never appear in the .md body ---


def test_r3_otp_context_and_links_never_appear_in_instant_md_body():
    client, _, address = _mint()
    routing_key = address.split("@")[0]
    inbox = Inbox.objects.get(routing_key=routing_key, domain__isnull=True)
    _make_message(
        inbox,
        "r3 instant msg",
        magic_link="https://example.test/login?token=r3instantsekrit",
        links=["https://example.test/login?token=r3instantsekrit"],
    )

    resp = client.get(_messages_url(address))
    assert resp.status_code == 200
    body = resp.content.decode()
    # The hand-built .md line vocabulary never emits an "otp_context" label
    # or a "links" line — magic_link itself IS an allowed envelope field
    # (it gets its own "Magic link:" line), so only the otp_context/links
    # *labels* are the thing under test here, not the magic-link URL string.
    assert "otp_context" not in body
    assert "\n- Links:" not in body
    assert "links" not in body.lower().split("magic link")[0]





def _encoded_word(text):
    import base64

    return "=?utf-8?B?" + base64.b64encode(text.encode()).decode() + "?="


def _ingest_real_mail(address, subject_header, from_header):
    """Store + parse a real RFC822 mail so the subject/from go through the
    production RFC2047 decoding, not a hand-built Message row."""
    from apps.ingest.parser import parse_message
    from apps.ingest.services import store_message

    raw = (
        f"From: {from_header}\r\nTo: {address}\r\nSubject: {subject_header}\r\n"
        f"Message-ID: <g3-{hashlib.sha1(subject_header.encode()).hexdigest()}@x.test>\r\n"
        "\r\nYour code is 482193.\r\n"
    ).encode()
    stored = store_message(
        raw_bytes=raw, headers={"X-Envelope-To": address, "X-Envelope-From": "sec@acme.test"}
    )
    return parse_message(stored.message.id)


@pytest.mark.parametrize(
    "payload",
    [
        "x\n## FAKE\n- Magic link: https://evil.example/x",
        "x\r\n## FAKE",
        "x\u2028## FAKE",
        "x\u2029## FAKE",
        "x\x85## FAKE",
        "x\x0b## FAKE\x0c",
        "x\r## FAKE",
    ],
    ids=["lf", "crlf", "u2028", "u2029", "nel", "vt-ff", "cr"],
)
def test_g3_encoded_newline_subject_renders_as_one_line(payload):
    client, _, address = _mint()
    message = _ingest_real_mail(address, _encoded_word(payload), "Sec <sec@acme.test>")
    # Precondition: the parser really does keep the line break (the attack).
    assert any(ch in message.subject for ch in "\n\r\u2028\u2029\x85\x0b\x0c")

    body = client.get(_messages_url(address)).content.decode()
    lines = body.split("\n")
    assert not any(line.startswith("## FAKE") for line in lines)
    assert not any(line.startswith("- Magic link") for line in lines)
    heading = [line for line in lines if "FAKE" in line]
    assert len(heading) == 1 and heading[0].startswith("## `")
    assert heading[0].endswith("`")
    for sep in ("\r", "\u2028", "\u2029", "\x85", "\x0b", "\x0c"):
        assert sep not in body


def test_g3_encoded_newline_from_cannot_forge_rows():
    client, _, address = _mint()
    evil_from = _encoded_word("Bank\n- Magic link: https://evil.example") + " <a@acme.test>"
    _ingest_real_mail(address, "hello", evil_from)
    body = client.get(_messages_url(address)).content.decode()
    from_lines = [line for line in body.split("\n") if line.startswith("- ")]
    assert not any(line.startswith("- Magic link") for line in from_lines)
    assert sum(1 for line in from_lines if line.startswith("- From:")) == 1


def test_g3_c0_and_bidi_controls_are_neutralised():
    client, _, address = _mint()
    inbox = Inbox.objects.get(routing_key=address.split("@")[0], domain__isnull=True)
    _make_message(inbox, "a\x01b\x1bc\u202ed\u2066e\x7ff")
    body = client.get(_messages_url(address)).content.decode()
    line = [ln for ln in body.split("\n") if ln.startswith("## `a")][0]
    for ch in "\x01\x1b\u202e\u2066\x7f":
        assert ch not in line
    assert line == "## `a b c d e f`"
