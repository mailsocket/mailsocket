"Public API."

import io
import json
import urllib.error
import urllib.parse
from datetime import timedelta
from email.message import Message
from unittest.mock import patch

import pytest
from django.core import mail
from django.core.management import call_command
from django.test import override_settings

from apps.billing import reconcile
from apps.billing.models import BillingPayment
from apps.billing.tests.test_billing_reconcile import (
    CUSTOMER_EMAIL,
    HC_URL,
    NOW,
    OLD,
    ON,
    TG_TOKEN,
    Edge,
    _Resp,
    alerts,
    local,
    ls_sub,
    plans_snapshot,
    run_cmd,
)

pytestmark = pytest.mark.django_db

DAY_AGO = NOW - timedelta(days=1)


def ls_inv(
    inv_id,
    *,
    sub="3001",
    status="paid",
    total=1200,
    refunded_amount=0,
    refunded=None,
    created=DAY_AGO,
    updated=OLD,
    currency="USD",
):
    return {
        "type": "subscription-invoices",
        "id": str(inv_id),
        "attributes": {
            "store_id": 482773,
            "subscription_id": int(sub) if sub else None,
            "customer_id": 77,
            "user_email": CUSTOMER_EMAIL,
            "user_name": "Customer Secret",
            "billing_reason": "renewal",
            "card_brand": "visa",
            "card_last_four": "4242",
            "currency": currency,
            "status": status,
            "refunded": status == "refunded" if refunded is None else refunded,
            "refunded_at": OLD if refunded_amount else None,
            "subtotal": total,
            "total": total,
            "total_formatted": f"${total / 100:,.2f}",
            "refunded_amount": refunded_amount,
            "created_at": created.isoformat() if created else None,
            "updated_at": updated,
            "urls": {"invoice_url": f"https://app.lemonsqueezy.com/my-orders/x/{inv_id}"},
        },
    }


def payment(inv_id, user, *, amount=1200, refunded_amount=0, refunded=False, currency="USD", sub="3001"):
    return BillingPayment.objects.create(
        user=user,
        ls_invoice_id=str(inv_id),
        ls_subscription_id=sub,
        amount_cents=amount,
        currency=currency,
        refunded_amount_cents=refunded_amount,
        refunded=refunded,
        paid_at=DAY_AGO,
    )


def payments_snapshot():
    return sorted(
        BillingPayment.objects.values_list(
            "ls_invoice_id",
            "user_id",
            "amount_cents",
            "currency",
            "refunded",
            "refunded_amount_cents",
            "refunded_at",
            "receipt_sent_at",
        )
    )


def run_inv(invoices, **kw):
    """One full run with a clean subscription 3001 held by the returned user."""
    return run_cmd(Edge([[ls_sub(3001)]], invoice_pages=invoices), **kw)


@pytest.fixture
def holder():
    return local("inv_holder", "3001")


# --- clean -------------------------------------------------------------------------


def test_bl21_invoice_pass_clean_exit_0_get_only_rows_untouched(holder):
    
    # a pending/void invoice without a row) -> exit 0, no alert, nothing written.
    payment(4001, holder)
    payment(4002, holder, refunded_amount=500)
    payment(4003, holder, refunded_amount=1200, refunded=True)
    invoices = [
        [
            ls_inv(4001),
            ls_inv(4002, status="partial_refund", refunded_amount=500),
            ls_inv(4003, status="refunded", refunded_amount=1200),
            ls_inv(4004, status="pending"),
            ls_inv(4005, status="void"),
        ]
    ]
    before, plans = payments_snapshot(), plans_snapshot()
    edge = Edge([[ls_sub(3001)]], invoice_pages=invoices)
    code, out, err = run_cmd(edge)
    assert (code, err) == (0, "")
    assert "billing_reconcile clean ls_subscriptions=1 mismatches=0 skipped_recent=0" in out.splitlines()
    assert "billing_reconcile invoices ls_invoices=5 in_window=5 days=35 skipped_recent=0" in out.splitlines()
    assert alerts() == []
    assert (payments_snapshot(), plans_snapshot()) == (before, plans)
    assert all(m == "GET" for m, _, _ in edge.requests)
    inv_paths = [p for p in edge.ls_paths() if p.startswith("/subscription-invoices")]
    assert inv_paths == [
        "/subscription-invoices?filter%5Bstore_id%5D=482773&page%5Bnumber%5D=1&page%5Bsize%5D=100"
    ]


# --- each kind -------------------------------------------------------------------------


INVOICE_CASES = {
    # LS paid invoice older than the grace, no local row (lost payment webhook)
    "missing_paid": (
        None,
        dict(),
        "invoice_missing_locally inv=4101 user_pk={pk} local=no BillingPayment row"
        " ls=status=paid total=1200 USD refunded=0 sub=3001",
    ),
    "missing_refunded": (
        None,
        dict(status="refunded", refunded_amount=1200),
        "invoice_missing_locally inv=4101 user_pk={pk} local=no BillingPayment row"
        " ls=status=refunded total=1200 USD refunded=1200 sub=3001",
    ),
    # BL-10: full refund at LS, local row not marked refunded
    "full_refund_not_recorded": (
        dict(),
        dict(status="refunded", refunded_amount=1200),
        "refund_not_recorded inv=4101 user_pk={pk} local=total=1200 USD refunded=0 fully=no"
        " ls=status=refunded total=1200 USD refunded=1200 sub=3001",
    ),
    # BL-10: refunded:true with refunded_amount 0 = fully refunded (webhook rule)
    "full_refund_amount_omitted": (
        dict(),
        dict(status="refunded", refunded_amount=0, refunded=True),
        "refund_not_recorded inv=4101 user_pk={pk} local=total=1200 USD refunded=0 fully=no"
        " ls=status=refunded total=1200 USD refunded=1200 sub=3001",
    ),
    # BL-11: partial refund at LS bigger than the one recorded
    "partial_refund_amount_differs": (
        dict(refunded_amount=300),
        dict(status="partial_refund", refunded_amount=500),
        "refund_not_recorded inv=4101 user_pk={pk} local=total=1200 USD refunded=300 fully=no"
        " ls=status=partial_refund total=1200 USD refunded=500 sub=3001",
    ),
    # BL-11: partial refund never recorded at all
    "partial_refund_missing": (
        dict(),
        dict(status="partial_refund", refunded_amount=500),
        "refund_not_recorded inv=4101 user_pk={pk} local=total=1200 USD refunded=0 fully=no"
        " ls=status=partial_refund total=1200 USD refunded=500 sub=3001",
    ),
    "amount_mismatch": (
        dict(amount=1000),
        dict(),
        "amount_mismatch inv=4101 user_pk={pk} local=total=1000 USD refunded=0 fully=no"
        " ls=status=paid total=1200 USD refunded=0 sub=3001",
    ),
    "currency_mismatch": (
        dict(currency="EUR"),
        dict(),
        "amount_mismatch inv=4101 user_pk={pk} local=total=1200 EUR refunded=0 fully=no"
        " ls=status=paid total=1200 USD refunded=0 sub=3001",
    ),
    # we show a refund LS does not know about
    "local_refund_ls_paid": (
        dict(refunded_amount=1200, refunded=True),
        dict(),
        "status_mismatch inv=4101 user_pk={pk} local=total=1200 USD refunded=1200 fully=yes"
        " ls=status=paid total=1200 USD refunded=0 sub=3001",
    ),
    # LS voided an invoice we recorded as paid
    "void_with_row": (
        dict(),
        dict(status="void"),
        "status_mismatch inv=4101 user_pk={pk} local=row total=1200 USD ls=status=void",
    ),
    "unknown_status_with_row": (
        dict(),
        dict(status="weird"),
        "status_mismatch inv=4101 user_pk={pk} local=row ls=status=weird",
    ),
    "unknown_status_no_row": (
        None,
        dict(status="weird"),
        "status_mismatch inv=4101 user_pk=- local=no row ls=status=weird",
    ),
}


@pytest.mark.parametrize("case", sorted(INVOICE_CASES))
def test_bl21_bl10_bl11_each_invoice_kind_exit_1_alert_rows_untouched(case, holder):
    
    # kind, alert-only (no BillingPayment / AccountPlan write), exit 1.
    row_kw, inv_kw, line = INVOICE_CASES[case]
    if row_kw is not None:
        payment(4101, holder, **row_kw)
    before, plans = payments_snapshot(), plans_snapshot()
    code, out, err = run_inv([[ls_inv(4101, **inv_kw)]])
    expected = line.format(pk=holder.pk)
    assert (code, err) == (1, "")
    assert "billing_reconcile MISMATCH ls_subscriptions=1 mismatches=1 skipped_recent=0" in out.splitlines()
    assert expected in out.splitlines()
    (msg,) = alerts()
    kind = expected.split(" ", 1)[0]
    assert msg.subject == f"[mailsocket] Billing reconcile: 1 mismatch(es) — {kind} x1"
    assert expected in msg.body and "Alert only: nothing was changed" in msg.body
    assert "billing_backfill_payments --apply" in msg.body
    assert (payments_snapshot(), plans_snapshot()) == (before, plans)


def test_missing_invoice_for_unknown_subscription_has_no_user():
    code, out, _ = run_cmd(Edge([[]], invoice_pages=[[ls_inv(4201, sub="9999")]]))
    assert code == 1
    assert (
        "invoice_missing_locally inv=4201 user_pk=- local=no BillingPayment row"
        " ls=status=paid total=1200 USD refunded=0 sub=9999"
    ) in out.splitlines()


def test_amount_and_refund_drift_on_one_invoice_are_both_reported(holder):
    payment(4301, holder, amount=1000)
    code, out, _ = run_inv([[ls_inv(4301, status="refunded", refunded_amount=1200)]])
    assert code == 1
    kinds = [line.split(" ", 1)[0] for line in out.splitlines() if " inv=4301 " in line]
    assert kinds == ["amount_mismatch", "refund_not_recorded"]


# --- grace window + --invoice-days ---------------------------------------------------


def test_missing_invoice_grace_is_2h_exactly(holder):
    # a payment webhook may be in flight for a fresh invoice: not reported inside the grace.
    edge_at = NOW - reconcile.INVOICE_MISSING_GRACE
    inside = {"i": ls_inv("i", created=edge_at + timedelta(seconds=1))["attributes"]}
    at_edge = {"e": ls_inv("e", created=edge_at)["attributes"]}
    assert reconcile.reconcile_invoices(inside, NOW) == ([], 1, 1)
    mismatches, compared, skipped = reconcile.reconcile_invoices(at_edge, NOW)
    assert ([m.kind for m in mismatches], compared, skipped) == (["invoice_missing_locally"], 1, 0)
    assert reconcile.INVOICE_MISSING_GRACE == timedelta(hours=2)
    # end to end (the command uses the real clock): 1 h old -> skipped, 3 h old -> alerted
    code, out, _ = run_inv([[ls_inv(4401, created=NOW - timedelta(hours=1))]])
    assert code == 0
    assert "billing_reconcile invoices ls_invoices=1 in_window=1 days=35 skipped_recent=1" in out.splitlines()
    code, out, _ = run_inv([[ls_inv(4401, created=NOW - timedelta(hours=3))]])
    assert code == 1 and any(
        line.startswith("invoice_missing_locally inv=4401 ") for line in out.splitlines()
    )


def test_grace_applies_to_missing_rows_only_never_to_recorded_drift(holder):
    # a young invoice WITH a row is still compared (amounts are facts already stored)
    payment(4451, holder, amount=900)
    code, out, _ = run_inv([[ls_inv(4451, created=NOW - timedelta(minutes=30))]])
    assert code == 1 and any(line.startswith("amount_mismatch inv=4451 ") for line in out.splitlines())


def test_recently_updated_invoice_is_skipped_webhook_in_flight(holder):
    payment(4501, holder)
    fresh = (NOW - timedelta(minutes=2)).isoformat()
    code, out, _ = run_inv([[ls_inv(4501, status="refunded", refunded_amount=1200, updated=fresh)]])
    assert code == 0
    assert "billing_reconcile invoices ls_invoices=1 in_window=1 days=35 skipped_recent=1" in out.splitlines()


def test_invoice_days_window_default_35_and_flag(holder):
    old = [[ls_inv(4601, created=NOW - timedelta(days=36)), ls_inv(4602, created=NOW - timedelta(days=34))]]
    code, out, _ = run_inv(old)
    assert code == 1
    assert "billing_reconcile invoices ls_invoices=2 in_window=1 days=35 skipped_recent=0" in out.splitlines()
    assert [line.split(" ")[1] for line in out.splitlines() if line.startswith("invoice_missing")] == [
        "inv=4602"
    ]
    edge = Edge([[ls_sub(3001)]], invoice_pages=old)
    with override_settings(**ON), patch("urllib.request.urlopen", side_effect=edge):
        lines = []
        code = reconcile.run(write=lines.append, write_err=lines.append, now=NOW, invoice_days=40)
    assert code == 1
    assert "billing_reconcile invoices ls_invoices=2 in_window=2 days=40 skipped_recent=0" in lines


def test_invoice_days_cli_flag_reaches_the_window(holder):
    edge = Edge([[ls_sub(3001)]], invoice_pages=[[ls_inv(4651, created=NOW - timedelta(days=10))]])
    out = io.StringIO()
    with (
        override_settings(**ON),
        patch("urllib.request.urlopen", side_effect=edge),
        patch("apps.billing.reconcile.time.sleep"),
    ):
        call_command("billing_reconcile", "--invoice-days", "5", stdout=out, stderr=io.StringIO())
    assert "billing_reconcile invoices ls_invoices=1 in_window=0 days=5 skipped_recent=0" in out.getvalue()
    assert alerts() == []


@pytest.mark.parametrize("days", [0, -1, reconcile.INVOICE_DAYS_MAX + 1, True, "35", None])
def test_invalid_invoice_days_is_exit_2_without_any_call(days):
    lines, errs = [], []
    with override_settings(**ON), patch("urllib.request.urlopen", side_effect=AssertionError("no HTTP")):
        code = reconcile.run(write=lines.append, write_err=errs.append, invoice_days=days)
    assert (code, lines) == (2, [])
    assert errs == [
        f"billing_reconcile ERROR: --invoice-days must be 1..{reconcile.INVOICE_DAYS_MAX}"
        " — nothing was compared"
    ]


@pytest.mark.parametrize("days", [1, reconcile.INVOICE_DAYS_MAX])
def test_invoice_days_bounds_are_inclusive(days):
    assert reconcile._valid_invoice_days(days) is True


def test_window_boundary_is_inclusive():
    since_edge = NOW - timedelta(days=3)
    invoices = {
        "a": ls_inv("a", created=since_edge)["attributes"],
        "b": ls_inv("b", created=since_edge - timedelta(seconds=1))["attributes"],
    }
    mismatches, compared, skipped = reconcile.reconcile_invoices(invoices, NOW, 3)
    assert (compared, skipped, [m.sub_id for m in mismatches]) == (1, 0, ["a"])


# --- LS down / pagination: exit 2, never clean ---------------------------------------


class InvoiceEdge(Edge):
    """Edge whose /subscription-invoices page N carries ``metas[N]`` (None -> no meta) or raises."""

    def __init__(self, invoice_pages, metas=None, inv_exc=None, **kw):
        super().__init__([[ls_sub(3001)]], invoice_pages=invoice_pages, **kw)
        self.metas, self.inv_exc = metas or {}, inv_exc

    def _ls(self, method, path):
        if not path.startswith("/subscription-invoices"):
            return super()._ls(method, path)
        if self.inv_exc:
            raise self.inv_exc
        number = int(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["page[number]"][0])
        meta = self.metas.get(number, {"currentPage": number, "lastPage": len(self.invoice_pages)})
        body = {"data": self.invoice_pages[number - 1]}
        if meta is not None:
            body["meta"] = {"page": meta}
        return _Resp(body)


THREE = [[ls_inv(4701)], [ls_inv(4702)], [ls_inv(4703)]]
INVOICE_ERRORS = {
    "shrinking_last_page": (
        THREE,
        {2: {"currentPage": 2, "lastPage": 1}},
        None,
        "inconsistent pagination on page 2: currentPage/lastPage/total=2/1/None, expected 2/3/None",
    ),
    "empty_middle_page": ([[ls_inv(4701)], [], [ls_inv(4703)]], {}, None, "empty page 2 before lastPage 3"),
    "missing_meta": (THREE, {2: None}, None, "malformed subscription-invoices page"),
    "total_differs": (
        THREE,
        {n: {"currentPage": n, "lastPage": 3, "total": 4} for n in (1, 2, 3)},
        None,
        "LS listed 3 distinct subscription-invoices, page 1 said total=4",
    ),
    "ls_down": (THREE, {}, urllib.error.URLError("down"), "LS unreachable (URLError)"),
    "ls_500": (
        THREE,
        {},
        urllib.error.HTTPError("u", 500, "x", Message(), None),
        "LS HTTP 500 on /subscription-invoices",
    ),
    "malformed_item": ([[{"id": "", "attributes": {}}]], {}, None, "malformed subscription-invoice item"),
    "missing_created_at": (
        [[ls_inv(4704, created=None)]],
        {},
        None,
        "malformed subscription-invoice 4704 (created_at)",
    ),
    "bool_total": (
        [[ls_inv(4705, total=True)]],
        {},
        None,
        "malformed subscription-invoice 4705 (total/refunded_amount)",
    ),
}


@pytest.mark.parametrize("case", sorted(INVOICE_ERRORS))
def test_bl21_invoice_listing_failure_is_exit_2_never_clean(case, holder):
    
    # no alert, no dead-man ping, nothing written — even though the subscription pass was clean.
    pages, metas, exc, expected = INVOICE_ERRORS[case]
    payment(4701, holder)
    before = payments_snapshot()
    edge = InvoiceEdge(pages, metas, inv_exc=exc)
    code, out, err = run_cmd(edge, HEALTHCHECKS_RECONCILE_URL=HC_URL)
    assert (code, out, edge.pinged(), alerts()) == (2, "", [], [])
    assert err.splitlines() == [f"billing_reconcile ERROR: {expected} — nothing was compared, no ping"]
    assert payments_snapshot() == before


def test_invoice_pages_are_all_read_in_order_with_pause():
    pages = [[ls_inv(4801)], [ls_inv(4802)], [ls_inv(4803)]]
    edge = InvoiceEdge(pages)
    code, out, _ = run_cmd(edge)
    numbers = [
        urllib.parse.parse_qs(urllib.parse.urlsplit(p).query)["page[number]"][0]
        for p in edge.ls_paths()
        if p.startswith("/subscription-invoices")
    ]
    assert numbers == ["1", "2", "3"]
    assert edge.sleeps == [reconcile.PAGE_PAUSE_SECONDS] * 2
    assert code == 1 and "ls_invoices=3 in_window=3" in out


# --- alert: ids + kind, never a customer email ---------------------------------------


def test_bl21_invoice_alert_in_telegram_has_ids_and_kind_and_no_at_sign(holder):
    payment(4901, holder)
    edge = Edge(
        [[ls_sub(3001)]],
        invoice_pages=[[ls_inv(4901, status="refunded", refunded_amount=1200), ls_inv(4902)]],
    )
    code, out, _ = run_cmd(edge, OPS_TELEGRAM_BOT_TOKEN=TG_TOKEN, OPS_TELEGRAM_CHAT_ID="42")
    assert code == 1 and "alert email=ok telegram=ok" in out
    (tg,) = [b for (_, u, _), b in zip(edge.requests, edge.bodies, strict=True) if "api.telegram.org" in u]
    text = json.loads(tg)["text"]
    assert "Billing reconcile: 2 mismatch(es) — invoice_missing_locally x1, refund_not_recorded x1" in text
    assert "refund_not_recorded inv=4901 user_pk=" in text and "invoice_missing_locally inv=4902 " in text
    assert "@" not in text
    assert CUSTOMER_EMAIL not in out and "Customer Secret" not in out
    assert all(CUSTOMER_EMAIL not in m.body and "Customer Secret" not in m.body for m in mail.outbox)


# --- unit pins (mutation) ---------------------------------------------------------------


def test_ls_money_reads_total_currency_refund_like_the_webhook():
    attrs = ls_inv(1, total=1500, currency="EURO-LONGX")["attributes"]
    assert reconcile._ls_money("1", attrs) == (1500, "EURO-LON", 0, False)
    assert reconcile._ls_money("1", {**attrs, "currency": None}) == (1500, "USD", 0, False)
    assert reconcile._ls_money("1", {**attrs, "refunded_amount": 1500}) == (1500, "EURO-LON", 1500, True)
    assert reconcile._ls_money("1", {**attrs, "refunded_amount": 1499}) == (1500, "EURO-LON", 1499, False)
    assert reconcile._ls_money("1", {**attrs, "status": "refunded"}) == (1500, "EURO-LON", 1500, True)
    assert reconcile._ls_money("1", {**attrs, "refunded": True}) == (1500, "EURO-LON", 1500, True)
    zero = {**attrs, "total": 0}
    assert reconcile._ls_money("1", zero) == (0, "EURO-LON", 0, False)  # $0 invoice: not "refunded"
    for bad in ({"total": None}, {"total": -1}, {"refunded_amount": True}, {"refunded_amount": "x"}):
        with pytest.raises(reconcile.ReconcileError, match=r"^malformed subscription-invoice 1 \(total/"):
            reconcile._ls_money("1", {**attrs, **bad})


def test_invoice_created_parses_or_fails_closed():
    assert reconcile._invoice_created("9", {"created_at": DAY_AGO.isoformat()}) == DAY_AGO
    for bad in (None, "", "not-a-date", "2026-13-45T10:00:00Z"):
        with pytest.raises(
            reconcile.ReconcileError, match=r"^malformed subscription-invoice 9 \(created_at\)$"
        ):
            reconcile._invoice_created("9", {"created_at": bad})


def test_missing_subscription_id_prints_dash():
    attrs = ls_inv(5, sub=None)["attributes"]
    assert reconcile._invoice_ls_text(attrs, 1, "USD", 0) == "status=paid total=1 USD refunded=0 sub=-"


def test_mismatch_line_key_default_is_sub_and_invoice_key_is_inv():
    assert reconcile.Mismatch("x", "1", None, "a", "b").line() == "x sub=1 user_pk=- local=a ls=b"
    assert reconcile.Mismatch("x", "1", 7, "a", "b", key="inv").line() == "x inv=1 user_pk=7 local=a ls=b"


def test_reconcile_invoices_is_sorted_and_counts(holder):
    payment("b2", holder, amount=1)
    invoices = {i: ls_inv(i)["attributes"] for i in ("c3", "b2", "a1")}
    mismatches, compared, skipped = reconcile.reconcile_invoices(invoices, NOW)
    assert [(m.kind, m.sub_id) for m in mismatches] == [
        ("amount_mismatch", "b2"),
        ("invoice_missing_locally", "a1"),
        ("invoice_missing_locally", "c3"),
    ]
    assert (compared, skipped) == (3, 0)
    assert all(m.key == "inv" for m in mismatches)
    assert {m.user_pk for m in mismatches} == {holder.pk}


def test_matching_row_without_refund_never_reports_status():
    from types import SimpleNamespace

    row = SimpleNamespace(
        amount_cents=1200, currency="USD", refunded_amount_cents=0, refunded=False, user_id=1
    )
    assert reconcile._compare_invoice_row("1", ls_inv(1)["attributes"], row) == []
    refunded = SimpleNamespace(**{**vars(row), "refunded_amount_cents": 1200, "refunded": True})
    attrs = ls_inv(1, status="refunded", refunded_amount=1200)["attributes"]
    assert reconcile._compare_invoice_row("1", attrs, refunded) == []
    # local full-refund flag set, amount equal, LS says only partial -> status_mismatch (we over-state)
    over = SimpleNamespace(**{**vars(row), "refunded_amount_cents": 500, "refunded": True})
    partial = ls_inv(1, status="partial_refund", refunded_amount=500)["attributes"]
    assert [m.kind for m in reconcile._compare_invoice_row("1", partial, over)] == ["status_mismatch"]


# --- mutation kills (mutation_backend.sh --only <invoice functions>, round 1 survivors) -----


def test_fetch_ls_invoices_forwards_sleep_and_clock():
    # kills fetch_ls_invoices mutmut_2/3/5/6 (sleep / clock not forwarded -> real time.sleep,
    # real monotonic clock: the injected deadline would never fire)
    pages = [[ls_inv(6001)], [ls_inv(6002)]]
    sleeps = []
    with override_settings(**ON), patch("urllib.request.urlopen", side_effect=InvoiceEdge(pages)):
        got = reconcile.fetch_ls_invoices(sleep=sleeps.append, clock=lambda: 0)
    assert sorted(got) == ["6001", "6002"] and sleeps == [reconcile.PAGE_PAUSE_SECONDS]
    ticks = iter([0, 0, reconcile.DEADLINE_SECONDS + 1])
    with override_settings(**ON), patch("urllib.request.urlopen", side_effect=InvoiceEdge(pages)):
        with pytest.raises(reconcile.ReconcileError, match="^deadline of 240s exceeded at page 2$"):
            reconcile.fetch_ls_invoices(sleep=lambda _s: None, clock=lambda: next(ticks))


def test_ls_money_refunded_flag_and_one_cent_boundary():
    # kills _ls_money mutmut_27/28/29 (the LS `refunded: true` flag ignored) and _38 (0 < total -> 1 < total)
    zero = {**ls_inv(1, total=0)["attributes"], "refunded": True}
    assert reconcile._ls_money("1", zero) == (0, "USD", 0, True)
    cent = {**ls_inv(1, total=1)["attributes"], "refunded_amount": 1}
    assert reconcile._ls_money("1", cent) == (1, "USD", 1, True)


@pytest.mark.parametrize("status", ["pending", "void"])
@pytest.mark.parametrize("with_row", [False, True])
def test_malformed_money_on_a_no_money_invoice_is_an_error_not_clean(holder, status, with_row):
    "Public API."
    if with_row:
        payment(6111, holder)
    bad = ls_inv(6111, status=status)
    bad["attributes"]["total"] = "12.00x"
    code, out, err = run_inv([[bad]])
    assert (code, out) == (2, "")
    assert "malformed subscription-invoice 6111" in err


def test_malformed_ls_total_on_an_invoice_with_a_row_names_the_invoice(holder):
    # kills _compare_invoice_row mutmut_2 (the error must name the invoice, not "None")
    payment(6101, holder)
    bad = ls_inv(6101)
    bad["attributes"]["total"] = "12.00x"
    code, out, err = run_inv([[bad]])
    assert (code, out) == (2, "")
    assert err.splitlines() == [
        "billing_reconcile ERROR: malformed subscription-invoice 6101 (total/refunded_amount)"
        " — nothing was compared, no ping"
    ]


@pytest.mark.parametrize(
    ("row_kw", "inv_kw", "kind"),
    [
        # LS: fully refunded at the amount we hold, but our row is not marked refunded -> lost webhook
        (dict(refunded_amount=500), dict(status="refunded", refunded_amount=500), "refund_not_recorded"),
        # we hold a partial refund LS does not show -> we over-state, not a lost refund
        (dict(refunded_amount=300), dict(), "status_mismatch"),
        # we hold MORE refund than LS, both fully refunded -> over-state
        (
            dict(refunded_amount=1200, refunded=True),
            dict(status="refunded", refunded_amount=1000),
            "status_mismatch",
        ),
    ],
)
def test_refund_direction_decides_the_kind(row_kw, inv_kw, kind, holder):
    # kills _compare_invoice_row mutmut_45/46 (the `missing` expression)
    payment(6201, holder, **row_kw)
    found = reconcile.reconcile_invoices({"6201": ls_inv(6201, **inv_kw)["attributes"]}, NOW)[0]
    assert [m.kind for m in found] == [kind]


def test_skips_are_counted_and_never_stop_the_invoice_loop(holder):
    # kills reconcile_invoices mutmut_24/41 (skipped = 1) and _27/_44 (continue -> break)
    fresh = (NOW - timedelta(minutes=1)).isoformat()
    young = NOW - timedelta(minutes=30)
    invoices = {
        "a": ls_inv("a", updated=fresh)["attributes"],
        "b": ls_inv("b", created=young)["attributes"],
        "c": ls_inv("c", updated=fresh)["attributes"],
        "d": ls_inv("d", created=young)["attributes"],
        "e": ls_inv("e")["attributes"],
    }
    mismatches, compared, skipped = reconcile.reconcile_invoices(invoices, NOW)
    assert ([m.sub_id for m in mismatches], compared, skipped) == (["e"], 5, 4)
