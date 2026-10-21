import hashlib

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, override_settings
from django.utils import timezone

from apps.inboxes.admin import InboxAdmin, MessageAdmin
from apps.inboxes.models import Inbox, Message

pytestmark = pytest.mark.django_db


def message_values(inbox, dedupe_key="a" * 64):
    raw = b"raw"
    return {
        "inbox": inbox,
        "dedupe_key": dedupe_key,
        "envelope_from": "",
        "envelope_recipient": "valid_key@in.inboxpipe.net",
        "raw_bytes": raw,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }


@override_settings(MAIL_DOMAIN="in.inboxpipe.net")
def test_public_ids_address_and_immutable_routing_key(user):
    inbox = Inbox.objects.create(user=user, routing_key="valid_key")
    message = Message.objects.create(**message_values(inbox))

    assert inbox.public_id.startswith("inbox_")
    assert len(inbox.public_id) <= 32
    assert message.public_id.startswith("msg_")
    assert len(message.public_id) <= 32
    assert inbox.address == "valid_key@in.inboxpipe.net"

    inbox.routing_key = "other_key"
    with pytest.raises(ValidationError, match="immutable"):
        inbox.save()
    message.public_id = "msg_changed"
    with pytest.raises(ValidationError, match="immutable"):
        message.save()


@override_settings(MAIL_DOMAIN="in.inboxpipe.net")
def test_public_id_generation_retries_a_collision(user):
    Inbox.objects.bulk_create(
        [Inbox(public_id="inbox_collision", user=user, routing_key="first_key")]
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        candidates = iter(("inbox_collision", "inbox_available"))
        monkeypatch.setattr("apps.inboxes.models._new_public_id", lambda prefix: next(candidates))
        inbox = Inbox.objects.create(user=user, routing_key="second_key")

    assert inbox.public_id == "inbox_available"


def test_routing_key_validation_and_database_constraints(user):
    invalid = Inbox(user=user, routing_key="UPPER_key")
    with pytest.raises(ValidationError):
        invalid.full_clean()
    with pytest.raises(IntegrityError), transaction.atomic():
        Inbox.objects.create(user=user, routing_key="UPPER_key")


def test_routing_key_is_never_reused_after_soft_delete(user):
    inbox = Inbox.objects.create(user=user, routing_key="never_reuse")
    inbox.deleted_at = timezone.now()
    inbox.is_enabled = False
    inbox.save()

    with pytest.raises(IntegrityError), transaction.atomic():
        Inbox.objects.create(user=user, routing_key="never_reuse")


def test_inbox_and_message_database_constraints(user):
    inbox = Inbox.objects.create(user=user, routing_key="constraint_key")
    Message.objects.create(**message_values(inbox))

    with pytest.raises(IntegrityError), transaction.atomic():
        Message.objects.create(**message_values(inbox))
    with pytest.raises(IntegrityError), transaction.atomic():
        Message.objects.create(**message_values(inbox, dedupe_key="BAD"))


def test_admin_contract_hides_raw_and_disables_message_writes(admin_user):
    inbox_admin = InboxAdmin(Inbox, admin.site)
    message_admin = MessageAdmin(Message, admin.site)
    request = RequestFactory().get("/admin/")
    request.user = admin_user
    inbox = Inbox.objects.create(user=admin_user, routing_key="admin_key")

    assert "routing_key" not in inbox_admin.get_readonly_fields(request, None)
    assert "routing_key" in inbox_admin.get_readonly_fields(request, inbox)
    assert "delete_selected" not in inbox_admin.get_actions(request)
    assert {"raw_bytes", "otp", "detected_otp", "magic_link"} <= set(message_admin.exclude)
    assert "delete_selected" not in message_admin.get_actions(request)
    assert "retry_parsing" in message_admin.get_actions(request)
    assert message_admin.has_add_permission(request) is False
    assert message_admin.has_delete_permission(request) is False
    assert message_admin.has_change_permission(request) is True

    post_request = RequestFactory().post("/admin/")
    post_request.user = admin_user
    assert message_admin.has_change_permission(post_request) is True


def test_admin_get_fields_never_exposes_live_credentials(admin_user):
    # Regression (@c7b20d4): `exclude` alone was a false-green. get_readonly_fields
    # fed every field back through the readonly list, and Django renders readonly
    # fields regardless of `exclude`, so otp/magic_link/links/detected_otp leaked
    # into the change form. Assert on the ACTUAL field list, not the `exclude` tuple.
    message_admin = MessageAdmin(Message, admin.site)
    request = RequestFactory().get("/admin/")
    request.user = admin_user
    inbox = Inbox.objects.create(user=admin_user, routing_key="admin_getfields")
    message = Message.objects.create(**message_values(inbox))

    fields = message_admin.get_fields(request, message)

    secret_fields = {"raw_bytes", "otp", "detected_otp", "magic_link", "links"}
    leaked = secret_fields & set(fields)
    assert not leaked, f"admin change form leaks live credentials: {sorted(leaked)}"
    assert "public_id" in fields
    assert "subject" in fields
