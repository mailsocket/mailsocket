"""Mutation kills for apps/billing/kill_switch.py (T3 quarterly 2026-10-11:
16/49 mutants survived in paused_response and ensure_billing_changes_enabled).

What the operator / customer actually sees while BILLING_CHANGES_ENABLED is
off, asserted exactly: the one WARNING line per refusal (event name, request
path / LS method + path, the refusing user's pk or None), the
BillingChangesPaused text, and the 303 back to the billing page carrying
PAUSED_MESSAGE at INFO level (mutmut 3.8.0 via scripts/quality/mutation_backend.sh).
"""

import logging

import pytest
from django.contrib import messages as dj_messages
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, override_settings
from django.urls import reverse

from apps.billing import kill_switch
from apps.billing import views as bviews
from apps.billing.tests.mutation_kill_helpers import _messages, _user
from apps.billing.tests.test_billing_kill_switch import OFF, ls_edge

pytestmark = pytest.mark.django_db

LOGGER = "apps.billing.kill_switch"
BILLING_URL = reverse("dashboard:billing-page")


def _request(path, user=None, *, with_user=True):
    request = RequestFactory().post(path)
    SessionMiddleware(lambda _request: None).process_request(request)
    request._messages = FallbackStorage(request)
    if with_user:
        request.user = user
    return request


def _assert_paused_redirect(resp, request):
    assert resp.status_code == 303
    assert resp["Location"] == BILLING_URL
    assert "Retry-After" not in resp  # a form redirect, not a 503
    assert [(m.level, str(m)) for m in request._messages] == [(dj_messages.INFO, kill_switch.PAUSED_MESSAGE)]


@override_settings(**OFF)
def test_paused_response_logs_path_and_signed_in_user_pk(caplog):
    # BL-22: kills paused_response mutmut_1, _2, _7, _12, _13, _14, _15 (user_pk
    # lost / looked up on the wrong object or attribute), _17 (path None),
    # _18 (user_pk None), _22 (event name changed)
    user = _user("ks_mut_log")
    request = _request("/app/billing/checkout/", user)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        resp = kill_switch.paused_response(request)
    assert _messages(caplog, LOGGER) == [
        f"billing_changes_paused_refused path=/app/billing/checkout/ user_pk={user.pk}"
    ]
    assert [r.levelno for r in caplog.records if r.name == LOGGER] == [logging.WARNING]
    _assert_paused_redirect(resp, request)


@override_settings(**OFF)
def test_paused_response_without_request_user_still_refuses_and_logs_none(caplog):
    # BL-22: kills paused_response mutmut_6 (`getattr(..., "pk", )` raises
    # AttributeError) and _11 (`getattr(request, "user", )` raises): a request
    # that never went through AuthenticationMiddleware must still get the
    # paused redirect, never a 500
    request = _request("/app/billing/change/", with_user=False)
    assert not hasattr(request, "user")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        resp = kill_switch.paused_response(request)
    assert _messages(caplog, LOGGER) == [
        "billing_changes_paused_refused path=/app/billing/change/ user_pk=None"
    ]
    _assert_paused_redirect(resp, request)


@override_settings(**OFF)
def test_paused_response_anonymous_user_logs_none(caplog):
    # BL-22: AnonymousUser has pk None -> logged as None, still the redirect
    request = _request("/app/billing/change/", AnonymousUser())
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        resp = kill_switch.paused_response(request)
    assert _messages(caplog, LOGGER) == [
        "billing_changes_paused_refused path=/app/billing/change/ user_pk=None"
    ]
    _assert_paused_redirect(resp, request)


@override_settings(**OFF)
@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/checkouts"), ("PATCH", "/subscriptions/sub_42")],
)
def test_ensure_paused_logs_method_and_path_and_raises_exact_text(caplog, method, path):
    # BL-22: kills ensure_billing_changes_enabled mutmut_3 (method None),
    # _4 (path None), _8 (event name changed), _10 (exception text None)
    with (
        caplog.at_level(logging.WARNING, logger=LOGGER),
        pytest.raises(kill_switch.BillingChangesPaused) as exc,
    ):
        kill_switch.ensure_billing_changes_enabled(method, path)
    assert str(exc.value) == f"{method} {path} refused: BILLING_CHANGES_ENABLED is off"
    assert exc.value.args == (f"{method} {path} refused: BILLING_CHANGES_ENABLED is off",)
    assert _messages(caplog, LOGGER) == [
        f"billing_changes_paused_ls_write_refused method={method} path={path}"
    ]
    assert [r.levelno for r in caplog.records if r.name == LOGGER] == [logging.WARNING]


@override_settings(**OFF)
def test_ls_patch_edge_refusal_names_the_real_call(caplog):
    # BL-22: through the real writer the operator's log line and the 500's
    # exception name the LS call that was refused; zero HTTP
    with (
        ls_edge() as edge,
        caplog.at_level(logging.WARNING, logger=LOGGER),
        pytest.raises(kill_switch.BillingChangesPaused) as exc,
    ):
        bviews._ls_patch("/subscriptions/sub_42", {"data": {}})
    assert edge.call_count == 0
    assert str(exc.value) == "PATCH /subscriptions/sub_42 refused: BILLING_CHANGES_ENABLED is off"
    assert _messages(caplog, LOGGER) == [
        "billing_changes_paused_ls_write_refused method=PATCH path=/subscriptions/sub_42"
    ]


@override_settings(ENABLE_BILLING=True, BILLING_CHANGES_ENABLED=True)
def test_ensure_enabled_returns_none_and_logs_nothing(caplog):
    # BL-22: switch on -> no exception, no log line
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert kill_switch.ensure_billing_changes_enabled("POST", "/checkouts") is None
    assert _messages(caplog, LOGGER) == []
