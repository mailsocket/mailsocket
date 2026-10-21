"""e2e_cleanup / e2e_seed management commands (e2e journey suite support).

e2e_cleanup: candidate matching (pattern, 48h window, QA-account ownership),
--qa-account mandatory outside testing/e2e settings, --prefix forbidden in
scoped mode, refusal when the QA account itself would match, fail-closed
refusals (subscription id, non-Free plan, BillingPayment, staff, team rows,
workspace ownership), the locked re-check uses fresh rows, dry-run default,
full FK erase order in one transaction, and that unrelated accounts are never
touched.
e2e_seed: refuses outside testing/e2e settings, refuses a mail dir that is
not e2e/.mail, and clears rate-limit buckets.
"""
import hashlib
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.accounts.management.commands import e2e_cleanup
from apps.accounts.models import (
    Account,
    AccountOwnership,
    AccountPlan,
    ApiKey,
    MonthlyUsage,
    RateLimitBucket,
    TeamInvite,
    TeamMembership,
)
from apps.billing.models import BillingEvent, BillingPayment
from apps.domains.models import Domain
from apps.inboxes.models import Inbox, Message
from apps.outbound.models import ApprovedRecipient, OutboundEmail
from apps.webhooks.models import Webhook, WebhookDelivery

pytestmark = pytest.mark.django_db

MAIL_DOMAIN = settings.MAIL_DOMAIN


def make_account(email, *, created_ago=timedelta(hours=1), sub_id=None, staff=False, plan=AccountPlan.Plan.FREE):
    user = get_user_model().objects.create_user(
        username="u_" + hashlib.sha1(email.encode()).hexdigest()[:12], email=email, password=None
    )
    user.is_staff = staff
    user.save(update_fields=["is_staff"])
    account = Account.objects.create(user=user, email_normalized=email, email_verified_at=timezone.now())
    Account.objects.filter(pk=account.pk).update(created_at=timezone.now() - created_ago)
    AccountPlan.objects.create(user=user, plan=plan, stripe_subscription_id=sub_id,
                               stripe_customer_id="cust" if sub_id else None)
    return user


def make_message(inbox, marker="m"):
    raw = f"Subject: {marker}\r\n\r\nbody".encode()
    digest = hashlib.sha256(raw + inbox.routing_key.encode()).hexdigest()
    return Message.objects.create(
        inbox=inbox, dedupe_key=digest, envelope_from="s@example.test", envelope_recipient=inbox.address,
        header_from="s@example.test", header_to=[inbox.address], header_cc=[], subject=marker,
        text_body="t", raw_bytes=raw, raw_sha256=digest, size_bytes=len(raw),
        ingest_status=Message.IngestStatus.PARSED,
    )


def full_account(email):
    """An e2e signup with one row of every type the erase order covers."""
    user = make_account(email)
    inbox = Inbox.objects.create(user=user, routing_key="r" + hashlib.sha1(email.encode()).hexdigest()[:10])
    message = make_message(inbox)
    domain = Domain.objects.create(user=user, name=f"{user.pk}.example.com", status=Domain.Status.VERIFIED,
                                   catch_all=True, catch_all_inbox=inbox)
    webhook = Webhook(inbox=inbox, url="https://example.test/hook")
    webhook.save(_url_validated=True)
    WebhookDelivery.objects.create(webhook=webhook, message=message, payload={}, event_id=f"evt_{user.pk}")
    OutboundEmail.objects.create(user=user, from_address=f"a@{domain.name}", to=["x@example.test"])
    ApprovedRecipient.objects.create(user=user, email_normalized=f"ok{user.pk}@example.test")
    ApiKey.objects.create(user=user, name="k", prefix=f"p{user.pk}", key_hash=f"h{user.pk}")
    TeamInvite.objects.create(account_id=user.pk, email_normalized=f"inv{user.pk}@example.test",
                              token_hash=f"t{user.pk}", invited_by=user)
    MonthlyUsage.objects.create(user=user, period_start=timezone.now().date().replace(day=1))
    return user


def run(*args):
    out = StringIO()
    call_command("e2e_cleanup", *args, stdout=out)
    return out.getvalue()


def test_dry_run_is_default_and_deletes_nothing():
    user = full_account(f"e2e-dry@{MAIL_DOMAIN}")
    out = run()
    assert "DRY-RUN" in out and "candidates=1" in out and "nothing deleted" in out
    assert get_user_model().objects.filter(pk=user.pk).exists()
    assert Inbox.objects.filter(user=user).exists()


def test_apply_erases_every_row_in_fk_order_and_keeps_set_null_audit_rows():
    user = full_account(f"e2e-full@{MAIL_DOMAIN}")
    event = BillingEvent.objects.create(event_key="evt_e2e_full", event_name="x", user=user)
    pk = user.pk
    out = run("--apply")
    assert "deleted 1 account(s)" in out
    assert not get_user_model().objects.filter(pk=pk).exists()
    for model, filt in [
        (Account, {"pk": pk}), (AccountPlan, {"user_id": pk}), (ApiKey, {"user_id": pk}),
        (Inbox, {"user_id": pk}), (Message, {"inbox__user_id": pk}), (Domain, {"user_id": pk}),
        (Webhook, {"inbox__user_id": pk}), (WebhookDelivery, {"webhook__inbox__user_id": pk}),
        (OutboundEmail, {"user_id": pk}), (ApprovedRecipient, {"user_id": pk}),
        (TeamInvite, {"account_id": pk}), (MonthlyUsage, {"user_id": pk}),
    ]:
        assert not model.objects.filter(**filt).exists(), model.__name__
    event.refresh_from_db()
    assert event.user_id is None
    # Printed steps appear in the contract order.
    positions = [out.index(f"    {step}:") for step in e2e_cleanup.ERASE_STEPS]
    assert positions == sorted(positions)


def test_erase_user_runs_steps_in_exact_contract_order():
    user = full_account(f"e2e-order@{MAIL_DOMAIN}")
    from django.db import transaction

    with transaction.atomic():
        done = e2e_cleanup.erase_user(user.pk, lambda line: None)
    assert [name for name, _ in done] == list(e2e_cleanup.ERASE_STEPS)
    assert done[0] == ("domain_catch_all_unset", 1)  # cycle broken first
    assert dict(done)["user"] >= 1


def test_unrelated_accounts_are_never_candidates():
    keep = [
        make_account(f"alice@{MAIL_DOMAIN}"),                      # no prefix
        make_account("e2e-x@example.com"),                         # other domain
        make_account(f"e2e-old@{MAIL_DOMAIN}", created_ago=timedelta(hours=49)),  # too old
        make_account(f"xe2e-y@{MAIL_DOMAIN}"),                     # prefix not at start
    ]
    target = make_account(f"e2e-new@{MAIL_DOMAIN}")
    out = run("--apply")
    assert "candidates=1" in out
    assert not get_user_model().objects.filter(pk=target.pk).exists()
    for user in keep:
        assert get_user_model().objects.filter(pk=user.pk).exists()


@pytest.mark.parametrize("case", ["subscription", "non_free_plan", "payment", "staff", "team_member_live",
                                  "member_elsewhere", "workspace"])
def test_refusals_fail_closed_and_delete_nothing(case):
    innocent = make_account(f"e2e-innocent@{MAIL_DOMAIN}")
    bad = make_account(f"e2e-bad@{MAIL_DOMAIN}", sub_id="sub_123" if case == "subscription" else None,
                       staff=case == "staff",
                       plan=AccountPlan.Plan.PAID if case == "non_free_plan" else AccountPlan.Plan.FREE)
    if case == "payment":
        BillingPayment.objects.create(user=bad, ls_invoice_id="inv_1", amount_cents=1200)
    elif case == "team_member_live":
        member = make_account("member@example.com")
        TeamMembership.objects.create(account_id=bad.pk, user=member)
    elif case == "member_elsewhere":
        owner = make_account("owner@example.com")
        TeamMembership.objects.create(account_id=owner.pk, user=bad)
    elif case == "workspace":
        ws = make_account("ws@example.com")
        AccountOwnership.objects.create(owner_user=bad, account_user=ws)
    with pytest.raises(CommandError, match="nothing deleted"):
        run("--apply")
    assert get_user_model().objects.filter(pk=bad.pk).exists()
    # Fail-closed for the WHOLE run: the plain throwaway is kept too.
    assert get_user_model().objects.filter(pk=innocent.pk).exists()


def test_refusal_is_reported_in_dry_run_too():
    make_account(f"e2e-sub@{MAIL_DOMAIN}", sub_id="sub_9")
    with pytest.raises(CommandError):
        run()


def test_qa_account_scoping_only_matches_its_inboxes():
    qa = make_account("qa-owner@example.com")
    Inbox.objects.create(user=qa, routing_key="e2e-run1")
    deleted_inbox = Inbox.objects.create(user=qa, routing_key="e2e-run2")
    Inbox.objects.filter(pk=deleted_inbox.pk).update(deleted_at=timezone.now(), is_enabled=False)
    ours1 = make_account(f"e2e-run1@{MAIL_DOMAIN}")
    ours2 = make_account(f"e2e-run2@{MAIL_DOMAIN}")
    stranger = make_account(f"e2e-stranger@{MAIL_DOMAIN}")
    out = run("--apply", "--qa-account", "qa-owner@example.com")
    assert "skip e2e-stranger" in out
    assert not get_user_model().objects.filter(pk__in=[ours1.pk, ours2.pk]).exists()
    assert get_user_model().objects.filter(pk=stranger.pk).exists()
    assert get_user_model().objects.filter(pk=qa.pk).exists()


def test_non_free_plan_without_subscription_is_refused():
    """P2-1: an admin-comped / manually upgraded plan (no subscription id, no
    payment row) is a real customer signal and is refused."""
    comped = make_account(f"e2e-comped@{MAIL_DOMAIN}", plan=AccountPlan.Plan.BUSINESS)
    with pytest.raises(CommandError, match="nothing deleted"):
        run("--apply")
    assert get_user_model().objects.filter(pk=comped.pk).exists()


@pytest.mark.parametrize("module", ["config.settings.production", "config.settings.development", ""])
def test_qa_account_is_mandatory_outside_testing_and_e2e_settings(monkeypatch, module):
    "Public API."
    target = make_account(f"e2e-unscoped@{MAIL_DOMAIN}")
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", module)
    if not module:
        monkeypatch.setattr(e2e_cleanup, "settings_modules", lambda: set())
    for args in ((), ("--apply",)):
        with pytest.raises(CommandError, match="--qa-account is required"):
            run(*args)
    assert get_user_model().objects.filter(pk=target.pk).exists()


def test_scoped_mode_works_under_production_settings(monkeypatch):
    qa = make_account("qa-prod@example.com")
    Inbox.objects.create(user=qa, routing_key="e2e-prodrun")
    ours = make_account(f"e2e-prodrun@{MAIL_DOMAIN}")
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.production")
    out = run("--apply", "--qa-account", "qa-prod@example.com")
    assert "deleted 1 account(s)" in out
    assert not get_user_model().objects.filter(pk=ours.pk).exists()


def test_customer_with_own_e2e_inbox_is_never_a_scoped_candidate():
    "Public API."
    qa = make_account("qa-owner@example.com")
    Inbox.objects.create(user=qa, routing_key="e2e-run1")
    customer = make_account(f"e2e-signup@{MAIL_DOMAIN}")
    Inbox.objects.create(user=customer, routing_key="e2e-signup")  # their own inbox
    out = run("--apply", "--qa-account", "qa-owner@example.com")
    assert "candidates=0" in out
    assert "skip e2e-signup@" in out
    assert get_user_model().objects.filter(pk=customer.pk).exists()
    assert Inbox.objects.filter(user=customer).exists()


def test_prefix_is_forbidden_in_scoped_mode():
    make_account("qa-owner@example.com")
    with pytest.raises(CommandError, match="--prefix is not allowed"):
        run("--qa-account", "qa-owner@example.com", "--prefix", "e2e-")
    with pytest.raises(CommandError, match="--prefix is not allowed"):
        run("--qa-account", "qa-owner@example.com", "--prefix", "qa-")


@pytest.mark.parametrize("how", ["matches_pattern", "owns_its_own_address"])
def test_qa_account_that_is_itself_a_candidate_is_refused(how):
    if how == "matches_pattern":
        qa = make_account(f"e2e-qa@{MAIL_DOMAIN}")
        Inbox.objects.create(user=qa, routing_key="e2e-qa")
        qa_email = f"e2e-qa@{MAIL_DOMAIN}"
    else:
        qa = make_account(f"qa-self@{MAIL_DOMAIN}")
        Inbox.objects.create(user=qa, routing_key="qa-self")
        qa_email = f"qa-self@{MAIL_DOMAIN}"
    with pytest.raises(CommandError, match="itself looks like an e2e throwaway"):
        run("--apply", "--qa-account", qa_email)
    assert get_user_model().objects.filter(pk=qa.pk).exists()


def test_locked_recheck_uses_a_fresh_user_row():
    """P2-3: the re-check under the lock must see a staff flag set after the
    candidate scan (the stale select_related User would miss it)."""
    target = make_account(f"e2e-race@{MAIL_DOMAIN}")
    real = e2e_cleanup.refusal_reason
    calls = []

    def racing(user):
        calls.append(user.pk)
        result = real(user)
        if len(calls) == 1:  # after the pre-check, before the locked re-check
            get_user_model().objects.filter(pk=user.pk).update(is_staff=True)
        return result

    with patch.object(e2e_cleanup, "refusal_reason", side_effect=racing):
        with pytest.raises(CommandError, match="became non-deletable \\(staff/superuser\\)"):
            run("--apply")
    assert len(calls) == 2
    assert get_user_model().objects.filter(pk=target.pk).exists()


def test_unknown_qa_account_and_bad_args_are_refused():
    with pytest.raises(CommandError, match="not found"):
        run("--qa-account", "nobody@example.com")
    with pytest.raises(CommandError, match="--hours"):
        run("--hours", "72")
    with pytest.raises(CommandError, match="--prefix"):
        run("--prefix", "%")


def test_failure_mid_erase_rolls_back_everything():
    first = make_account(f"e2e-a@{MAIL_DOMAIN}")
    second = make_account(f"e2e-b@{MAIL_DOMAIN}")
    real = e2e_cleanup.erase_user
    calls = []

    def flaky(pk, log):
        calls.append(pk)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(pk, log)

    with patch.object(e2e_cleanup, "erase_user", side_effect=flaky):
        with pytest.raises(RuntimeError):
            run("--apply")
    assert get_user_model().objects.filter(pk__in=[first.pk, second.pk]).count() == 2


# ------------------------------ e2e_seed --------------------------------------


def test_seed_refuses_non_e2e_settings(monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.production")
    with pytest.raises(CommandError, match="refusing to run under settings"):
        call_command("e2e_seed", stdout=StringIO())


@pytest.mark.parametrize("sub", ["mail", "home", "e2e", "e2e/.mail/.."])
def test_seed_refuses_a_mail_dir_that_is_not_e2e_mail(tmp_path, settings, monkeypatch, sub):
    """P3-4: a mis-set EMAIL_FILE_PATH (e.g. $HOME) is never emptied."""
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.testing")
    target = tmp_path / sub
    target.mkdir(parents=True, exist_ok=True)
    keep = target / "precious.txt"
    keep.write_text("x")
    settings.EMAIL_FILE_PATH = str(target)
    with pytest.raises(CommandError, match="must resolve to a directory ending in e2e/.mail"):
        call_command("e2e_seed", stdout=StringIO())
    assert keep.exists()


def test_seed_clears_buckets_and_mail_dir(tmp_path, settings, monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.testing")
    mail = tmp_path / "e2e" / ".mail"
    settings.EMAIL_FILE_PATH = str(mail)
    mail.mkdir(parents=True)
    (mail / "old.log").write_text("x")
    RateLimitBucket.objects.create(bucket_hash="h" * 64, window_started_at=timezone.now(), count=3,
                                   expires_at=timezone.now() + timedelta(hours=1))
    out = StringIO()
    call_command("e2e_seed", stdout=out)
    assert "mail_files_removed=1" in out.getvalue()
    assert not RateLimitBucket.objects.exists()
    assert list(mail.iterdir()) == []
