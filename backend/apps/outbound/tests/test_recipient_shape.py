"Public API."

import pytest
from rest_framework.test import APIClient

from apps.accounts.services import create_api_key
from apps.outbound.models import OutboundEmail, SuppressionEntry
from apps.outbound.tests.test_outbound import eligible_domain, make_user
from apps.outbound.validation import AddressFormatError, validate_recipient_address

pytestmark = pytest.mark.django_db(transaction=True)

SUPPRESSED = "victim@example.com"


@pytest.fixture(autouse=True)
def _enable_outbound(settings):
    settings.ENABLE_OUTBOUND = True


def _setup(name):
    from apps.outbound.services import suppress

    user = make_user(name)
    domain = eligible_domain(user, f"{name.replace('_', '-')}.example.com")
    suppress(SUPPRESSED, reason=SuppressionEntry.Reason.HARD_BOUNCE, ttl_days=90)
    _, token = create_api_key(user, "g4")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return user, domain, client


SMUGGLED = [
    "Bob <victim@example.com>",
    "<victim@example.com>",
    '"Bob" <victim@example.com>',
    "victim@example.com (Bob)",
    "victim@example.com, other@example.org",
    "other@example.org,victim@example.com",
    "victim@example.com; other@example.org",
    "victim@example.com other@example.org",
    "Bob <victim@example.com>, Al <a@example.org>",
]


@pytest.mark.parametrize("value", SMUGGLED)
def test_gate_rejects_non_bare_addr_spec(value):
    with pytest.raises(AddressFormatError):
        validate_recipient_address(value)


@pytest.mark.parametrize("value", ["victim@example.com", "Victim.Name+tag@Example.COM", "a@b.co"])
def test_gate_accepts_bare_addr_spec(value):
    assert validate_recipient_address(value) == value


@pytest.mark.parametrize("field", ["to", "cc", "bcc"])
@pytest.mark.parametrize("value", SMUGGLED[:5])
def test_api_rejects_display_name_and_multi_address_recipients(field, value):
    user, domain, client = _setup(f"g4_{field}_{SMUGGLED.index(value)}")
    body = {"from_address": f"hello@{domain.name}", "to": ["ok@example.org"]}
    body[field] = [value] if field != "to" else [value]
    resp = client.post("/api/v1/outbound_emails", body, format="json")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    assert OutboundEmail.objects.filter(user=user).count() == 0


def test_api_rejects_display_name_reply_to():
    user, domain, client = _setup("g4_reply_to")
    resp = client.post(
        "/api/v1/outbound_emails",
        {
            "from_address": f"hello@{domain.name}",
            "to": ["ok@example.org"],
            "reply_to": "Bob <victim@example.com>",
        },
        format="json",
    )
    assert resp.status_code == 422
    assert OutboundEmail.objects.filter(user=user).count() == 0


def test_uppercase_suppressed_address_is_refused():
    user, domain, client = _setup("g4_upper")
    resp = client.post(
        "/api/v1/outbound_emails",
        {"from_address": f"hello@{domain.name}", "to": ["VICTIM@Example.COM"]},
        format="json",
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "recipient_rejected"
    assert OutboundEmail.objects.filter(user=user).count() == 0


@pytest.mark.parametrize(
    "legacy",
    ["Bob <victim@example.com>", "ok@example.org, VICTIM@example.com"],
)
def test_drain_recheck_catches_legacy_display_name_row(legacy):
    """A row queued BEFORE the gate fix (bypassing validation) is refused at
    drain: the suppression re-check extracts every real address."""
    from apps.outbound.engine import claim_one, deliver_claim

    user, domain, client = _setup(f"g4_legacy_{len(legacy)}")
    resp = client.post(
        "/api/v1/outbound_emails",
        {"from_address": f"hello@{domain.name}", "to": ["ok@example.org"]},
        format="json",
    )
    assert resp.status_code == 201
    OutboundEmail.objects.filter(user=user).update(to=[legacy])

    def bomb(**kw):
        raise AssertionError("transport must not be called for a suppressed recipient")

    pk, token = claim_one()
    assert deliver_claim(pk, token, transport=bomb) == "rejected"
    assert OutboundEmail.objects.get(pk=pk).error_code == "recipient_rejected"


def test_is_suppressed_extracts_addr_spec():
    from apps.outbound.services import is_suppressed, suppress

    suppress(SUPPRESSED, reason=SuppressionEntry.Reason.MANUAL)
    assert is_suppressed("Bob <victim@example.com>")
    assert is_suppressed("<VICTIM@example.com>")
    assert is_suppressed("a@example.org, victim@example.com")
    assert not is_suppressed("a@example.org")
