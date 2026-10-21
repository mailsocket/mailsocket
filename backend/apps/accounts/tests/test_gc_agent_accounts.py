"Public API."
from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan, ApiKey
from apps.accounts.services import AGENT_ACCOUNT_DOMAIN, register_agent
from apps.inboxes.models import Inbox

pytestmark = pytest.mark.django_db


def _run(**options):
    out = StringIO()
    call_command("gc_agent_accounts", stdout=out, **options)
    return out.getvalue()


def _make_real_account(email="real_user@example.test"):
    User = get_user_model()
    user = User.objects.create_user(username="real_user", email=email, password="password")
    Account.objects.create(user=user, email_normalized=email, email_verified_at=timezone.now())
    AccountPlan.objects.create(user=user, plan=AccountPlan.Plan.FREE)
    return user


def test_gc_deletes_idle_agent_account():
    user, api_key, _ = register_agent("idle-bot")
    inbox = Inbox.objects.filter(user=user).first()
    User = get_user_model()
    # Backdate creation and last_used_at well past the idle window.
    User.objects.filter(pk=user.pk).update(date_joined=timezone.now() - timedelta(days=30))
    ApiKey.objects.filter(pk=api_key.pk).update(
        last_used_at=timezone.now() - timedelta(days=30)
    )

    out = _run(idle_days=7)

    assert "deleted_accounts=1" in out
    assert not User.objects.filter(pk=user.pk).exists()
    assert not Account.objects.filter(user_id=user.pk).exists()
    assert not AccountPlan.objects.filter(user_id=user.pk).exists()
    assert not ApiKey.objects.filter(pk=api_key.pk).exists()
    if inbox is not None:
        assert not Inbox.objects.filter(pk=inbox.pk).exists()


def test_gc_keeps_recently_used_agent_account():
    user, api_key, _ = register_agent("active-bot")
    ApiKey.objects.filter(pk=api_key.pk).update(last_used_at=timezone.now())

    out = _run(idle_days=7)

    assert "deleted_accounts=0" in out
    User = get_user_model()
    assert User.objects.filter(pk=user.pk).exists()


def test_gc_keeps_freshly_registered_never_used_agent_account():
    "Public API."
    user, api_key, _ = register_agent("brand-new-bot")
    assert api_key.last_used_at is None

    out = _run(idle_days=7)

    assert "deleted_accounts=0" in out
    User = get_user_model()
    assert User.objects.filter(pk=user.pk).exists()


def test_gc_never_touches_a_real_non_agent_domain_account():
    real_user = _make_real_account()
    User = get_user_model()
    # Make the real account look maximally idle: old join date, no api key use.
    User.objects.filter(pk=real_user.pk).update(date_joined=timezone.now() - timedelta(days=365))

    out = _run(idle_days=1)

    assert "deleted_accounts=0" in out
    assert User.objects.filter(pk=real_user.pk).exists()
    assert AGENT_ACCOUNT_DOMAIN not in real_user.email


def test_gc_dry_run_deletes_nothing():
    user, api_key, _ = register_agent("dry-run-bot")
    User = get_user_model()
    User.objects.filter(pk=user.pk).update(date_joined=timezone.now() - timedelta(days=30))
    ApiKey.objects.filter(pk=api_key.pk).update(
        last_used_at=timezone.now() - timedelta(days=30)
    )

    out = _run(idle_days=7, dry_run=True)

    assert "DRY-RUN" in out
    assert User.objects.filter(pk=user.pk).exists()
