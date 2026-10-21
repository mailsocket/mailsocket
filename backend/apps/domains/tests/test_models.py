"Public API."
import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan
from apps.domains.models import Domain

pytestmark = pytest.mark.django_db(transaction=True)


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


def test_domain_create_lowercases_and_sets_txt_host():
    user = make_user("dom_owner_a")
    domain = Domain.objects.create(user=user, name="OTP.Example.COM")
    assert domain.name == "otp.example.com"
    assert domain.verification_txt_host == "_mailsocket-verify.otp.example.com"
    assert domain.status == Domain.Status.PENDING_DNS
    assert domain.public_id.startswith("dom_")
    assert domain.verification_token


def test_domain_name_must_be_valid_fqdn():
    user = make_user("dom_owner_b")
    with pytest.raises(ValidationError):
        domain = Domain(user=user, name="not-a-fqdn")
        domain.full_clean(exclude=["public_id", "verification_txt_host"])


@pytest.mark.parametrize(
    "name",
    [
        "sub.inboxpipe.net",
        "otp.mailsocket.app",
        "deep.sub.mailsocket.app",
        "inboxpipe.net",
        "mailsocket.app",
        "x.inboxpipe.net",
        "a.b.mailsocket.app",
    ],
)
def test_domain_rejects_reserved_apex_subdomains(name):
    user = make_user("dom_owner_c")
    with pytest.raises(ValidationError):
        Domain.objects.create(user=user, name=name)


@pytest.mark.parametrize(
    "name",
    [
        "notinboxpipe.net",
        "evilmailsocket.app",
        "myinboxpipe.net",
        "example.com",
    ],
)
def test_domain_allows_reserved_apex_lookalikes(name):
    "Public API."
    user = make_user(f"dom_owner_allow_{name.replace('.', '_')}")
    domain = Domain.objects.create(user=user, name=name)
    assert domain.name == name
    assert domain.status == Domain.Status.PENDING_DNS


def test_domain_name_is_immutable_after_create():
    user = make_user("dom_owner_d")
    domain = Domain.objects.create(user=user, name="immutable.example.com")
    domain.name = "changed.example.com"
    with pytest.raises(ValidationError):
        domain.save()


def test_domain_name_globally_unique_across_owners():
    user_a = make_user("dom_owner_e")
    user_b = make_user("dom_owner_f")
    Domain.objects.create(user=user_a, name="taken.example.com")
    with pytest.raises(IntegrityError):
        Domain.objects.create(user=user_b, name="taken.example.com")


def test_domain_status_check_constraint_rejects_invalid_value():
    user = make_user("dom_owner_g")
    domain = Domain.objects.create(user=user, name="status.example.com")
    with pytest.raises(IntegrityError):
        Domain.objects.filter(pk=domain.pk).update(status="bogus")
