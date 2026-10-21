"""Additive OTP-enrichment fields (otp_context / otp_candidates / provider_hint).

These are serializer-computed, optional fields layered on top of the proven
`otp` / `otp_confidence` extraction. The hot path itself lives in
`apps.ingest.parser` and is covered by `apps/ingest/tests/test_parser.py`; here
we prove (a) the new ranking helper agrees with `extract_otp` and reuses its
scoring, (b) the three serializer fields behave as documented, and (c) the
`otp`/`otp_confidence` values are byte-for-byte identical for representative
fixtures (explicit regression assertion).
"""

import hashlib

import pytest

from apps.api.serializers import MessageSerializer, MessageSummarySerializer
from apps.inboxes.models import Inbox, Message
from apps.ingest.parser import extract_otp, extract_otp_candidates

pytestmark = pytest.mark.django_db(transaction=True)


# --- parser-level: ranking helper reuses the extractor's scoring --------------

REPRESENTATIVE_FIXTURES = [
    # (subject, text, html, expected_otp, expected_confidence)
    ("", "Your verification code is 654321", "", "654321", 0.95),
    ("", "Your code is 123 456", "", "123456", 0.95),
    ("Slack code", "Use 7755 to sign in", "", "7755", 0.35),
    ("", "Order #483920 costs $49.99 and was placed in 2026.", "", None, 0.0),
    ("", "Call phone +1 415 555 2671", "", None, 0.0),
    ("", "No text code", "<p>Verification code <b>667788</b></p>", "667788", 0.95),
]


@pytest.mark.parametrize(
    ("subject", "text", "html", "expected_otp", "expected_confidence"),
    REPRESENTATIVE_FIXTURES,
)
def test_otp_values_identical_after_enrichment(subject, text, html, expected_otp, expected_confidence):
    otp, confidence = extract_otp(subject, text, html)
    assert otp == expected_otp
    assert confidence == expected_confidence


def test_extract_otp_candidates_ranks_both_and_top_matches_primary():
    subject = ""
    text = "Your verification code is 123456. Thank you. Here is your reference number 987654."
    html = ""

    candidates = extract_otp_candidates(subject, text, html)
    otp, _confidence = extract_otp(subject, text, html)

    codes = [c["code"] for c in candidates]
    assert codes == ["123456", "987654"]
    assert candidates[0]["confidence"] == 0.95
    assert candidates[1]["confidence"] == 0.35
    # The primary otp is exactly the top-ranked candidate.
    assert otp == codes[0]


def test_extract_otp_candidates_empty_when_nothing_plausible():
    assert extract_otp_candidates("", "Order #483920 costs $49.99", "") == []


# --- serializer-level: the three computed fields ------------------------------

def _message(inbox, *, otp=None, detected_otp=None, subject="", text_body="", html_body="", header_from="Sender <sender@example.test>"):
    raw = f"Subject: {subject}\r\n\r\n{text_body}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    return Message.objects.create(
        inbox=inbox,
        dedupe_key=digest,
        envelope_from="sender@example.test",
        envelope_recipient=inbox.address,
        header_from=header_from,
        header_to=[inbox.address],
        header_cc=[],
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        otp=otp,
        otp_confidence=0.95 if otp else None,
        detected_otp=detected_otp if detected_otp is not None else otp,
        raw_bytes=raw,
        raw_sha256=digest,
        size_bytes=len(raw),
        ingest_status=Message.IngestStatus.PARSED,
    )


@pytest.fixture
def inbox(user):
    return Inbox.objects.create(user=user, routing_key="otp_enrich_box", label="Enrich")


def test_otp_context_contains_code_and_word_no_tags(inbox):
    msg = _message(inbox, otp="481920", text_body="Your code is 481920. It expires in 10 minutes.")

    context = MessageSerializer(msg).data["otp_context"]

    assert context is not None
    assert "481920" in context
    assert "code" in context
    assert len(context) <= 160
    assert "<" not in context and ">" not in context


def test_otp_context_strips_html(inbox):
    msg = _message(inbox, otp="481920", text_body="", html_body="<p>Your code is <b>481920</b></p>")

    context = MessageSerializer(msg).data["otp_context"]

    assert context is not None
    assert "481920" in context
    assert "<b>" not in context and "<p>" not in context


def test_otp_context_null_when_no_otp(inbox):
    msg = _message(inbox, otp=None, detected_otp=None, text_body="No numeric credential here")

    assert MessageSerializer(msg).data["otp_context"] is None


def test_otp_candidates_lists_also_ran_and_primary_stays_top(inbox):
    msg = _message(
        inbox,
        otp="123456",
        text_body="Your verification code is 123456. Thank you. Here is your reference number 987654.",
    )

    data = MessageSerializer(msg).data

    assert data["otp"] == "123456"
    candidates = data["otp_candidates"]
    assert candidates is not None
    assert [c["code"] for c in candidates] == ["987654"]
    assert candidates[0]["confidence"] == 0.35


def test_otp_candidates_null_for_single_code(inbox):
    msg = _message(inbox, otp="481920", text_body="Your code is 481920")

    assert MessageSerializer(msg).data["otp_candidates"] is None


def test_otp_candidates_null_when_no_otp(inbox):
    msg = _message(inbox, otp=None, detected_otp=None, text_body="no otp here")

    assert MessageSerializer(msg).data["otp_candidates"] is None


def test_provider_hint_from_known_domain(inbox):
    msg = _message(inbox, otp="123456", text_body="code", header_from="GitHub <noreply@github.com>")

    assert MessageSerializer(msg).data["provider_hint"] == "github"


def test_provider_hint_null_for_unknown_domain(inbox):
    msg = _message(inbox, otp="123456", text_body="code", header_from="Someone <someone@totally-unknown.test>")

    assert MessageSerializer(msg).data["provider_hint"] is None


def test_summary_serializer_exposes_new_fields(inbox):
    msg = _message(inbox, otp="123456", text_body="Your code is 123456", header_from="noreply@github.com")

    data = MessageSummarySerializer(msg).data

    assert data["otp_context"] is not None
    assert data["otp_candidates"] is None
    assert data["provider_hint"] == "github"


def test_otp_context_skips_same_digits_inside_a_url(inbox):
    # qafix2 #1: the code also appears inside an earlier link; the snippet must
    # anchor on the standalone code, not on the URL digits.
    text = "Open https://example.com/s?id=481920&x=" + "y" * 60 + " later. Your code is 481920 today."
    msg = _message(inbox, otp="481920", text_body=text)

    context = MessageSerializer(msg).data["otp_context"]

    assert context is not None
    assert "Your code is 481920" in context
    assert "https://" not in context


def test_otp_candidates_exclude_url_digits(inbox):
    msg = _message(
        inbox,
        otp="482193",
        text_body="Your verification code is 482193. Or sign in at https://example.com/auth?token=1234567",
    )

    assert MessageSerializer(msg).data["otp_candidates"] is None
