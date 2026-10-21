"Public API."
import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan
from apps.domains.models import Domain
from apps.domains.services import (
    MX_TARGET_HOST,
    VERIFY_DEMOTE_STRIKE_COUNT,
    recheck_domain,
    verify_domain,
)

pytestmark = pytest.mark.django_db(transaction=True)


class FakeRecord:
    """Stands in for a dnspython rdata: TXT records expose `.strings`
    (list[bytes]), MX records expose `.exchange`."""

    def __init__(self, *, strings=None, exchange=None):
        self.strings = strings or []
        self.exchange = exchange


class FakeResolver:
    """Injectable stand-in for dns.resolver.Resolver — never hits the
    network. `answers` maps (qname, rdtype) -> list[FakeRecord] | Exception
    class to raise (mirroring dns.exception.DNSException subclasses)."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def resolve(self, qname, rdtype):
        self.calls.append((qname, rdtype))
        result = self.answers.get((qname, rdtype), NXDOMAIN)
        if isinstance(result, type) and issubclass(result, Exception):
            raise result()
        return result


import dns.exception  # noqa: E402  (after FakeResolver so import ordering doesn't matter)

NXDOMAIN = dns.exception.DNSException


def make_user(name):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user,
        email_normalized=f"{name}@example.test",
        email_verified_at=timezone.now(),
    )
    AccountPlan.objects.create(user=user, plan=AccountPlan.Plan.BUSINESS)
    return user


def make_domain(user, name="verify.example.com"):
    return Domain.objects.create(user=user, name=name)


@pytest.fixture(autouse=True)
def _isolate_domain_state():
    yield


def test_verify_domain_advances_pending_to_verified_when_both_records_match():
    user = make_user("verify_a")
    domain = make_domain(user)
    resolver = FakeResolver({
        (domain.verification_txt_host, "TXT"): [FakeRecord(strings=[domain.verification_token.encode()])],
        (domain.name, "MX"): [FakeRecord(exchange=f"{MX_TARGET_HOST}.")],
    })
    result = verify_domain(domain, resolver=resolver)
    assert result.status == Domain.Status.VERIFIED
    assert result.verified_at is not None
    assert result.last_error == ""


def test_verify_domain_stays_pending_when_txt_missing():
    user = make_user("verify_b")
    domain = make_domain(user)
    resolver = FakeResolver({})  # no TXT, no MX
    result = verify_domain(domain, resolver=resolver)
    assert result.status == Domain.Status.PENDING_DNS
    assert "TXT" in result.last_error


def test_verify_domain_stays_pending_when_txt_ok_but_mx_missing():
    user = make_user("verify_c")
    domain = make_domain(user)
    resolver = FakeResolver({
        (domain.verification_txt_host, "TXT"): [FakeRecord(strings=[domain.verification_token.encode()])],
        (domain.name, "MX"): [FakeRecord(exchange="someone-elses-mx.example.net.")],
    })
    result = verify_domain(domain, resolver=resolver)
    assert result.status == Domain.Status.PENDING_DNS
    assert "MX" in result.last_error


def test_verify_domain_manual_recheck_on_verified_domain_reruns_dns_and_stays_verified():
    "Public API."
    user = make_user("verify_d")
    domain = make_domain(user)
    domain.status = Domain.Status.VERIFIED
    domain.verified_at = timezone.now()
    domain.save(update_fields=["status", "verified_at"])

    resolver = FakeResolver({
        (domain.verification_txt_host, "TXT"): [FakeRecord(strings=[domain.verification_token.encode()])],
        (domain.name, "MX"): [FakeRecord(exchange=f"{MX_TARGET_HOST}.")],
    })
    before = domain.last_checked_at
    result = verify_domain(domain, resolver=resolver)
    assert result.status == Domain.Status.VERIFIED
    assert result.last_checked_at != before
    assert resolver.calls  # DNS WAS re-run this time


def test_verify_domain_manual_recheck_failure_on_verified_domain_does_not_demote():
    "Public API."
    user = make_user("verify_d2")
    domain = make_domain(user)
    domain.status = Domain.Status.VERIFIED
    domain.verified_at = timezone.now()
    domain.save(update_fields=["status", "verified_at"])

    resolver = FakeResolver({})  # DNS now fails
    result = verify_domain(domain, resolver=resolver)
    assert result.status == Domain.Status.VERIFIED
    assert "TXT" in result.last_error


def test_verify_domain_is_noop_for_disabled_domain():
    user = make_user("verify_e")
    domain = make_domain(user)
    domain.status = Domain.Status.DISABLED
    domain.disabled_at = timezone.now()
    domain.save(update_fields=["status", "disabled_at"])

    resolver = FakeResolver({})
    result = verify_domain(domain, resolver=resolver)
    assert result.status == Domain.Status.DISABLED
    assert resolver.calls == []


def test_recheck_domain_verified_still_good_stays_verified_and_resets_strikes():
    "Public API."
    user = make_user("verify_f0")
    domain = make_domain(user)
    domain.status = Domain.Status.VERIFIED
    domain.verified_at = timezone.now()
    domain.consecutive_failures = 2  # pretend it had some prior strikes
    domain.save(update_fields=["status", "verified_at", "consecutive_failures"])

    resolver = FakeResolver({
        (domain.verification_txt_host, "TXT"): [FakeRecord(strings=[domain.verification_token.encode()])],
        (domain.name, "MX"): [FakeRecord(exchange=f"{MX_TARGET_HOST}.")],
    })
    before = domain.last_checked_at
    result = recheck_domain(domain, resolver=resolver)
    assert result.status == Domain.Status.VERIFIED
    assert result.last_checked_at != before
    assert result.consecutive_failures == 0


def test_recheck_domain_demotes_to_failed_only_after_n_consecutive_strikes():
    "Public API."
    assert VERIFY_DEMOTE_STRIKE_COUNT == 3, "test assumes the documented N=3"
    user = make_user("verify_f1")
    domain = make_domain(user)
    domain.status = Domain.Status.VERIFIED
    domain.verified_at = timezone.now()
    domain.save(update_fields=["status", "verified_at"])

    failing_resolver = FakeResolver({})  # no TXT, no MX -> every check fails

    domain = recheck_domain(domain, resolver=failing_resolver)
    assert domain.status == Domain.Status.VERIFIED
    assert domain.consecutive_failures == 1

    domain = recheck_domain(domain, resolver=failing_resolver)
    assert domain.status == Domain.Status.VERIFIED
    assert domain.consecutive_failures == 2

    domain = recheck_domain(domain, resolver=failing_resolver)
    assert domain.status == Domain.Status.FAILED
    assert domain.consecutive_failures == 3
    assert domain.last_error


def test_recheck_domain_success_after_failures_resets_strikes_and_stays_verified():
    "Public API."
    user = make_user("verify_f2")
    domain = make_domain(user)
    domain.status = Domain.Status.VERIFIED
    domain.verified_at = timezone.now()
    domain.save(update_fields=["status", "verified_at"])

    failing_resolver = FakeResolver({})
    domain = recheck_domain(domain, resolver=failing_resolver)
    domain = recheck_domain(domain, resolver=failing_resolver)
    assert domain.consecutive_failures == 2
    assert domain.status == Domain.Status.VERIFIED

    good_resolver = FakeResolver({
        (domain.verification_txt_host, "TXT"): [FakeRecord(strings=[domain.verification_token.encode()])],
        (domain.name, "MX"): [FakeRecord(exchange=f"{MX_TARGET_HOST}.")],
    })
    domain = recheck_domain(domain, resolver=good_resolver)
    assert domain.status == Domain.Status.VERIFIED
    assert domain.consecutive_failures == 0


def test_recheck_domain_on_pending_domain_delegates_to_verify_domain():
    """recheck_domain on a not-yet-verified domain must behave like
    verify_domain (never demote a pending domain that was never verified)."""
    user = make_user("verify_f3")
    pending = make_domain(user, name="verify-f-pending.example.com")
    resolver = FakeResolver({})
    for _ in range(VERIFY_DEMOTE_STRIKE_COUNT + 2):
        pending = recheck_domain(pending, resolver=resolver)
    assert pending.status == Domain.Status.PENDING_DNS


def test_verify_domain_queries_txt_before_mx_in_order():
    "Public API."
    user = make_user("verify_h1")
    domain = make_domain(user)
    resolver = FakeResolver({
        (domain.verification_txt_host, "TXT"): [FakeRecord(strings=[domain.verification_token.encode()])],
        (domain.name, "MX"): [FakeRecord(exchange=f"{MX_TARGET_HOST}.")],
    })
    verify_domain(domain, resolver=resolver)
    assert resolver.calls == [
        (domain.verification_txt_host, "TXT"),
        (domain.name, "MX"),
    ]


def test_verify_domain_never_queries_mx_when_txt_fails():
    "Public API."
    user = make_user("verify_h2")
    domain = make_domain(user)
    resolver = FakeResolver({})  # no TXT record at all
    verify_domain(domain, resolver=resolver)
    assert resolver.calls == [(domain.verification_txt_host, "TXT")]
    assert (domain.name, "MX") not in resolver.calls


def test_verify_domain_account_hour_throttle_branch(monkeypatch):
    "Public API."
    from django.test import override_settings
    from rest_framework.test import APIClient

    from apps.accounts.services import create_api_key

    user = make_user("verify_f4_account_hour")
    _, token = create_api_key(user, "f4-throttle-test")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    class _AlwaysFailResolver:
        def resolve(self, qname, rdtype):
            import dns.exception

            raise dns.exception.DNSException()

    monkeypatch.setattr(
        "apps.domains.services._default_resolver", lambda: _AlwaysFailResolver()
    )

    with override_settings(ENABLE_BYOD=True):
        domains = [
            make_domain(user, name=f"f4-acct-hour-{i}.example.com") for i in range(21)
        ]

        responses = []
        for domain in domains:
            resp = client.post(f"/api/v1/domains/{domain.public_id}/verify")
            responses.append(resp)

        # First 20 calls each hit a brand-new domain (fresh 30s bucket) and a
        # not-yet-exhausted account-hour bucket -> all succeed.
        for resp in responses[:20]:
            assert resp.status_code == 200, resp.json()

        # The 21st call is on ITS OWN never-before-verified domain (30s
        # bucket fresh too) yet must be rejected -- proving it's the
        # ACCOUNT-hour cap (20/hour), not the per-domain 30s cap, that trips.
        final = responses[20]
        assert final.status_code == 429
        assert final.json()["error"]["code"] == "domain_verify_rate_limited"
        assert "Retry-After" in final
        assert int(final["Retry-After"]) > 0


def test_verify_domain_txt_scan_accepts_multiple_txt_records():
    user = make_user("verify_g")
    domain = make_domain(user)
    resolver = FakeResolver({
        (domain.verification_txt_host, "TXT"): [
            FakeRecord(strings=[b"some-other-value"]),
            FakeRecord(strings=[domain.verification_token.encode()]),
        ],
        (domain.name, "MX"): [FakeRecord(exchange=f"{MX_TARGET_HOST}.")],
    })
    result = verify_domain(domain, resolver=resolver)
    assert result.status == Domain.Status.VERIFIED
