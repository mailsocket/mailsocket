"Public API."

import importlib

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.accounts import activity
from apps.accounts.models import AccountWeeklyActivity
from apps.accounts.tests.test_funnel_report import make_account, make_inbox, receive
from apps.outbound.models import OutboundEmail
from apps.outbound.tests.test_outbound import auth_client, eligible_domain, make_user, send_body

pytestmark = pytest.mark.django_db(transaction=True)

RAW_ENV = "  Bounces.Example.TEST.  "


def _setting_loaded_from_env(monkeypatch, value):
    """OUTBOUND_BOUNCE_DOMAIN as config.settings.base computes it from the env ``value``."""
    import config.settings.base as base

    monkeypatch.setenv("OUTBOUND_BOUNCE_DOMAIN", value)
    try:
        return importlib.reload(base).OUTBOUND_BOUNCE_DOMAIN
    finally:
        monkeypatch.delenv("OUTBOUND_BOUNCE_DOMAIN", raising=False)
        importlib.reload(base)


def _engine_envelope_from(name):
    """Enqueue one email through the API and drain it with the real engine; returns
    the envelope_from the engine handed to the SMTP transport."""
    from apps.outbound.engine import claim_one, deliver_claim

    user = make_user(name)
    domain = eligible_domain(user, f"{name.replace('_', '-')}.example.com")
    resp = auth_client(user).post("/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json")
    assert resp.status_code == 201, resp.content
    sent = []
    pk, token = claim_one()
    assert deliver_claim(pk, token, transport=lambda **kw: sent.append(kw)) == "sent"
    assert OutboundEmail.objects.get(pk=pk).status == OutboundEmail.Status.SENT
    (call,) = sent
    return call["envelope_from"], OutboundEmail.objects.get(pk=pk).public_id


def _activates(name, envelope_from):
    user = make_account(name)
    inbox = make_inbox(user, at=timezone.now())
    receive(inbox, at=timezone.now(), sender=envelope_from, marker=name)
    return AccountWeeklyActivity.objects.filter(user=user).exists()


def test_td09_engine_emits_canonical_bounce_domain_and_its_mail_never_activates(monkeypatch, settings):
    settings.ENABLE_OUTBOUND = True
    canonical = _setting_loaded_from_env(monkeypatch, RAW_ENV)
    assert canonical == "bounces.example.test"
    with override_settings(OUTBOUND_BOUNCE_DOMAIN=canonical):
        envelope_from, public_id = _engine_envelope_from("td09_canon")
        localpart, domain = envelope_from.rsplit("@", 1)
        assert localpart.startswith(f"{public_id}.")  # the VERP sender, built by the engine
        assert domain in activity.system_sender_domains()
        assert activity.system_sender_domains() == (domain,)
        assert _activates("td09-verp", envelope_from) is False
        assert _activates("td09-normal", "person@example.test") is True  # positive control


def test_td09_control_raw_value_through_the_engine_would_be_missed_by_the_filter(monkeypatch, settings):
    # Sabotage control: without the settings canonicalisation the engine emits the raw
    # domain, which is not a system_sender_domains() member and DOES activate the account.
    settings.ENABLE_OUTBOUND = True
    with override_settings(OUTBOUND_BOUNCE_DOMAIN=RAW_ENV.strip()):
        envelope_from, _ = _engine_envelope_from("td09_raw")
        domain = envelope_from.rsplit("@", 1)[1]
        assert domain == "Bounces.Example.TEST."
        assert domain not in activity.system_sender_domains()
        assert _activates("td09-raw", envelope_from) is True
