"Public API."

import logging
from datetime import datetime
from datetime import timezone as dt_tz
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core import mail
from django.core.mail import get_connection, send_mail
from django.db import transaction
from django.test import override_settings

from apps.accounts.models import AccountOwnership
from apps.billing import receipts
from apps.billing.models import BillingPayment
from apps.billing.tests.mutation_kill_helpers import (
    OPS,
    _messages,
    _notify_calls,
    _payment,
    _user,
)

pytestmark = pytest.mark.django_db


# --- receipts._clean -----------------------------------------------------------


def test_clean_none_is_empty_and_whitespace_collapsed():
    # kills receipts.x__clean__mutmut_5 (`or True` -> "None") and _7 ("XXXX")
    assert receipts._clean(None) == ""
    assert receipts._clean("  a\n b\t") == "a b"


# --- receipts.customer_email -----------------------------------------------------


def test_customer_email_unknown_user_is_empty():
    # kills x_customer_email__mutmut_4 (return "XXXX")
    assert receipts.customer_email(987654321) == ""


def test_customer_email_prefers_account_email_over_user_email():
    # kills x_customer_email__mutmut_12/13/15/16 (account lookup dropped /
    # `and` instead of `or`): the Account address wins over User.email.
    user = _user("ce_acct", email="user-field@example.test", account_email="acct@example.test")
    assert receipts.customer_email(user.pk) == "acct@example.test"


def test_customer_email_without_account_falls_back_to_user_email_then_empty():
    # kills x_customer_email__mutmut_14/17/18 (no Account: user.email) and
    # _19 (nothing at all: "", never a placeholder address)
    user = _user("ce_noacct", email="only-user@example.test", account=False)
    assert receipts.customer_email(user.pk) == "only-user@example.test"
    bare = _user("ce_bare", email="", account=False)
    assert receipts.customer_email(bare.pk) == ""


def test_customer_email_workspace_goes_to_human_owner():
    # kills x_customer_email__mutmut_5/7 (ownership link ignored)
    owner = _user("ce_owner", email="human@example.test", account_email="human@example.test")
    ws = _user("ce_ws", email="", account_email="ws-123@workspace.invalid")
    AccountOwnership.objects.create(owner_user=owner, account_user=ws, name="ws")
    assert receipts.customer_email(ws.pk) == "human@example.test"


# --- receipts._send ----------------------------------------------------------------


def test_send_uses_the_bounded_smtp_connection():
    # kills x__send__mutmut_1/2/8/14: the 10 s SMTP timeout is on the
    # connection that actually sends (an unbounded SMTP call would hang the
    # webhook worker). Equivalent, not pinned: _5 (from_email None ->
    # send_mail uses DEFAULT_FROM_EMAIL) and _7/_13/_16 (fail_silently is
    # only read when send_mail builds its own connection; we pass one).
    made = []

    def _conn(*args, **kwargs):
        made.append((args, kwargs, get_connection(*args, **kwargs)))
        return made[-1][2]

    with (
        patch("apps.billing.receipts.get_connection", side_effect=_conn),
        patch("apps.billing.receipts.send_mail", wraps=send_mail) as sm,
    ):
        receipts._send("Sub\nject", "body", "to@example.test")
    assert [(a, k) for a, k, _ in made] == [((), {"timeout": 10})]
    assert sm.call_args.kwargs["connection"] is made[0][2]
    msg = mail.outbox[-1]
    assert (msg.subject, msg.body, msg.to) == ("Sub ject", "body", ["to@example.test"])
    assert msg.from_email == settings.DEFAULT_FROM_EMAIL
    # audit 2026-10-11 #6: pin the exact call too. _5/_7/_13/_16 are
    # behaviour-equivalent TODAY (Django falls back to DEFAULT_FROM_EMAIL, and
    # ignores fail_silently when a connection is passed), but an SMTP error
    # must reach our own except/log in the callers: if `connection=` is ever
    # dropped, fail_silently=True would swallow it silently.
    assert sm.call_args.args == ("Sub ject", "body", settings.DEFAULT_FROM_EMAIL, ["to@example.test"])
    assert sm.call_args.kwargs == {"fail_silently": False, "connection": made[0][2]}


# --- receipts._send_payment_receipt ----------------------------------------------


@override_settings(DASHBOARD_HOST="pay.example.test")
def test_receipt_exact_body_card_and_invoice_fallbacks():
    # kills x__send_payment_receipt__mutmut_32-35/38/39/43/44/47-51/57/58/69:
    # the full body, line by line, incl. "Card" when LS sent no brand, the
    # portal fallback when there is no invoice url, and the dashboard host.
    user = _user("rc_body", account_email="payer@example.test")
    p = _payment(user, card_brand="", card_last_four="4242", invoice_url="")
    receipts._send_payment_receipt(p.pk, "Pro")
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    assert msg.to == ["payer@example.test"]
    assert msg.subject == "mailsocket receipt — $12.00 (Pro)"
    assert msg.body == "\n".join(
        [
            "Thanks for your payment.",
            "",
            "Plan: Pro",
            "Amount: $12.00",
            "Date: 2026-10-09 01:58 UTC",
            "Reason: Renewal",
            "Paid with: Card ••4242",
            "Invoice: available in the billing portal",
            "",
            receipts.NOT_DOUBLE_LINE,
            "",
            receipts.SUPPORT_LINE,
            "",
            "Billing: https://pay.example.test/app/billing/",
        ]
    )


@override_settings(DASHBOARD_HOST="")
def test_receipt_billing_link_default_host():
    # kills x__send_payment_receipt__mutmut_60/61
    p = _payment(_user("rc_host"), invoice="inv_host")
    receipts._send_payment_receipt(p.pk, "Pro")
    assert mail.outbox[0].body.endswith("\nBilling: https://dash.mailsocket.app/app/billing/")


def test_receipt_billing_link_when_dashboard_host_setting_absent(settings):
    # kills x__send_payment_receipt__mutmut_56 (getattr without default ->
    # AttributeError -> no receipt at all) and _59 ("https://XXXX/...")
    del settings.DASHBOARD_HOST
    p = _payment(_user("rc_nohost"), invoice="inv_nohost")
    receipts._send_payment_receipt(p.pk, "Pro")
    assert mail.outbox[0].body.endswith("\nBilling: https://dash.mailsocket.app/app/billing/")


@override_settings(TIME_ZONE="Asia/Bangkok")
def test_receipt_date_is_utc_even_when_server_timezone_is_not():
    # kills x__send_payment_receipt__mutmut_28 (astimezone(None) = local tz:
    # the mail would print 08:58 and still say "UTC")
    p = _payment(_user("rc_tz"), invoice="inv_tz")
    receipts._send_payment_receipt(p.pk, "Pro")
    assert "\nDate: 2026-10-09 01:58 UTC\n" in mail.outbox[0].body


def test_receipt_date_falls_back_to_created_at_not_now():
    # kills x__send_payment_receipt__mutmut_25 (`created_at and now()`)
    p = _payment(_user("rc_created"), invoice="inv_created", paid_at=None)
    BillingPayment.objects.filter(pk=p.pk).update(created_at=datetime(2026, 1, 2, 3, 4, tzinfo=dt_tz.utc))
    receipts._send_payment_receipt(p.pk, "Pro")
    assert "\nDate: 2026-01-02 03:04 UTC\n" in mail.outbox[0].body


def test_receipt_claim_is_per_invoice_and_at_most_once():
    # kills x__send_payment_receipt__mutmut_5 (claim UPDATE without pk= marks
    # EVERY unsent payment as sent -> other customers never get a receipt)
    # and _6 (claim without receipt_sent_at__isnull -> a second call re-sends)
    a = _payment(_user("rc_a", account_email="a@example.test"), invoice="inv_a")
    b = _payment(_user("rc_b", account_email="b@example.test"), invoice="inv_b")
    receipts._send_payment_receipt(a.pk, "Pro")
    b.refresh_from_db()
    assert b.receipt_sent_at is None
    receipts._send_payment_receipt(a.pk, "Pro")
    receipts._send_payment_receipt(b.pk, "Pro")
    assert [m.to for m in mail.outbox] == [["a@example.test"], ["b@example.test"]]


def test_receipt_without_user_sends_nothing_and_logs_invoice(caplog):
    # kills x__send_payment_receipt__mutmut_15 (mail to "XXXX") and the
    # log-line mutants _17-_22
    p = _payment(None, invoice="inv_orphan")
    with caplog.at_level(logging.ERROR, logger="apps.billing.receipts"):
        receipts._send_payment_receipt(p.pk, "Pro")
    assert mail.outbox == []
    assert _messages(caplog, "apps.billing.receipts") == ["billing_receipt_no_recipient invoice=inv_orphan"]
    p.refresh_from_db()
    assert p.receipt_sent_at is not None  # claimed: never retried (at-most-once)


def test_receipt_send_failure_logged_with_payment_pk(caplog):
    # kills x__send_payment_receipt__mutmut_70-75
    p = _payment(_user("rc_fail"), invoice="inv_fail")
    with (
        patch("apps.billing.receipts.send_mail", side_effect=OSError("smtp down")),
        caplog.at_level(logging.ERROR, logger="apps.billing.receipts"),
    ):
        receipts._send_payment_receipt(p.pk, "Pro")
    assert _messages(caplog, "apps.billing.receipts") == [f"billing_receipt_send_failed payment_pk={p.pk}"]


# --- receipts.queue_payment_receipt ------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_queue_receipt_hook_is_robust_a_raising_send_never_breaks_the_commit():
    # kills x_queue_payment_receipt__mutmut_2/4/10 (robust=True dropped): the
    # on_commit hook is the second guard; a raising sender must not escape
    # the webhook's transaction.atomic() nor skip later hooks.
    later = []
    with patch("apps.billing.receipts._send_payment_receipt", side_effect=RuntimeError("boom")) as sender:
        with transaction.atomic():
            receipts.queue_payment_receipt(7, "Pro")
            transaction.on_commit(lambda: later.append("ran"))
    assert sender.call_count == 1
    assert later == ["ran"]


@pytest.mark.django_db(transaction=True)
def test_queued_receipt_is_sent_after_commit_for_that_payment_and_plan(caplog):
    # audit 2026-10-11 #6, kills x_queue_payment_receipt__mutmut_6/7/8/9 (the
    # lambda's arguments: payment_pk None / description None / one argument
    # dropped). They survived because robust=True swallows the TypeError and
    # the earlier tests patch the sender out. Real hook, real commit.
    user = _user("rc_queue", account_email="queued@example.test")
    p = _payment(user, invoice="inv_queue", card_brand="visa", card_last_four="4242")
    with caplog.at_level(logging.ERROR):
        with transaction.atomic():
            receipts.queue_payment_receipt(p.pk, "Pro")
            assert mail.outbox == []  # nothing before the commit
    assert [(m.to, m.subject) for m in mail.outbox] == [
        (["queued@example.test"], "mailsocket receipt — $12.00 (Pro)")
    ]
    assert "Plan: Pro\n" in mail.outbox[0].body
    assert "Paid with: Visa ••4242\n" in mail.outbox[0].body
    p.refresh_from_db()
    assert p.receipt_sent_at is not None
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []


@pytest.mark.django_db(transaction=True)
def test_queued_receipt_is_dropped_on_rollback():
    user = _user("rc_rollback", account_email="rollback@example.test")
    p = _payment(user, invoice="inv_rollback")

    class Abort(Exception):
        pass

    with pytest.raises(Abort):
        with transaction.atomic():
            receipts.queue_payment_receipt(p.pk, "Pro")
            raise Abort
    assert mail.outbox == []
    p.refresh_from_db()
    assert p.receipt_sent_at is None


@pytest.mark.django_db(transaction=True)
def test_queue_receipt_robust_hook_failure_is_logged_by_django(caplog):
    # robust=True: a raising sender is LOGGED (django.db.backends.base), not
    # raised, and the commit itself stands.
    user = _user("rc_robust_log")
    with (
        patch("apps.billing.receipts._send_payment_receipt", side_effect=RuntimeError("boom")),
        caplog.at_level(logging.ERROR, logger="django.db.backends.base"),
    ):
        with transaction.atomic():
            p = _payment(user, invoice="inv_robust_log")
            receipts.queue_payment_receipt(p.pk, "Pro")
    errors = [r for r in caplog.records if r.name == "django.db.backends.base"]
    assert len(errors) == 1
    assert errors[0].exc_info[0] is RuntimeError
    assert BillingPayment.objects.filter(pk=p.pk).exists()


def test_queue_receipt_failure_is_logged_never_raised(caplog):
    # kills x_queue_payment_receipt__mutmut_11-16
    with (
        patch("apps.billing.receipts.transaction.on_commit", side_effect=RuntimeError("no db")),
        caplog.at_level(logging.ERROR, logger="apps.billing.receipts"),
    ):
        receipts.queue_payment_receipt(7, "Pro")
    assert _messages(caplog, "apps.billing.receipts") == ["billing_receipt_queue_failed payment_pk=7"]


# --- receipts.send_downgrade_notice -------------------------------------------------


def test_downgrade_notice_exact_body_without_from_name():
    # kills x_send_downgrade_notice__mutmut_6/7 ("None limits ..." / "XXXX"),
    # _10 (join), _11 (lstrip keeps the trailing space), _12/_13 (blank lines)
    user = _user("dn_body", account_email="dn@example.test")
    receipts.send_downgrade_notice(user, target_name="Pro", when="November 7, 2026")
    assert len(mail.outbox) == 1
    assert mail.outbox[0].body == "\n".join(
        [
            "Your mailsocket plan changed to Pro now.",
            "",
            "No charge now, and nothing is refunded for the rest of this period. "
            "Your new Pro price applies from November 7, 2026.",
            "",
            receipts.SUPPORT_LINE,
        ]
    )


def test_downgrade_notice_exact_mail_with_from_name():
    # audit 2026-10-11 #6, kills x_send_downgrade_notice__mutmut_5 (`if
    # from_name and False`: the "<old plan> limits ended immediately." promise
    # silently dropped) and _14 (subject None -> empty subject). This sentence
    # is what we tell a paying customer about losing their old limits NOW.
    user = _user("dn_from", account_email="dn-from@example.test")
    receipts.send_downgrade_notice(user, target_name="Pro", when="November 7, 2026", from_name="Business")
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    assert msg.to == ["dn-from@example.test"]
    assert msg.from_email == settings.DEFAULT_FROM_EMAIL
    assert msg.subject == "mailsocket: your plan changed to Pro"
    assert msg.body == "\n".join(
        [
            "Your mailsocket plan changed to Pro now. Business limits ended immediately.",
            "",
            "No charge now, and nothing is refunded for the rest of this period. "
            "Your new Pro price applies from November 7, 2026.",
            "",
            receipts.SUPPORT_LINE,
        ]
    )


def test_downgrade_notice_without_recipient_sends_nothing():
    user = _user("dn_norcpt", account=False)  # no Account, empty user email
    receipts.send_downgrade_notice(user, target_name="Pro", when="soon", from_name="Business")
    assert mail.outbox == []


def test_downgrade_notice_failure_logged_with_user_pk(caplog):
    # kills x_send_downgrade_notice__mutmut_20-28/31/32
    user = _user("dn_fail")
    with (
        patch("apps.billing.receipts.send_mail", side_effect=OSError("smtp down")),
        caplog.at_level(logging.ERROR, logger="apps.billing.receipts"),
    ):
        receipts.send_downgrade_notice(user, target_name="Pro", when="soon")
    assert _messages(caplog, "apps.billing.receipts") == [
        f"billing_downgrade_notice_failed user_pk={user.pk}"
    ]


def test_downgrade_notice_never_raises_even_for_an_object_without_pk(caplog):
    # kills x_send_downgrade_notice__mutmut_29/30 (getattr without a default
    # raises inside the except block: "never raises" broken)
    with caplog.at_level(logging.ERROR, logger="apps.billing.receipts"):
        receipts.send_downgrade_notice(object(), target_name="Pro", when="soon")
    assert _messages(caplog, "apps.billing.receipts") == ["billing_downgrade_notice_failed user_pk=None"]


def test_alerts_clean_and_plan_name():
    # kills alerts.x__clean__mutmut_5/7 and x__plan_name__mutmut_2/4/5/6
    from apps.billing import alerts

    assert alerts._clean(None) == ""
    assert alerts._plan_name("") == "-"
    assert alerts._plan_name(None) == "-"
    assert alerts._plan_name("paid") == "Pro (paid)"
    assert alerts._plan_name("legacy") == "legacy (legacy)"  # unknown key: shown as-is


@override_settings(DASHBOARD_HOST="ops.example.test")
def test_alerts_admin_url_uses_dashboard_host():
    # kills x__admin_url__mutmut_9/10/16/17
    from apps.billing import alerts

    assert alerts._admin_url(5) == "https://ops.example.test/admin/accounts/accountplan/5/change/"


@override_settings(DASHBOARD_HOST="")
def test_alerts_admin_url_default_host():
    # kills x__admin_url__mutmut_19/20
    from apps.billing import alerts

    assert alerts._admin_url(5) == "https://dash.mailsocket.app/admin/accounts/accountplan/5/change/"


def test_alerts_admin_url_when_dashboard_host_setting_absent(settings):
    # kills x__admin_url__mutmut_15 (getattr without default raises) and _18
    from apps.billing import alerts

    del settings.DASHBOARD_HOST
    assert alerts._admin_url(5) == "https://dash.mailsocket.app/admin/accounts/accountplan/5/change/"


def test_alerts_account_email_resolution():
    # kills x__account_email__mutmut_4/5 ("(unknown)"), _6/_7/_10/_11
    # (Account email ignored), _9/_12/_13 (fallbacks); _15 (select_related
    # dropped) is pinned by test_billing_alert_recipients_query_budget
    from apps.billing import alerts

    assert alerts._account_email(987654321) == "(unknown)"
    u = _user("ae_acct", email="user-field@example.test", account_email="acct@example.test")
    assert alerts._account_email(u.pk) == "acct@example.test"
    no_acct = _user("ae_noacct", email="plain@example.test", account=False)
    assert alerts._account_email(no_acct.pk) == "plain@example.test"
    bare = _user("ae_bare", email="", account=False)
    assert alerts._account_email(bare.pk) == "ae_bare"
    owner = _user("ae_owner", email="human@example.test", account_email="human@example.test")
    ws = _user("ae_ws", email="", account_email="ws-9@workspace.invalid")
    AccountOwnership.objects.create(owner_user=owner, account_user=ws, name="ws")
    assert alerts._account_email(ws.pk) == "ws-9@workspace.invalid (workspace of human@example.test)"


def test_billing_alert_recipients_query_budget(django_assert_max_num_queries):
    "Public API."
    from apps.billing import alerts

    owner = _user("qb_owner", email="human@example.test", account_email="human@example.test")
    ws = _user("qb_ws", email="", account_email="ws-7@workspace.invalid")
    AccountOwnership.objects.create(owner_user=owner, account_user=ws, name="ws")
    with django_assert_max_num_queries(2):
        assert receipts.customer_email(ws.pk) == "human@example.test"
    with django_assert_max_num_queries(3):
        assert alerts._account_email(ws.pk) == "ws-7@workspace.invalid (workspace of human@example.test)"


@override_settings(DASHBOARD_HOST="")
def test_event_alert_exact_email_and_telegram_bodies():
    # kills x__event_alert__mutmut_16 (empty Amount line), _20/_33 (joins),
    # _26-_30 (subscription "-" fallback, blank line, closing sentence)
    from apps.billing import alerts

    u = _user("ev_body", account_email="ev@example.test")
    with _notify_calls() as notify:
        alerts._event_alert("subscription_cancelled", u.pk, "paid", "paid", "active", "", amount=None)
    admin = f"https://dash.mailsocket.app/admin/accounts/accountplan/{u.pk}/change/"
    notify.assert_called_once_with(
        "money",
        "Billing event subscription_cancelled: Pro (paid)",
        "Event: subscription_cancelled\nPlan: Pro (paid) -> Pro (paid)\nStatus after: active",
        link=admin,
        email_title="Billing event subscription_cancelled: Pro (paid) — ev@example.test",
        email_body="\n".join(
            [
                "Event: subscription_cancelled",
                "Account: ev@example.test",
                f"User pk: {u.pk}",
                "Plan: Pro (paid) -> Pro (paid)",
                "Status after: active",
                "LS subscription id: -",
                f"Admin: {admin}",
                "",
                "Check the account got the expected plan.",
            ]
        ),
    )


def test_event_alert_failure_logged(caplog):
    # kills x__event_alert__mutmut_52-59
    from apps.billing import alerts

    with _notify_calls() as notify, caplog.at_level(logging.ERROR, logger="apps.billing.alerts"):
        notify.side_effect = RuntimeError("boom")
        alerts._event_alert("subscription_created", 41, None, "paid", "active", "sub_1")
    assert _messages(caplog, "apps.billing.alerts") == [
        "billing_alert_send_failed event=subscription_created user_pk=41"
    ]


def test_problem_alert_exact_bodies_dash_for_empty_values():
    # kills x__problem_alert__mutmut_6/8/9 ("-" for None/""), _32/_35 (joins)
    from apps.billing import alerts

    with _notify_calls() as notify:
        alerts._problem_alert(
            "missing user_pk",
            "subscription_created",
            [
                ("LS webhook id", None),
                ("LS subscription id", ""),
                ("LS customer id", 0),
                ("LS customer email", "c@example.test"),
            ],
            footer=("", "F1"),
        )
    notify.assert_called_once_with(
        "warn",
        "Billing webhook problem: missing user_pk",
        "Problem: missing user_pk\nEvent: subscription_created\nLS webhook id: -\n"
        "LS subscription id: -\nLS customer id: 0\n\nF1",
        email_body="Problem: missing user_pk\nEvent: subscription_created\nLS webhook id: -\n"
        "LS subscription id: -\nLS customer id: 0\nLS customer email: c@example.test\n\nF1",
    )


def test_problem_alert_failure_logged(caplog):
    # kills x__problem_alert__mutmut_37-44
    from apps.billing import alerts

    with _notify_calls() as notify, caplog.at_level(logging.ERROR, logger="apps.billing.alerts"):
        notify.side_effect = RuntimeError("boom")
        alerts._problem_alert("no plan for user", "subscription_updated", [])
    assert _messages(caplog, "apps.billing.alerts") == [
        "billing_alert_send_failed problem=no plan for user event=subscription_updated"
    ]


@override_settings(DASHBOARD_HOST="")
def test_refund_alert_exact_call():
    # kills x__refund_alert__mutmut_3/4 (admin link), _9/_11/_12 ("-"),
    # _14-_17 (customer email kept OUT of Telegram), _23-_28/_32/_34-_36/
    # _38/_42 (level, title, bodies, link, email body)
    from apps.billing import alerts

    u = _user("rf_call", account_email="rf@example.test")
    admin = f"https://dash.mailsocket.app/admin/accounts/accountplan/{u.pk}/change/"
    with _notify_calls() as notify:
        alerts._refund_alert(
            "Full refund: $12.00 of $12.00 (invoice i1)",
            "subscription_payment_refunded",
            u.pk,
            [
                ("LS invoice id", "i1"),
                ("LS webhook id", None),
                ("Note", ""),
                ("LS Customer EMAIL", "c@x.test"),
            ],
            ("", "Footer"),
        )
    notify.assert_called_once_with(
        "money",
        "Full refund: $12.00 of $12.00 (invoice i1)",
        "Event: subscription_payment_refunded\nLS invoice id: i1\nLS webhook id: -\nNote: -\n\nFooter",
        link=admin,
        email_title="Full refund: $12.00 of $12.00 (invoice i1) — rf@example.test",
        email_body="\n".join(
            [
                "Event: subscription_payment_refunded",
                "Account: rf@example.test",
                "LS invoice id: i1",
                "LS webhook id: -",
                "Note: -",
                "LS Customer EMAIL: c@x.test",
                f"Admin: {admin}",
                "",
                "Footer",
            ]
        ),
    )


def test_refund_alert_failure_logged(caplog):
    # kills x__refund_alert__mutmut_43-50
    from apps.billing import alerts

    with _notify_calls() as notify, caplog.at_level(logging.ERROR, logger="apps.billing.alerts"):
        notify.side_effect = RuntimeError("boom")
        alerts._refund_alert("t", "order_refunded", 42, [], ())
    assert _messages(caplog, "apps.billing.alerts") == [
        "billing_alert_send_failed event=order_refunded user_pk=42"
    ]


@pytest.mark.django_db(transaction=True)
@override_settings(OPS_ALERT_EMAIL=OPS)
@pytest.mark.parametrize(
    ("target", "queue"),
    [
        (
            "_event_alert",
            lambda a: a.queue_event_alert(
                "subscription_created",
                user_pk=1,
                plan_before=None,
                plan_after="paid",
                status_after="active",
                subscription_id="s",
            ),
        ),
        ("_problem_alert", lambda a: a.queue_problem_alert("r", "subscription_created")),
        (
            "_refund_alert",
            lambda a: a.queue_refund_alert(
                title="t", details=(), footer=(), event_name="order_refunded", user_pk=1
            ),
        ),
    ],
)
def test_queued_alert_hooks_are_robust(target, queue):
    # kills x_queue_event_alert__mutmut_5/7/23, x_queue_problem_alert__mutmut_6/8/18,
    # x_queue_refund_alert__mutmut_10/12/24 (robust=True dropped): a raising
    # alert callback must not escape the webhook commit or skip later hooks.
    from apps.billing import alerts

    later = []
    with patch(f"apps.billing.alerts.{target}", side_effect=RuntimeError("boom")) as cb:
        with transaction.atomic():
            queue(alerts)
            transaction.on_commit(lambda: later.append("ran"))
    assert cb.call_count == 1
    assert later == ["ran"]


@override_settings(OPS_ALERT_EMAIL=OPS)
def test_queue_refund_alert_passes_event_name(django_capture_on_commit_callbacks):
    # kills x_queue_refund_alert__mutmut_15 (event name dropped from the callback)
    from apps.billing import alerts

    with patch("apps.billing.alerts._refund_alert") as cb, django_capture_on_commit_callbacks(execute=True):
        alerts.queue_refund_alert(
            title="t", details=[("a", " b ")], footer=["f"], event_name="order_refunded", user_pk=3
        )
    cb.assert_called_once_with("t", "order_refunded", 3, [("a", "b")], ("f",))


@override_settings(OPS_ALERT_EMAIL=OPS)
@pytest.mark.parametrize(
    ("queue", "expected"),
    [
        (
            lambda a: a.queue_event_alert(
                "subscription_created",
                user_pk=1,
                plan_before=None,
                plan_after="paid",
                status_after="active",
                subscription_id="s",
            ),
            "billing_alert_queue_failed event=subscription_created",
        ),
        (
            lambda a: a.queue_problem_alert("bad thing", "subscription_updated"),
            "billing_alert_queue_failed problem=bad thing event=subscription_updated",
        ),
        (
            lambda a: a.queue_refund_alert(
                title="t", details=(), footer=(), event_name="order_refunded", user_pk=1
            ),
            "billing_alert_queue_failed event=order_refunded",
        ),
    ],
)
def test_queue_alert_failures_logged_never_raised(caplog, queue, expected):
    # kills x_queue_event_alert__mutmut_24-29, x_queue_problem_alert__mutmut_19-26,
    # x_queue_refund_alert__mutmut_25-30
    from apps.billing import alerts

    with (
        patch("apps.billing.alerts.transaction.on_commit", side_effect=RuntimeError("no db")),
        caplog.at_level(logging.ERROR, logger="apps.billing.alerts"),
    ):
        queue(alerts)
    assert _messages(caplog, "apps.billing.alerts") == [expected]
