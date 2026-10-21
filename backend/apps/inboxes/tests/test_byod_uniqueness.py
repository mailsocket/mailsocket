"Public API."
import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan
from apps.domains.models import Domain
from apps.inboxes.models import Inbox

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


def make_domain(user, name, *, verified=True):
    domain = Domain.objects.create(user=user, name=name)
    if verified:
        domain.status = Domain.Status.VERIFIED
        domain.save(update_fields=["status"])
    return domain


def test_shared_domain_duplicate_routing_key_raises_integrity_error():
    """domain IS NULL on both rows -> same routing_key collides (today's
    exact pre-Stage-A behaviour, preserved)."""
    user = make_user("uniq_shared_a")
    Inbox.objects.create(user=user, routing_key="samekey")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Inbox.objects.create(user=user, routing_key="samekey")


def test_custom_domain_same_routing_key_once_per_domain():
    """domain IS NOT NULL: the same routing_key on the SAME domain collides."""
    user = make_user("uniq_custom_a")
    domain = make_domain(user, "custom-a.example.com")
    Inbox.objects.create(user=user, routing_key="login", domain=domain)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Inbox.objects.create(user=user, routing_key="login", domain=domain)


def test_same_routing_key_on_two_different_custom_domains_is_allowed():
    """The exact case a single composite unique gets right (domain values
    differ) — included so the partial-constraint choice isn't the only path
    exercised for the "two different custom domains" scenario."""
    user = make_user("uniq_custom_b")
    domain_1 = make_domain(user, "custom-b1.example.com")
    domain_2 = make_domain(user, "custom-b2.example.com")
    Inbox.objects.create(user=user, routing_key="login", domain=domain_1)
    # Must NOT raise.
    Inbox.objects.create(user=user, routing_key="login", domain=domain_2)
    assert Inbox.objects.filter(routing_key="login").count() == 2


def test_same_routing_key_shared_and_custom_domain_coexist():
    """The exact bug a single composite unique would introduce: NULL is
    "distinct" from NULL in Postgres for a plain composite unique, so it
    would NOT block a shared-domain duplicate (see test above) but this
    variant -- one NULL-domain inbox and one custom-domain inbox sharing a
    routing_key -- must be ALLOWED under both the correct two-partial design
    and a naive composite one; asserted here for completeness."""
    user = make_user("uniq_mixed_a")
    domain = make_domain(user, "mixed-a.example.com")
    Inbox.objects.create(user=user, routing_key="shared-and-custom")
    # Must NOT raise: different domain scope (NULL vs a real domain).
    Inbox.objects.create(user=user, routing_key="shared-and-custom", domain=domain)


def test_inbox_active_route_idx_composite_index_exists():
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = %s",
            ["inbox_active_route_idx"],
        )
        row = cursor.fetchone()
    assert row is not None, "inbox_active_route_idx is missing"
    indexdef = row[0]
    
    # reversed order would still "contain" both column names as substrings
    # but be a much weaker index for the Postfix relay lookup this backs.
    domain_pos = indexdef.index("domain_id")
    routing_key_pos = indexdef.index("routing_key")
    assert domain_pos < routing_key_pos, (
        f"inbox_active_route_idx must order (domain_id, routing_key): {indexdef!r}"
    )
    
    # exist — an unconditional index would silently stop excluding
    # soft-deleted/disabled inboxes from the active-route lookup.
    assert "deleted_at IS NULL" in indexdef
    assert "is_enabled" in indexdef


def test_both_partial_unique_constraints_exist_in_postgres():
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE indexname IN (%s, %s)",
            ["inbox_shared_routing_key_uniq", "inbox_custom_routing_key_uniq"],
        )
        rows = dict(cursor.fetchall())
    assert "inbox_shared_routing_key_uniq" in rows
    assert "inbox_custom_routing_key_uniq" in rows
    assert "UNIQUE" in rows["inbox_shared_routing_key_uniq"]
    assert "UNIQUE" in rows["inbox_custom_routing_key_uniq"]
    assert "domain_id" in rows["inbox_custom_routing_key_uniq"]
    
    # this, both assertions above would still pass for a single UNCONDITIONAL
    # unique index, which would break shared/custom coexistence (the whole
    # point of using two partial indexes instead of one composite).
    assert "domain_id IS NULL" in rows["inbox_shared_routing_key_uniq"]
    assert "domain_id IS NOT NULL" in rows["inbox_custom_routing_key_uniq"]


def test_address_property_uses_domain_name_when_bound():
    user = make_user("uniq_address_a")
    domain = make_domain(user, "address-a.example.com")
    inbox = Inbox.objects.create(user=user, routing_key="hello", domain=domain)
    assert inbox.address == "hello@address-a.example.com"


def test_address_property_falls_back_to_mail_domain_when_unbound(settings):
    user = make_user("uniq_address_b")
    inbox = Inbox.objects.create(user=user, routing_key="hello2")
    assert inbox.address == f"hello2@{settings.MAIL_DOMAIN}"
