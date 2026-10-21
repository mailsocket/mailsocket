"Public API."

import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.accounts import funnel_verdict as fv
from apps.accounts.tests.test_funnel_report import NOW, SINCE, T0, activated_account, make_account, report

pytestmark = pytest.mark.django_db

P = {"pass_signups": 30, "pass_activations": 10}


# --- the rule itself (pure) ---------------------------------------------------


@pytest.mark.parametrize(
    "signups, activated, closed, expected",
    [
        (30, 10, False, fv.PASS),  # reached early: final, counts only grow
        (30, 10, True, fv.PASS),
        (30, 9, False, fv.PROVISIONAL),
        (29, 29, True, fv.ITERATE),
        (10, 3, True, fv.ITERATE),  # exactly 30 %
        (10, 2, True, fv.KILL),  # 20 %
        (9, 9, True, fv.KILL),  # under 10 signups
    ],
)
def test_channel_result_thresholds(signups, activated, closed, expected):
    assert fv.channel_result(signups, activated, closed, **P) == expected


def test_many_signups_with_low_activation_is_kill_not_iterate():
    assert fv.channel_result(40, 9, True, **P) == fv.KILL  # 22 % < 30 %
    assert fv.channel_result(40, 12, True, **P) == fv.PASS


def test_default_clocks_are_7_days_for_posts_and_30_for_directory_prs():
    assert fv.channel_days_for("hn") == 7
    assert fv.channel_days_for("mcp") == 7
    assert fv.channel_days_for("free-for-dev") == 30
    assert fv.channel_days_for("tensorblock") == 30
    assert fv.channel_days_for("tensorblock", {"tensorblock": 45}) == 45
    assert fv.channel_days_for("hn", {"hn": 3}) == 3


def _channel(tag, result, signups=0, activated=0, below=True):
    return {"tag": tag, "result": result, "signups": signups, "activated": activated, "below_floor": below}


def test_product_kill_pivot_needs_two_closed_attempts_below_floor():
    one = fv.verdict([_channel("hn", fv.KILL, 4, 1)], **P)
    assert one["result"] == fv.NEXT_CHANNEL and one["kill_pivot_ready"] is False
    two = fv.verdict([_channel("hn", fv.KILL, 4, 1), _channel("mcp", fv.KILL, 2, 0)], **P)
    assert two["attempts_below_floor"] == 2 and two["kill_pivot_ready"] is True
    assert two["result"] == fv.KILL
    # a KILL with >= 10 signups and >= 3 activations is not below the floor
    above = fv.verdict([_channel("hn", fv.KILL, 12, 3, below=False), _channel("mcp", fv.KILL, 2, 0)], **P)
    assert above["kill_pivot_ready"] is False and above["result"] == fv.NEXT_CHANNEL


def test_one_closed_failed_channel_never_makes_the_overall_verdict_kill():
    "Public API."
    for index in range(3):
        make_account(f"lone{index}", ref="hn")
    rep = report(tags=["hn"], now=T0 + timedelta(days=60))
    assert rep.verdict["channels"][0]["result"] == fv.KILL  # the channel is stopped
    assert rep.verdict["result"] == fv.NEXT_CHANNEL != fv.KILL  # the product is not
    assert rep.verdict["kill_pivot_ready"] is False


def test_overall_result_order():
    assert (
        fv.verdict([_channel("a", fv.KILL), _channel("b", fv.PROVISIONAL)], **P)["result"] == fv.PROVISIONAL
    )
    assert fv.verdict([_channel("a", fv.KILL), _channel("b", fv.ITERATE, 12, 4)], **P)["result"] == fv.ITERATE
    # pooled: two closed channels 6+6 signups, 2+2 activated = 12 / 33 % -> ITERATE overall
    pooled = fv.verdict([_channel("a", fv.KILL, 6, 2), _channel("b", fv.KILL, 6, 2)], **P)
    assert pooled["result"] == fv.ITERATE
    # pooled PASS even though no single channel passed
    both = fv.verdict([_channel("a", fv.PROVISIONAL, 15, 5), _channel("b", fv.KILL, 15, 5)], **P)
    assert both["result"] == fv.PASS and both["provisional"] is False


# --- wired into funnel_report -------------------------------------------------


def test_directory_pr_tag_is_not_judged_at_day_7():
    """A free-for-dev PR whose signups come on days 10-19 counts; at day 15 it
    is PROVISIONAL (not KILL) while the community post is already final."""
    for index in range(12):
        activated_account(f"ffd{index}", created=T0 + timedelta(days=10 + index % 10), ref="free-for-dev")
    activated_account("hn0", ref="hn")
    at_day_15 = report(tags=["hn", "free-for-dev"], now=T0 + timedelta(days=15))
    channels = {c["tag"]: c for c in at_day_15.verdict["channels"]}
    assert channels["hn"]["result"] == fv.KILL and channels["hn"]["days"] == 7
    assert channels["free-for-dev"]["result"] == fv.PROVISIONAL and channels["free-for-dev"]["days"] == 30
    assert at_day_15.verdict["result"] == fv.PROVISIONAL
    later = report(tags=["hn", "free-for-dev"], now=T0 + timedelta(days=60))
    ffd = next(c for c in later.verdict["channels"] if c["tag"] == "free-for-dev")
    assert (ffd["signups"], ffd["activated"], ffd["result"]) == (12, 12, fv.ITERATE)
    assert later.verdict["result"] == fv.ITERATE


def test_channel_days_override_shortens_or_extends_a_window():
    activated_account("late-hn", created=T0 + timedelta(days=8), ref="hn")
    seven = report(tags=["hn"], now=T0 + timedelta(days=60))
    assert seven.verdict["channels"][0]["signups"] == 0
    ten = report(tags=["hn"], now=T0 + timedelta(days=60), channel_days={"hn": 10})
    assert ten.verdict["channels"][0]["signups"] == 1 and ten.verdict["channels"][0]["days"] == 10


def test_report_days_never_moves_a_verdict_clock():
    "Public API."
    activated_account("hn-day9", created=T0 + timedelta(days=9), ref="hn")
    activated_account("ffd-day20", created=T0 + timedelta(days=20), ref="free-for-dev")
    rep = report(days=14, tags=["hn", "free-for-dev"], now=T0 + timedelta(days=60))
    channels = {c["tag"]: c for c in rep.verdict["channels"]}
    assert (channels["hn"]["days"], channels["hn"]["signups"]) == (7, 0)
    assert (channels["free-for-dev"]["days"], channels["free-for-dev"]["signups"]) == (30, 1)
    text = _run("--since", SINCE.isoformat(), "--days", "14", "--tags", "hn")
    assert "channel hn: KILL (7-day window" in text


def test_unactivated_signups_count_toward_signups_only():
    for index in range(10):
        make_account(f"cold{index}", ref="mcp")
    rep = report(tags=["mcp"], now=T0 + timedelta(days=60))
    ch = rep.verdict["channels"][0]
    assert (ch["signups"], ch["activated"], ch["result"], ch["below_floor"]) == (10, 0, fv.KILL, True)


def _run(*args):
    out = StringIO()
    with patch("apps.accounts.funnel.timezone.now", return_value=NOW):
        call_command("funnel_report", *args, stdout=out)
    return out.getvalue()


def test_command_channel_days_flag_and_text():
    activated_account("cmd-ffd", created=T0 + timedelta(days=1), ref="free-for-dev")
    text = _run("--since", SINCE.isoformat(), "--tags", "free-for-dev", "--channel-days", "free-for-dev=3")
    assert "channel free-for-dev: KILL (3-day window, closes 2026-09-17)" in text
    assert (
        "product KILL/PIVOT needs 2 closed channel attempts below 10 signups or 3 activations: 1 so far"
        in text
    )
    data = json.loads(_run("--since", SINCE.isoformat(), "--tags", "free-for-dev", "--json"))
    assert data["verdict"]["channels"][0]["days"] == 30
    assert data["verdict"]["result"] in {fv.PASS, fv.ITERATE, fv.KILL, fv.PROVISIONAL}


@pytest.mark.parametrize("value", ["hn", "hn=0", "hn=400", "<x>=5", "hn=five", "hn=5,"])
def test_bad_channel_days_are_refused(value):
    with pytest.raises(CommandError):
        _run("--since", SINCE.isoformat(), "--tags", "hn", "--channel-days", value)
