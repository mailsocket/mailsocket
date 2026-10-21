"Public API."

import pytest

from apps.feedback.alerts import scrub


@pytest.mark.parametrize(
    "text",
    [
        "login https://dash.mailsocket.app/verify-email/MTI3:1tAbCdEfGhIjKlMnOpQrSt/ fails",
        "reset link https://dash.mailsocket.app/password-reset/MQ/c8x-7a9b0c1d2e3f4a5b6c7d8e9f0a1b/ broke",
        "magic https://app.example.com/auth?token=eyJhbGciOiJIUzI1NiJ9abcdefghijklmnop&x=1",
        "see https://example.com/m/0123456789abcdefABCDEF_-xyz",
    ],
)
def test_feedback_email_scrub_redacts_url_tokens(text):
    from apps.feedback.alerts import scrub

    out = scrub(text)
    assert "[redacted]" in out
    for token in (
        "1tAbCdEfGhIjKlMnOpQrSt",
        "c8x-7a9b0c1d2e3f4a5b6c7d8e9f0a1b",
        "eyJhbGciOiJIUzI1NiJ9abcdefghijklmnop",
        "0123456789abcdefABCDEF_-xyz",
    ):
        assert token not in out


def test_feedback_scrub_keeps_ordinary_urls_and_text():
    from apps.feedback.alerts import scrub

    for text in (
        "the page https://mailsocket.app/docs/try/ is slow",
        "https://dash.mailsocket.app/app/inboxes/ shows nothing",
        "Order 2024 shipped.",
    ):
        assert scrub(text) == text


@pytest.mark.parametrize(
    "token",
    [
        "AbCdEfGhIjKlMnOpQrStUvWxYz_-AbCdEfGhIjKl",  # token_urlsafe can have no digit (~0.07%)
        "eJyrVkrOz0nNS1eyUlAqKs0rSS1SqKcA_eHmbW:1vQabc:AbCdEfGhIjKlMnOp",
    ],
)
def test_alphabetic_url_tokens_are_redacted(token):
    "Public API."
    for url in (f"https://dash.mailsocket.app/accounts/verify/{token}/", f"https://x.io/reset?token={token}"):
        out = scrub(f"see {url} please")
        assert token not in out
        assert "[redacted]" in out


@pytest.mark.parametrize(
    "url",
    ["https://mailsocket.app/docs/quickstart-guide", "https://dash.mailsocket.app/settings/notifications/"],
)
def test_ordinary_word_paths_are_kept(url):
    assert scrub(url) == url


def test_email_body_never_shows_a_token_from_the_page_path():
    "Public API."
    from types import SimpleNamespace

    from apps.feedback.alerts import email_body

    token = "abcdefghijklmnopqrstuvwxyzabcdefgh"  # digit-free, single case
    report = SimpleNamespace(
        pk=1,
        plan="free",
        page_path=f"/verify-email/{token}/",
        request_id="r",
        message="hi",
        email="",
        user_id=None,
    )
    body = email_body(report)
    assert token not in body


@pytest.mark.parametrize(
    "token", ["abcdefghijklmnopqrstuvwxyzabcdefgh", "ABCDEFGHIJKLMNOPQRSTUVWXYZABCDEFGH"]
)
def test_single_case_digit_free_long_tokens_are_redacted(token):
    "Public API."
    assert token not in scrub(f"https://dash.mailsocket.app/accounts/verify-email/{token}/")
