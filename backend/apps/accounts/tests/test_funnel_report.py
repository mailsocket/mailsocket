"Public API."

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.apps import apps as django_apps
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from apps.accounts import funnel
from apps.accounts.activity import SYSTEM_SENDERS, backfill_weekly_activity
from apps.accounts.attribution import AGENT_REGISTER_REF
from apps.accounts.entitlements import record_message_once
from apps.accounts.models import Account, AccountWeeklyActivity
from apps.accounts.services import DEMO_USER_EMAIL, create_product_user
from apps.inboxes.models import Inbox, Message

pytestmark = pytest.mark.django_db

SINCE = date(2026, 9, 7)  # a Monday
T0 = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)  # every activation window closed


def make_account(name, *, created=T0, ref=None, host=None, verified=True, email=None):
    user = create_product_user(email or f"{name}@example.test", "pw-not-used-123456")
    Account.objects.filter(user=user).update(
        created_at=created,
        src_ref=ref,
        src_referrer_host=host,
        email_verified_at=created if verified else None,
    )
    return user


def make_inbox(user, *, at):
    count = Inbox.objects.filter(user=user).count()
    inbox = Inbox.objects.create(user=user, routing_key=f"fun{user.pk}x{count}")
    Inbox.objects.filter(pk=inbox.pk).update(created_at=at)
    return inbox


def receive(inbox, *, at, sender="sender@example.test", marker="m"):
    """Store a message at ``at`` and meter it exactly like ingest does."""
    raw = f"Subject: {marker}\r\n\r\nbody {inbox.pk} {at.isoformat()} {marker}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    message = Message.objects.create(
        inbox=inbox,
        dedupe_key=digest,
        envelope_from=sender,
        envelope_recipient=inbox.address,
        raw_bytes=raw,
        raw_sha256=digest,
        size_bytes=len(raw),
    )
    Message.objects.filter(pk=message.pk).update(received_at=at)
    assert record_message_once(Message.objects.select_related("inbox").get(pk=message.pk))
    return message


def store_unmetered(inbox, *, marker, at=None):
    """A retained Message that never went through record_message_once."""
    raw = f"Subject: {marker}\r\n\r\nunmetered {marker}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    message = Message.objects.create(
        inbox=inbox,
        dedupe_key=digest,
        envelope_from="a@example.test",
        envelope_recipient=inbox.address,
        raw_bytes=raw,
        raw_sha256=digest,
        size_bytes=len(raw),
    )
    if at is not None:
        Message.objects.filter(pk=message.pk).update(received_at=at)
    return message


def activated_account(name, *, created=T0, ref=None, host=None, message_after=timedelta(days=1)):
    user = make_account(name, created=created, ref=ref, host=host)
    inbox = make_inbox(user, at=created + timedelta(minutes=5))
    receive(inbox, at=created + message_after)
    return user


def report(**kwargs):
    kwargs.setdefault("now", NOW)
    return funnel.build_report(SINCE, kwargs.pop("days", 7), kwargs.pop("tags", None), **kwargs)


def row(rep, source):
    return next(r for r in rep.rows if r.source == source)


def run(*args):
    out = StringIO()
    with patch("apps.accounts.funnel.timezone.now", return_value=NOW):
        call_command("funnel_report", *args, stdout=out)
    return out.getvalue()


# --- AC2: source attribution -------------------------------------------------


def test_source_is_ref_then_referrer_host_then_direct_and_agents_get_their_own_row():
    make_account("a", ref="hn", host="news.ycombinator.com")
    make_account("b", host="github.com")
    make_account("c")
    make_account("d", ref=AGENT_REGISTER_REF)
    rep = report()
    assert sorted(r.source for r in rep.rows) == sorted(
        ["hn", "referrer:github.com", "direct", AGENT_REGISTER_REF]
    )
    assert all(r.signups == 1 for r in rep.rows)
    assert rep.totals.signups == 4


def test_real_agent_register_path_is_its_own_source():
    from apps.accounts.services import register_agent

    user, _key, _token = register_agent("bot")
    Account.objects.filter(user=user).update(created_at=T0)
    assert row(report(), AGENT_REGISTER_REF).signups == 1


def test_window_is_since_plus_days_end_exclusive():
    make_account("before", created=T0 - timedelta(days=1))
    make_account("first", created=datetime(2026, 9, 7, 0, 0, tzinfo=UTC))
    make_account("last", created=datetime(2026, 9, 13, 23, 59, tzinfo=UTC))
    make_account("after", created=datetime(2026, 9, 14, 0, 0, tzinfo=UTC))
    assert report().totals.signups == 2
    assert report(days=8).totals.signups == 3


def test_verified_and_with_inbox_columns():
    make_account("unverified", verified=False)
    user = make_account("inbox-only")
    make_inbox(user, at=T0 + timedelta(hours=1))
    total = report().totals
    assert (total.signups, total.verified, total.with_inbox, total.activated) == (2, 1, 1, 0)


# --- AC2 / AC3: activation boundary ------------------------------------------


def test_message_on_day_7_activates_day_8_does_not():
    activated_account("day7", ref="hn", message_after=timedelta(days=6, hours=23, minutes=59))
    activated_account("day8", ref="mcp", message_after=timedelta(days=7))
    rep = report()
    assert row(rep, "hn").activated == 1
    assert row(rep, "mcp").activated == 0
    assert rep.totals.activated == 1


def test_inbox_but_no_message_is_not_activated():
    user = make_account("lonely", ref="hn")
    make_inbox(user, at=T0 + timedelta(hours=1))
    rep = report()
    assert row(rep, "hn").with_inbox == 1
    assert row(rep, "hn").activated == 0


def test_inbox_created_after_day_7_does_not_activate():
    user = make_account("late", ref="hn")
    early = make_inbox(user, at=T0 + timedelta(days=8))
    receive(early, at=T0 + timedelta(days=1))  # message counted, inbox outside the window
    assert row(report(), "hn").activated == 0


def test_soft_deleted_inbox_still_counts_as_created():
    user = activated_account("deleted-inbox", ref="hn")
    Inbox.objects.filter(user=user).update(deleted_at=T0 + timedelta(days=2))
    assert row(report(), "hn").activated == 1


def test_purged_message_still_counts_via_the_durable_activity_row():
    user = activated_account("purged", ref="hn")
    assert AccountWeeklyActivity.objects.filter(user=user).count() == 1
    Message.objects.filter(inbox__user=user).delete()  # Free retention purge
    assert row(report(), "hn").activated == 1


def test_self_generated_welcome_mail_never_activates():
    user = make_account("welcome-only", ref="hn")
    inbox = make_inbox(user, at=T0 + timedelta(minutes=1))
    for index, sender in enumerate(SYSTEM_SENDERS):
        receive(inbox, at=T0 + timedelta(minutes=2), sender=sender, marker=f"sys{index}")
    assert not AccountWeeklyActivity.objects.filter(user=user).exists()
    assert row(report(), "hn").activated == 0


def test_real_onboarding_welcome_mail_does_not_activate():
    from apps.inboxes.services import provision_onboarding

    user = make_account("onboarded", ref="hn")
    provision_onboarding(user)
    assert Message.objects.filter(inbox__user=user).count() == 1
    Inbox.objects.filter(user=user).update(created_at=T0 + timedelta(minutes=1))
    Message.objects.filter(inbox__user=user).update(received_at=T0 + timedelta(minutes=2))
    assert not AccountWeeklyActivity.objects.filter(user=user).exists()
    assert row(report(), "hn").activated == 0


def test_retained_message_without_activity_row_still_counts():
    """Belt and braces: a retained Message that was never metered counts."""
    user = make_account("unmetered", ref="hn")
    inbox = make_inbox(user, at=T0 + timedelta(minutes=1))
    store_unmetered(inbox, marker="x", at=T0 + timedelta(days=2))
    assert row(report(), "hn").activated == 1


def test_activation_open_counts_accounts_still_inside_their_7_days():
    make_account("fresh", ref="hn", created=T0 + timedelta(days=5))
    rep = report(now=T0 + timedelta(days=6))
    assert row(rep, "hn").activation_open == 1
    assert row(report(), "hn").activation_open == 0


# --- AC3: the hook and its backfill ------------------------------------------


def test_activity_row_is_append_only_one_per_week_with_first_message_time():
    user = make_account("weekly")
    inbox = make_inbox(user, at=T0)
    receive(inbox, at=T0 + timedelta(hours=1), marker="one")
    receive(inbox, at=T0 + timedelta(hours=2), marker="two")
    receive(inbox, at=T0 + timedelta(days=7), marker="next-week")
    rows = list(AccountWeeklyActivity.objects.filter(user=user).order_by("week_start"))
    assert [(r.week_start, r.first_message_at) for r in rows] == [
        (date(2026, 9, 7), T0 + timedelta(hours=1)),
        (date(2026, 9, 14), T0 + timedelta(days=7)),
    ]
    stored = {f.name for f in AccountWeeklyActivity._meta.get_fields()}
    assert stored == {"id", "user", "week_start", "first_message_at"}  # no content, no address


def test_hook_failure_never_breaks_usage_metering():
    user = make_account("hook-fails")
    inbox = make_inbox(user, at=T0)
    with patch("apps.accounts.activity.record_weekly_activity", side_effect=RuntimeError("boom")):
        receive(inbox, at=T0 + timedelta(hours=1))
    assert user.monthly_usage.get().messages_received == 1
    assert not AccountWeeklyActivity.objects.filter(user=user).exists()


@pytest.mark.parametrize("break_module", ["module_missing", "attribute_missing"])
def test_activity_import_failure_never_breaks_storage_or_usage_metering(break_module):
    "Public API."
    import sys

    from django.db import transaction

    from apps.accounts import activity

    user = make_account(f"import-{break_module}")
    inbox = make_inbox(user, at=T0)
    if break_module == "module_missing":
        broken = patch.dict(sys.modules, {"apps.accounts.activity": None})  # import -> ImportError
    else:
        broken = _deleted_attribute(activity, "record_weekly_activity")
    with transaction.atomic():  # parser.process_claim runs inside the request/worker txn
        with broken:
            message = receive(inbox, at=T0 + timedelta(hours=1))
    assert Message.objects.get(pk=message.pk).usage_counted is True
    assert user.monthly_usage.get().messages_received == 1
    assert not AccountWeeklyActivity.objects.filter(user=user).exists()
    assert hasattr(activity, "record_weekly_activity")  # restored


class _deleted_attribute:
    def __init__(self, module, name):
        self.module, self.name = module, name

    def __enter__(self):
        self.saved = getattr(self.module, self.name)
        delattr(self.module, self.name)  # from .activity import name -> ImportError

    def __exit__(self, *exc):
        setattr(self.module, self.name, self.saved)


def test_reconcile_and_backfill_record_weeks_of_unmetered_messages():
    user = make_account("reconciled")
    inbox = make_inbox(user, at=timezone.now())
    store_unmetered(inbox, marker="r")
    out = StringIO()
    call_command("reconcile_monthly_usage", stdout=out)
    assert "weekly_activity_added=1" in out.getvalue()
    assert AccountWeeklyActivity.objects.filter(user=user).count() == 1
    assert backfill_weekly_activity(timezone.now() - timedelta(days=1)) == 0  # idempotent


def test_account_erase_paths_remove_activity_rows():
    """CP-4: the new FK to User is CASCADE, so the existing erase orders
    (gc_agent_accounts, settings_delete, e2e_cleanup) need no new step."""
    from apps.accounts.services import register_agent

    agent, _key, _token = register_agent("gc-bot")
    receive(make_inbox(agent, at=T0), at=T0 + timedelta(hours=1))
    assert AccountWeeklyActivity.objects.filter(user=agent).exists()
    Account.objects.filter(user=agent).update(created_at=T0)
    type(agent).objects.filter(pk=agent.pk).update(date_joined=T0)
    call_command("gc_agent_accounts", idle_days=7, stdout=StringIO())
    assert not type(agent).objects.filter(pk=agent.pk).exists()
    assert not AccountWeeklyActivity.objects.filter(user_id=agent.pk).exists()


def test_system_senders_match_the_real_generators():
    from apps.dashboard.demo import DEMO_SAMPLE_SENDER
    from apps.dashboard.views import TEST_CODE_SENDER
    from apps.inboxes import services as inbox_services

    assert DEMO_SAMPLE_SENDER in SYSTEM_SENDERS
    assert TEST_CODE_SENDER in SYSTEM_SENDERS
    assert "welcome@mailsocket.app" in inbox_services.WELCOME_FROM
    assert "welcome@mailsocket.app" in SYSTEM_SENDERS


# --- AC4: exclusions -----------------------------------------------------------


def test_default_exclusion_setting_names_qa_owner_and_e2e_pattern():
    assert settings.FUNNEL_EXCLUDED_EMAILS == []
    assert f"e2e-*@{settings.MAIL_DOMAIN}" in settings.FUNNEL_EXCLUDED_EMAIL_GLOBS


@override_settings(FUNNEL_EXCLUDED_EMAILS=["qa-e2e@example.test", "owner@example.test"])
def test_staff_qa_e2e_owner_demo_and_workspace_accounts_are_excluded():
    activated_account("real", ref="hn")
    excluded = [
        make_account("qa", ref="hn", email="qa-e2e@example.test"),
        make_account("owner", ref="hn", email="owner@example.test"),
        make_account("e2e", ref="hn", email=f"e2e-run42-signup@{settings.MAIL_DOMAIN}"),
        make_account("demo", ref="hn", email=DEMO_USER_EMAIL),
        make_account("ws", ref="hn", email=f"w1@{settings.WORKSPACE_ACCOUNT_DOMAIN}"),
    ]
    staff = make_account("staff", ref="hn")
    staff.is_staff = True
    staff.save(update_fields=["is_staff"])
    superuser = make_account("root", ref="hn")
    superuser.is_superuser = True
    superuser.save(update_fields=["is_superuser"])
    for user in [*excluded, staff, superuser]:
        receive(make_inbox(user, at=T0), at=T0 + timedelta(hours=1), marker=str(user.pk))
    rep = report(north_star=True)
    assert row(rep, "hn").signups == 1
    assert row(rep, "hn").activated == 1
    week = next(w for w in rep.north_star if w["week_start"] == "2026-09-07")
    assert week["activated_accounts"] == 1


def test_e2e_glob_does_not_exclude_lookalike_customers():
    make_account("cust1", ref="hn", email="e2e-fan@gmail.com")
    make_account("cust2", ref="hn", email=f"my-e2e-box@{settings.MAIL_DOMAIN}")
    assert row(report(), "hn").signups == 2


@override_settings(
    FUNNEL_EXCLUDED_EMAILS=["Founder@Example.test"], FUNNEL_EXCLUDED_EMAIL_GLOBS=["*@corp.test"]
)
def test_exclusion_list_is_driven_by_settings():
    make_account("founder", ref="hn", email="founder@example.test")
    make_account("corp", ref="hn", email="anyone@corp.test")
    make_account("qa", ref="hn", email="qa-e2e@example.test")  # no longer listed -> counted
    assert row(report(), "hn").signups == 1


@override_settings(FUNNEL_EXCLUDED_EMAIL_GLOBS=["a*b*c"])
def test_glob_with_two_stars_is_refused():
    with pytest.raises(ValueError):
        report()


def test_extra_env_var_appends_exclusions(monkeypatch):
    from apps.api.tests.test_wait_capacity_headroom import _load_settings_module

    monkeypatch.setenv("FUNNEL_EXCLUDED_EMAILS_EXTRA", " Me@Personal.test , ,second@x.test")
    base = _load_settings_module("base")
    assert base.FUNNEL_EXCLUDED_EMAILS[-2:] == ["me@personal.test", "second@x.test"]
    assert base.FUNNEL_EXCLUDED_EMAILS == ["me@personal.test", "second@x.test"]


# --- AC5: verdict + output ---------------------------------------------------


def test_verdict_pass_needs_both_thresholds_and_untagged_sources_do_not_count():
    for index in range(3):
        activated_account(f"hn{index}", ref="hn")
    make_account("mcp0", ref="mcp")
    activated_account("other", ref="reddit")
    rep = report(tags=["hn", "mcp", "tensorblock"], pass_signups=4, pass_activations=3)
    assert [r.source for r in rep.rows] == ["hn", "mcp", "tensorblock"]
    assert row(rep, "tensorblock").signups == 0
    channels = rep.verdict.pop("channels")
    assert rep.verdict == {
        "result": "PASS",
        "pass_signups": 4,
        "pass_activations": 3,
        "signups": 4,
        "activated": 3,
        "provisional": False,
        "attempts_below_floor": 2,  # hn and mcp closed below 10 signups (activation item 3)
        "kill_pivot_ready": True,
    }
    assert [(c["tag"], c["days"], c["result"]) for c in channels] == [
        ("hn", 7, "KILL"),
        ("mcp", 7, "KILL"),
        ("tensorblock", 30, "PROVISIONAL"),  # directory PR clock (activation item 3)
    ]
    assert report(tags=["hn", "mcp"], pass_signups=5, pass_activations=3).verdict["result"] == "KILL"
    assert report(tags=["hn", "mcp"], pass_signups=4, pass_activations=4).verdict["result"] == "KILL"


def test_verdict_defaults_are_30_and_10_and_no_verdict_without_tags():
    rep = report(tags=["hn"])
    assert (rep.verdict["pass_signups"], rep.verdict["pass_activations"]) == (30, 10)
    assert rep.verdict["result"] == "NEXT_CHANNEL"  # one failed channel never kills the product
    assert report().verdict is None


def test_verdict_is_provisional_while_activation_windows_are_open():
    assert report(tags=["hn"], now=T0 + timedelta(days=10)).verdict["provisional"] is True


def test_text_output_has_table_totals_verdict_and_no_pii():
    activated_account("alice.smith", ref="hn")
    make_account("bob", host="github.com")
    out = run("--since", SINCE.isoformat(), "--tags", "hn,mcp,free-for-dev,tensorblock", "--north-star")
    assert "@" not in out
    assert "alice" not in out and "bob" not in out and "example" not in out
    lines = out.splitlines()
    assert lines[1].split() == list(funnel.COLUMNS)
    assert any(line.split()[:5] == ["hn", "1", "1", "1", "1"] for line in lines)
    assert any(line.startswith("TOTAL") and line.split()[1:5] == ["1", "1", "1", "1"] for line in lines)
    # activation item 3: free-for-dev / tensorblock run on a 30-day clock, still open at NOW
    assert (
        "VERDICT PROVISIONAL: tags=free-for-dev,hn,mcp,tensorblock signups 1/30, activated 1/10"
        " (provisional: a channel window or activation window is still open)"
    ) in out
    assert "  channel hn: KILL (7-day window, closes 2026-09-21): signups 1, activated 1, below floor" in out
    assert "North Star" in out
    assert "referrer:github.com" not in out  # --tags filters rows


def test_json_schema_and_no_pii():
    activated_account("carol", ref="hn")
    out = run("--since", SINCE.isoformat(), "--days", "7", "--tags", "hn", "--json", "--north-star")
    assert "@" not in out
    data = json.loads(out)
    assert set(data) == {
        "since",
        "until",
        "days",
        "activation_days",
        "generated_at",
        "rows",
        "totals",
        "tags",
        "verdict",
        "north_star",
        "demo_started",
        "time_to_first_mail",
    }
    assert data["since"] == "2026-09-07" and data["until"] == "2026-09-14" and data["days"] == 7
    assert data["rows"] == [
        {
            "source": "hn",
            "signups": 1,
            "verified": 1,
            "with_inbox": 1,
            "activated": 1,
            "activation_open": 0,
            "retained": 0,
            "pricing_viewed": 0,
            "checkout_started": 0,
        }
    ]
    assert data["totals"]["source"] == "TOTAL"
    assert data["tags"] == ["hn"]
    verdict_keys = {
        "result",
        "pass_signups",
        "pass_activations",
        "signups",
        "activated",
        "provisional",
        "channels",
        "attempts_below_floor",
        "kill_pivot_ready",
    }
    assert set(data["verdict"]) == verdict_keys
    assert len(data["north_star"]) == 8
    assert set(data["north_star"][0]) == {"week", "week_start", "activated_accounts", "partial"}


def test_json_without_tags_has_null_verdict_and_north_star():
    data = json.loads(run("--since", SINCE.isoformat(), "--json"))
    assert data["verdict"] is None and data["north_star"] is None and data["tags"] is None
    assert data["rows"] == [] and data["totals"]["signups"] == 0


@pytest.mark.parametrize(
    "args",
    [
        ["--since", "2026-13-01"],
        ["--since", "2026-09-07", "--days", "0"],
        ["--since", "2026-09-07", "--tags", "hn,<script>"],
        ["--since", "2026-09-07", "--tags", " , "],
        ["--since", "2026-09-07", "--pass-signups", "-1"],
    ],
)
def test_bad_arguments_are_refused(args):
    with pytest.raises(CommandError):
        run(*args)


def test_since_is_required():
    with pytest.raises(CommandError):
        run()


# --- AC6: North Star ---------------------------------------------------------


def test_north_star_counts_distinct_accounts_per_iso_week_last_8_weeks():
    a = make_account("ns-a", created=T0 - timedelta(weeks=10))
    b = make_account("ns-b", created=T0 - timedelta(weeks=10))
    inbox_a, inbox_b = make_inbox(a, at=T0 - timedelta(weeks=10)), make_inbox(b, at=T0 - timedelta(weeks=10))
    receive(inbox_a, at=T0, marker="a1")
    receive(inbox_a, at=T0 + timedelta(days=1), marker="a2")  # same week, counted once
    receive(inbox_b, at=T0 + timedelta(days=2), marker="b1")
    receive(inbox_b, at=T0 + timedelta(weeks=1), marker="b2")
    receive(inbox_b, at=T0 - timedelta(weeks=9), marker="old")  # older than 8 weeks
    Message.objects.all().delete()  # purge: the number must survive
    with funnel.read_only_snapshot():
        weeks = funnel.weekly_activated(now=T0 + timedelta(weeks=1, days=3))
    assert len(weeks) == 8
    assert weeks[-1] == {
        "week": "2026-W38",
        "week_start": "2026-09-14",
        "activated_accounts": 1,
        "partial": True,
    }
    assert weeks[-2] == {
        "week": "2026-W37",
        "week_start": "2026-09-07",
        "activated_accounts": 2,
        "partial": False,
    }
    assert weeks[0]["week_start"] == "2026-07-27"
    assert sum(w["activated_accounts"] for w in weeks) == 3


def test_north_star_includes_retained_unmetered_messages_once():
    user = make_account("ns-retained")
    inbox = make_inbox(user, at=T0)
    receive(inbox, at=T0 + timedelta(hours=1))  # metered + retained: still 1
    with funnel.read_only_snapshot():
        weeks = funnel.weekly_activated(now=T0 + timedelta(days=1))
    assert weeks[-1]["activated_accounts"] == 1


def test_north_star_counts_a_retained_message_that_was_never_metered():
    user = make_account("ns-unmetered")
    inbox = make_inbox(user, at=T0)
    store_unmetered(inbox, marker="u", at=T0 + timedelta(hours=1))
    assert not AccountWeeklyActivity.objects.exists()
    with funnel.read_only_snapshot():
        weeks = funnel.weekly_activated(now=T0 + timedelta(days=1))
    assert weeks[-1]["activated_accounts"] == 1





def verp_sender(public_id="obe_r2test", domain=None):
    from apps.outbound.verp import build_verp_localpart

    return f"{build_verp_localpart(public_id)}@{domain or settings.OUTBOUND_BOUNCE_DOMAIN}"


def test_system_sender_domains_cover_the_outbound_verp_sender():
    from apps.accounts.activity import system_sender_domains

    assert system_sender_domains() == (settings.OUTBOUND_BOUNCE_DOMAIN.lower(),)
    with override_settings(OUTBOUND_BOUNCE_DOMAIN="Bounces.Example.TEST."):
        assert system_sender_domains() == ("bounces.example.test",)
    with override_settings(OUTBOUND_BOUNCE_DOMAIN=""):
        assert system_sender_domains() == ()


@pytest.mark.parametrize("metered", [True, False])
def test_outbound_verp_mail_never_activates_but_normal_external_mail_does(metered):
    verp = make_account("verp-only", ref="verp")
    verp_inbox = make_inbox(verp, at=T0 + timedelta(minutes=1))
    normal = make_account("normal", ref="normal")
    normal_inbox = make_inbox(normal, at=T0 + timedelta(minutes=1))
    senders = [
        verp_sender(),
        verp_sender("obe_upper").upper(),  # case-insensitive
        "x@sub." + settings.OUTBOUND_BOUNCE_DOMAIN,  # subdomain of the bounce domain
    ]
    for index, sender in enumerate(senders):
        at = T0 + timedelta(hours=1 + index)
        if metered:
            receive(verp_inbox, at=at, sender=sender, marker=f"v{index}")
        else:
            Message.objects.filter(pk=store_unmetered(verp_inbox, marker=f"v{index}", at=at).pk).update(
                envelope_from=sender
            )
    look_alike = "news@not" + settings.OUTBOUND_BOUNCE_DOMAIN  # not the domain: external
    if metered:
        receive(normal_inbox, at=T0 + timedelta(hours=2), sender=look_alike)
    else:
        message = store_unmetered(normal_inbox, marker="n", at=T0 + timedelta(hours=2))
        Message.objects.filter(pk=message.pk).update(envelope_from=look_alike)
    assert not AccountWeeklyActivity.objects.filter(user=verp).exists()
    assert AccountWeeklyActivity.objects.filter(user=normal).exists() is metered
    rep = report()
    assert row(rep, "verp").activated == 0
    assert row(rep, "normal").activated == 1


@pytest.mark.parametrize("metered", [True, False])
def test_north_star_ignores_outbound_verp_mail_and_counts_normal_external_mail(metered):
    verp = make_account("ns-verp")
    normal = make_account("ns-normal")
    for user, sender in ((verp, verp_sender()), (normal, "person@example.test")):
        inbox = make_inbox(user, at=T0)
        if metered:
            receive(inbox, at=T0 + timedelta(hours=1), sender=sender)
        else:
            message = store_unmetered(inbox, marker=f"ns-{user.pk}", at=T0 + timedelta(hours=1))
            Message.objects.filter(pk=message.pk).update(envelope_from=sender)
    with funnel.read_only_snapshot():
        weeks = funnel.weekly_activated(now=T0 + timedelta(days=1))
    assert weeks[-1]["activated_accounts"] == 1
    assert set(AccountWeeklyActivity.objects.values_list("user_id", flat=True)) == (
        {normal.pk} if metered else set()
    )


def test_backfill_skips_outbound_verp_mail():
    verp = make_account("bf-verp")
    normal = make_account("bf-normal")
    for user, sender in ((verp, verp_sender()), (normal, "person@example.test")):
        message = store_unmetered(make_inbox(user, at=timezone.now()), marker=f"bf-{user.pk}")
        Message.objects.filter(pk=message.pk).update(envelope_from=sender)
    assert backfill_weekly_activity(timezone.now() - timedelta(days=1)) == 1
    assert list(AccountWeeklyActivity.objects.values_list("user_id", flat=True)) == [normal.pk]





def _migration_0022():
    import importlib

    return importlib.import_module("apps.accounts.migrations.0022_account_weekly_activity")


def test_migration_max_retention_comes_from_plan_limits_default_90():
    mig = _migration_0022()
    expected = max(plan["retention_days"] for plan in settings.BILLING_PLAN_LIMITS.values())
    assert mig.max_retention_days() == expected
    with override_settings(BILLING_PLAN_LIMITS={"free": {"retention_days": 1}, "x": {"retention_days": 45}}):
        assert mig.max_retention_days() == 45
    with override_settings(BILLING_PLAN_LIMITS={}):
        assert mig.max_retention_days() == mig.DEFAULT_MAX_RETENTION_DAYS == 90
    assert "up to 30" not in open(mig.__file__).read()
    assert mig.SYSTEM_SENDERS == list(SYSTEM_SENDERS)


def test_migration_backfill_is_bounded_by_max_retention_and_skips_system_and_verp_mail():
    mig = _migration_0022()
    horizon = mig.max_retention_days()
    now = timezone.now()
    cases = {
        "inside": ("person@example.test", now - timedelta(days=horizon - 1)),
        "too-old": ("person@example.test", now - timedelta(days=horizon + 1)),
        "verp": (verp_sender().upper(), now - timedelta(days=1)),
        "welcome": ("Welcome@MailSocket.app", now - timedelta(days=1)),
    }
    users = {}
    for name, (sender, at) in cases.items():
        users[name] = make_account(f"mig-{name}")
        message = store_unmetered(make_inbox(users[name], at=at), marker=f"mig-{name}", at=at)
        Message.objects.filter(pk=message.pk).update(envelope_from=sender)

    class _Editor:
        connection = connection

    assert mig.backfill(None, _Editor()) == 1
    assert list(AccountWeeklyActivity.objects.values_list("user_id", flat=True)) == [users["inside"].pk]
    with override_settings(BILLING_PLAN_LIMITS={"x": {"retention_days": horizon + 5}}):
        assert mig.backfill(None, _Editor()) == 1  # the wider bound picks up "too-old"
    assert mig.backfill(None, _Editor()) == 0  # idempotent


# --- read-only ---------------------------------------------------------------


def _row_counts():
    return {model._meta.label: model.objects.count() for model in django_apps.get_models()}


def test_report_changes_no_row_counts():
    activated_account("ro1", ref="hn")
    make_account("ro2", host="github.com")
    before = _row_counts()
    run("--since", SINCE.isoformat(), "--tags", "hn", "--north-star")
    run("--since", SINCE.isoformat(), "--json")
    assert _row_counts() == before


def test_snapshot_is_a_read_only_transaction():
    from django.db import DatabaseError, transaction

    with transaction.atomic():
        with pytest.raises(DatabaseError):
            with funnel.read_only_snapshot():
                with connection.cursor() as cursor:
                    cursor.execute("SHOW transaction_read_only")
                    assert cursor.fetchone()[0] == "on"
                make_account("write-attempt")


def test_bounce_domain_setting_is_canonical_so_emitted_verp_sender_matches_the_filter(monkeypatch):
    "Public API."
    import importlib

    import config.settings.base as base

    monkeypatch.setenv("OUTBOUND_BOUNCE_DOMAIN", "  Bounces.Example.TEST.  ")
    try:
        reloaded = importlib.reload(base)
        assert reloaded.OUTBOUND_BOUNCE_DOMAIN == "bounces.example.test"
    finally:
        monkeypatch.delenv("OUTBOUND_BOUNCE_DOMAIN", raising=False)
        importlib.reload(base)

    from django.test import override_settings

    from apps.accounts import activity
    from apps.outbound.verp import build_verp_localpart

    with override_settings(OUTBOUND_BOUNCE_DOMAIN="bounces.example.test"):
        sender = f"{build_verp_localpart('abc123')}@bounces.example.test"
        domain = sender.rsplit("@", 1)[1]
        assert domain in activity.system_sender_domains()
