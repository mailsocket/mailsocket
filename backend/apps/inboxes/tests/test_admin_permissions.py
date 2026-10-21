import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware

from apps.inboxes.admin import MessageAdmin
from apps.inboxes.models import Message


@pytest.fixture
def request_factory():
    from django.test import RequestFactory

    return RequestFactory()


def _staff(**perms):
    User = get_user_model()
    user = User.objects.create_user(
        username="staffer", password="x", is_staff=True, is_superuser=False
    )
    return user


@pytest.mark.django_db
def test_staff_without_message_perm_is_denied(request_factory):
    "Public API."
    admin = MessageAdmin(Message, AdminSite())
    user = _staff()
    request = request_factory.get("/admin/inboxes/message/")
    request.user = user

    assert admin.has_change_permission(request) is False
    assert admin.has_view_permission(request) is False
    # retry_parsing requires 'change' -> must be filtered out of actions
    assert "retry_parsing" not in admin.get_actions(request)


@pytest.mark.django_db
def test_staff_with_change_perm_sees_retry_action(request_factory):
    admin = MessageAdmin(Message, AdminSite())
    user = _staff()
    perm = Permission.objects.get(
        content_type__app_label="inboxes", codename="change_message"
    )
    user.user_permissions.add(perm)
    user = get_user_model().objects.get(pk=user.pk)  # refresh perm cache
    request = request_factory.get("/admin/inboxes/message/")
    request.user = user

    assert admin.has_change_permission(request) is True
    assert "retry_parsing" in admin.get_actions(request)


@pytest.mark.django_db
def test_admin_retry_is_the_explicit_failed_to_received_transition(request_factory, user):
    from apps.inboxes.models import Inbox

    inbox = Inbox.objects.create(user=user, routing_key="admin_retry")
    message = Message.objects.create(
        inbox=inbox,
        dedupe_key="a" * 64,
        envelope_recipient="admin_retry@in.inboxpipe.net",
        raw_bytes=b"Subject: retry\r\n\r\nbody",
        raw_sha256="b" * 64,
        size_bytes=22,
        ingest_status=Message.IngestStatus.FAILED,
        parse_attempts=5,
        parse_error="failed",
    )
    request = request_factory.post("/admin/inboxes/message/")
    SessionMiddleware(lambda ignored: None).process_request(request)
    request.session.save()
    request._messages = FallbackStorage(request)

    MessageAdmin(Message, AdminSite()).retry_parsing(request, Message.objects.filter(pk=message.pk))

    message.refresh_from_db()
    assert message.ingest_status == Message.IngestStatus.RECEIVED
    assert message.parse_attempts == 0
    assert message.parse_error == ""
    assert message.next_parse_at is None



# admin disable/soft-delete of an active catch-all target must be refused,



def _admin_request(request_factory, actor):
    request = request_factory.post("/admin/inboxes/inbox/")
    SessionMiddleware(lambda ignored: None).process_request(request)
    request.session.save()
    request._messages = FallbackStorage(request)
    request.user = actor
    return request


@pytest.mark.django_db
def test_admin_disable_of_active_catch_all_target_is_refused(request_factory, user, admin_user):
    from apps.domains.models import Domain
    from apps.inboxes.admin import InboxAdmin
    from apps.inboxes.models import Inbox

    domain = Domain.objects.create(user=user, name="admin-guard-disable.example.com")
    domain.status = Domain.Status.VERIFIED
    domain.save(update_fields=["status"])
    sentinel = Inbox.objects.create(user=user, routing_key="sentinel", domain=domain)
    domain.catch_all = True
    domain.catch_all_inbox = sentinel
    domain.save(update_fields=["catch_all", "catch_all_inbox"])

    request = _admin_request(request_factory, admin_user)
    InboxAdmin(Inbox, AdminSite()).disable(request, Inbox.objects.filter(pk=sentinel.pk))

    sentinel.refresh_from_db()
    assert sentinel.is_enabled is True


@pytest.mark.django_db
def test_admin_soft_delete_of_active_catch_all_target_is_refused(request_factory, user, admin_user):
    from apps.domains.models import Domain
    from apps.inboxes.admin import InboxAdmin
    from apps.inboxes.models import Inbox

    domain = Domain.objects.create(user=user, name="admin-guard-softdelete.example.com")
    domain.status = Domain.Status.VERIFIED
    domain.save(update_fields=["status"])
    sentinel = Inbox.objects.create(user=user, routing_key="sentinel", domain=domain)
    domain.catch_all = True
    domain.catch_all_inbox = sentinel
    domain.save(update_fields=["catch_all", "catch_all_inbox"])

    request = _admin_request(request_factory, admin_user)
    InboxAdmin(Inbox, AdminSite()).soft_delete(request, Inbox.objects.filter(pk=sentinel.pk))

    sentinel.refresh_from_db()
    assert sentinel.is_enabled is True
    assert sentinel.deleted_at is None


@pytest.mark.django_db
def test_admin_delete_model_of_active_catch_all_target_is_refused(request_factory, user, admin_user):
    from apps.domains.models import Domain
    from apps.inboxes.admin import InboxAdmin
    from apps.inboxes.models import Inbox

    domain = Domain.objects.create(user=user, name="admin-guard-deletemodel.example.com")
    domain.status = Domain.Status.VERIFIED
    domain.save(update_fields=["status"])
    sentinel = Inbox.objects.create(user=user, routing_key="sentinel", domain=domain)
    domain.catch_all = True
    domain.catch_all_inbox = sentinel
    domain.save(update_fields=["catch_all", "catch_all_inbox"])

    request = _admin_request(request_factory, admin_user)
    InboxAdmin(Inbox, AdminSite()).delete_model(request, sentinel)

    sentinel.refresh_from_db()
    assert sentinel.is_enabled is True
    assert sentinel.deleted_at is None


@pytest.mark.django_db
def test_admin_account_suspend_does_not_disable_active_catch_all_target(request_factory):
    from django.contrib.auth import get_user_model
    from django.utils import timezone as tz

    from apps.accounts.admin import AccountAdmin
    from apps.accounts.models import Account
    from apps.domains.models import Domain
    from apps.inboxes.models import Inbox

    owner = get_user_model().objects.create_user(
        username="admin_guard_suspend_owner",
        email="admin_guard_suspend_owner@example.test",
        password="password",
    )
    account = Account.objects.create(
        user=owner,
        email_normalized="admin_guard_suspend_owner@example.test",
        email_verified_at=tz.now(),
    )
    domain = Domain.objects.create(user=owner, name="admin-guard-suspend.example.com")
    domain.status = Domain.Status.VERIFIED
    domain.save(update_fields=["status"])
    sentinel = Inbox.objects.create(user=owner, routing_key="sentinel", domain=domain)
    domain.catch_all = True
    domain.catch_all_inbox = sentinel
    domain.save(update_fields=["catch_all", "catch_all_inbox"])

    admin_user = get_user_model().objects.create_superuser(
        username="admin_guard_suspend_admin",
        email="admin_guard_suspend_admin@example.test",
        password="password",
    )
    request = request_factory.post("/admin/accounts/account/")
    SessionMiddleware(lambda ignored: None).process_request(request)
    request.session.save()
    request._messages = FallbackStorage(request)
    request.user = admin_user

    AccountAdmin(Account, AdminSite()).suspend_and_disable_inboxes(
        request, Account.objects.filter(pk=account.pk)
    )

    account.refresh_from_db()
    assert account.is_suspended is True
    
    # so the sentinel is no longer an active target and IS disabled — a
    # suspended tenant's catch-all must not keep receiving mail.
    domain.refresh_from_db()
    assert domain.catch_all is False and domain.catch_all_inbox_id is None
    sentinel.refresh_from_db()
    assert sentinel.is_enabled is False
    assert sentinel.deleted_at is not None
