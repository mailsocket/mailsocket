"Public API."

import pytest
from django.test import override_settings

from apps.api.tests.test_wait_capacity_headroom import _load_production
from apps.billing.checkout_token import checkout_custom, verify


def test_production_reads_secret_key_fallbacks(monkeypatch):
    module = _load_production(monkeypatch, SECRET_KEY_FALLBACKS="old-one, ,older-two")
    assert module.SECRET_KEY_FALLBACKS == ["old-one", "older-two"]


def test_production_fallbacks_default_empty(monkeypatch):
    monkeypatch.delenv("SECRET_KEY_FALLBACKS", raising=False)
    module = _load_production(monkeypatch)
    assert module.SECRET_KEY_FALLBACKS == []


@pytest.mark.parametrize("listed, ok", [(True, True), (False, False)])
def test_checkout_signature_survives_rotation_only_with_fallback(listed, ok):
    with override_settings(SECRET_KEY="old-key-" + "x" * 40):
        custom = checkout_custom(42)
    fallbacks = ["old-key-" + "x" * 40] if listed else []
    with override_settings(SECRET_KEY="new-key-" + "y" * 40, SECRET_KEY_FALLBACKS=fallbacks):
        assert (verify(custom) == 42) is ok
