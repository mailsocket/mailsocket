"""Plan-change confirm page with the billing flag OFF (audit 2026-10-11 sweep).

Strengthened copy of test_billing_confirm.py::test_confirm_flag_off_404,
which only checked the status code. It lives here because
test_billing_confirm.py is over the no-grow size limit (health ratchet); the
original test is kept unchanged. Flag off must mean: 404 for GET and for a
POST that carries a VALID signed token, no Lemon Squeezy call, no
PlanChangeRequest row, plan row unchanged.
"""

from unittest.mock import patch

import pytest
from django.test import override_settings
from django.urls import reverse

from apps.accounts.models import AccountPlan
from apps.billing import views as bviews
from apps.billing.models import PlanChangeRequest
from apps.billing.tests.test_billing_plan_change import login, make_user

pytestmark = pytest.mark.django_db(transaction=True)

PAID, BUSINESS = "2176466", "2187025"
CHANGE_URL = reverse("dashboard:billing-change")


@override_settings(ENABLE_BILLING=False)
def test_confirm_flag_off_404_get_and_signed_post_touch_nothing():
    user = make_user("bcf_flag", plan=AccountPlan.Plan.PAID, sub_id="sub_bcf_flag", price_id=PAID)
    token = bviews._make_token(user.pk, PAID, BUSINESS, 13115)
    with (
        patch("apps.billing.views._ls_get") as mock_get,
        patch("apps.billing.views._ls_patch") as mock_patch,
    ):
        assert login(user).get(CHANGE_URL, {"plan": "business"}).status_code == 404
        post = login(user).post(CHANGE_URL, {"plan": "business", "token": token})
        assert post.status_code == 404
    mock_get.assert_not_called()
    mock_patch.assert_not_called()
    assert not PlanChangeRequest.objects.filter(user=user).exists()
    plan = AccountPlan.objects.get(user=user)
    assert (plan.plan, plan.stripe_subscription_id, plan.stripe_price_id) == (
        AccountPlan.Plan.PAID,
        "sub_bcf_flag",
        PAID,
    )
