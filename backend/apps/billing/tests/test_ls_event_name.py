"Public API."

import hashlib
import hmac
import json

import pytest
from django.test import Client, override_settings

pytestmark = pytest.mark.django_db(transaction=True)

SECRET = "test-ls-webhook-secret"


def _post_ls(meta):
    body = json.dumps({"meta": meta, "data": {"id": "sub_x", "attributes": {}}}).encode()
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return Client().post(
        "/billing/webhook/ls/", data=body, content_type="application/json", HTTP_X_SIGNATURE=sig
    )


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
@pytest.mark.parametrize(
    "event_name",
    [{"x": 1}, ["a"], 7, None, "", "e" * 65],
    ids=["dict", "list", "int", "none", "empty", "long"],
)
def test_ls_non_string_or_oversized_event_name_is_400(event_name):
    from apps.billing.models import BillingEvent

    resp = _post_ls({"event_name": event_name, "webhook_id": f"wh_{id(event_name)}"})
    assert resp.status_code == 400
    assert BillingEvent.objects.count() == 0


@override_settings(ENABLE_BILLING=True, LS_WEBHOOK_SECRET=SECRET)
def test_ls_non_string_webhook_id_falls_back_to_body_hash():
    from apps.billing.models import BillingEvent

    resp = _post_ls({"event_name": "order_created", "webhook_id": {"x": 1}})
    assert resp.status_code == 200
    assert BillingEvent.objects.count() == 1
