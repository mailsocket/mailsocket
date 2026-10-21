import hashlib
import time
from datetime import timedelta
from email.message import EmailMessage
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.inboxes.models import Inbox, Message
from apps.ingest.parser import (
    ParsedFields,
    ParserLimitError,
    _bounded_extraction,
    extract_magic_link,
    extract_otp,
    extract_otp_candidates,
    mask_non_otp_spans,
    parse_message,
    parse_raw_message,
)

pytestmark = pytest.mark.django_db(transaction=True)
RECIPIENT = "parser_box@in.inboxpipe.net"


@pytest.fixture
def inbox(user):
    return Inbox.objects.create(user=user, routing_key="parser_box", label="Parser")


def make_message(inbox, raw, **overrides):
    digest = hashlib.sha256(raw).hexdigest()
    values = {
        "inbox": inbox,
        "dedupe_key": digest,
        "envelope_from": "sender@example.test",
        "envelope_recipient": RECIPIENT,
        "raw_bytes": raw,
        "raw_sha256": digest,
        "size_bytes": len(raw),
    }
    values.update(overrides)
    return Message.objects.create(**values)


def test_case_11_plain_text_headers_body_and_first_standalone_otp(inbox):
    raw = (
        b"From: Alice Example <alice@example.test>\r\n"
        b"To: Bob <bob@example.test>, carol@example.test\r\n"
        b"Cc: Dan <dan@example.test>\r\n"
        b"Message-ID: <parser-11@example.test>\r\n"
        b"Subject: Your sign-in code\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Ignore 123 and 123456789. First code 4567, then 87654321."
    )
    message = parse_message(make_message(inbox, raw).pk)

    assert message.ingest_status == Message.IngestStatus.PARSED
    assert message.provider_message_id == "<parser-11@example.test>"
    assert message.header_from == "Alice Example <alice@example.test>"
    assert message.header_to == ["Bob <bob@example.test>", "carol@example.test"]
    assert message.header_cc == ["Dan <dan@example.test>"]
    assert message.subject == "Your sign-in code"
    assert "First code 4567" in message.text_body
    assert message.detected_otp == "4567"
    assert message.parse_attempts == 1
    assert message.parsed_at is not None
    assert message.parse_lease_token is None


def test_root_attachment_body_not_used_as_text_or_otp(inbox):
    # Re-audit regression (@b8f9a69): a single-part root flagged as an
    # attachment must NOT become text_body/OTP. Attachment-disposition is
    # skipped at every level, including root.
    raw = (
        b"From: Alice <alice@example.test>\r\n"
        b"Subject: nope\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Disposition: attachment; filename=secret.txt\r\n\r\n"
        b"SECRET 7777"
    )
    message = parse_message(make_message(inbox, raw).pk)

    assert message.ingest_status == Message.IngestStatus.PARSED
    assert message.text_body == ""
    assert message.detected_otp is None


def test_case_12_multipart_prefers_plain_and_ignores_html_and_attachment(inbox):
    raw = (
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=outer\r\n\r\n"
        b"--outer\r\nContent-Type: multipart/alternative; boundary=inner\r\n\r\n"
        b"--inner\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nPlain code 2468\r\n"
        b"--inner\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<b>HTML secret 9999</b>\r\n"
        b"--inner--\r\n"
        b"--outer\r\nContent-Type: text/plain\r\nContent-Disposition: attachment; filename=secret.txt\r\n\r\nAttachment 7777\r\n"
        b"--outer--\r\n"
    )
    message = parse_message(make_message(inbox, raw).pk)

    assert message.text_body.strip() == "Plain code 2468"
    assert message.detected_otp == "2468"
    assert "HTML secret" not in message.text_body
    assert "Attachment" not in message.text_body
    assert message.html_body.strip() == "<b>HTML secret 9999</b>"


def test_case_13_charset_defect_is_bounded_and_raw_is_unchanged(inbox):
    raw = (
        b"Subject: damaged charset\r\n"
        b"Content-Type: text/plain; charset=x-does-not-exist\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n"
        b"code 5432 and bad byte \xff"
    )
    original_digest = hashlib.sha256(raw).hexdigest()
    message = parse_message(make_message(inbox, raw).pk)

    assert message.ingest_status == Message.IngestStatus.PARSED
    assert message.detected_otp == "5432"
    assert "\ufffd" in message.text_body
    assert len(message.text_body) <= 100_000
    assert bytes(message.raw_bytes) == raw
    assert message.raw_sha256 == original_digest


def test_case_16_attempt_budget_becomes_failed_and_raw_remains(inbox):
    raw = b"X-One: 1\r\nX-Two: 2\r\n\r\nbody"
    message = make_message(inbox, raw)

    with override_settings(MAX_PARSE_HEADERS=1, MAX_PARSE_ATTEMPTS=2):
        first = parse_message(message.pk)
        second = parse_message(message.pk)
        third = parse_message(message.pk)

    assert first.ingest_status == Message.IngestStatus.RECEIVED
    assert first.next_parse_at is not None
    assert second.ingest_status == Message.IngestStatus.FAILED
    assert second.parse_attempts == 2
    assert third.ingest_status == Message.IngestStatus.FAILED
    assert third.parse_attempts == 2
    assert "resource limit" in second.parse_error.lower()
    assert bytes(second.raw_bytes) == raw


def test_stale_lease_token_cannot_overwrite_newer_parsed_state(inbox):
    raw = b"Subject: fenced\r\n\r\nnew body 4321"
    message = make_message(
        inbox,
        raw,
        ingest_status=Message.IngestStatus.PARSED,
        subject="newer result",
        parsed_at=timezone.now(),
    )
    stale_token = __import__("uuid").uuid4()

    from apps.ingest.parser import finalize_success, record_failure

    fields = ParsedFields(None, "", [], [], "stale result", "stale", "", None, 0.0, None, [], None, ())
    assert finalize_success(message.pk, stale_token, fields) is False
    assert record_failure(message.pk, stale_token, RuntimeError("secret raw detail")) is False
    message.refresh_from_db()
    assert message.subject == "newer result"
    assert message.ingest_status == Message.IngestStatus.PARSED


def test_retry_backoff_is_bounded(inbox):
    raw = b"X-One: 1\r\nX-Two: 2\r\n\r\nbody"
    message = make_message(inbox, raw, parse_attempts=3)
    before = timezone.now()
    with override_settings(
        MAX_PARSE_HEADERS=1,
        MAX_PARSE_ATTEMPTS=5,
        PARSE_RETRY_BASE_SECONDS=60,
        PARSE_RETRY_MAX_SECONDS=100,
    ):
        message = parse_message(message.pk)
    assert before + timedelta(seconds=99) <= message.next_parse_at <= timezone.now() + timedelta(seconds=101)
    assert message.parse_error == "Parser resource limit: header count limit exceeded"


def test_mime_depth_guard_is_a_retryable_failure(inbox):
    raw = (
        b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=outer\r\n\r\n"
        b"--outer\r\nContent-Type: multipart/mixed; boundary=inner\r\n\r\n"
        b"--inner\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        b"--inner--\r\n--outer--\r\n"
    )
    with override_settings(MAX_PARSE_MIME_DEPTH=0):
        message = parse_message(make_message(inbox, raw).pk)
    assert message.ingest_status == Message.IngestStatus.RECEIVED
    assert message.parse_error == "Parser resource limit: MIME depth limit exceeded"
    assert bytes(message.raw_bytes) == raw


def test_mime_limits_fire_during_parse_before_full_tree_is_built():
    many_parts = (
        b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n"
        + (b"--x\r\nContent-Type: text/plain\r\n\r\nbody\r\n" * 1000)
        + b"--x--\r\n"
    )
    attachments = 0
    real_attach = EmailMessage.attach

    def count_attach(self, payload):
        nonlocal attachments
        attachments += 1
        return real_attach(self, payload)

    with override_settings(MAX_PARSE_MIME_PARTS=5), patch.object(EmailMessage, "attach", count_attach):
        with pytest.raises(ParserLimitError, match="MIME parts limit exceeded"):
            parse_raw_message(many_parts)
    assert attachments == 4

    deeply_nested = (b"Content-Type: message/rfc822\r\n\r\n" * 1000) + b"Subject: leaf\r\n\r\nbody"
    with override_settings(MAX_PARSE_MIME_DEPTH=5):
        with pytest.raises(ParserLimitError, match="MIME depth limit exceeded") as caught:
            parse_raw_message(deeply_nested)
    assert not isinstance(caught.value, RecursionError)


def test_attached_email_body_is_not_selected_for_body_or_otp(inbox):
    raw = (
        b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=outer\r\n\r\n"
        b"--outer\r\nContent-Type: message/rfc822\r\n"
        b"Content-Disposition: attachment; filename=secret.eml\r\n\r\n"
        b"Content-Type: text/plain\r\n\r\nATTACHMENT SECRET 7777\r\n"
        b"--outer--\r\n"
    )
    message = parse_message(make_message(inbox, raw).pk)
    assert message.ingest_status == Message.IngestStatus.PARSED
    assert message.text_body == ""
    assert message.detected_otp is None


def test_text_truncation_does_not_fabricate_otp(inbox):
    raw = b"Content-Type: text/plain; charset=utf-8\r\n\r\n" + (b"a" * 99_992) + b"123456789"
    message = parse_message(make_message(inbox, raw).pk)
    assert len(message.text_body.encode("utf-8")) == 100_000
    assert message.detected_otp is None


def test_extraction_is_bounded_independently_of_stored_text():
    raw = (
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        + (b"a" * 100_000)
        + b" code 654321 https://auth.test/verify?token=abcdefghijklmnopqrstuv"
    )
    with override_settings(MAX_PARSE_TEXT_BYTES=200_000, MAX_EXTRACT_BYTES=100_000):
        fields = parse_raw_message(raw)
    assert "654321" in fields.text_body
    assert fields.otp is None
    assert fields.magic_link is None


def test_pathological_200kb_extraction_completes_under_one_second():
    body = (("1234 <https://" * 15_000)[:200_000]).encode()
    raw = b"Content-Type: text/plain; charset=utf-8\r\n\r\n" + body

    started = time.perf_counter()
    parse_raw_message(raw)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"pathological parse took {elapsed:.3f}s"


def test_pathological_phone_run_extraction_completes_under_one_second():
    # Repeated "1234 " runs make every "1234" an OTP_RE candidate; the unpatched
    # phone-window walk rescans the whole run per candidate (O(n^2)).
    body = (("1234 " * 50_000)[:200_000]).encode()
    raw = b"Content-Type: text/plain; charset=utf-8\r\n\r\n" + body

    started = time.perf_counter()
    parse_raw_message(raw)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"pathological phone-run parse took {elapsed:.3f}s"


def test_bounded_extraction_on_solid_no_space_is_linear():
    # Regression (@c7b20d4): re.sub(r"\S*$", "", bounded) is O(k^2) on some
    # Python/regex builds for a long no-space run at the cut edge (
    # 27.9s). A 300KB solid run must complete well under 50ms.
    value = "A" * 300_000

    started = time.perf_counter()
    result = _bounded_extraction(value, 100_000)
    elapsed = time.perf_counter() - started

    assert result == "", "solid no-space run must strip the entire truncated token"
    assert elapsed < 0.050, f"_bounded_extraction on 300KB no-space run took {elapsed * 1000:.1f}ms"


OTP_SAMPLES = [
    ("GitHub authentication code: 123456", "", "", "123456"),
    ("", "Your Google verification code is 654321", "", "654321"),
    ("", "Stripe security code: 482193", "", "482193"),
    ("Slack code", "Use 7755 to sign in", "", "7755"),
    ("Notion login", "Your one-time code is 804211", "", "804211"),
    ("Vercel verification", "Code: 918273", "", "918273"),
    ("", "Your code is 123 456", "", "123456"),
    ("", "OTP 123-456", "", "123456"),
    ("", "Mã xác nh\u1eadn c\u1ee7a b\u1ea1n là 345678", "", "345678"),
    ("", "M\u1eadt kh\u1ea9u m\u1ed9t l\u1ea7n: 887766", "", "887766"),
    ("Code 445566", "Body code 112233", "", "445566"),
    ("", "No text code", "<p>Verification code <b>667788</b></p>", "667788"),
    ("", "Reference 7711", "", "7711"),
    ("", "Order #483920 costs $49.99 and was placed in 2026.", "", None),
    ("", "Invoice 778899 total €1200 due in 2025", "", None),
    ("", "Call phone +1 415 555 2671", "", None),
    ("", "ZIP 94107", "", None),
    ("", "The conference ran in 2024.", "", None),
    # P3-1: a real 4-digit code in the year range must survive when an OTP
    # keyword is adjacent, but a bare copyright/date year must still be rejected.
    ("", "Your verification code is 2025", "", "2025"),
    ("", "© 2024 Acme Corporation. All rights reserved.", "", None),
    ("", "Total: 1200 VND", "", None),
    ("", "No numeric credential here", "", None),
]


@pytest.mark.parametrize(("subject", "text", "html", "expected"), OTP_SAMPLES)
def test_otp_sample_set(subject, text, html, expected):
    otp, confidence = extract_otp(subject, text, html)
    assert otp == expected
    assert 0.0 <= confidence <= 1.0


MAGIC_SAMPLES = [
    (
        "Sign in: https://slack.test/auth?token=abcdefghijklmnopqrstuvwxyz123456",
        "",
        "https://slack.test/auth?token=abcdefghijklmnopqrstuvwxyz123456",
    ),
    (
        "",
        '<a href="https://notion.test/login?token=abcdefghijklmnopqrstuv">Log in</a>',
        "https://notion.test/login?token=abcdefghijklmnopqrstuv",
    ),
    (
        "https://vercel.test/verify/0123456789abcdef0123456789abcdef",
        "",
        "https://vercel.test/verify/0123456789abcdef0123456789abcdef",
    ),
    (
        "Verify https://app.test/verify?code=abcdefghijklmnopqrstuv "
        "unsubscribe https://app.test/unsubscribe?token=zzzzzzzzzzzzzzzzzzzzzz",
        "",
        "https://app.test/verify?code=abcdefghijklmnopqrstuv",
    ),
    (
        "Open <https://example.test/magic?\n token=abcdefghijklmnopqrstuv>",
        "",
        "https://example.test/magic?token=abcdefghijklmnopqrstuv",
    ),
    ("Docs: https://example.test/help", "", None),
    (
        "",
        '<a href="https://accounts.test/session?t=abcdefghijklmnopqrstuv">Xác nh\u1eadn</a>',
        "https://accounts.test/session?t=abcdefghijklmnopqrstuv",
    ),
    (
        "",
        '<a href="https://accounts.test/activate?k=abcdefghijklmnopqrstuv">Activate</a>',
        "https://accounts.test/activate?k=abcdefghijklmnopqrstuv",
    ),
    ("https://site.test/privacy?token=abcdefghijklmnopqrstuv", "", None),
    ("https://facebook.com/login?token=abcdefghijklmnopqrstuv", "", None),
    (
        "First https://site.test/confirm?token=abcdefghijklmnopqrstuv and duplicate "
        "https://site.test/confirm?token=abcdefghijklmnopqrstuv",
        "",
        "https://site.test/confirm?token=abcdefghijklmnopqrstuv",
    ),
    (
        "",
        '<a href="https://site.test/auth?login=abcdefghijklmnopqrstuv">Sign in</a>'
        '<a href="https://site.test/terms">Terms</a>',
        "https://site.test/auth?login=abcdefghijklmnopqrstuv",
    ),
    ("", '<a href="mailto:support@example.test">Email support</a>', None),
    (
        "https://myterms.example.com/verify?token=abcdefghijklmnopqrstuv",
        "",
        "https://myterms.example.com/verify?token=abcdefghijklmnopqrstuv",
    ),
]


@pytest.mark.parametrize(("text", "html", "expected"), MAGIC_SAMPLES)
def test_magic_link_sample_set(text, html, expected):
    magic_link, links = extract_magic_link(text, html)
    assert magic_link == expected
    assert len(links) == len(set(links))
    if expected:
        assert expected in links


def test_mailto_and_tracking_links_are_listed_but_never_ranked():
    html = (
        '<a href="mailto:support@example.test">Support</a>'
        '<a href="https://tracking.test/open?token=abcdefghijklmnopqrstuv"><img src="pixel"></a>'
    )
    magic_link, links = extract_magic_link("", html)
    assert magic_link is None
    assert links == [
        "mailto:support@example.test",
        "https://tracking.test/open?token=abcdefghijklmnopqrstuv",
    ]


def test_urls_over_2048_characters_are_not_extracted():
    oversized = "https://auth.test/verify?token=" + ("a" * 2_100)
    magic_link, links = extract_magic_link(oversized, f'<a href="{oversized}">Verify</a>')
    assert magic_link is None
    assert links == []


def test_parse_step_persists_extractors_and_back_compat(inbox):
    raw = (
        b"Subject: Verification code 123456\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b'<a href="https://auth.test/verify?token=abcdefghijklmnopqrstuv">Verify</a>'
    )
    message = parse_message(make_message(inbox, raw).pk)
    assert message.otp == message.detected_otp == "123456"
    assert message.otp_confidence == 0.95
    assert message.magic_link == "https://auth.test/verify?token=abcdefghijklmnopqrstuv"
    assert message.links == [message.magic_link]


# --- qafix2 #1: digits inside URLs / e-mails / tokens are never OTP candidates ---

QA_MAGIC_MAIL = (
    "Sign in by clicking https://example.com/auth/magic?token=qa1003abcdef123 - link expires soon."
)

URL_DIGIT_SAMPLES = [
    # (subject, text, html, expected_otp, expected_candidates)
    ("", QA_MAGIC_MAIL, "", None, []),
    ("", "Sign in: https://example.com/login/482913/confirm", "", None, []),
    ("", "Open https://example.com/a?id=771234&u=99887766", "", None, []),
    ("", "Continue at https://example.com/app#/verify/553311", "", None, []),
    ("", "Track: https://t.example.com/c/1234/5678?utm_id=9012&utm_source=mail", "", None, []),
    ("", "Visit www.example.com/r/445566 to log in", "", None, []),
    ("", "Go to example.com/verify/918273 now", "", None, []),
    ("", "Questions? Write to support4455@example.com", "", None, []),
    ("", "Your token qa1003abcdef123 is ready", "", None, []),
    ("Sign in https://x.test/l?t=12345678", "no code", "", None, []),
    (
        "",
        "",
        '<p>Click <a href="https://example.com/m?token=ab1234cd&amp;uid=667788">here</a> to sign in.</p>',
        None,
        [],
    ),
    ("", "", "<p>Open https://example.com/magic?token=7788990</p>", None, []),
    # A real code next to a link survives with its original confidence; the
    # link digits do not even appear as also-ran candidates.
    (
        "",
        "Your verification code is 482193. Or sign in at https://example.com/auth?token=1234567",
        "",
        "482193",
        ["482193"],
    ),
    ("", "Sign in https://example.com/auth?token=abc999111 or enter 5521 on the page", "", "5521", ["5521"]),
    ("", "https://example.com/auth?token=abc\n\n739214\n", "", "739214", ["739214"]),
    ("", "Visit https://example.com/login.\nCode: 6061", "", "6061", ["6061"]),
    (
        "",
        "",
        '<p>Your code: <b>806512</b></p><a href="https://example.com/v?t=111222">verify</a>',
        "806512",
        ["806512"],
    ),
    ("", "Your code is 482193.Thanks", "", "482193", ["482193"]),
]


@pytest.mark.parametrize(("subject", "text", "html", "expected", "expected_candidates"), URL_DIGIT_SAMPLES)
def test_url_email_token_digits_are_never_otp_candidates(subject, text, html, expected, expected_candidates):
    otp, _confidence = extract_otp(subject, text, html)
    assert otp == expected
    assert [c["code"] for c in extract_otp_candidates(subject, text, html)] == expected_candidates


def test_code_next_to_link_keeps_original_confidence():
    otp, confidence = extract_otp(
        "", "Your verification code is 482193. Or sign in at https://example.com/auth?token=1234567", ""
    )
    assert (otp, confidence) == ("482193", 0.95)
    otp, confidence = extract_otp(
        "", "Sign in https://example.com/auth?token=abc999111 or enter 5521 on the page", ""
    )
    assert (otp, confidence) == ("5521", 0.35)


def test_mask_preserves_length_and_only_blanks_masked_digits():
    text = "Code 482193 at https://ex.com/a?t=1003 mail a1@b.io"
    masked = mask_non_otp_spans(text)
    assert len(masked) == len(text)
    assert masked.startswith("Code 482193 at https://ex.com/a?t=____")
    assert masked.endswith("a_@b.io")


def test_qa_magic_link_mail_parses_to_link_without_otp(inbox):
    raw = (
        b"Subject: Sign in to Example\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n" + QA_MAGIC_MAIL.encode()
    )
    message = parse_message(make_message(inbox, raw).pk)
    assert message.otp is None
    assert message.detected_otp is None
    assert message.otp_confidence == 0.0
    assert message.magic_link == "https://example.com/auth/magic?token=qa1003abcdef123"


def test_pathological_url_mask_run_completes_under_one_second():
    body = (("a1.b2.c3.d4 x1@y2.zz https://e.com/1234 qa1003abcdef " * 4_000)[:200_000]).encode()
    raw = b"Content-Type: text/plain; charset=utf-8\r\n\r\n" + body
    started = time.perf_counter()
    fields = parse_raw_message(raw)
    elapsed = time.perf_counter() - started
    assert fields.otp is None
    assert elapsed < 1.0, f"pathological mask parse took {elapsed:.3f}s"


# --- qafix2 r2: URL/domain spans stop at prose delimiters; IDN hosts masked ---

R2_DELIMITER_SAMPLES: list[tuple[str, str | None, list[str]]] = [
    # (text, expected_otp, expected_candidates)
    ("Use https://example.com/login,OTP:482193", "482193", ["482193"]),
    ("Use example.com/login;code:482193", "482193", ["482193"]),
    ("Sign in (https://example.com/login) code 482193", "482193", ["482193"]),
    ("(https://x.io/a?b=1) 123456", "123456", ["123456"]),
    ("https://x.io/a.\n482193", "482193", ["482193"]),
    ("Sign in at https://x.io/login. Your code is 482193.", "482193", ["482193"]),
    ("Sign in at https://x.io/login.Your code is 482193", "482193", ["482193"]),
    ("[x](https://y/12345678) Your code is 654321", "654321", ["654321"]),
    ("Open \u201chttps://x.io/a?b=1\u201d 482193", "482193", ["482193"]),
    ("Open <https://x.io/a?b=1>482193", "482193", ["482193"]),
]

R2_IDN_SAMPLES: list[tuple[str, str | None, list[str]]] = [
    ("Visit 例子.公司/login/123456", None, []),
    ("Visit 例子.公司/login/123456 and enter code 778899", "778899", ["778899"]),
    ("Go to bücher.de/verify/918273 now", None, []),
    ("Go to xn--fsqu00a.xn--55qx5d/login/123456", None, []),
    # Space-less CJK prose is NOT a host: the code survives.
    ("验证码为123456.请勿泄露", "123456", ["123456"]),
]


def _r2_parts(part, value):
    if part == "subject":
        return value, "no code here", ""
    if part == "text":
        return "", value, ""
    return "", "", "<p>" + value.replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>") + "</p>"


@pytest.mark.parametrize("part", ["subject", "text", "html"])
@pytest.mark.parametrize(("value", "expected", "expected_candidates"), R2_DELIMITER_SAMPLES + R2_IDN_SAMPLES)
def test_r2_url_span_ends_at_prose_delimiters(part, value, expected, expected_candidates):
    if part == "subject" and "\n" in value:
        value = value.replace("\n", " ")
    subject, text, html = _r2_parts(part, value)
    otp, _confidence = extract_otp(subject, text, html)
    assert otp == expected
    assert [c["code"] for c in extract_otp_candidates(subject, text, html)] == expected_candidates


@pytest.mark.parametrize(
    "raw_body",
    [
        b"Use https://example.com/login,OTP:482193",
        b"Use example.com/login;code:482193",
    ],
)
def test_r2_gpt_strings_parse_end_to_end(inbox, raw_body):
    raw = b"Subject: Sign in\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n" + raw_body
    message = parse_message(make_message(inbox, raw).pk)
    assert message.otp == "482193"
    assert message.detected_otp == "482193"


def test_r2_url_keeps_its_own_query_fragment_and_paren_digits_masked():
    for text in (
        "Open https://example.com/a?id=771234&u=99887766",
        "Continue at https://example.com/app#/verify/553311",
        "Open https://en.wikipedia.org/wiki/Foo_(bar_123456) now",
        "Port https://example.com:8443/x?c=1:482193",
        "Visit 例子.公司/login/123456",
    ):
        assert extract_otp_candidates("", text, "") == [], text


def test_r2_mask_preserves_length_and_stops_at_delimiters():
    text = "Use https://example.com/l1,OTP:482193; 例子.公司/a/42 (https://x.io/a?b=1) 123456"
    masked = mask_non_otp_spans(text)
    assert len(masked) == len(text)
    assert masked == "Use https://example.com/l_,OTP:482193; 例子.公司/a/__ (https://x.io/a?b=_) 123456"


# --- qafix2 r3: "(" / ")" are URL characters (nested/unbalanced); only a ")"
# closing a "(" opened in the prose BEFORE the link ends the span ---

R3_PAREN_SAMPLES = [
    # (text, expected_otp, expected_candidates)
    ("https://x.io/a(123456", None, []),
    ("https://x.io/a((123456))", None, []),
    ("https://x.io/a(bar_(123456))", None, []),
    ("https://en.wikipedia.org/wiki/Foo_(bar) code 482193", "482193", ["482193"]),
    # Prose-wrapped links still release the code that follows them.
    ("(https://x.io/a?b=1) 123456", "123456", ["123456"]),
    ("(https://x.io/a(b)) 123456", "123456", ["123456"]),
    ("((https://x.io/a)) 123456", "123456", ["123456"]),
    ("(see x.io/a(bar_(778811))) code 482193", "482193", ["482193"]),
    ("Open x.io/a(123456 then enter (x.io/b) 654321", "654321", ["654321"]),
    # Code glued to the ")" that closes the prose "(": the trim releases it.
    ("(https://x.io/a?b=1)123456", "123456", ["123456"]),
    ("[x](https://y/a?t=12)654321 is your code", "654321", ["654321"]),
    ("(see x.io/a(b))482193", "482193", ["482193"]),
    # A stray ")" with no prose "(" open stays in the URL.
    ("(a) https://x.io/b)123456", None, []),
]


@pytest.mark.parametrize("part", ["subject", "text", "html"])
@pytest.mark.parametrize(("value", "expected", "expected_candidates"), R3_PAREN_SAMPLES)
def test_r3_url_parentheses_nested_or_unbalanced(part, value, expected, expected_candidates):
    subject, text, html = _r2_parts(part, value)
    otp, _confidence = extract_otp(subject, text, html)
    assert otp == expected
    assert [c["code"] for c in extract_otp_candidates(subject, text, html)] == expected_candidates


@pytest.mark.parametrize(
    "raw_body",
    [
        b"Sign in https://x.io/a(123456",
        b"Sign in https://x.io/a((123456))",
        b"Sign in https://x.io/a(bar_(123456))",
    ],
)
def test_r3_gpt_paren_urls_parse_end_to_end(inbox, raw_body):
    raw = b"Subject: Sign in\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n" + raw_body
    message = parse_message(make_message(inbox, raw).pk)
    assert message.otp is None
    assert message.detected_otp is None


def test_r3_mask_parentheses_exact():
    cases = {
        "https://x.io/a(123456": "https://x.io/a(______",
        "https://x.io/a((123456))": "https://x.io/a((______))",
        "https://x.io/a(bar_(123456))": "https://x.io/a(bar_(______))",
        "(https://x.io/a?b=1) 123456": "(https://x.io/a?b=_) 123456",
        "(https://x.io/a(1)) 123456": "(https://x.io/a(_)) 123456",
        "(x.io/a?b=1) 123456": "(x.io/a?b=_) 123456",
        "(https://x.io/a(1))123456": "(https://x.io/a(_))123456",
        "(x.io/a?b=1)123456": "(x.io/a?b=_)123456",
        "Use https://example.com/l1,OTP:482193": "Use https://example.com/l_,OTP:482193",
    }
    for text, expected in cases.items():
        masked = mask_non_otp_spans(text)
        assert len(masked) == len(text)
        assert masked == expected, text


# --- qafix2 r4: prose "(" depth is order-aware; a ")" with nothing open is
# ignored, so an earlier stray ")" cannot cancel the "(" wrapping a link ---

R4_ORDER_SAMPLES = [
    # (text, expected_otp, expected_candidates)
    (") (https://x.io/a?b=1)123456", "123456", ["123456"]),
    ("Prior note). (https://x.io/a?b=1)123456", "123456", ["123456"]),
    ("(a) (b) (https://x.io/q)482193", "482193", ["482193"]),
    ("((https://x.io/q))482193", "482193", ["482193"]),
    (")))(https://x.io/q)482193", "482193", ["482193"]),
    # Same order bug across a long (> short-gap) prose gap.
    (")" * 300 + " " + "(" + "x" * 300 + " https://x.io/q)482193", "482193", ["482193"]),
    # Controls: once the prose "(" is closed, a stray ")" stays in the URL.
    ("(a) ) https://x.io/b)123456", None, []),
    ("(a)) https://x.io/b)123456", None, []),
]


@pytest.mark.parametrize("part", ["subject", "text", "html"])
@pytest.mark.parametrize(("value", "expected", "expected_candidates"), R4_ORDER_SAMPLES)
def test_r4_prose_paren_depth_is_order_aware(part, value, expected, expected_candidates):
    subject, text, html = _r2_parts(part, value)
    otp, _confidence = extract_otp(subject, text, html)
    assert otp == expected
    assert [c["code"] for c in extract_otp_candidates(subject, text, html)] == expected_candidates


@pytest.mark.parametrize(
    "raw_body",
    [
        b"Sign in ) (https://x.io/a?b=1)123456",
        b"Prior note). (https://x.io/a?b=1)123456",
    ],
)
def test_r4_gpt_strings_parse_end_to_end(inbox, raw_body):
    raw = b"Subject: Sign in\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n" + raw_body
    message = parse_message(make_message(inbox, raw).pk)
    assert message.otp == "123456"
    assert message.detected_otp == "123456"


def test_r4_mask_order_aware_exact():
    cases = {
        ") (https://x.io/a?b=1)123456": ") (https://x.io/a?b=_)123456",
        "Prior note). (https://x.io/a?b=1)123456": "Prior note). (https://x.io/a?b=_)123456",
        ")))(https://x.io/q1)482193": ")))(https://x.io/q_)482193",
        ") (x.io/a?b=1)123456": ") (x.io/a?b=_)123456",
        "(a) ) https://x.io/b)123456": "(a) ) https://x.io/b)______",
    }
    for text, expected in cases.items():
        masked = mask_non_otp_spans(text)
        assert len(masked) == len(text)
        assert masked == expected, text


@pytest.mark.parametrize("short_gap", [0, 256, 10**9])
def test_r4_prose_paren_depth_matches_sequential_walk(monkeypatch, short_gap):
    import random

    from apps.ingest import parser as parser_module

    def reference(gap, depth):
        for char in gap:
            if char == "(":
                depth += 1
            elif char == ")" and depth:
                depth -= 1
        return depth

    # Exercise both the str.find hop path and the C prefix-minimum path.
    monkeypatch.setattr(parser_module, "_SHORT_GAP", short_gap)
    rng = random.Random(4)
    for _ in range(5000):
        gap = "".join(rng.choice("()a") for _ in range(rng.randint(0, 24)))
        depth = rng.randint(0, 4)
        text = "x" + gap + "y"
        got = parser_module._prose_paren_depth(text, 1, 1 + len(gap), depth)
        assert got == reference(gap, depth), (gap, depth)
