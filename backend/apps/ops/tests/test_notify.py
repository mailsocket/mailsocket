"""tg-notify: the single ops notifier (email + Telegram).

Load-bearing: each test names the regression it catches.
"""
import json
import logging
import urllib.error
from email.message import Message
from io import StringIO
from unittest import mock

import pytest
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from apps.ops import notify as ops_notify
from apps.ops.notify import notify
from apps.ops.testing import FAKE_CHAT_ID, FAKE_TOKEN, fake_telegram

OPS = "ops@example.test"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


# --- both channels ------------------------------------------------------------


@override_settings(OPS_ALERT_EMAIL=OPS)
def test_both_channels_called_with_expected_shape():
    with fake_telegram() as tg:
        result = notify("down", "Inbound mail is DOWN", "body line", link="https://mailsocket.app/status")
    assert result == {"email": "ok", "telegram": "ok"}
    # Email: historic format.
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    assert msg.subject == "[mailsocket] Inbound mail is DOWN"
    assert msg.to == [OPS]
    assert "body line" in msg.body and "https://mailsocket.app/status" in msg.body
    # Telegram: plain text sendMessage, previews off, bounded timeout.
    assert len(tg.calls) == 1
    call = tg.calls[0]
    assert call["url"] == f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
    assert call["timeout"] <= 5
    payload = call["payload"]
    assert payload["chat_id"] == FAKE_CHAT_ID
    assert payload["disable_web_page_preview"] is True
    assert "parse_mode" not in payload  # plain text: no escaping pitfalls
    assert payload["text"] == "\U0001F534 Inbound mail is DOWN\n\nbody line\n\nhttps://mailsocket.app/status"


@pytest.mark.parametrize(
    "level,emoji",
    [("down", "🔴"), ("warn", "🟠"), ("ok", "🟢"), ("money", "💰"), ("info", "ℹ️"), ("bogus", "ℹ️")],
)
def test_level_emoji_prefix(level, emoji):
    with fake_telegram() as tg:
        notify(level, "T", "b")
    assert tg.texts[0].startswith(f"{emoji} T")


@override_settings(OPS_ALERT_EMAIL=OPS)
def test_email_failure_still_sends_telegram():
    with fake_telegram() as tg, mock.patch(
        "apps.ops.notify.send_mail", side_effect=OSError("smtp down")
    ):
        result = notify("warn", "T", "b")
    assert result == {"email": "error:OSError", "telegram": "ok"}
    assert len(tg.calls) == 1


@override_settings(OPS_ALERT_EMAIL=OPS)
def test_telegram_failure_still_sends_email():
    with fake_telegram(fail=urllib.error.URLError("unreachable")) as tg:
        result = notify("warn", "T", "b")
    assert len(tg.calls) == 1
    assert result == {"email": "ok", "telegram": "error:URLError"}
    assert len(mail.outbox) == 1


@override_settings(OPS_ALERT_EMAIL=OPS)
def test_telegram_ok_false_is_an_error():
    with fake_telegram(ok=False):
        result = notify("warn", "T", "b")
    assert result["telegram"] == "error:TelegramNotOk"


@override_settings(OPS_ALERT_EMAIL=OPS)
@pytest.mark.parametrize("token,chat", [("", ""), (FAKE_TOKEN, ""), ("", FAKE_CHAT_ID)])
def test_telegram_disabled_when_unset(token, chat):
    with fake_telegram(token=token, chat_id=chat) as tg:
        result = notify("info", "T", "b")
    assert tg.calls == []
    assert result == {"email": "ok", "telegram": "disabled"}


def test_both_disabled_is_silent_noop():
    with mock.patch("apps.ops.notify.urllib.request.urlopen") as urlopen:
        result = notify("info", "T", "b")
    urlopen.assert_not_called()
    assert result == {"email": "disabled", "telegram": "disabled"}
    assert mail.outbox == []
    assert not ops_notify.any_channel_enabled()


@override_settings(OPS_ALERT_EMAIL=OPS)
def test_never_raises_when_everything_breaks():
    with fake_telegram(fail=RuntimeError("boom")), mock.patch(
        "apps.ops.notify.send_mail", side_effect=RuntimeError("boom")
    ), mock.patch("apps.ops.notify.cache.add", side_effect=RuntimeError("cache down")):
        result = notify("down", "T", "b", dedupe_key="k")
    assert result == {"email": "error:RuntimeError", "telegram": "error:RuntimeError"}
    assert ops_notify.failed(result)


@override_settings(OPS_ALERT_EMAIL=OPS)
@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.URLError(f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage refused"),
        urllib.error.HTTPError(
            f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage", 401, "Unauthorized", Message(), None
        ),
        ValueError(f"bad url bot{FAKE_TOKEN}"),
    ],
)
def test_token_never_in_logs_result_or_exception(exc, caplog):
    """The token sits in the Bot API URL path; urllib errors can carry it."""
    caplog.set_level(logging.DEBUG)
    with fake_telegram(fail=exc):
        result = notify("down", "T", "b")
    assert result["telegram"].startswith("error:")
    assert "ops_notify_failed channel=telegram" in caplog.text  # failure IS logged
    assert FAKE_TOKEN not in caplog.text
    assert FAKE_TOKEN not in json.dumps(result)
    assert "FAKE-test-token" not in caplog.text


def test_http_error_reports_status_code():
    exc = urllib.error.HTTPError("https://x/", 403, "Forbidden", Message(), None)
    with fake_telegram(fail=exc):
        assert notify("info", "T", "b")["telegram"] == "error:HTTPError(403)"


def test_telegram_text_truncated_to_4096_utf16_units():
    with fake_telegram() as tg:
        notify("down", "T", "x" * 10000 + "🔥" * 3000)
    text = tg.texts[0]
    assert len(text.encode("utf-16-le")) // 2 <= 4096
    assert text.endswith("…")


@override_settings(OPS_ALERT_EMAIL=OPS)
def test_customer_emails_and_token_redacted_from_telegram_but_not_email():
    with fake_telegram() as tg:
        notify("money", "T", f"who: alice@customer.test token={FAKE_TOKEN}")
    assert "alice@customer.test" not in tg.texts[0]
    assert "[email]" in tg.texts[0]
    assert FAKE_TOKEN not in tg.texts[0]
    assert "alice@customer.test" in mail.outbox[0].body


@override_settings(OPS_ALERT_EMAIL=OPS)
def test_email_overrides_stay_out_of_telegram():
    with fake_telegram() as tg:
        notify("money", "T", "safe", email_title="T — bob@customer.test", email_body="Account: bob")
    assert mail.outbox[0].subject == "[mailsocket] T — bob@customer.test"
    assert mail.outbox[0].body == "Account: bob"
    assert "bob" not in tg.texts[0]


@override_settings(OPS_ALERT_EMAIL=OPS)
def test_dedupe_key_sends_once():
    with fake_telegram() as tg:
        first = notify("warn", "T", "b", dedupe_key="same")
        second = notify("warn", "T", "b", dedupe_key="same")
    assert first == {"email": "ok", "telegram": "ok"}
    assert second == {"email": "deduped", "telegram": "deduped"}
    assert len(tg.calls) == 1 and len(mail.outbox) == 1


# --- send_test_alert -------------------------------------------------------------


@override_settings(OPS_ALERT_EMAIL=OPS)
def test_send_test_alert_both_ok():
    out = StringIO()
    with fake_telegram() as tg:
        call_command("send_test_alert", stdout=out)
    assert out.getvalue().splitlines() == ["email: ok", "telegram: ok"]
    assert tg.texts[0].startswith("ℹ️ Test alert")
    assert mail.outbox[0].subject == "[mailsocket] Test alert"


@override_settings(OPS_ALERT_EMAIL=OPS)
def test_send_test_alert_reports_disabled_and_error_type(caplog):
    out = StringIO()
    with fake_telegram(fail=urllib.error.URLError(f"bot{FAKE_TOKEN}")):
        call_command("send_test_alert", stdout=out)
    assert out.getvalue().splitlines() == ["email: ok", "telegram: error:URLError"]
    assert FAKE_TOKEN not in out.getvalue() + caplog.text

    out = StringIO()
    call_command("send_test_alert", stdout=out)  # Telegram unset
    assert out.getvalue().splitlines() == ["email: ok", "telegram: disabled"]


def test_send_test_alert_fails_when_nothing_delivered():
    out = StringIO()
    with pytest.raises(CommandError):
        call_command("send_test_alert", stdout=out)
    assert out.getvalue().splitlines() == ["email: disabled", "telegram: disabled"]
