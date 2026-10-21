"Public API."

import hashlib

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, AccountPlan
from apps.accounts.services import create_api_key
from apps.api.tests.support import body_without_request_id
from apps.domains.models import Domain
from apps.inboxes.models import Inbox, Message

pytestmark = pytest.mark.django_db(transaction=True)


def make_user(name, *, plan=AccountPlan.Plan.PAID):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user, plan=plan)
    return user


def auth_client(user):
    _, token = create_api_key(user, "public-messages-test")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def make_domain(user, name, *, status=Domain.Status.VERIFIED):
    domain = Domain.objects.create(user=user, name=name)
    domain.status = status
    domain.save(update_fields=["status"])
    return domain


def _make_message(inbox, marker, **overrides):
    raw = f"Subject: {marker}\r\n\r\nbody {marker}".encode()
    digest = hashlib.sha256(raw).hexdigest()
    values = {
        "inbox": inbox,
        "dedupe_key": digest,
        "envelope_from": "security@acme.test",
        "envelope_recipient": inbox.address,
        "header_from": "Security <security@acme.test>",
        "header_to": [inbox.address],
        "header_cc": [],
        "subject": marker,
        "text_body": f"Your verification code is 482193. {marker}",
        "html_body": "<p>hi</p>",
        "raw_bytes": raw,
        "raw_sha256": digest,
        "size_bytes": len(raw),
        "ingest_status": Message.IngestStatus.PARSED,
        "otp": "482193",
        "otp_confidence": 0.95,
        "detected_otp": "482193",
    }
    values.update(overrides)
    return Message.objects.create(**values)


def _public_url(email):
    return reverse("api:public-inbox-messages", kwargs={"email": email})





def test_p1_default_private_shared_inbox_is_404():
    user = make_user("p1_owner")
    inbox = Inbox.objects.create(user=user, label="", routing_key="p1inbox")
    assert inbox.is_public is False  # THE non-negotiable default

    resp = APIClient().get(_public_url(inbox.address))
    assert resp.status_code == 404


# --- P2: opted-in inbox is readable, payload excludes sensitive fields ------


def test_p2_public_inbox_returns_200_with_no_sensitive_keys():
    user = make_user("p2_owner")
    inbox = Inbox.objects.create(user=user, label="", routing_key="p2inbox", is_public=True)
    inbox.metadata = {"secret_run_id": "internal-123"}
    inbox.save(update_fields=["metadata"])
    _make_message(inbox, "public msg")

    resp = APIClient().get(_public_url(inbox.address))
    assert resp.status_code == 200
    rows = resp.data["data"]
    assert len(rows) == 1
    keys = set(rows[0].keys())
    assert "html" not in keys
    assert "text" not in keys
    assert "metadata" not in keys
    assert "otp_candidates" not in keys
    # sanity: it does carry the ordinary summary fields
    assert "subject" in keys and "otp" in keys



# no-auth surface --------------------------------------------------------


def test_r3_public_payload_excludes_otp_context_and_links():
    user = make_user("r3_owner")
    inbox = Inbox.objects.create(user=user, label="", routing_key="r3inbox", is_public=True)
    # links[] carries a DISTINCT sentinel URL (not equal to magic_link) so we
    # can prove links CONTENT — not just the "links" key — is stripped from
    # the public payload. magic_link is left as the deliberately-kept
    # envelope field per spec.
    links_only_sentinel = "https://links-only-sentinel-9f3e.example/track"
    msg = _make_message(
        inbox,
        "r3 public msg",
        magic_link="https://example.test/login?token=r3sekrit",
        links=["https://example.test/login?token=r3sekrit", links_only_sentinel],
    )
    # sanity: the source data really does have a populated otp_context/links
    from apps.api.serializers import MessageSerializer

    owner_data = MessageSerializer(msg).data
    assert owner_data["otp_context"]
    assert owner_data["links"]
    assert links_only_sentinel in owner_data["links"]
    assert links_only_sentinel != owner_data["magic_link"]

    resp = APIClient().get(_public_url(inbox.address))
    assert resp.status_code == 200
    rows = resp.data["data"]
    assert len(rows) == 1
    row = rows[0]
    keys = set(row.keys())
    assert "otp_context" not in keys
    assert "links" not in keys
    # the otp_context snippet text must not leak elsewhere in the payload
    # either. (magic_link is a deliberately-kept envelope field per spec —
    # its URL value legitimately equals one links[] entry, so we only assert
    # the "links" list itself is gone by key, not that every URL string is
    # absent — EXCEPT for the links-only sentinel below, which proves links
    # content specifically (not just the key) is stripped.)
    payload_str = str(resp.data)
    assert owner_data["otp_context"] not in payload_str
    assert links_only_sentinel not in payload_str


# --- P3: BYOD inbox with is_public forced via ORM ⇒ 404 (read-time guard) ---
#

# single test only exercised layer 1 and was vacuous w.r.t. the layer-2 DB
# guard):
#
#  LAYER 1 (test_p3_byod_address_rejected_at_parse_is_404): querying the
#  inbox's OWN byod-domain address is rejected by parse_shared_address()
#  before any DB query runs (domain != settings.MAIL_DOMAIN). This test
#  passes even if the read-time domain__isnull=True guard is deleted.
#
#  LAYER 2 (test_p3b_byod_routing_key_collision_on_shared_domain_is_404): a
#  BYOD inbox's routing_key is queried on the SHARED domain address instead
#  (parse succeeds — it's a valid shared-domain shape), so this can only
#  404 via the read-time domain__isnull=True exclusion. It fails if that
#  guard is removed.


@override_settings(ENABLE_BYOD=True)
def test_p3_byod_address_rejected_at_parse_is_404():
    user = make_user("p3_owner")
    domain = make_domain(user, "p3-verified.example.com")
    # Bypass the serializer/view write-time check entirely — write directly
    # to the DB, exactly as the design's R1 threat model describes.
    inbox = Inbox.objects.create(
        user=user, label="", routing_key="p3inbox", domain=domain, is_public=True
    )
    assert inbox.is_public is True
    assert inbox.domain_id is not None

    resp = APIClient().get(_public_url(inbox.address))
    assert resp.status_code == 404


@override_settings(ENABLE_BYOD=True)
def test_p3b_byod_routing_key_collision_on_shared_domain_is_404():
    from django.conf import settings

    user = make_user("p3b_owner")
    domain = make_domain(user, "p3b-verified.example.com")
    inbox = Inbox.objects.create(
        user=user, label="", routing_key="xbyod", domain=domain, is_public=True
    )
    assert inbox.is_public is True
    assert inbox.domain_id is not None

    # Same routing_key, but queried on the SHARED MAIL_DOMAIN — parse_shared_
    # address() accepts this shape (it's not the byod domain), so the only
    # thing standing between this request and a 200 leaking the BYOD inbox's
    # messages is the read-time domain__isnull=True exclusion in
    # PublicInboxMessagesView. Delete that guard and this test fails.
    collision_address = f"xbyod@{settings.MAIL_DOMAIN}"
    resp = APIClient().get(_public_url(collision_address))
    assert resp.status_code == 404


# --- P4: POST /inboxes {public:true, domain:<verified>} ⇒ 422 ---------------


@override_settings(ENABLE_BYOD=True)
def test_p4_create_public_on_custom_domain_is_422():
    user = make_user("p4_owner")
    domain = make_domain(user, "p4-verified.example.com")
    client = auth_client(user)
    resp = client.post("/api/v1/inboxes", {"public": True, "domain": domain.public_id}, format="json")
    assert resp.status_code == 422
    assert resp.data["error"]["code"] == "public_not_allowed_on_custom_domain"
    assert Inbox.objects.filter(domain=domain).count() == 0


def test_p4_create_public_on_shared_domain_succeeds():
    user = make_user("p4b_owner")
    client = auth_client(user)
    resp = client.post("/api/v1/inboxes", {"public": True, "routing_key": "p4bshared"}, format="json")
    assert resp.status_code == 201
    inbox = Inbox.objects.get(routing_key="p4bshared", domain__isnull=True)
    assert inbox.is_public is True


# --- P5: disabled / soft-deleted public inbox ⇒ 404 -------------------------


def test_p5_disabled_public_inbox_is_404():
    user = make_user("p5_owner")
    inbox = Inbox.objects.create(
        user=user, label="", routing_key="p5inbox", is_public=True, is_enabled=False
    )
    resp = APIClient().get(_public_url(inbox.address))
    assert resp.status_code == 404


def test_p5_deleted_public_inbox_is_404():
    user = make_user("p5b_owner")
    inbox = Inbox.objects.create(
        user=user, label="", routing_key="p5binbox", is_public=True, deleted_at=timezone.now()
    )
    resp = APIClient().get(_public_url(inbox.address))
    assert resp.status_code == 404


# --- P6: unknown vs existing-private ⇒ byte-identical 404 -------------------


def test_p6_unknown_and_private_addresses_return_identical_404_body():
    user = make_user("p6_owner")
    Inbox.objects.create(user=user, label="", routing_key="p6private", is_public=False)

    resp_unknown = APIClient().get(_public_url("p6doesnotexist@in.inboxpipe.net"))
    resp_private = APIClient().get(_public_url("p6private@in.inboxpipe.net"))
    assert resp_unknown.status_code == resp_private.status_code == 404
    assert body_without_request_id(resp_unknown) == body_without_request_id(resp_private)


# --- P7: 31st request in a minute from one IP ⇒ 429 -------------------------


def test_p7_rate_limit_429_on_31st_request():
    user = make_user("p7_owner")
    inbox = Inbox.objects.create(user=user, label="", routing_key="p7inbox", is_public=True)
    client = APIClient()
    for _ in range(30):
        resp = client.get(_public_url(inbox.address))
        assert resp.status_code == 200
    resp = client.get(_public_url(inbox.address))
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


# --- P8: cross-tenant isolation + no identifiers in payload -----------------


_FORBIDDEN_IDENTIFIER_KEYS = {
    "user",
    "user_id",
    "tenant",
    "tenant_id",
    "account",
    "account_id",
    "owner",
}


def _walk_values(node):
    """Yield every scalar leaf value in a JSON-like structure (dict/list/scalar)."""
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk_values(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _walk_values(v)
    else:
        yield node


def test_p8_cross_tenant_isolation_and_no_identifiers_leaked():
    tenant_a = make_user("p8_tenant_a")
    tenant_b = make_user("p8_tenant_b")
    inbox_a = Inbox.objects.create(user=tenant_a, label="", routing_key="p8tenanta", is_public=True)
    inbox_b = Inbox.objects.create(user=tenant_b, label="", routing_key="p8tenantb", is_public=False)
    _make_message(inbox_a, "tenant a msg")
    
    # too — without this, test_p8 is vacuous for message-query isolation:
    # a globally mis-scoped message query (e.g. missing an inbox filter)
    # would still return only A's single message and pass. Distinctive
    # sentinel values (subject/otp/id) let us assert none of them leak into
    # tenant A's public response.
    msg_b = _make_message(
        inbox_b,
        "TENANT_B_SENTINEL_SUBJECT_9f3e",
        otp="778899",
        detected_otp="778899",
    )

    resp_a = APIClient().get(_public_url(inbox_a.address))
    assert resp_a.status_code == 200

    # Deterministic, unlike `str(tenant_a.pk) not in str(resp.data)`: a small
    # int pk (e.g. 1) can be a substring of an unrelated random base62
    # public_id, making that assertion flap depending on which ids get
    
    # value in the parsed payload and assert neither tenant pk appears as a
    # standalone value; (2) assert no message dict carries an internal
    # identifier KEY at all.
    rows = resp_a.data["data"]
    assert len(rows) == 1
    for row in rows:
        assert _FORBIDDEN_IDENTIFIER_KEYS.isdisjoint(row.keys())

    leaked_values = set(_walk_values(resp_a.data))
    assert tenant_a.pk not in leaked_values
    assert tenant_b.pk not in leaked_values
    assert str(tenant_a.pk) not in leaked_values
    assert str(tenant_b.pk) not in leaked_values
    # inbox_id (a public_id, not an internal tenant/user identifier) IS a
    # legitimate MessageSummarySerializer field — only USER/TENANT pk
    # leakage is the concern here, not the inbox's own public id.

    # R3 FIX 2: none of tenant B's sentinel values may appear anywhere in
    # tenant A's public response, recursively.
    assert "TENANT_B_SENTINEL_SUBJECT_9f3e" not in leaked_values
    assert "778899" not in leaked_values
    assert msg_b.public_id not in leaked_values

    resp_b = APIClient().get(_public_url(inbox_b.address))
    assert resp_b.status_code == 404

    
    # pinned byte-identical 404s itself — do it here, like P6, against a
    # guaranteed-unknown shared-domain address.
    resp_unknown = APIClient().get(_public_url("p8doesnotexist@in.inboxpipe.net"))
    assert resp_unknown.status_code == resp_b.status_code == 404
    assert body_without_request_id(resp_unknown) == body_without_request_id(resp_b)


# --- P9: malformed/normalized address variants never 500 -------------------


@pytest.mark.parametrize(
    "email",
    [
        "NOT-AN-EMAIL",
        "two@ats@in.inboxpipe.net",
        "@in.inboxpipe.net",
        "a@",
        "sp ace@in.inboxpipe.net",
        "valid@unknown-domain.example.com",
    ],
)
def test_p9_malformed_or_foreign_domain_addresses_never_500(email):
    resp = APIClient().get(_public_url(email))
    assert resp.status_code == 404


def test_p9_uppercase_address_normalizes_to_the_same_inbox():
    user = make_user("p9_owner")
    inbox = Inbox.objects.create(user=user, label="", routing_key="p9inbox", is_public=True)
    upper = inbox.address.upper()
    resp = APIClient().get(_public_url(upper))
    assert resp.status_code == 200





def test_random_slug_public_inbox_is_public_in_the_single_insert():
    """No second UPDATE flips is_public after the row is committed private."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    user = make_user("g5_public_atomic")
    client = auth_client(user)
    with CaptureQueriesContext(connection) as ctx:
        resp = client.post("/api/v1/inboxes", {"public": True}, format="json")
    assert resp.status_code == 201
    inbox = Inbox.objects.get(public_id=resp.json()["data"]["id"])
    assert inbox.is_public is True
    inbox_writes = [
        q["sql"]
        for q in ctx.captured_queries
        if '"inboxes_inbox"' in q["sql"] and q["sql"].lstrip().upper().startswith(("UPDATE", "INSERT"))
    ]
    assert len(inbox_writes) == 1, inbox_writes
    assert inbox_writes[0].lstrip().upper().startswith("INSERT")


def test_create_random_inbox_accepts_is_public():
    from apps.inboxes.services import create_random_inbox

    user = make_user("g5_public_service")
    assert create_random_inbox(user, is_public=True).is_public is True
    assert create_random_inbox(user).is_public is False
