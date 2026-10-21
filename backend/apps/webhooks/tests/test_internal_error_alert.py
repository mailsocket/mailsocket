"Public API."

import pytest
from django.core import mail
from django.test import override_settings

from apps.ops.testing import fake_telegram
from apps.webhooks import engine
from apps.webhooks.models import Webhook, WebhookDelivery
from apps.webhooks.services import derive_signing_secret
from apps.webhooks.tests.test_non_ascii_url import make_delivery, make_webhook

pytestmark = pytest.mark.django_db(transaction=True)


def _boom(kind):
    def transport(url, body, headers):
        raise kind(f"kaboom {url} {headers['X-Mailsocket-Signature']}")

    return transport


@override_settings(OPS_ALERT_EMAIL="ops@example.test")
def test_internal_error_queues_one_ids_only_alert():
    _, _, hook = make_webhook("r1_alert")
    delivery = make_delivery(hook, "one")
    secret = derive_signing_secret(hook)
    with fake_telegram() as tg:
        engine.drain(batch_size=10, transport=_boom(RuntimeError))
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    text = msg.subject + "\n" + msg.body + "\n" + "\n".join(tg.texts)
    assert "internal_error" in text
    assert "RuntimeError" in text
    assert delivery.public_id in text
    for forbidden in ("receiver.example", "kaboom", secret, "v1=", "482193", "Sender"):
        assert forbidden not in text
    assert len(tg.texts) == 1


@override_settings(OPS_ALERT_EMAIL="ops@example.test")
def test_internal_error_alert_is_rate_limited_per_error_type_per_hour():
    _, _, hook = make_webhook("r1_rate")
    for i in range(3):
        make_delivery(hook, f"rt{i}")
    with fake_telegram():
        engine.drain(batch_size=10, transport=_boom(RuntimeError))
        assert len(mail.outbox) == 1
        # a different error type is its own bucket
        WebhookDelivery.objects.update(next_attempt_at=None)
        make_delivery(hook, "other-type")
        WebhookDelivery.objects.filter(status=WebhookDelivery.Status.PENDING).update(next_attempt_at=None)
        engine.drain(batch_size=10, transport=_boom(ValueError))
    assert len(mail.outbox) == 2
    assert "ValueError" in mail.outbox[1].body


@override_settings(OPS_ALERT_EMAIL="ops@example.test")
def test_alert_failure_never_breaks_the_drain(monkeypatch):
    from apps.ops import notify as ops_notify

    def broken(*a, **k):
        raise RuntimeError("alert channel down")

    monkeypatch.setattr(ops_notify, "notify", broken)
    _, _, hook = make_webhook("r1_broken")
    make_delivery(hook, "a")
    make_delivery(hook, "b")
    result = engine.drain(batch_size=10, transport=_boom(RuntimeError))
    assert result.claimed == 2
    assert result.retrying == 2


def test_normal_transport_failures_do_not_alert(settings):
    from apps.webhooks.transport import DeliveryTransportError

    settings.OPS_ALERT_EMAIL = "ops@example.test"
    _, _, hook = make_webhook("r1_normal")
    make_delivery(hook, "n")

    def transport(url, body, headers):
        raise DeliveryTransportError("timeout")

    with fake_telegram() as tg:
        engine.drain(batch_size=10, transport=transport)
    assert mail.outbox == []
    assert tg.texts == []


# --- R-2: admin form error, never a 500 ----------------------------------------


@pytest.mark.parametrize(
    "url",
    ["https://example.com/caf\u00e9", "http://example.com/hook", "https://127.0.0.1/hook"],
)
def test_admin_form_maps_ssrf_error_to_form_error(url):
    from apps.webhooks.admin import WebhookAdminForm

    _, _, hook = make_webhook(f"r2_{abs(hash(url)) % 10000}")
    form = WebhookAdminForm(data={"inbox": hook.inbox_id, "url": url, "is_enabled": "on"}, instance=hook)
    assert not form.is_valid()
    assert "url" in form.errors
    assert Webhook.objects.get(pk=hook.pk).url == "https://receiver.example/hook"


def test_admin_change_view_with_bad_url_is_a_form_error_not_500(admin_client):
    _, _, hook = make_webhook("r2_view")
    resp = admin_client.post(
        f"/admin/webhooks/webhook/{hook.pk}/change/",
        {"inbox": hook.inbox_id, "url": "https://example.com/caf\u00e9", "is_enabled": "on"},
    )
    assert resp.status_code == 200
    assert b"errorlist" in resp.content
    assert Webhook.objects.get(pk=hook.pk).url == "https://receiver.example/hook"
