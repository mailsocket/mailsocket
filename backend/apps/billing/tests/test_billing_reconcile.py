"Public API."

import io
import json
import socket
import urllib.error
import urllib.parse
from datetime import timedelta
from email.message import Message
from unittest.mock import patch

import pytest
from django.core import mail
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import AccountPlan
from apps.billing import reconcile
from apps.billing import views as bviews
from apps.billing.tests.test_billing_ls import make_user

pytestmark = pytest.mark.django_db

PAID, SCALE, BUSINESS = "2176466", "2176478", "2187025"
PRICES = {PAID: 1200, SCALE: 4900, BUSINESS: 14900}
OPS = "ops@example.test"
HC_URL = "https://hc-ping.example.test/uuid-1"
TG_TOKEN = "123:tok"
ON = dict(ENABLE_BILLING=True, LS_API_KEY="test-ls-key", LS_STORE_ID="482773", OPS_ALERT_EMAIL=OPS)
NOW = timezone.now().replace(microsecond=0)
OLD = (NOW - timedelta(days=2)).isoformat()  # LS updated_at outside the in-flight grace
RENEWS = NOW + timedelta(days=20)
CUSTOMER_EMAIL = "customer-secret@example.test"


# --- the HTTP edge ------------------------------------------------------------


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")
        self.read_args = []

    def read(self, *args):
        self.read_args.append(args)
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False


class Edge:
    """Fake urlopen: LS GET /subscriptions (paged) + /variants/{id}, the
    Healthchecks ping and Telegram. Records every request."""

    def __init__(
        self, pages=None, *, sub_exc=None, variant_exc=None, prices=None, ping_exc=None, invoice_pages=None
    ):
        self.pages = pages if pages is not None else [[]]
        
        self.invoice_pages = invoice_pages if invoice_pages is not None else [[]]
        self.sub_exc, self.variant_exc, self.ping_exc = sub_exc, variant_exc, ping_exc
        self.prices = prices or PRICES
        self.requests = []
        self.bodies = []

    def __call__(self, req, *args, **kwargs):
        url, method = req.full_url, req.get_method()
        self.requests.append((method, url, kwargs.get("timeout")))
        self.bodies.append(req.data)
        if url.startswith(bviews.LS_API_BASE):
            return self._ls(method, url.removeprefix(bviews.LS_API_BASE))
        if url == HC_URL:
            if self.ping_exc:
                raise self.ping_exc
            return _Resp({})
        if url.startswith("https://api.telegram.org/"):
            return _Resp({"ok": True})
        raise AssertionError(f"unexpected HTTP call {method} {url}")

    def _ls(self, method, path):
        assert method == "GET", f"reconcile must never write to LS: {method} {path}"
        if path.startswith("/variants/"):
            if self.variant_exc:
                raise self.variant_exc
            vid = path.removeprefix("/variants/")
            return _Resp(
                {
                    "data": {
                        "attributes": {"price": self.prices[vid], "interval": "month", "interval_count": 1}
                    }
                }
            )
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        number = int(query["page[number]"][0])
        if path.startswith("/subscription-invoices?"):
            pages = self.invoice_pages
        else:
            assert path.startswith("/subscriptions?"), path
            if self.sub_exc:
                raise self.sub_exc
            pages = self.pages
        body = {
            "meta": {"page": {"currentPage": number, "lastPage": len(pages)}},
            "data": pages[number - 1],
        }
        return _Resp(body)

    def ls_paths(self):
        return [
            u.removeprefix(bviews.LS_API_BASE)
            for m, u, _ in self.requests
            if u.startswith(bviews.LS_API_BASE)
        ]

    def pinged(self):
        return [u for _, u, _ in self.requests if u == HC_URL]


def ls_sub(sub_id, *, variant=PAID, status="active", renews=RENEWS, ends=None, updated=OLD):
    return {
        "type": "subscriptions",
        "id": str(sub_id),
        "attributes": {
            "store_id": 482773,
            "customer_id": 77,
            "variant_id": int(variant),
            "user_email": CUSTOMER_EMAIL,
            "status": status,
            "cancelled": status == "cancelled",
            "renews_at": renews.isoformat() if renews else None,
            "ends_at": ends.isoformat() if ends else None,
            "updated_at": updated,
        },
    }


def local(
    name, sub_id, *, plan=AccountPlan.Plan.PAID, variant=PAID, status=AccountPlan.Status.ACTIVE, end=RENEWS
):
    user = make_user(name)
    AccountPlan.objects.filter(user=user).update(
        plan=plan,
        status=status,
        stripe_subscription_id=sub_id,
        stripe_price_id=variant,
        current_period_end=end,
        updated_at=NOW - timedelta(days=2),
    )
    return user


def plans_snapshot():
    return sorted(
        AccountPlan.objects.values_list(
            "user_id", "plan", "status", "stripe_subscription_id", "stripe_price_id", "current_period_end"
        )
    )


def run_cmd(edge, **settings_kw):
    """call_command with the edge patched; returns (exit code, stdout, stderr)."""
    out, err, sleeps = io.StringIO(), io.StringIO(), []
    with (
        override_settings(**{**ON, **settings_kw}),
        patch("urllib.request.urlopen", side_effect=edge),
        patch("apps.billing.reconcile.time.sleep", side_effect=sleeps.append),
    ):
        try:
            call_command("billing_reconcile", stdout=out, stderr=err)
            code = 0
        except SystemExit as exc:
            code = exc.code
    edge.sleeps = sleeps
    return code, out.getvalue(), err.getvalue()


def alerts():
    return [m for m in mail.outbox if "Billing reconcile" in m.subject]


# --- clean --------------------------------------------------------------------


def test_bl21_clean_run_exit_0_no_alert_plans_untouched_get_only():
    # BL-21: matching LS and local state -> exit 0, one "clean" line, no alert, nothing written.
    local("rc_a", "101")
    local("rc_b", "102", plan=AccountPlan.Plan.SCALE, variant=SCALE, status=AccountPlan.Status.PAST_DUE)
    ends = NOW + timedelta(days=5)
    local("rc_c", "103", plan=AccountPlan.Plan.BUSINESS, variant=BUSINESS, end=ends)
    make_user("rc_free")  # Free plan without a subscription: not compared
    before = plans_snapshot()
    edge = Edge(
        [
            [
                ls_sub(101),
                ls_sub(102, variant=SCALE, status="past_due"),
                ls_sub(103, variant=BUSINESS, status="cancelled", renews=None, ends=ends),
                ls_sub(99, status="expired", renews=None),  # history nobody holds
            ]
        ]
    )
    code, out, err = run_cmd(edge)
    assert (code, err) == (0, "")
    assert "billing_reconcile clean ls_subscriptions=4 mismatches=0 skipped_recent=0" in out
    assert "healthchecks: skipped (HEALTHCHECKS_RECONCILE_URL not set)" in out
    assert alerts() == []
    assert plans_snapshot() == before  # alert-only: never an auto-fix
    assert all(m == "GET" for m, _, _ in edge.requests)
    assert (
        edge.ls_paths()[0]
        == "/subscriptions?filter%5Bstore_id%5D=482773&page%5Bnumber%5D=1&page%5Bsize%5D=100"
    )
    assert sorted(edge.ls_paths()[1:-1]) == sorted(f"/variants/{v}" for v in PRICES)
    assert edge.ls_paths()[-1] == (  
        "/subscription-invoices?filter%5Bstore_id%5D=482773&page%5Bnumber%5D=1&page%5Bsize%5D=100"
    )
    assert all(t == 13 for _, _, t in edge.requests)  # _ls_get's (3, 10) bound on every LS call


# --- each mismatch kind ---------------------------------------------------------


MISMATCH_CASES = {
    "status": (
        dict(status=AccountPlan.Status.ACTIVE),
        dict(status="past_due"),
        "status sub=201 user_pk={pk} local=active ls=status=past_due",
    ),
    "unpaid_is_past_due": (
        dict(status=AccountPlan.Status.ACTIVE),
        dict(status="unpaid"),
        "status sub=201 user_pk={pk} local=active ls=status=unpaid",
    ),
    "variant": (
        dict(),
        dict(variant=SCALE),
        "variant sub=201 user_pk={pk} local=paid/2176466 ls=scale/2176478",
    ),
    "renews_at": (
        dict(end=RENEWS - timedelta(days=30)),
        dict(),
        f"renews_at sub=201 user_pk={{pk}} local={(RENEWS - timedelta(days=30)).isoformat()} "
        f"ls=renews_at={RENEWS.isoformat()}",
    ),
    "renews_at_missing_locally": (
        dict(end=None),
        dict(),
        f"renews_at sub=201 user_pk={{pk}} local=none ls=renews_at={RENEWS.isoformat()}",
    ),
    "expired_at_ls": (
        dict(),
        dict(status="expired", renews=None),
        "expired_at_ls sub=201 user_pk={pk} local=paid/active ls=status=expired",
    ),
    "unknown_variant": (
        dict(),
        dict(variant="9999999"),
        "unknown_variant sub=201 user_pk={pk} local=paid ls=variant=9999999",
    ),
    "unknown_status": (
        dict(),
        dict(status="weird"),
        "unknown_status sub=201 user_pk={pk} local=active ls=status=weird",
    ),
}


@pytest.mark.parametrize("case", sorted(MISMATCH_CASES))
def test_bl21_each_mismatch_kind_exit_1_alert_ids_and_kind_plan_untouched(case):
    # BL-21: every kind of drift is alerted (never silent), with ids + kind, and nothing is fixed.
    local_kw, ls_kw, line = MISMATCH_CASES[case]
    user = local(f"rc_{case}"[:30], "201", **local_kw)
    before = plans_snapshot()
    code, out, err = run_cmd(Edge([[ls_sub(201, **ls_kw)]]))
    expected = line.format(pk=user.pk)
    assert (code, err) == (1, "")
    assert "billing_reconcile MISMATCH ls_subscriptions=1 mismatches=1 skipped_recent=0" in out
    assert expected in out.splitlines()
    (msg,) = alerts()
    kind = expected.split(" ", 1)[0]
    assert msg.subject == f"[mailsocket] Billing reconcile: 1 mismatch(es) — {kind} x1"
    assert msg.to == [OPS]
    assert expected in msg.body and "Alert only: nothing was changed" in msg.body
    assert plans_snapshot() == before


def test_bl21_missing_local_and_local_only_are_both_reported():
    # LS bills a subscription no plan holds (paid, no access) / a plan holds one LS no longer lists.
    user = local("rc_lonly", "301")
    code, out, _ = run_cmd(Edge([[ls_sub(302)]]))
    assert code == 1
    lines = out.splitlines()
    assert "local_only sub=301 user_pk=%s local=paid/active ls=not listed by LS" % user.pk in lines
    assert (
        "missing_local sub=302 user_pk=- local=no plan holds it ls=status=active variant=2176466 customer=77"
        in lines
    )
    (msg,) = alerts()
    assert msg.subject == "[mailsocket] Billing reconcile: 2 mismatch(es) — local_only x1, missing_local x1"


def test_bl18_price_mismatch_between_ls_and_pricing_page_is_alerted():
    # BL-18: catalogue at provider == price on site, checked by the scheduled job.
    local("rc_price", "401")
    code, out, _ = run_cmd(Edge([[ls_sub(401)]], prices={**PRICES, SCALE: 5900}))
    assert code == 1
    assert (
        "price sub=- user_pk=- local=scale site=$49/mo ls=variant=2176478 5900 cents/monthx1"
        in out.splitlines()
    )
    assert [m.subject for m in alerts()] == ["[mailsocket] Billing reconcile: 1 mismatch(es) — price x1"]


def test_bl21_recently_updated_subscription_is_skipped_not_alerted():
    # A webhook may be in flight: compared on the next run, never a false alarm now.
    local("rc_recent", "501", status=AccountPlan.Status.ACTIVE)
    AccountPlan.objects.filter(stripe_subscription_id="501").update(updated_at=NOW)
    fresh = (NOW - timedelta(minutes=2)).isoformat()
    code, out, _ = run_cmd(Edge([[ls_sub(501, status="past_due", updated=fresh)]]))
    assert code == 0
    assert "billing_reconcile clean ls_subscriptions=1 mismatches=0 skipped_recent=1" in out
    local("rc_recent2", "502")
    AccountPlan.objects.filter(stripe_subscription_id="502").update(updated_at=timezone.now())
    code, out, _ = run_cmd(Edge([[ls_sub(501, status="past_due", updated=fresh)]]))
    assert (code, "skipped_recent=2" in out) == (0, True)


def test_bl21_telegram_carries_ids_and_kind_but_never_the_customer_email():
    # apps/ops/notify.py rule: Telegram never carries customer emails.
    local("rc_tg", "601")
    edge = Edge([[ls_sub(601, status="past_due"), ls_sub(602)]])
    code, out, _ = run_cmd(edge, OPS_TELEGRAM_BOT_TOKEN=TG_TOKEN, OPS_TELEGRAM_CHAT_ID="42")
    assert code == 1
    assert "alert email=ok telegram=ok" in out
    tg = [
        b
        for r, b in zip(edge.requests, edge.bodies, strict=True)
        if r[1].startswith("https://api.telegram.org/")
    ]
    assert len(tg) == 1
    tg_text = json.loads(tg[0])["text"]
    assert "Billing reconcile: 2 mismatch(es) — missing_local x1, status x1" in tg_text
    assert "status sub=601 user_pk=" in tg_text and "missing_local sub=602 user_pk=-" in tg_text
    assert CUSTOMER_EMAIL not in tg_text and "[email]" not in tg_text
    with (
        patch("urllib.request.urlopen") as spy,
        override_settings(OPS_TELEGRAM_BOT_TOKEN=TG_TOKEN, OPS_TELEGRAM_CHAT_ID="42"),
    ):
        spy.return_value = _Resp({"ok": True})
        reconcile.send_alert([reconcile.Mismatch("status", "601", 5, "active", "status=past_due")])
    text = json.loads(spy.call_args.args[0].data)["text"]
    assert "status sub=601 user_pk=5 local=active ls=status=past_due" in text
    assert CUSTOMER_EMAIL not in out
    assert all(CUSTOMER_EMAIL not in m.body and CUSTOMER_EMAIL not in m.subject for m in mail.outbox)


def test_bl21_alert_is_capped_in_telegram_full_list_in_email():
    many = [reconcile.Mismatch("local_only", str(i), i, "paid/active", "x") for i in range(45)]
    with patch("apps.billing.reconcile.ops_notify.notify") as notify:
        reconcile.send_alert(many)
    level, title, tg_body = notify.call_args.args
    assert (level, title) == ("money", "Billing reconcile: 45 mismatch(es) — local_only x45")
    assert tg_body.count("local_only sub=") == 40
    assert "... and 5 more (full list in the email and the cron log)" in tg_body
    assert notify.call_args.kwargs["email_body"].count("local_only sub=") == 45


# --- LS down / malformed: exit 2, never a false "clean" --------------------------


DOWN = {
    "urlerror": dict(sub_exc=urllib.error.URLError("down")),
    "http_500": dict(sub_exc=urllib.error.HTTPError("u", 500, "x", Message(), None)),
    "http_429": dict(sub_exc=urllib.error.HTTPError("u", 429, "slow down", Message(), None)),
    "timeout": dict(sub_exc=socket.timeout("timed out")),
    "variants_down": dict(variant_exc=urllib.error.URLError("down")),
}


@pytest.mark.parametrize("failure", sorted(DOWN))
def test_bl21_ls_down_exit_2_no_clean_no_alert_no_ping(failure):
    local("rc_down", "701", status=AccountPlan.Status.ACTIVE)
    edge = Edge([[ls_sub(701, status="past_due")]], **DOWN[failure])
    code, out, err = run_cmd(edge, HEALTHCHECKS_RECONCILE_URL=HC_URL)
    assert code == 2
    assert "clean" not in out and "MISMATCH" not in out
    assert err.startswith("billing_reconcile ERROR: ") and "nothing was compared, no ping" in err
    assert alerts() == []
    assert edge.pinged() == []


@pytest.mark.parametrize(
    "page",
    [
        {"data": [], "meta": {}},
        {"data": "x", "meta": {"page": {"lastPage": 1}}},
        {"data": [], "meta": {"page": {"lastPage": True}}},
        {"data": [], "meta": {"page": {"lastPage": 0}}},
        {"data": [{"id": "", "attributes": {}}], "meta": {"page": {"lastPage": 1}}},
        {"data": [{"id": "9", "attributes": "x"}], "meta": {"page": {"lastPage": 1}}},
        ["not", "a", "dict"],
    ],
)
def test_bl21_malformed_ls_page_is_an_error_not_clean(page):
    edge = Edge()
    with patch.object(Edge, "_ls", lambda self, m, p: _Resp(page)):
        code, out, err = run_cmd(edge, HEALTHCHECKS_RECONCILE_URL=HC_URL)
    assert (code, "clean" in out, edge.pinged()) == (2, False, [])
    assert "malformed" in err


def test_bl21_unexpected_exception_is_exit_2_not_mismatch():
    with patch("apps.billing.reconcile.reconcile", side_effect=RuntimeError("boom")):
        code, out, err = run_cmd(Edge([[ls_sub(1)]]))
    assert (code, out) == (2, "")
    assert err.strip() == "billing_reconcile ERROR: RuntimeError — nothing was compared, no ping"


@pytest.mark.parametrize("override", [dict(ENABLE_BILLING=False), dict(LS_API_KEY="")])
def test_bl21_billing_off_or_no_api_key_is_exit_2_without_any_call(override):
    edge = Edge([[ls_sub(1)]])
    code, out, err = run_cmd(edge, HEALTHCHECKS_RECONCILE_URL=HC_URL, **override)
    assert (code, out, edge.requests) == (2, "", [])
    assert "nothing was compared" in err


# --- pagination ---------------------------------------------------------------------


def test_bl21_pagination_reads_every_page_and_finds_drift_on_the_last_one():
    for i in range(1, 6):
        local(f"rc_pg{i}", str(800 + i))
    pages = [[ls_sub(801), ls_sub(802)], [ls_sub(803), ls_sub(804)], [ls_sub(805, variant=BUSINESS)]]
    edge = Edge(pages)
    code, out, _ = run_cmd(edge)
    assert code == 1
    numbers = [
        urllib.parse.parse_qs(urllib.parse.urlsplit(p).query)["page[number]"][0]
        for p in edge.ls_paths()
        if p.startswith("/subscriptions")
    ]
    assert numbers == ["1", "2", "3"]
    assert edge.sleeps == [reconcile.PAGE_PAUSE_SECONDS] * 2  # rate-limit friendly
    assert "ls_subscriptions=5 mismatches=1" in out
    assert any(line.startswith("variant sub=805 ") for line in out.splitlines())
    assert not any(line.startswith("local_only") for line in out.splitlines())


def one_per_page(n):
    """n non-empty pages (an empty page before lastPage is itself an error)."""
    return [[ls_sub(10_000 + i)] for i in range(n)]


def test_bl21_too_many_pages_or_deadline_is_exit_2():
    edge = Edge(one_per_page(reconcile.MAX_PAGES + 1))
    code, _, err = run_cmd(edge)
    assert code == 2
    assert err.splitlines() == [
        f"billing_reconcile ERROR: {reconcile.MAX_PAGES + 1} pages > MAX_PAGES {reconcile.MAX_PAGES}"
        " — nothing was compared, no ping"
    ]
    assert len(edge.ls_paths()) == 1

    ticks = iter([0, 0, reconcile.DEADLINE_SECONDS + 1])
    with patch("urllib.request.urlopen", side_effect=Edge(one_per_page(3))):
        with override_settings(**ON), pytest.raises(reconcile.ReconcileError, match="deadline"):
            reconcile.fetch_ls_subscriptions(sleep=lambda _s: None, clock=lambda: next(ticks))


# --- dead-man --------------------------------------------------------------------


@pytest.mark.parametrize("drift", [False, True])
def test_bl21_deadman_pinged_after_every_completed_run(drift):
    local("rc_hc", "901")
    edge = Edge([[ls_sub(901, status="past_due" if drift else "active")]])
    code, out, _ = run_cmd(edge, HEALTHCHECKS_RECONCILE_URL=HC_URL)
    assert code == (1 if drift else 0)
    assert edge.pinged() == [HC_URL]
    assert "healthchecks: ping ok" in out
    assert HC_URL not in out  # the ping URL is a secret-ish id: never printed
    methods = [m for m, u, _ in edge.requests if u == HC_URL]
    assert methods == ["GET"]


def test_bl21_deadman_failure_never_changes_the_verdict_and_non_https_is_refused():
    local("rc_hc2", "902")
    edge = Edge([[ls_sub(902)]], ping_exc=urllib.error.URLError("hc down"))
    code, out, _ = run_cmd(edge, HEALTHCHECKS_RECONCILE_URL=HC_URL)
    assert (code, "healthchecks: ping failed (URLError)" in out) == (0, True)
    edge = Edge([[ls_sub(902)]])
    code, out, _ = run_cmd(edge, HEALTHCHECKS_RECONCILE_URL="http://hc.example.test/x")
    assert (code, edge.pinged()) == (0, [])
    assert "healthchecks: skipped (HEALTHCHECKS_RECONCILE_URL is not https)" in out


# --- unit pins -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("$12/mo", 1200),
        ("$149/mo", 14900),
        ("$12.50/mo", 1250),
        ("Custom", None),
        ("$12", None),
        (None, None),
    ],
)
def test_site_price_cents(text, cents):
    assert reconcile._site_price_cents(text) == cents


def test_renews_tolerance_boundary():
    user = local("rc_tol", "951", end=RENEWS + reconcile.RENEWS_TOLERANCE)
    plan = AccountPlan.objects.get(user=user)
    attrs = ls_sub(951)["attributes"]
    assert reconcile._check_period("951", attrs, plan) == []
    plan.current_period_end = RENEWS + reconcile.RENEWS_TOLERANCE + timedelta(seconds=1)
    assert [m.kind for m in reconcile._check_period("951", attrs, plan)] == ["renews_at"]
    assert reconcile._check_period("951", {**attrs, "renews_at": None}, plan) == []


def test_recent_grace_boundary():
    now = timezone.now()
    assert reconcile._recent(now - reconcile.RECENT_GRACE + timedelta(seconds=1), now) is True
    assert reconcile._recent(now - reconcile.RECENT_GRACE, now) is False
    assert reconcile._recent(None, now) is False


def test_status_map_matches_what_the_webhook_writes():
    m = reconcile.LIVE_STATUS_TO_LOCAL
    assert {k: str(v) for k, v in m.items()} == {
        "active": "active",
        "on_trial": "active",
        "cancelled": "active",
        "past_due": "past_due",
        "unpaid": "past_due",
        "paused": "past_due",
    }


# --- mutation kills (mutation_focus.sh round 1 survivors that were not equivalent) -------


@pytest.mark.parametrize(
    ("failure", "line"),
    [
        (
            dict(sub_exc=urllib.error.HTTPError("u", 500, "x", Message(), None)),
            "LS HTTP 500 on /subscriptions",
        ),
        (dict(sub_exc=urllib.error.URLError("down")), "LS unreachable (URLError)"),
        (
            dict(variant_exc=urllib.error.HTTPError("u", 404, "x", Message(), None)),
            "LS HTTP 404 on /variants/2176466",
        ),
        (dict(variant_exc=TimeoutError("slow")), "LS variant 2176466 unreadable (TimeoutError)"),
    ],
)
def test_error_lines_name_the_failing_call_without_query_or_secrets(failure, line):
    code, out, err = run_cmd(Edge([[]], **failure))
    assert (code, out) == (2, "")
    assert err.splitlines() == [f"billing_reconcile ERROR: {line} — nothing was compared, no ping"]
    assert "test-ls-key" not in err


@pytest.mark.parametrize(
    ("page", "line"),
    [
        ({"data": "x", "meta": {"page": {"lastPage": 1}}}, "malformed subscriptions page"),
        ({"data": [["not-a-dict"]], "meta": {"page": {"lastPage": 1}}}, "malformed subscription item"),
    ],
)
def test_malformed_page_and_item_exact_error(page, line):
    with patch.object(Edge, "_ls", lambda self, m, p: _Resp(page)):
        code, _, err = run_cmd(Edge())
    assert code == 2
    assert err.splitlines() == [f"billing_reconcile ERROR: {line} — nothing was compared, no ping"]
    with pytest.raises(reconcile.ReconcileError, match="^malformed subscription item$"):
        reconcile._parse_item(["not-a-dict"])


@pytest.mark.parametrize(
    ("override", "line"),
    [
        (dict(ENABLE_BILLING=False), "billing_reconcile ERROR: ENABLE_BILLING is off — nothing was compared"),
        (dict(LS_API_KEY=""), "billing_reconcile ERROR: LS_API_KEY is not set — nothing was compared"),
    ],
)
def test_billing_off_exact_error_line(override, line):
    assert run_cmd(Edge(), **override)[2].splitlines() == [line]


def test_unexpected_exception_is_logged_with_traceback(caplog):
    with caplog.at_level("ERROR", logger="apps.billing.reconcile"):
        with patch("apps.billing.reconcile.reconcile", side_effect=RuntimeError("boom")):
            assert run_cmd(Edge([[ls_sub(1)]]))[0] == 2
    (rec,) = [r for r in caplog.records if r.name == "apps.billing.reconcile"]
    assert (rec.getMessage(), rec.exc_info[0]) == ("billing_reconcile_failed", RuntimeError)


def test_variant_check_stale_price_id_with_matching_plan_and_missing_variant():
    user = local("rc_stale", "1001", variant=None)
    plan = AccountPlan.objects.get(user=user)
    attrs = ls_sub(1001)["attributes"]
    assert [m.line() for m in reconcile._check_variant("1001", attrs, plan)] == [
        f"variant sub=1001 user_pk={user.pk} local=paid/none ls=paid/2176466"
    ]
    assert [m.line() for m in reconcile._check_variant("1001", {**attrs, "variant_id": None}, plan)] == [
        f"unknown_variant sub=1001 user_pk={user.pk} local=paid ls=variant="
    ]


def test_cancelled_subscription_is_compared_on_ends_at_not_renews_at():
    ends = NOW + timedelta(days=5)
    user = local("rc_ends", "1101", end=NOW + timedelta(days=20))
    code, out, _ = run_cmd(Edge([[ls_sub(1101, status="cancelled", renews=None, ends=ends)]]))
    assert code == 1
    assert (
        f"renews_at sub=1101 user_pk={user.pk} local={(NOW + timedelta(days=20)).isoformat()} "
        f"ls=ends_at={ends.isoformat()}"
    ) in out.splitlines()


def test_empty_subscription_id_is_not_a_held_subscription():
    local("rc_empty", "")
    code, out, _ = run_cmd(Edge([[]]))
    assert code == 0 and "ls_subscriptions=0 mismatches=0" in out


def test_skips_are_counted_and_never_stop_the_comparison():
    fresh = (NOW - timedelta(minutes=1)).isoformat()
    local("rc_s1", "1201")
    local("rc_s2", "1202")
    lonely = local("rc_s3", "1203")  # local-only, NOT recent: compared after the recent ones
    AccountPlan.objects.filter(stripe_subscription_id="1202").update(updated_at=timezone.now())
    AccountPlan.objects.filter(stripe_subscription_id="1201").update(updated_at=timezone.now())
    late_drift = local("rc_s4", "1204")
    pages = [[ls_sub(1205, updated=fresh), ls_sub(1206, updated=fresh), ls_sub(1204, status="past_due")]]
    code, out, _ = run_cmd(Edge(pages))
    lines = out.splitlines()
    assert code == 1
    assert "billing_reconcile MISMATCH ls_subscriptions=3 mismatches=2 skipped_recent=4" in lines
    assert f"status sub=1204 user_pk={late_drift.pk} local=active ls=status=past_due" in lines
    assert f"local_only sub=1203 user_pk={lonely.pk} local=paid/active ls=not listed by LS" in lines


def test_page_cap_is_inclusive_and_deadline_is_strict():
    edge = Edge(one_per_page(reconcile.MAX_PAGES))
    assert run_cmd(edge)[0] == 1  # every listed subscription is missing_local: read to the end
    assert len([p for p in edge.ls_paths() if p.startswith("/subscriptions")]) == reconcile.MAX_PAGES
    ticks = iter([0, reconcile.DEADLINE_SECONDS])
    with patch("urllib.request.urlopen", side_effect=Edge([[]])), override_settings(**ON):
        assert reconcile.fetch_ls_subscriptions(sleep=lambda _s: None, clock=lambda: next(ticks)) == {}


def test_alert_exactly_at_the_line_cap_has_no_overflow_note_and_exact_bodies():
    rows = [reconcile.Mismatch("variant", str(i), i, "a", "b") for i in range(reconcile.ALERT_MAX_LINES)]
    with patch("apps.billing.reconcile.ops_notify.notify") as notify:
        reconcile.send_alert(rows)
    body = "\n".join([m.line() for m in rows] + list(reconcile.ALERT_FOOTER))
    assert notify.call_args.args[2] == body
    assert notify.call_args.kwargs == {"email_body": body}


def test_deadman_results_request_shape_and_exact_lines():
    lines = []
    with override_settings(HEALTHCHECKS_RECONCILE_URL=""):
        assert reconcile.ping_deadman(lines.append) == "skipped"
    with override_settings(HEALTHCHECKS_RECONCILE_URL="http://x.test/p"):
        assert reconcile.ping_deadman(lines.append) == "invalid"
    seen = []

    responses = []

    def ok(req, timeout=None):
        seen.append((req.get_method(), req.full_url, req.get_header("User-agent"), timeout))
        responses.append(_Resp({}))
        return responses[-1]

    with override_settings(HEALTHCHECKS_RECONCILE_URL=HC_URL):
        with patch("urllib.request.urlopen", side_effect=ok):
            assert reconcile.ping_deadman(lines.append) == "ok"
        with patch("urllib.request.urlopen", side_effect=OSError("x")):
            assert reconcile.ping_deadman(lines.append) == "error"
    assert seen == [("GET", HC_URL, "mailsocket-reconcile/1", 10)]
    assert responses[0].read_args == [(1024,)]  # bounded read of the ping reply
    assert lines == [
        "healthchecks: skipped (HEALTHCHECKS_RECONCILE_URL not set)",
        "healthchecks: skipped (HEALTHCHECKS_RECONCILE_URL is not https)",
        "healthchecks: ping ok",
        "healthchecks: ping failed (OSError)",
    ]
