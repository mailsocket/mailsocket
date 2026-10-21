"Public API."

from datetime import timedelta
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.api.tests.test_auth_fail_closed import make_user
from apps.domains.models import Domain
from apps.domains.services import (
    DOMAIN_PENDING_CLAIM_DAYS,
    DomainCreateRefused,
    create_domain,
    release_expired_pending_claims,
)
from apps.inboxes.models import Inbox

pytestmark = pytest.mark.django_db(transaction=True)


def _age(domain, days):
    Domain.objects.filter(pk=domain.pk).update(created_at=timezone.now() - timedelta(days=days))


def test_old_pending_claim_is_released_and_name_can_be_added_by_owner():
    squatter, owner = make_user("f34_squat"), make_user("f34_owner")
    claim = create_domain(squatter, "brand.example.com")
    _age(claim, DOMAIN_PENDING_CLAIM_DAYS + 1)
    domain = create_domain(owner, "brand.example.com")
    assert domain.user_id == owner.pk
    assert not Domain.objects.filter(pk=claim.pk).exists()


def test_young_pending_claim_still_blocks():
    squatter, owner = make_user("f34_young_s"), make_user("f34_young_o")
    claim = create_domain(squatter, "young.example.com")
    _age(claim, DOMAIN_PENDING_CLAIM_DAYS - 1)
    with pytest.raises(DomainCreateRefused):
        create_domain(owner, "young.example.com")
    assert Domain.objects.filter(pk=claim.pk).exists()


@pytest.mark.parametrize("status", [Domain.Status.VERIFIED, Domain.Status.FAILED, Domain.Status.DISABLED])
def test_non_pending_rows_are_never_released(status):
    user = make_user(f"f34_keep_{status}")
    domain = create_domain(user, f"{status}.keep.example.com")
    Domain.objects.filter(pk=domain.pk).update(status=status)
    _age(domain, 365)
    assert release_expired_pending_claims() == 0
    assert Domain.objects.filter(pk=domain.pk).exists()


def test_old_pending_claim_with_bound_inbox_is_kept():
    user = make_user("f34_bound")
    domain = create_domain(user, "bound.example.com")
    Inbox.objects.create(user=user, routing_key="bound_box", domain=domain)
    _age(domain, 365)
    assert release_expired_pending_claims() == 0
    assert Domain.objects.filter(pk=domain.pk).exists()


def test_verify_domains_command_releases_expired_claims():
    user = make_user("f34_cmd")
    old = create_domain(user, "old.example.com")
    fresh = create_domain(user, "fresh.example.com")
    _age(old, DOMAIN_PENDING_CLAIM_DAYS + 3)
    out = StringIO()
    with mock.patch("apps.domains.management.commands.verify_domains.verify_domain"):
        call_command("verify_domains", stdout=out)
    assert not Domain.objects.filter(pk=old.pk).exists()
    assert Domain.objects.filter(pk=fresh.pk).exists()
    assert "released 1 expired pending claim" in out.getvalue()


def test_sweep_verifies_before_releasing(monkeypatch):
    "Public API."
    from django.core.management import call_command

    from apps.domains import services
    from apps.domains.management.commands import verify_domains as cmd

    domain = create_domain(make_user("g6_verify_first"), "now-correct.example.com")
    _age(domain, DOMAIN_PENDING_CLAIM_DAYS + 1)
    calls = []

    def fake_verify(d):
        calls.append(d.pk)
        Domain.objects.filter(pk=d.pk).update(status=Domain.Status.VERIFIED, verified_at=timezone.now())

    monkeypatch.setattr(cmd, "verify_domain", fake_verify)
    monkeypatch.setattr(cmd, "recheck_domain", lambda d: None)
    call_command("verify_domains")
    domain.refresh_from_db()
    assert domain.status == Domain.Status.VERIFIED
    assert calls == [domain.pk]
    assert services.release_expired_pending_claims() == 0


def test_create_domain_does_not_release_an_expired_claim_whose_dns_is_now_valid(monkeypatch):
    "Public API."
    from apps.domains import services

    squatter, other = make_user("g6_dns_ok_a"), make_user("g6_dns_ok_b")
    claim = create_domain(squatter, "dns-ok.example.com")
    _age(claim, DOMAIN_PENDING_CLAIM_DAYS + 1)

    def fake_verify(d, **_kw):
        Domain.objects.filter(pk=d.pk).update(status=Domain.Status.VERIFIED, verified_at=timezone.now())

    monkeypatch.setattr(services, "verify_domain", fake_verify)
    with pytest.raises(DomainCreateRefused):
        create_domain(other, "dns-ok.example.com")
    claim.refresh_from_db()
    assert claim.user_id == squatter.pk
    assert claim.status == Domain.Status.VERIFIED


def test_create_domain_still_releases_an_expired_claim_whose_dns_is_wrong(monkeypatch):
    from apps.domains import services

    squatter, owner = make_user("g6_dns_bad_a"), make_user("g6_dns_bad_b")
    claim = create_domain(squatter, "dns-bad.example.com")
    _age(claim, DOMAIN_PENDING_CLAIM_DAYS + 1)
    monkeypatch.setattr(services, "verify_domain", lambda d, **_kw: d)
    domain = create_domain(owner, "dns-bad.example.com")
    assert domain.user_id == owner.pk
    assert not Domain.objects.filter(pk=claim.pk).exists()


def test_create_domain_keeps_an_expired_claim_when_the_dns_check_errors(monkeypatch):
    "Public API."
    from apps.domains import services

    squatter, other = make_user("g6_dns_err_a"), make_user("g6_dns_err_b")
    claim = create_domain(squatter, "dns-err.example.com")
    _age(claim, DOMAIN_PENDING_CLAIM_DAYS + 1)

    def boom(d, **_kw):
        raise TimeoutError("dns timeout")

    monkeypatch.setattr(services, "verify_domain", boom)
    with pytest.raises(DomainCreateRefused):
        create_domain(other, "dns-err.example.com")
    assert Domain.objects.get(pk=claim.pk).user_id == squatter.pk


def test_create_domain_never_releases_a_claim_it_did_not_check(monkeypatch):
    "Public API."
    from apps.domains import services

    squatter, other = make_user("g6_race_a"), make_user("g6_race_b")
    claim = create_domain(squatter, "race.example.com")
    _age(claim, DOMAIN_PENDING_CLAIM_DAYS - 1)  # not yet expired at check time
    real = services._expired_pending_claims
    calls = {"n": 0}

    def expire_between_check_and_delete():
        calls["n"] += 1
        if calls["n"] == 2:  # second evaluation = inside the delete transaction
            _age(claim, DOMAIN_PENDING_CLAIM_DAYS + 1)
        return real()

    monkeypatch.setattr(services, "_expired_pending_claims", expire_between_check_and_delete)
    monkeypatch.setattr(services, "verify_domain", lambda d, **_kw: d)
    with pytest.raises(DomainCreateRefused):
        create_domain(other, "race.example.com")
    assert Domain.objects.filter(pk=claim.pk, user=squatter).exists()
