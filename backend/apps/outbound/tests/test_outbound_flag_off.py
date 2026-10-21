"Public API."
import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient as RestAPIClient

from apps.accounts.models import Account, AccountPlan
from apps.accounts.services import create_api_key
from apps.domains.models import Domain

pytestmark = pytest.mark.django_db(transaction=True)


def _user_and_client(settings):
    assert settings.ENABLE_OUTBOUND is False
    user = get_user_model().objects.create_user(
        username="flagoff", email="flagoff@example.test", password="password"
    )
    Account.objects.create(user=user, email_normalized="flagoff@example.test", email_verified_at=timezone.now())
    AccountPlan.objects.create(user=user, plan=AccountPlan.Plan.BUSINESS)
    _, token = create_api_key(user, "flag-off-test")
    client = RestAPIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return user, client


def test_flag_off_by_default():
    from django.conf import settings

    assert settings.ENABLE_OUTBOUND is False


def test_flag_off_list_create_404(settings):
    user, client = _user_and_client(settings)
    assert client.get("/api/v1/outbound_emails").status_code == 404
    assert client.post(
        "/api/v1/outbound_emails",
        {"from_address": "hello@example.com", "to": ["x@example.com"]},
        format="json",
    ).status_code == 404


def test_flag_off_detail_404(settings):
    user, client = _user_and_client(settings)
    assert client.get("/api/v1/outbound_emails/obe_doesnotexist").status_code == 404


def test_flag_off_sending_setup_404(settings):
    user, client = _user_and_client(settings)
    domain = Domain.objects.create(user=user, name="flagoff.example.com")
    domain.status = Domain.Status.VERIFIED
    domain.save(update_fields=["status"])
    assert client.post(f"/api/v1/domains/{domain.public_id}/sending_setup").status_code == 404


def test_flag_off_allowlist_surface_404(settings):
    "Public API."
    user, client = _user_and_client(settings)
    assert client.get("/api/v1/outbound_allowlist").status_code == 404
    assert client.post("/api/v1/outbound_allowlist", {"email": "x@example.test"}, format="json").status_code == 404
    assert client.delete("/api/v1/outbound_allowlist/apr_doesnotexist000000000000").status_code == 404
    assert client.post("/api/v1/outbound_allowlist/mode", {"enabled": True}, format="json").status_code == 404


def test_flag_off_drain_is_a_total_noop():
    from apps.outbound.engine import drain

    result = drain(transport=lambda **kw: None)
    assert result.claimed == 0
    assert result.sent == 0


def test_flag_off_bounce_recipient_ingested_as_ordinary_unrouted_mail():
    """CODE FIX 0 (gate R2): with ENABLE_OUTBOUND off, an obe_ localpart on
    OUTBOUND_BOUNCE_DOMAIN must NOT be treated as a bounce at all — no
    BounceEvent row, no record_hard_bounce call — it falls through to the
    ordinary resolve_inbox path exactly like it would before CODE FIX 0
    existed (and 404s there, since no such Inbox/domain is configured),
    proving the new recognition branch is genuinely inert, not just
    unreachable because nothing sends to that address in this environment.
    """
    from apps.ingest.services import IngestError, store_message
    from apps.outbound.models import BounceEvent

    class _Headers(dict):
        def get(self, key, default=None):
            return super().get(key, default)

    raw = (
        b"From: mailer-daemon@bounces.inboxpipe.net\r\n"
        b"To: obe_flagoffbounce01@bounces.inboxpipe.net\r\n"
        b"Subject: Undelivered\r\n"
        b"Content-Type: multipart/report; report-type=delivery-status\r\n"
        b"\r\n"
        b"Status: 5.1.1\r\nAction: failed\r\n"
    )
    headers = _Headers(
        {
            "X-Envelope-To": "obe_flagoffbounce01@bounces.inboxpipe.net",
            "X-Envelope-From": "mailer-daemon@bounces.inboxpipe.net",
        }
    )
    with pytest.raises(IngestError) as exc_info:
        store_message(raw_bytes=raw, headers=headers)
    assert exc_info.value.status_code == 404
    assert BounceEvent.objects.count() == 0
