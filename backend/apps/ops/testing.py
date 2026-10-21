"""Test helper: a fake Telegram Bot API (no network, fake token only).

Usage::

    with fake_telegram() as tg:
        notify(...)
    assert tg.texts == [...]

Patches ``urllib.request.urlopen`` as seen by :mod:`apps.ops.notify` and
sets FAKE bot token / chat id settings. ``fail`` makes every call raise the
given exception; ``ok=False`` returns ``{"ok": false}``.
"""
import contextlib
import json
from unittest import mock

from django.test import override_settings

FAKE_TOKEN = "123456789:FAKE-test-token-not-real-AAAAAAAAAA"
FAKE_CHAT_ID = "4242"


class _Response:
    def __init__(self, payload, status=200):
        self._raw = json.dumps(payload).encode()
        self.status = status

    def read(self, *_):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeTelegram:
    def __init__(self, *, fail=None, ok=True):
        self.calls = []
        self.fail = fail
        self.ok = ok

    def __call__(self, request, timeout=None):
        payload = json.loads(request.data.decode())
        self.calls.append({"url": request.full_url, "timeout": timeout, "payload": payload})
        if self.fail is not None:
            raise self.fail
        return _Response({"ok": self.ok, "result": {"message_id": len(self.calls)}})

    @property
    def texts(self):
        return [c["payload"]["text"] for c in self.calls]


@contextlib.contextmanager
def fake_telegram(*, fail=None, ok=True, token=FAKE_TOKEN, chat_id=FAKE_CHAT_ID):
    fake = FakeTelegram(fail=fail, ok=ok)
    with override_settings(OPS_TELEGRAM_BOT_TOKEN=token, OPS_TELEGRAM_CHAT_ID=chat_id):
        with mock.patch("apps.ops.notify.urllib.request.urlopen", side_effect=fake):
            yield fake
