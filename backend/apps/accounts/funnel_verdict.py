"Public API."

from datetime import timedelta

PASS, ITERATE, KILL, PROVISIONAL = "PASS", "ITERATE", "KILL", "PROVISIONAL"
NEXT_CHANNEL = "NEXT_CHANNEL"  # overall only: channels failed, try another
COMMUNITY_CHANNEL_DAYS = 7
DEFAULT_CHANNEL_DAYS = {"free-for-dev": 30, "tensorblock": 30}
ITERATE_MIN_SIGNUPS = 10
ITERATE_MIN_RATE = 0.30
FLOOR_SIGNUPS = 10
FLOOR_ACTIVATIONS = 3
KILL_PIVOT_ATTEMPTS = 2


def channel_days_for(tag, overrides=None) -> int:
    """``--channel-days`` override, else 30 for directory PRs, else 7."""
    overrides = overrides or {}
    return int(overrides.get(tag, DEFAULT_CHANNEL_DAYS.get(tag, COMMUNITY_CHANNEL_DAYS)))


def channel_result(signups, activated, closed, pass_signups, pass_activations) -> str:
    if signups >= pass_signups and activated >= pass_activations:
        return PASS
    if not closed:
        return PROVISIONAL
    if signups >= ITERATE_MIN_SIGNUPS and activated >= ITERATE_MIN_RATE * signups:
        return ITERATE
    return KILL


def judge_channel(tag, counts, *, start, days, activation_days, now, pass_signups, pass_activations) -> dict:
    """``counts(tag, start, end) -> (signups, activated)`` for the tag's window."""
    end = start + timedelta(days=days)
    signups, activated = counts(tag, start, end)
    closes_at = end + timedelta(days=activation_days)
    closed = closes_at <= now
    return {
        "tag": tag,
        "days": days,
        "signups": signups,
        "activated": activated,
        "result": channel_result(signups, activated, closed, pass_signups, pass_activations),
        "below_floor": signups < FLOOR_SIGNUPS or activated < FLOOR_ACTIVATIONS,
        "closes": closes_at.date().isoformat(),
    }


def overall(channels: list[dict], signups, activated, pass_signups, pass_activations, kill_ready) -> str:
    results = {channel["result"] for channel in channels}
    if signups >= pass_signups and activated >= pass_activations:
        return PASS
    if PROVISIONAL in results:
        return PROVISIONAL
    if (
        ITERATE in results
        or channel_result(signups, activated, True, pass_signups, pass_activations) == ITERATE
    ):
        return ITERATE
    return KILL if kill_ready else NEXT_CHANNEL


def verdict(channels: list[dict], pass_signups, pass_activations) -> dict:
    failed = sum(1 for c in channels if c["result"] == KILL and c["below_floor"])
    signups = sum(c["signups"] for c in channels)
    activated = sum(c["activated"] for c in channels)
    kill_ready = failed >= KILL_PIVOT_ATTEMPTS
    result = overall(channels, signups, activated, pass_signups, pass_activations, kill_ready)
    return {
        "result": result,
        "pass_signups": pass_signups,
        "pass_activations": pass_activations,
        "signups": signups,
        "activated": activated,
        "provisional": result == PROVISIONAL,
        "channels": channels,
        "attempts_below_floor": failed,
        "kill_pivot_ready": kill_ready,
    }


def render(v: dict, tags: list[str]) -> list[str]:
    lines = [
        f"VERDICT {v['result']}: tags={','.join(tags)} signups {v['signups']}/{v['pass_signups']}, "
        f"activated {v['activated']}/{v['pass_activations']}"
        + (" (provisional: a channel window or activation window is still open)" if v["provisional"] else "")
    ]
    for c in v["channels"]:
        floor = ", below floor" if c["below_floor"] else ""
        lines.append(
            f"  channel {c['tag']}: {c['result']} ({c['days']}-day window, closes {c['closes']}): "
            f"signups {c['signups']}, activated {c['activated']}{floor}"
        )
    lines.append(
        f"  product KILL/PIVOT needs {KILL_PIVOT_ATTEMPTS} closed channel attempts below "
        f"{FLOOR_SIGNUPS} signups or {FLOOR_ACTIVATIONS} activations: {v['attempts_below_floor']} so far"
        + (" -> READY" if v["kill_pivot_ready"] else "")
    )
    return lines
