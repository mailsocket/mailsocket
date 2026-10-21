"Public API."

import itertools
from unittest.mock import patch

import pytest
from django.conf import settings
from django.test import override_settings

from apps.billing import reconcile
from apps.billing.tests.test_billing_reconcile import HC_URL, ON, Edge, ls_sub

pytestmark = pytest.mark.django_db


def test_deadman_with_the_setting_absent_is_skipped_not_an_error():
    # BL-21: kills ping_deadman mutmut_7 (getattr without default -> AttributeError
    # from a "never raises" function) and _10 (default "XXXX" -> "invalid" instead
    # of "skipped"): a settings module without HEALTHCHECKS_RECONCILE_URL
    # (e.g. one that does not inherit base.py) means "not configured"
    lines = []
    with override_settings():
        del settings.HEALTHCHECKS_RECONCILE_URL
        assert not hasattr(settings, "HEALTHCHECKS_RECONCILE_URL")
        with patch("urllib.request.urlopen", side_effect=AssertionError("no HTTP when not configured")):
            assert reconcile.ping_deadman(lines.append) == "skipped"
    assert lines == ["healthchecks: skipped (HEALTHCHECKS_RECONCILE_URL not set)"]


def test_deadman_https_url_is_pinged_once_with_get():
    # BL-21: (re-check of the "suspicious" ping_deadman mutmut_18, `not` dropped
    # on the https test): an https URL is pinged, never reported "invalid"
    lines, seen = [], []

    def edge(req, timeout=None):
        seen.append((req.get_method(), req.full_url, req.get_header("User-agent"), timeout))
        return Edge()(req, timeout=timeout)

    with (
        override_settings(HEALTHCHECKS_RECONCILE_URL=HC_URL),
        patch("urllib.request.urlopen", side_effect=edge),
    ):
        assert reconcile.ping_deadman(lines.append) == "ok"
    assert seen == [("GET", HC_URL, "mailsocket-reconcile/1", reconcile.PING_TIMEOUT_SECONDS)]
    assert lines == ["healthchecks: ping ok"]


@pytest.mark.parametrize("url", ["http://hc.example.test/x", "ftp://hc.example.test/x", "hc.example.test/x"])
def test_deadman_non_https_url_is_refused_without_any_http(url):
    # BL-21: kills ping_deadman mutmut_18 (`not` dropped on the https test) fast:
    # before, the only non-https case ran an UNPATCHED urlopen, so the mutant
    # made a real network call and ended as a mutmut timeout / suspicious
    lines = []
    with (
        override_settings(HEALTHCHECKS_RECONCILE_URL=url),
        patch(
            "urllib.request.urlopen", side_effect=AssertionError("non-https URL must never be requested")
        ) as edge,
    ):
        assert reconcile.ping_deadman(lines.append) == "invalid"
    assert edge.call_count == 0
    assert lines == ["healthchecks: skipped (HEALTHCHECKS_RECONCILE_URL is not https)"]


@pytest.mark.parametrize("n_pages", [1, 2, 3])
def test_fetch_reads_each_page_exactly_once_in_order(n_pages):
    # BL-21: kills fetch_ls_subscriptions mutmut_23 (`number += 1` -> `number = 1`:
    # page 1 re-read until the 240 s deadline; it used to end as a mutmut
    # TIMEOUT). A fake clock that advances 1 s per read turns that loop into a
    # fast ReconcileError, and the page sequence is asserted exactly.
    pages = [[ls_sub(1400 + i)] for i in range(n_pages)]
    edge = Edge(pages)
    ticks = itertools.count()
    sleeps = []
    with patch("urllib.request.urlopen", side_effect=edge), override_settings(**ON):
        subs = reconcile.fetch_ls_subscriptions(sleep=sleeps.append, clock=lambda: next(ticks))
    assert sorted(subs) == [str(1400 + i) for i in range(n_pages)]
    numbers = [p.split("page%5Bnumber%5D=")[1].split("&")[0] for p in edge.ls_paths()]
    assert numbers == [str(i) for i in range(1, n_pages + 1)]
    assert sleeps == [reconcile.PAGE_PAUSE_SECONDS] * (n_pages - 1)
