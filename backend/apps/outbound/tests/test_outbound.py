"Public API."

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, AccountPlan
from apps.accounts.services import create_api_key
from apps.domains.models import Domain
from apps.outbound.models import OutboundEmail, SuppressionEntry

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _enable_outbound(settings):
    settings.ENABLE_OUTBOUND = True


def _error_without_request_id(resp):
    """#15: every error envelope now carries a
    per-request request_id, so byte-identical-body no-oracle assertions
    compare the JSON body with request_id stripped instead. Delegates to
    the shared apps.api.tests.support helper (full-body deep-copy +
    existence assertion) so every no-oracle comparison across the test
    suite uses one implementation."""
    from apps.api.tests.support import body_without_request_id

    return body_without_request_id(resp)


def make_user(name, *, plan=AccountPlan.Plan.BUSINESS, email=None):
    user = get_user_model().objects.create_user(
        username=name, email=email or f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=email or f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user, plan=plan)
    return user


def auth_client(user):
    _, token = create_api_key(user, "outbound-test")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def member_auth_client(owner, member):
    "Public API."
    _, token = create_api_key(owner, "member-minted", created_by=member)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def _bomb_transport(**kw):
    """Gate R2 hardening: every rejection/cap/suppression/TOCTOU path below
    must prove the send path NEVER reaches transport — replaces the no-op
    `lambda **kw: None`, which would silently pass even if a future
    regression let a rejected/capped/suppressed row actually send."""
    raise AssertionError("transport must not be called on a rejected/capped/suppressed send")


def eligible_domain(user, name):
    """A Domain that satisfies all four legs of the invariant."""
    domain = Domain.objects.create(user=user, name=name)
    domain.status = Domain.Status.VERIFIED
    domain.dkim_status = Domain.DkimStatus.VERIFIED
    domain.dkim_selector = "msout1"
    domain.dkim_public_key_txt = "v=DKIM1; k=rsa; p=fake"
    from apps.outbound.dkim_keys import encrypt_private_key, generate_keypair

    private_pem, _ = generate_keypair()
    domain.dkim_private_key_enc = encrypt_private_key(private_pem)
    domain.save()
    return domain


def send_body(from_address, to=None, **extra):
    body = {"from_address": from_address, "to": to or ["dest@example.test"]}
    body.update(extra)
    return body


# --- (1) send-as invariant, all four causes + denylist, no-oracle -----------


@pytest.mark.parametrize(
    "mutate",
    [
        "no_domain_row",
        "wrong_owner",
        "not_verified",
        "dkim_not_verified",
    ],
)
def test_invariant_all_four_causes_collapse_to_uniform_403(mutate):
    user_a = make_user(f"inv_a_{mutate}")
    user_b = make_user(f"inv_b_{mutate}")
    domain_name = f"inv-{mutate}.example.com"

    if mutate == "no_domain_row":
        from_address = f"hello@{domain_name}"
    elif mutate == "wrong_owner":
        eligible_domain(user_b, domain_name)
        from_address = f"hello@{domain_name}"
    elif mutate == "not_verified":
        domain = Domain.objects.create(user=user_a, name=domain_name)
        domain.dkim_status = Domain.DkimStatus.VERIFIED
        domain.save(update_fields=["dkim_status"])
        from_address = f"hello@{domain_name}"
    else:  # dkim_not_verified
        domain = Domain.objects.create(user=user_a, name=domain_name)
        domain.status = Domain.Status.VERIFIED
        domain.save(update_fields=["status"])
        from_address = f"hello@{domain_name}"

    client = auth_client(user_a)
    resp = client.post("/api/v1/outbound_emails", send_body(from_address), format="json")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "sending_domain_not_eligible"
    assert OutboundEmail.objects.filter(user=user_a).count() == 0


def test_invariant_four_eligibility_causes_plus_denylist_byte_identical_403_bodies():
    """FIX7 (no-oracle): the four eligibility-failure 403s (not-owned,
    unverified, dkim-unverified, no-domain-row) PLUS the reserved-apex
    denylist 403 must produce response.content that is BYTE-IDENTICAL
    across all five, not merely equal status+code checked independently —
    so no cause-specific detail/message text can ever leak between them."""
    responses = []

    # (1) no Domain row at all.
    user1 = make_user("byte403_no_row")
    resp = auth_client(user1).post(
        "/api/v1/outbound_emails", send_body("hello@byte403-no-row.example.com"), format="json"
    )
    responses.append(resp)

    # (2) not owned (belongs to a different tenant).
    user2a = make_user("byte403_wrong_owner_a")
    user2b = make_user("byte403_wrong_owner_b")
    eligible_domain(user2b, "byte403-wrong-owner.example.com")
    resp = auth_client(user2a).post(
        "/api/v1/outbound_emails",
        send_body("hello@byte403-wrong-owner.example.com"),
        format="json",
    )
    responses.append(resp)

    # (3) not verified (status).
    user3 = make_user("byte403_not_verified")
    domain3 = Domain.objects.create(user=user3, name="byte403-not-verified.example.com")
    domain3.dkim_status = Domain.DkimStatus.VERIFIED
    domain3.save(update_fields=["dkim_status"])
    resp = auth_client(user3).post(
        "/api/v1/outbound_emails",
        send_body("hello@byte403-not-verified.example.com"),
        format="json",
    )
    responses.append(resp)

    # (4) dkim not verified.
    user4 = make_user("byte403_dkim_unverified")
    domain4 = Domain.objects.create(user=user4, name="byte403-dkim-unverified.example.com")
    domain4.status = Domain.Status.VERIFIED
    domain4.save(update_fields=["status"])
    resp = auth_client(user4).post(
        "/api/v1/outbound_emails",
        send_body("hello@byte403-dkim-unverified.example.com"),
        format="json",
    )
    responses.append(resp)

    # (5) reserved-apex denylist.
    user5 = make_user("byte403_reserved_apex")
    from django.conf import settings as django_settings

    resp = auth_client(user5).post(
        "/api/v1/outbound_emails",
        send_body(f"hello@{django_settings.MAIL_DOMAIN}"),
        format="json",
    )
    responses.append(resp)

    for resp in responses:
        assert resp.status_code == 403
    bodies = [_error_without_request_id(resp) for resp in responses]
    assert all(body == bodies[0] for body in bodies), bodies


def test_invariant_denylist_mail_domain_rejected_even_with_no_domain_row(settings):
    user = make_user("inv_denylist_mail_domain")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails",
        send_body(f"attacker@{settings.MAIL_DOMAIN}"),
        format="json",
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "sending_domain_not_eligible"


def test_invariant_denylist_wins_even_if_domain_lookup_is_monkeypatched(monkeypatch, settings):
    """Belt-and-braces: force check_sending_domain_invariant's Domain lookup
    to return an eligible-looking row for a reserved apex, and prove the
    denylist check (which runs FIRST, unconditionally) still refuses."""
    user = make_user("inv_denylist_monkeypatch")

    class _FakeDomain:
        status = Domain.Status.VERIFIED
        dkim_status = Domain.DkimStatus.VERIFIED
        name = settings.MAIL_DOMAIN.lower()

    class _FakeQuerySet:
        def filter(self, **kwargs):
            return self

        def first(self):
            return _FakeDomain()

    monkeypatch.setattr(
        "apps.domains.models.Domain.objects", _FakeQuerySet(), raising=False
    )
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails",
        send_body(f"attacker@{settings.MAIL_DOMAIN}"),
        format="json",
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "sending_domain_not_eligible"


def test_invariant_eligible_domain_succeeds():
    user = make_user("inv_success")
    domain = eligible_domain(user, "inv-success.example.com")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp.status_code == 201, resp.json()
    assert resp.json()["data"]["status"] == "queued"
    row = OutboundEmail.objects.get(public_id=resp.json()["data"]["id"])
    assert row.domain_id == domain.pk
    # Enforcement point #2: DB assert (from_address ends with @domain.name).
    assert row.from_address.endswith("@" + domain.name)


# --- FIX1 (HIGH — send-as reserved-apex coverage + label-boundary control) --


@pytest.mark.parametrize(
    "reserved_domain_attr",
    ["MAIL_DOMAIN"],
)
def test_invariant_reserved_apex_denylist_no_domain_row_403(settings, reserved_domain_attr):
    """@in.inboxpipe.net / @mailsocket.app / @inboxpipe.net reserved-apex
    from-addresses must all 403 via the no-Domain-row path (nobody legally
    owns these — a Domain row can never be created for them)."""
    user = make_user("reserved_apex_no_row")
    client = auth_client(user)
    for reserved in (settings.MAIL_DOMAIN, "mailsocket.app", "inboxpipe.net"):
        resp = client.post(
            "/api/v1/outbound_emails", send_body(f"attacker@{reserved}"), format="json"
        )
        assert resp.status_code == 403, (reserved, resp.json())
        assert resp.json()["error"]["code"] == "sending_domain_not_eligible"
    assert OutboundEmail.objects.filter(user=user).count() == 0


def test_invariant_reserved_apex_denylist_fires_even_with_eligible_looking_row(
    monkeypatch, settings
):
    """Belt-and-braces: for EACH reserved apex (not just MAIL_DOMAIN), force
    the Domain lookup to return an eligible-looking row and prove the
    explicit denylist (checked FIRST, unconditionally) still refuses —
    proving the denylist is independent of the Domain lookup ever being
    bugged/monkeypatched."""
    user = make_user("reserved_apex_monkeypatch")
    client = auth_client(user)

    for reserved in (settings.MAIL_DOMAIN, "mailsocket.app", "inboxpipe.net"):

        class _FakeDomain:
            status = Domain.Status.VERIFIED
            dkim_status = Domain.DkimStatus.VERIFIED
            name = reserved.lower()

        class _FakeQuerySet:
            def filter(self, **kwargs):
                return self

            def first(self):
                return _FakeDomain()

        monkeypatch.setattr(
            "apps.domains.models.Domain.objects", _FakeQuerySet(), raising=False
        )
        resp = client.post(
            "/api/v1/outbound_emails", send_body(f"attacker@{reserved}"), format="json"
        )
        assert resp.status_code == 403, (reserved, resp.json())
        assert resp.json()["error"]["code"] == "sending_domain_not_eligible"


def test_invariant_label_boundary_lookalike_domain_positive_control():
    """Regression guard for a broken suffix match (str.endswith instead of
    a real label-boundary check): a genuinely owned, verified+dkim-verified
    domain whose name merely ENDS WITH the reserved apex's characters
    (notinboxpipe.net is NOT a subdomain of inboxpipe.net) must be ALLOWED,
    not swept up by the denylist."""
    user = make_user("label_boundary_lookalike")
    domain = eligible_domain(user, "notinboxpipe.net")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp.status_code == 201, resp.json()
    assert resp.json()["data"]["status"] == "queued"


# --- CRLF / control-char rejection in EVERY user field -----------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("subject", "hello\r\nBcc: evil@example.com"),
        ("reply_to", "evil@example.com\r\nBcc: x@example.com"),
        ("body_text", "line one\x00line two"),
    ],
)
def test_crlf_and_control_chars_rejected_422(field, value):
    user = make_user(f"crlf_{field}")
    domain = eligible_domain(user, f"crlf-{field}.example.com")
    client = auth_client(user)
    body = send_body(f"hello@{domain.name}", **{field: value})
    resp = client.post("/api/v1/outbound_emails", body, format="json")
    assert resp.status_code == 422


def test_crlf_in_recipient_address_rejected_422():
    user = make_user("crlf_to")
    domain = eligible_domain(user, "crlf-to.example.com")
    client = auth_client(user)
    body = send_body(f"hello@{domain.name}", to=["a@example.com\r\nBcc:x@example.com"])
    resp = client.post("/api/v1/outbound_emails", body, format="json")
    assert resp.status_code == 422


def test_too_many_recipients_rejected_422():
    user = make_user("too_many_rcpt")
    domain = eligible_domain(user, "too-many-rcpt.example.com")
    client = auth_client(user)
    body = send_body(f"hello@{domain.name}", to=[f"r{i}@example.com" for i in range(11)])
    resp = client.post("/api/v1/outbound_emails", body, format="json")
    assert resp.status_code == 422


# --- (1b) drain-time TOCTOU re-check (enforcement point #3) ------------------


def test_toctou_domain_demoted_after_enqueue_rejected_at_drain():
    from apps.outbound.engine import claim_one, deliver_claim

    user = make_user("toctou_a")
    domain = eligible_domain(user, "toctou-a.example.com")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp.status_code == 201

    # Demote AFTER enqueue, BEFORE drain.
    domain.dkim_status = Domain.DkimStatus.FAILED
    domain.save(update_fields=["dkim_status"])

    pk, token = claim_one()
    result = deliver_claim(pk, token, transport=_bomb_transport)
    assert result == "rejected"
    row = OutboundEmail.objects.get(pk=pk)
    assert row.status == OutboundEmail.Status.REJECTED
    assert row.error_code == "domain_no_longer_eligible"


def test_toctou_domain_status_demoted_to_failed_after_enqueue_rejected_at_drain():
    """FIX2: the STATUS demote case (distinct from the dkim_status demote
    above) — a domain's VERIFIED status itself can be demoted to FAILED
    (e.g. a later MX/TXT recheck failure) independent of DKIM. Both legs of
    the invariant must be re-checked at drain, not just one."""
    from apps.outbound.engine import claim_one, deliver_claim

    user = make_user("toctou_status")
    domain = eligible_domain(user, "toctou-status.example.com")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp.status_code == 201

    domain.status = Domain.Status.FAILED
    domain.save(update_fields=["status"])

    pk, token = claim_one()
    result = deliver_claim(pk, token, transport=_bomb_transport)
    assert result == "rejected"
    row = OutboundEmail.objects.get(pk=pk)
    assert row.status == OutboundEmail.Status.REJECTED
    assert row.error_code == "domain_no_longer_eligible"


def test_toctou_account_suspended_after_enqueue_rejected_at_drain():
    from apps.outbound.engine import claim_one, deliver_claim

    user = make_user("toctou_b")
    domain = eligible_domain(user, "toctou-b.example.com")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp.status_code == 201

    Account.objects.filter(user=user).update(is_suspended=True)

    pk, token = claim_one()
    result = deliver_claim(pk, token, transport=_bomb_transport)
    assert result == "rejected"


def test_toctou_plan_suspended_after_enqueue_rejected_at_drain():
    """FIX2: plan SUSPEND — check_sending_domain_invariant_at_drain also
    requires AccountPlan.status == ACTIVE; a plan demoted to SUSPENDED
    (billing failure) mid-queue must reject at drain just like an
    account-level suspend."""
    from apps.outbound.engine import claim_one, deliver_claim

    user = make_user("toctou_plan_suspend")
    domain = eligible_domain(user, "toctou-plan-suspend.example.com")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp.status_code == 201

    AccountPlan.objects.filter(user=user).update(status=AccountPlan.Status.SUSPENDED)

    pk, token = claim_one()
    result = deliver_claim(pk, token, transport=_bomb_transport)
    assert result == "rejected"
    row = OutboundEmail.objects.get(pk=pk)
    assert row.status == OutboundEmail.Status.REJECTED
    assert row.error_code == "domain_no_longer_eligible"


def test_toctou_domain_deleted_after_enqueue_rejected_at_drain():
    from apps.outbound.engine import claim_one, deliver_claim

    user = make_user("toctou_c")
    domain = eligible_domain(user, "toctou-c.example.com")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp.status_code == 201
    domain.delete()

    pk, token = claim_one()
    result = deliver_claim(pk, token, transport=_bomb_transport)
    assert result == "rejected"


# --- (2) quota atomicity ------------------------------------------------------


def test_quota_atomic_two_concurrent_enqueues_cannot_both_slip_past_cap(monkeypatch):
    """Two enqueues in a tight sequence, hour cap set to exactly one send's
    worth of recipients — the second must be refused, proving the quota
    check + create happen inside the SAME locked transaction (design §3
    step 9 / §7 'Quota atomicity')."""
    from apps.accounts import entitlements as entitlements_module

    business_limits = dict(entitlements_module.settings.BILLING_PLAN_LIMITS["business"])
    business_limits["outbound_per_hour"] = 1
    business_limits["outbound_per_day"] = 100
    monkeypatch.setitem(entitlements_module.settings.BILLING_PLAN_LIMITS, "business", business_limits)
    user = make_user("quota_atomic")
    domain = eligible_domain(user, "quota-atomic.example.com")
    client = auth_client(user)

    resp1 = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp1.status_code == 201

    resp2 = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp2.status_code == 403
    assert resp2.json()["error"]["code"] == "outbound_quota_exceeded"
    assert OutboundEmail.objects.filter(user=user).count() == 1


def test_quota_day_and_hour_caps_enforced_independently(monkeypatch):
    from apps.accounts import entitlements as entitlements_module

    business_limits = dict(entitlements_module.settings.BILLING_PLAN_LIMITS["business"])
    business_limits["outbound_per_hour"] = 100
    business_limits["outbound_per_day"] = 1
    monkeypatch.setitem(entitlements_module.settings.BILLING_PLAN_LIMITS, "business", business_limits)
    user = make_user("quota_day")
    domain = eligible_domain(user, "quota-day.example.com")
    client = auth_client(user)
    resp1 = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp1.status_code == 201
    resp2 = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp2.status_code == 403
    assert resp2.json()["error"]["code"] == "outbound_quota_exceeded"


# --- FIX3 (HIGH — quota atomicity: recipient burn, rollback, concurrency) ----


def test_quota_recipient_count_burns_full_count_not_one(monkeypatch):
    """A 3-recipient send must consume 3 of the daily/hourly cap, not 1 —
    can_send_outbound sums recipient_count, so the SECOND send (which alone
    would fit under a naive per-row count) must be refused once the first
    3-recipient send has already burned 3 of a 4-recipient cap."""
    from apps.accounts import entitlements as entitlements_module

    business_limits = dict(entitlements_module.settings.BILLING_PLAN_LIMITS["business"])
    business_limits["outbound_per_hour"] = 4
    business_limits["outbound_per_day"] = 100
    monkeypatch.setitem(entitlements_module.settings.BILLING_PLAN_LIMITS, "business", business_limits)
    user = make_user("quota_recipient_burn")
    domain = eligible_domain(user, "quota-recipient-burn.example.com")
    client = auth_client(user)

    resp1 = client.post(
        "/api/v1/outbound_emails",
        send_body(f"hello@{domain.name}", to=["a@example.com", "b@example.com", "c@example.com"]),
        format="json",
    )
    assert resp1.status_code == 201, resp1.json()
    row = OutboundEmail.objects.get(public_id=resp1.json()["data"]["id"])
    assert row.recipient_count == 3

    # Only 1 of the 4-recipient hourly cap remains — a 2-recipient send must
    # now be refused (a naive "count rows, not recipients" bug would allow
    # this, since it would only be the SECOND row).
    resp2 = client.post(
        "/api/v1/outbound_emails",
        send_body(f"hello@{domain.name}", to=["d@example.com", "e@example.com"]),
        format="json",
    )
    assert resp2.status_code == 403
    assert resp2.json()["error"]["code"] == "outbound_quota_exceeded"

    # But exactly 1 more recipient still fits.
    resp3 = client.post(
        "/api/v1/outbound_emails",
        send_body(f"hello@{domain.name}", to=["f@example.com"]),
        format="json",
    )
    assert resp3.status_code == 201, resp3.json()
    assert OutboundEmail.objects.filter(user=user).count() == 2


def test_quota_refused_create_leaves_counter_and_rowcount_unchanged(monkeypatch):
    """A refused enqueue (403 outbound_quota_exceeded) must roll back
    EVERYTHING inside the atomic gate — both the durable row count AND the
    quota counter it feeds (the SUM(recipient_count) query itself) must be
    byte-identical before and after the refused attempt. We assert the
    NUMERIC quota sum explicitly (not just the can_send_outbound boolean),
    to prove there is no partial/ghost consumption on the rolled-back
    attempt."""
    from datetime import timedelta

    from django.db.models import Sum
    from django.utils import timezone

    from apps.accounts import entitlements as entitlements_module
    from apps.accounts.entitlements import can_send_outbound

    business_limits = dict(entitlements_module.settings.BILLING_PLAN_LIMITS["business"])
    business_limits["outbound_per_hour"] = 1
    business_limits["outbound_per_day"] = 100
    monkeypatch.setitem(entitlements_module.settings.BILLING_PLAN_LIMITS, "business", business_limits)
    user = make_user("quota_refused_rollback")
    domain = eligible_domain(user, "quota-refused-rollback.example.com")
    client = auth_client(user)

    def hour_quota_sum():
        """Exact same query shape can_send_outbound uses for the hourly
        window: SUM(recipient_count) over the trailing 1-hour window."""
        now = timezone.now()
        return (
            OutboundEmail.objects.filter(user=user, created_at__gte=now - timedelta(hours=1))
            .aggregate(total=Sum("recipient_count"))["total"]
            or 0
        )

    resp1 = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp1.status_code == 201

    before_rowcount = OutboundEmail.objects.filter(user=user).count()
    before_quota_sum = hour_quota_sum()
    from django.db import transaction as db_transaction

    with db_transaction.atomic():
        before_ok, _ = can_send_outbound(user, 1)
    assert before_ok is False  # already at cap

    resp2 = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp2.status_code == 403
    assert resp2.json()["error"]["code"] == "outbound_quota_exceeded"

    after_rowcount = OutboundEmail.objects.filter(user=user).count()
    after_quota_sum = hour_quota_sum()
    with db_transaction.atomic():
        after_ok, _ = can_send_outbound(user, 1)
    assert after_rowcount == before_rowcount == 1
    assert before_ok is False
    assert after_ok is False
    # Explicit numeric proof: the durable quota SUM(recipient_count) the
    # refused attempt would have fed into is byte-identical before/after —
    # delta == 0, not just the boolean "still refused" outcome.
    assert after_quota_sum == before_quota_sum
    assert after_quota_sum - before_quota_sum == 0


def test_quota_genuine_concurrent_boundary_two_connections_only_one_wins(monkeypatch):
    """Genuine concurrency proof (not just sequential requests in one
    thread): two SEPARATE DB connections both racing at cap-1 must yield
    exactly one 201 and one 429/403 — the select_for_update lock on the
    plan row serializes the two transactions so they can never both slip
    past can_send_outbound's SUM check."""
    import threading

    from django.db import close_old_connections

    from apps.accounts import entitlements as entitlements_module

    business_limits = dict(entitlements_module.settings.BILLING_PLAN_LIMITS["business"])
    business_limits["outbound_per_hour"] = 1
    business_limits["outbound_per_day"] = 100
    monkeypatch.setitem(entitlements_module.settings.BILLING_PLAN_LIMITS, "business", business_limits)
    user = make_user("quota_concurrent")
    domain = eligible_domain(user, "quota-concurrent.example.com")
    _, token = create_api_key(user, "concurrent-test")

    barrier = threading.Barrier(2)
    results = []
    lock = threading.Lock()

    def fire():
        close_old_connections()
        local_client = APIClient()
        local_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        barrier.wait(timeout=10)
        resp = local_client.post(
            "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
        )
        with lock:
            results.append(resp.status_code)
        close_old_connections()

    threads = [threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert sorted(results) == [201, 403]
    assert OutboundEmail.objects.filter(user=user).count() == 1


@pytest.mark.parametrize("plan", [AccountPlan.Plan.FREE, AccountPlan.Plan.PAID, AccountPlan.Plan.SCALE])
def test_non_business_tiers_cannot_send(plan):
    user = make_user(f"tier_{plan}", plan=plan)
    domain = eligible_domain(user, f"tier-{plan}.example.com")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "outbound_entitlement_required"


def test_demo_or_agent_user_cannot_send_even_on_business_plan(settings):
    """Wall #2: even if a demo/agent account somehow held a Business plan,
    the explicit is_demo_or_agent_user check refuses independently."""
    from apps.accounts.services import AGENT_ACCOUNT_DOMAIN

    user = make_user("agent_wall", email=f"agent_wall@{AGENT_ACCOUNT_DOMAIN}")
    domain = eligible_domain(user, "agent-wall.example.com")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "outbound_entitlement_required"
    assert OutboundEmail.objects.filter(user=user).count() == 0


# --- FIX4 (HIGH — tier walls: 3 real independent-wall paths) ----------------


def test_real_demo_user_cannot_send_even_on_business_plan():
    """(a) genuine get_demo_user() path — not a hand-rolled fixture. Both
    walls proven independently: 403 at the default (Free) plan, AND 403
    again after monkeypatching the demo account onto Business."""
    from apps.accounts.services import get_demo_user

    demo_user = get_demo_user()
    domain = eligible_domain(demo_user, "demo-wall-real.example.com")
    client = auth_client(demo_user)

    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "outbound_entitlement_required"

    # Wall #2 independent-of-tier proof: force Business and prove STILL 403.
    AccountPlan.objects.filter(user=demo_user).update(plan=AccountPlan.Plan.BUSINESS)
    resp2 = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp2.status_code == 403
    assert resp2.json()["error"]["code"] == "outbound_entitlement_required"
    assert OutboundEmail.objects.filter(user=demo_user).count() == 0


def test_agent_register_minted_key_cannot_send_even_on_business_plan():
    """(b) a REAL /api/v1/agents/register-minted key (not a hand-rolled
    agent user) — both walls proven independently."""
    from apps.accounts.services import register_agent

    agent_user, api_key, token = register_agent("wall-test-agent")
    domain = eligible_domain(agent_user, "agent-register-wall.example.com")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "outbound_entitlement_required"

    # Wall #2 independent-of-tier proof.
    AccountPlan.objects.filter(user=agent_user).update(plan=AccountPlan.Plan.BUSINESS)
    resp2 = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp2.status_code == 403
    assert resp2.json()["error"]["code"] == "outbound_entitlement_required"
    assert OutboundEmail.objects.filter(user=agent_user).count() == 0


def test_member_minted_key_cannot_send():
    "Public API."
    owner = make_user("member_wall_owner")
    member = make_user("member_wall_member")
    domain = eligible_domain(owner, "member-wall.example.com")
    client = member_auth_client(owner, member)

    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "account_forbidden"
    assert OutboundEmail.objects.filter(user=owner).count() == 0


def test_plan_limits_outbound_free_paid_scale_are_zero_business_nonzero():
    from apps.accounts.entitlements import plan_limits

    for plan in ("free", "paid", "scale"):
        limits = plan_limits(plan)
        assert limits["outbound_per_day"] == 0
        assert limits["outbound_per_hour"] == 0
    business = plan_limits("business")
    assert business["outbound_per_day"] == 500
    assert business["outbound_per_hour"] == 100
    assert business["outbound_recipients_per_message"] == 10


# --- lifecycle: queued -> sending -> sent / failed / rejected / bounced ------


def test_lifecycle_success_path_sent(monkeypatch):
    from apps.outbound.engine import claim_one, deliver_claim

    user = make_user("lifecycle_sent")
    domain = eligible_domain(user, "lifecycle-sent.example.com")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp.status_code == 201
    row = OutboundEmail.objects.get(public_id=resp.json()["data"]["id"])
    assert row.status == OutboundEmail.Status.QUEUED

    sent_calls = []

    def fake_transport(**kwargs):
        sent_calls.append(kwargs)

    pk, token = claim_one()
    result = deliver_claim(pk, token, transport=fake_transport)
    assert result == "sent"
    row.refresh_from_db()
    assert row.status == OutboundEmail.Status.SENT
    assert row.sent_at is not None
    assert len(sent_calls) == 1
    # The signed message must carry a DKIM-Signature header (fail-closed —
    # see the dedicated DKIM tests below for the deeper assertion).
    assert b"DKIM-Signature" in sent_calls[0]["raw_message"]


def test_lifecycle_tempfail_retries_then_permanent_fails_after_max_attempts(settings):
    from apps.outbound.engine import claim_one, deliver_claim
    from apps.outbound.transport import SmtpTransportError

    settings.OUTBOUND_MAX_ATTEMPTS = 2
    user = make_user("lifecycle_retry")
    domain = eligible_domain(user, "lifecycle-retry.example.com")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    row_id = resp.json()["data"]["id"]

    def failing_transport(**kwargs):
        raise SmtpTransportError("tempfail", "connection refused")

    pk, token = claim_one()
    result = deliver_claim(pk, token, transport=failing_transport)
    assert result == "retrying"
    row = OutboundEmail.objects.get(public_id=row_id)
    assert row.status == OutboundEmail.Status.QUEUED
    assert row.attempt_count == 1
    row.next_attempt_at = timezone.now()
    row.save(update_fields=["next_attempt_at"])

    pk, token = claim_one()
    result = deliver_claim(pk, token, transport=failing_transport)
    assert result == "failed"
    row.refresh_from_db()
    assert row.status == OutboundEmail.Status.FAILED


def test_lifecycle_permanent_failure_does_not_retry():
    from apps.outbound.engine import claim_one, deliver_claim
    from apps.outbound.transport import SmtpTransportError

    user = make_user("lifecycle_permanent")
    domain = eligible_domain(user, "lifecycle-permanent.example.com")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    row_id = resp.json()["data"]["id"]

    def permanent_transport(**kwargs):
        raise SmtpTransportError("permanent", "mailbox unavailable")

    pk, token = claim_one()
    result = deliver_claim(pk, token, transport=permanent_transport)
    assert result == "failed"
    row = OutboundEmail.objects.get(public_id=row_id)
    assert row.status == OutboundEmail.Status.FAILED
    assert row.attempt_count == 1


# --- global drain cap ---------------------------------------------------------


def test_drain_global_cap_zero_sends_nothing_rows_stay_queued(settings):
    settings.OUTBOUND_GLOBAL_HOURLY_CAP = 0
    from apps.outbound.engine import drain

    user = make_user("global_cap")
    domain = eligible_domain(user, "global-cap.example.com")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    row_id = resp.json()["data"]["id"]

    result = drain(transport=_bomb_transport)
    assert result.claimed == 0
    assert result.global_cap_exhausted is True
    row = OutboundEmail.objects.get(public_id=row_id)
    assert row.status == OutboundEmail.Status.QUEUED


def test_drain_global_cap_allows_exactly_n_then_stops(settings):
    settings.OUTBOUND_GLOBAL_HOURLY_CAP = 1
    from apps.outbound.engine import drain

    user = make_user("global_cap_n")
    domain = eligible_domain(user, "global-cap-n.example.com")
    client = auth_client(user)
    for i in range(2):
        client.post(
            "/api/v1/outbound_emails",
            send_body(f"hello@{domain.name}", to=[f"r{i}@example.com"]),
            format="json",
        )

    result = drain(transport=lambda **kw: None)
    assert result.claimed == 1
    assert result.sent == 1
    assert result.global_cap_exhausted is True
    assert OutboundEmail.objects.filter(status=OutboundEmail.Status.QUEUED).count() == 1


def test_drain_global_cap_window_rollover_drains_remainder_next_window(settings):
    """FIX5: the row that stayed queued when the fleet bucket was exhausted
    must NOT be lost — once the hourly window rolls over, the next drain
    pass sends it. Advances the bucket clock by expiring the window row
    directly (increment_rate_limit's own reset condition: expires_at <=
    now), mirroring what a real hour boundary does without sleeping."""
    from apps.accounts.models import RateLimitBucket
    from apps.outbound.engine import GLOBAL_BUCKET_NAME, drain

    settings.OUTBOUND_GLOBAL_HOURLY_CAP = 1
    user = make_user("global_cap_rollover")
    domain = eligible_domain(user, "global-cap-rollover.example.com")
    client = auth_client(user)
    for i in range(2):
        client.post(
            "/api/v1/outbound_emails",
            send_body(f"hello@{domain.name}", to=[f"r{i}@example.com"]),
            format="json",
        )

    first_pass_sent = []
    result1 = drain(transport=lambda **kw: first_pass_sent.append(kw))
    assert result1.claimed == 1
    assert result1.sent == 1
    assert result1.global_cap_exhausted is True
    remaining = OutboundEmail.objects.filter(status=OutboundEmail.Status.QUEUED)
    assert remaining.count() == 1
    remaining_pk = remaining.first().pk

    # Advance the window: force the global bucket's expires_at into the
    # past so increment_rate_limit's own reset branch fires on next call.
    from apps.accounts.services import _bucket_hash

    bucket_hash = _bucket_hash(GLOBAL_BUCKET_NAME, "global")
    RateLimitBucket.objects.filter(bucket_hash=bucket_hash).update(
        expires_at=timezone.now() - timezone.timedelta(seconds=1)
    )

    second_pass_sent = []
    result2 = drain(transport=lambda **kw: second_pass_sent.append(kw))
    assert result2.claimed == 1
    assert result2.sent == 1
    # NOTE: with OUTBOUND_GLOBAL_HOURLY_CAP=1, the bucket is used up again
    # by this single send, so global_cap_exhausted is True at the END of
    # this pass too — the assertion that matters is that the remainder was
    # NOT lost (claimed==1, sent==1, nothing left QUEUED), not that the cap
    # magically has room after consuming its only slot.
    row = OutboundEmail.objects.get(pk=remaining_pk)
    assert row.status == OutboundEmail.Status.SENT
    assert OutboundEmail.objects.filter(status=OutboundEmail.Status.QUEUED).count() == 0


def test_drain_batch_size_caps_claims_per_pass(settings):
    settings.OUTBOUND_GLOBAL_HOURLY_CAP = 1_000_000
    from apps.outbound.engine import drain

    user = make_user("drain_batch")
    domain = eligible_domain(user, "drain-batch.example.com")
    client = auth_client(user)
    for i in range(5):
        client.post(
            "/api/v1/outbound_emails",
            send_body(f"hello@{domain.name}", to=[f"r{i}@example.com"]),
            format="json",
        )

    result = drain(batch_size=2, transport=lambda **kw: None)
    assert result.claimed == 2


# --- suppression: global, enforced at enqueue AND at send --------------------


def test_suppression_blocks_at_enqueue():
    from apps.outbound.services import suppress

    user = make_user("suppress_enqueue")
    domain = eligible_domain(user, "suppress-enqueue.example.com")
    suppress("blocked@example.com", reason=SuppressionEntry.Reason.MANUAL)

    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails",
        send_body(f"hello@{domain.name}", to=["blocked@example.com"]),
        format="json",
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "recipient_rejected"
    assert OutboundEmail.objects.filter(user=user).count() == 0


def test_suppression_added_after_enqueue_caught_at_drain():
    from apps.outbound.engine import claim_one, deliver_claim
    from apps.outbound.services import suppress

    user = make_user("suppress_drain")
    domain = eligible_domain(user, "suppress-drain.example.com")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails",
        send_body(f"hello@{domain.name}", to=["latebounce@example.com"]),
        format="json",
    )
    assert resp.status_code == 201

    suppress("latebounce@example.com", reason=SuppressionEntry.Reason.HARD_BOUNCE, ttl_days=90)

    pk, token = claim_one()
    result = deliver_claim(pk, token, transport=_bomb_transport)
    assert result == "rejected"
    row = OutboundEmail.objects.get(pk=pk)
    assert row.error_code == "recipient_rejected"


def test_suppression_full_chain_hard_bounce_to_bounced_to_422_enqueue():
    """FIX6: the full chain THROUGH record_hard_bounce (now reachable via
    CODE FIX 0's ingest wiring — see apps/ingest/tests/test_bounce_chain.py
    for the end-to-end proof fed through the real /internal/ingest
    endpoint). This test proves the SAME chain at the services layer:
    record_hard_bounce -> sent->bounced + SuppressionEntry -> a subsequent
    enqueue to that address 422s."""
    from apps.outbound.services import record_hard_bounce

    user = make_user("suppress_full_chain")
    domain = eligible_domain(user, "suppress-full-chain.example.com")
    sent_row = OutboundEmail.objects.create(
        user=user,
        domain=domain,
        from_address=f"hello@{domain.name}",
        to=["gone@example.test"],
        status=OutboundEmail.Status.SENT,
        recipient_count=1,
    )

    record_hard_bounce(sent_row.public_id)

    sent_row.refresh_from_db()
    assert sent_row.status == OutboundEmail.Status.BOUNCED
    assert sent_row.bounced_at is not None
    assert SuppressionEntry.objects.filter(email_normalized="gone@example.test").exists()

    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails",
        send_body(f"hello@{domain.name}", to=["gone@example.test"]),
        format="json",
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "recipient_rejected"


def test_suppression_is_global_across_tenants():
    from apps.outbound.services import is_suppressed, suppress

    suppress("shared-bad@example.com", reason=SuppressionEntry.Reason.COMPLAINT)
    assert is_suppressed("shared-bad@example.com") is True
    # Case-insensitivity via normalize_email.
    assert is_suppressed("Shared-Bad@Example.com") is True


def test_suppression_hard_bounce_expires_after_ttl():
    from apps.outbound.services import is_suppressed, suppress

    suppress("expiring@example.com", reason=SuppressionEntry.Reason.HARD_BOUNCE, ttl_days=90)
    entry = SuppressionEntry.objects.get(email_normalized="expiring@example.com")
    entry.expires_at = timezone.now() - timezone.timedelta(seconds=1)
    entry.save(update_fields=["expires_at"])
    assert is_suppressed("expiring@example.com") is False


# --- auto-pause circuit breaker ----------------------------------------------


def test_auto_pause_triggers_above_20_percent_over_50_sends(settings):
    from apps.outbound.services import maybe_auto_pause

    settings.OUTBOUND_AUTO_PAUSE_WINDOW = 50
    settings.OUTBOUND_AUTO_PAUSE_BOUNCE_RATIO = 0.2
    user = make_user("auto_pause_trigger")
    domain = eligible_domain(user, "auto-pause-trigger.example.com")

    # 40 sent, 11 bounced -> 11/51 ratio... use exactly 50 rows: 39 sent, 11 bounced (22%).
    for _ in range(39):
        OutboundEmail.objects.create(
            user=user, domain=domain, from_address=f"hello@{domain.name}",
            to=["x@example.com"], status=OutboundEmail.Status.SENT, recipient_count=1,
        )
    for _ in range(11):
        OutboundEmail.objects.create(
            user=user, domain=domain, from_address=f"hello@{domain.name}",
            to=["x@example.com"], status=OutboundEmail.Status.BOUNCED, recipient_count=1,
        )
    assert maybe_auto_pause(user.pk) is True
    account = Account.objects.get(user=user)
    assert account.outbound_paused_at is not None


def test_auto_pause_does_not_trigger_below_threshold(settings):
    from apps.outbound.services import maybe_auto_pause

    settings.OUTBOUND_AUTO_PAUSE_WINDOW = 50
    settings.OUTBOUND_AUTO_PAUSE_BOUNCE_RATIO = 0.2
    user = make_user("auto_pause_safe")
    domain = eligible_domain(user, "auto-pause-safe.example.com")
    for _ in range(45):
        OutboundEmail.objects.create(
            user=user, domain=domain, from_address=f"hello@{domain.name}",
            to=["x@example.com"], status=OutboundEmail.Status.SENT, recipient_count=1,
        )
    for _ in range(5):
        OutboundEmail.objects.create(
            user=user, domain=domain, from_address=f"hello@{domain.name}",
            to=["x@example.com"], status=OutboundEmail.Status.BOUNCED, recipient_count=1,
        )
    assert maybe_auto_pause(user.pk) is False
    account = Account.objects.get(user=user)
    assert account.outbound_paused_at is None


def test_auto_paused_account_rejected_at_drain():
    from apps.outbound.engine import claim_one, deliver_claim

    user = make_user("auto_pause_drain")
    domain = eligible_domain(user, "auto-pause-drain.example.com")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp.status_code == 201

    Account.objects.filter(user=user).update(outbound_paused_at=timezone.now())

    pk, token = claim_one()
    result = deliver_claim(pk, token, transport=_bomb_transport)
    assert result == "rejected"


# --- DKIM signing: real dkimpy call verifies; fail-closed on unverified ------


def test_dkim_signing_produces_a_verifiable_signature():
    """Real dkimpy sign + verify round-trip — proves the signing call is
    real dkimpy, not a stub, and that a signed message actually verifies
    against the published public key."""
    import dkim

    from apps.outbound.dkim_keys import encrypt_private_key, generate_keypair
    from apps.outbound.dkim_sign import sign_message

    private_pem, txt_value = generate_keypair()
    encrypted = encrypt_private_key(private_pem)
    raw_message = (
        b"From: sender@dkim-verify.example.com\r\n"
        b"To: dest@example.com\r\n"
        b"Subject: dkim test\r\n"
        b"\r\n"
        b"hello world\r\n"
    )
    signature = sign_message(
        raw_message,
        domain_name="dkim-verify.example.com",
        selector="msout1",
        private_key_enc=encrypted,
    )
    signed = signature + raw_message
    assert signed.startswith(b"DKIM-Signature:") or b"DKIM-Signature:" in signature

    # Verify using the SAME public key material we published in txt_value,
    # via a fake DNS resolver that returns exactly that TXT record — dkimpy
    # verification calls DNS itself, so monkeypatch its resolver hook.
    def fake_dns_lookup(name, timeout=5):
        assert name == b"msout1._domainkey.dkim-verify.example.com."
        return txt_value.encode()

    result = dkim.verify(signed, dnsfunc=fake_dns_lookup)
    assert result is True


def test_dkim_sign_failure_is_fail_closed_never_sent_unsigned(monkeypatch):
    """If dkimpy signing raises, the drain MUST NOT fall through to sending
    an unsigned message — it must finalize as a permanent failure."""
    from apps.outbound.engine import claim_one, deliver_claim

    user = make_user("dkim_fail_closed")
    domain = eligible_domain(user, "dkim-fail-closed.example.com")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    row_id = resp.json()["data"]["id"]

    def boom(*args, **kwargs):
        from apps.outbound.dkim_sign import DkimSigningError

        raise DkimSigningError("dkimpy blew up")

    monkeypatch.setattr("apps.outbound.engine.sign_message", boom)

    transport_calls = []
    pk, token = claim_one()
    result = deliver_claim(pk, token, transport=lambda **kw: transport_calls.append(kw))
    assert result == "failed"
    assert transport_calls == []  # never reached the transport — nothing sent
    row = OutboundEmail.objects.get(public_id=row_id)
    assert row.status == OutboundEmail.Status.FAILED
    assert row.error_code == "dkim_sign_failed"


def test_dkim_unverified_domain_blocked_before_signing_ever_runs():
    """dkim_status != verified must be caught by the invariant re-check
    BEFORE any signing attempt — covered end-to-end via the enqueue 403,
    proving unverified DKIM domains never even reach the queue."""
    user = make_user("dkim_unverified")
    domain = Domain.objects.create(user=user, name="dkim-unverified.example.com")
    domain.status = Domain.Status.VERIFIED
    domain.dkim_status = Domain.DkimStatus.PENDING_DNS
    domain.save(update_fields=["status", "dkim_status"])

    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "sending_domain_not_eligible"
    assert OutboundEmail.objects.count() == 0


# --- sending_setup provisioning + DKIM TXT verify/recheck 3-strike demote ----


def test_sending_setup_requires_verified_domain():
    from apps.outbound.services import SendingSetupRefused, provision_sending_setup

    user = make_user("setup_unverified")
    domain = Domain.objects.create(user=user, name="setup-unverified.example.com")
    with pytest.raises(SendingSetupRefused):
        provision_sending_setup(domain)


def test_sending_setup_generates_keypair_and_pending_dns():
    from apps.outbound.services import provision_sending_setup

    user = make_user("setup_ok")
    domain = Domain.objects.create(user=user, name="setup-ok.example.com")
    domain.status = Domain.Status.VERIFIED
    domain.save(update_fields=["status"])

    domain = provision_sending_setup(domain)
    assert domain.dkim_status == Domain.DkimStatus.PENDING_DNS
    assert domain.dkim_public_key_txt
    assert bytes(domain.dkim_private_key_enc)


def test_sending_setup_endpoint_returns_txt_to_publish():
    user = make_user("setup_endpoint")
    domain = Domain.objects.create(user=user, name="setup-endpoint.example.com")
    domain.status = Domain.Status.VERIFIED
    domain.save(update_fields=["status"])
    client = auth_client(user)
    resp = client.post(f"/api/v1/domains/{domain.public_id}/sending_setup")
    assert resp.status_code == 200
    assert resp.json()["data"]["dkim_status"] == "pending_dns"
    assert resp.json()["data"]["dkim_public_key_txt"]


def test_dkim_recheck_3_strike_demote(monkeypatch):
    from apps.domains.services import recheck_domain

    user = make_user("dkim_strikes")
    domain = eligible_domain(user, "dkim-strikes.example.com")

    class _FailingResolver:
        def resolve(self, qname, rdtype):
            import dns.exception

            raise dns.exception.DNSException()

    monkeypatch.setattr("apps.domains.services._default_resolver", lambda: _FailingResolver())

    for _ in range(2):
        domain = recheck_domain(domain)
        assert domain.dkim_status == Domain.DkimStatus.VERIFIED  # not demoted yet
    domain = recheck_domain(domain)
    assert domain.dkim_status == Domain.DkimStatus.FAILED
    assert domain.dkim_consecutive_failures == 3


# --- FIX8 (HIGH — detail endpoint no-oracle: foreign vs unknown 404) --------


def test_detail_foreign_obe_vs_unknown_obe_byte_identical_404():
    """Foreign (belongs to a different tenant, real row) vs unknown (never
    existed) obe_ public_ids must produce a byte-identical 404 — both
    status_code AND response.content — so tenant existence/cross-tenant
    ownership can never be inferred from the response."""
    user_a = make_user("detail_404_a")
    user_b = make_user("detail_404_b")
    domain_b = eligible_domain(user_b, "detail-404-b.example.com")

    foreign_row = OutboundEmail.objects.create(
        user=user_b,
        domain=domain_b,
        from_address=f"hello@{domain_b.name}",
        to=["x@example.com"],
        status=OutboundEmail.Status.SENT,
        recipient_count=1,
    )

    client_a = auth_client(user_a)
    foreign_resp = client_a.get(f"/api/v1/outbound_emails/{foreign_row.public_id}")
    unknown_resp = client_a.get("/api/v1/outbound_emails/obe_doesnotexistatall12")

    assert foreign_resp.status_code == unknown_resp.status_code == 404
    assert _error_without_request_id(foreign_resp) == _error_without_request_id(unknown_resp)


# --- FIX9 (MEDIUM — DKIM verify against the STORED public key) -------------


def test_dkim_signed_message_verifies_against_stored_domain_public_key():
    """The signed message must verify against the PUBLIC key STORED on
    domain.dkim_public_key_txt — not a transiently-generated key that
    merely happens to be the matching pair. This proves the key we publish
    for the customer to put in DNS is genuinely the one the signer used.

    Deliberately does NOT use the eligible_domain() test fixture, which
    stubs dkim_public_key_txt as a fake constant unrelated to the real
    generated keypair — using it here would make this test pass for the
    wrong reason (a transient key that happens to also fail dkimpy's own
    parse, silently "passing" only because it FAILS the same way every
    time). This test uses provision_sending_setup, the real production
    call, so dkim_public_key_txt is genuinely the TXT half of the stored
    private key.
    """
    import dkim

    from apps.outbound.services import provision_sending_setup

    user = make_user("dkim_stored_key")
    domain = Domain.objects.create(user=user, name="dkim-stored-key.example.com")
    domain.status = Domain.Status.VERIFIED
    domain.save(update_fields=["status"])
    domain = provision_sending_setup(domain)
    domain.dkim_status = Domain.DkimStatus.VERIFIED
    domain.save(update_fields=["dkim_status"])
    stored_txt_value = domain.dkim_public_key_txt

    from apps.outbound.dkim_sign import sign_message

    raw_message = (
        b"From: sender@dkim-stored-key.example.com\r\n"
        b"To: dest@example.com\r\n"
        b"Subject: dkim stored-key test\r\n"
        b"\r\n"
        b"hello world\r\n"
    )
    signature = sign_message(
        raw_message,
        domain_name=domain.name,
        selector=domain.dkim_selector,
        private_key_enc=bytes(domain.dkim_private_key_enc),
    )
    signed = signature + raw_message

    def fake_dns_lookup(name, timeout=5):
        expected = f"{domain.dkim_selector}._domainkey.{domain.name}.".encode()
        assert name == expected
        # Feed the STORED public key TXT value, not a fresh keypair — this
        # is what proves the stored key corresponds to the signing key.
        return stored_txt_value.encode()

    d = dkim.DKIM(signed)
    result = d.verify(dnsfunc=fake_dns_lookup)
    assert result is True
    assert d.domain == domain.name.encode()


# --- D2 Part A (): VERP MAC on envelope_from ----


def test_engine_envelope_from_is_macd():
    from apps.outbound.engine import claim_one, deliver_claim
    from apps.outbound.verp import verify_verp_localpart

    user = make_user("d2_engine_macd")
    domain = eligible_domain(user, "d2-engine-macd.example.com")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/outbound_emails", send_body(f"hello@{domain.name}"), format="json"
    )
    row_id = resp.json()["data"]["id"]

    captured = {}

    def capture_transport(*, envelope_from, envelope_to, raw_message):
        captured["envelope_from"] = envelope_from

    claim = claim_one()
    pk, token = claim
    result = deliver_claim(pk, token, transport=capture_transport)
    assert result == "sent"

    row = OutboundEmail.objects.get(public_id=row_id)
    local, _, _domain_part = captured["envelope_from"].partition("@")
    assert verify_verp_localpart(local) == row.public_id
    assert len(local) <= 64


def test_verify_uses_compare_digest():
    from unittest import mock

    from apps.outbound.verp import build_verp_localpart, verify_verp_localpart

    localpart = build_verp_localpart("obe_comparedigest1")
    with mock.patch("apps.outbound.verp.hmac.compare_digest", wraps=__import__("hmac").compare_digest) as spy:
        result = verify_verp_localpart(localpart)
    assert result == "obe_comparedigest1"
    assert spy.call_count == 1


def test_verify_verp_localpart_non_ascii_mac_returns_none_never_raises():
    "Public API."
    from apps.outbound.verp import verify_verp_localpart

    arabic_indic_digit_mac = "\u0661" * 20
    assert verify_verp_localpart(f"obe_x.{arabic_indic_digit_mac}") is None


def test_production_settings_flag_off_imports_without_dkim_key():
    """Mirror of test_billing_ls.py's test_27 (the OUTBOUND_DKIM_MASTER_KEY
    2026-09-28 incident regression): ENABLE_OUTBOUND unset -> clean import
    with no OUTBOUND_DKIM_MASTER_KEY in env; ENABLE_OUTBOUND=true with no
    key -> ImproperlyConfigured."""
    import importlib
    import os
    import sys

    from django.core.exceptions import ImproperlyConfigured

    base_required_env = {
        "SECRET_KEY": "test-secret-key",
        "ALLOWED_HOSTS": "example.com",
        "INGEST_SHARED_SECRET": "test-ingest-shared-secret",
        "AUTH_RATE_LIMIT_SECRET": "test-auth-rate-limit-secret",
        "API_KEY_PEPPER": "test-api-key-pepper",
        "WEBHOOK_SIGNING_MASTER_KEY": "test-webhook-signing-master-key",
        "DB_NAME": "test_db",
        "DB_USERNAME": "test_user",
        "DB_PASS": "test_pass",
        "DB_HOST": "localhost",
        "DB_PORT": "5432",
    }
    production_module = "config.settings.production"
    base_module = "config.settings.base"

    def reimport(env):
        old_environ = os.environ.copy()
        os.environ.clear()
        os.environ.update(env)
        for mod_name in (production_module, base_module):
            sys.modules.pop(mod_name, None)
        try:
            return importlib.import_module(production_module)
        finally:
            os.environ.clear()
            os.environ.update(old_environ)
            for mod_name in (production_module, base_module):
                sys.modules.pop(mod_name, None)

    # flag-off: clean import, no OUTBOUND_DKIM_MASTER_KEY required.
    module = reimport(base_required_env)
    assert module.ENABLE_OUTBOUND is False

    # flag-on + missing key -> ImproperlyConfigured.
    with pytest.raises(ImproperlyConfigured):
        reimport({**base_required_env, "ENABLE_OUTBOUND": "true"})
