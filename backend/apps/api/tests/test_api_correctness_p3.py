"Public API."

import pytest
from django.urls import Resolver404, resolve
from rest_framework.test import APIClient

from apps.accounts.models import AccountPlan, ApiKey
from apps.api.tests.test_idempotency import client_for, make_user

pytestmark = pytest.mark.django_db(transaction=True)

REGISTER = "/api/v1/agents/register"
BATCH = "/api/v1/messages/batch"


def test_register_name_over_60_is_422_and_mints_nothing():
    before = ApiKey.objects.count()
    resp = APIClient().post(REGISTER, {"name": "x" * 61}, format="json")
    assert resp.status_code == 422
    body = resp.json()["error"]
    assert body["code"] == "validation_error"
    assert "name" in body["fields"]
    assert ApiKey.objects.count() == before


def test_register_name_of_60_is_kept_verbatim():
    resp = APIClient().post(REGISTER, {"name": "y" * 60}, format="json")
    assert resp.status_code == 201
    assert ApiKey.objects.filter(name="y" * 60 + " (self-registered)").exists()


def test_register_non_string_name_is_still_ignored():
    resp = APIClient().post(REGISTER, {"name": 12345}, format="json")
    assert resp.status_code == 201


@pytest.mark.parametrize(
    "body",
    [{}, {"action": "explode", "ids": ["msg_x"]}, {"action": "fetch", "ids": []}, {"ids": "nope"}],
)
def test_batch_bad_body_is_422_even_for_free_plan(body):
    free = make_user(f"doc3_free_{abs(hash(str(body))) % 10000}", plan=AccountPlan.Plan.FREE)
    resp = client_for(free).post(BATCH, body, format="json")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_batch_good_body_from_free_plan_is_still_402():
    free = make_user("doc3_free_ok", plan=AccountPlan.Plan.FREE)
    resp = client_for(free).post(BATCH, {"action": "fetch", "ids": ["msg_x"]}, format="json")
    assert resp.status_code == 402
    assert resp.json()["error"]["code"] == "plan_upgrade_required"


def test_batch_bad_body_with_idempotency_key_from_free_plan_is_422():
    free = make_user("doc3_free_idem", plan=AccountPlan.Plan.FREE)
    resp = client_for(free).post(BATCH, {"action": "explode"}, format="json", HTTP_IDEMPOTENCY_KEY="k-doc3")
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "variant",
    ["/api/v1/inboxes/", "/api/v1/INBOXES", "/api/v1/domains/", BATCH + "/", "/api/v1/outbound_emails/"],
)
def test_f2_6_no_alternate_spelling_of_an_idempotent_route_resolves(variant):
    with pytest.raises(Resolver404):
        resolve(variant)
