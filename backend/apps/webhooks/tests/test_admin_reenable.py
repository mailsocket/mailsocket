"Public API."

from unittest import mock

import pytest

from apps.webhooks.admin import WebhookAdminForm
from apps.webhooks.models import Webhook
from apps.webhooks.security import SSRFValidationError

pytestmark = pytest.mark.django_db


def _disabled_webhook():
    from apps.webhooks.tests.test_non_ascii_url import make_webhook  # existing helper

    _, _, wh = make_webhook("reenable")
    Webhook.objects.filter(pk=wh.pk).update(is_enabled=False)
    wh.refresh_from_db()
    return wh


def _form(wh, **changes):
    data = {"inbox": wh.inbox_id, "url": wh.url, "is_enabled": wh.is_enabled}
    data.update(changes)
    return WebhookAdminForm(data=data, instance=wh)


def test_reenable_with_url_that_no_longer_validates_is_a_form_error():
    wh = _disabled_webhook()
    with mock.patch(
        "apps.webhooks.admin.validate_webhook_target", side_effect=SSRFValidationError("ssrf_private_address")
    ):
        form = _form(wh, is_enabled=True)
        assert not form.is_valid()
    assert "ssrf_private_address" in str(form.errors)
    wh.refresh_from_db()
    assert wh.is_enabled is False


def test_saving_a_disabled_webhook_unchanged_does_not_revalidate():
    wh = _disabled_webhook()
    with mock.patch(
        "apps.webhooks.admin.validate_webhook_target", side_effect=AssertionError("must not run")
    ):
        assert _form(wh).is_valid()
