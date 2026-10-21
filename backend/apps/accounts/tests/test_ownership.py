import contextlib
import hashlib
from io import StringIO
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings

pytestmark = pytest.mark.django_db(transaction=True)

EXPAND = [("accounts", "0019_idempotency_record"), ("inboxes", "0001_initial")]
CONTRACT = [
    ("accounts", "0019_idempotency_record"),
    ("inboxes", "0005_message_usage_counted"),
]


def _migrate_every_app_to_latest():
    """Bring the test DB back to the latest leaf of EVERY app.

    These tests migrate the shared test database backwards (to inboxes 0001 /
    0004). Without this, every test that runs after them in a shuffled order
    sees an old schema (`column "domain_id" ... does not exist`, audit
    2026-10-11 T3-V2: 153 failed + 42 errors with pytest-randomly).
    A test that failed half way can leave rows the forward migrations refuse
    (inboxes 0003 gate: null-owned inboxes), so the tables are emptied first;
    pytest-django's transactional teardown flushes them anyway.
    """
    call_command("flush", interactive=False, inhibit_post_migrate=True, verbosity=0)
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    executor.migrate(leaves)
    remaining = MigrationExecutor(connection).migration_plan(leaves)
    if remaining:
        raise RuntimeError(f"test DB not restored to the latest migrations: {remaining}")


def _restore_or_stop_session(session, nodeid):
    """Restore the schema; if that fails, stop the whole pytest session.

    A failed flush / migrate / verification leaves the shared test DB on a
    partial schema. Every later test would then fail (or pass) for the wrong
    reason, so no further test may run: set session.shouldstop and re-raise.
    """
    try:
        _migrate_every_app_to_latest()
    except BaseException as exc:
        session.shouldstop = (
            f"test DB schema restoration failed after {nodeid}: "
            f"{type(exc).__name__}: {exc}; stopping, later tests would run on a broken schema"
        )
        raise


@pytest.fixture(autouse=True)
def _restore_latest_schema(request):
    # autouse => set up first, torn down LAST (after expanded_schema), and its
    # teardown runs even when the test or another fixture's teardown failed.
    request.getfixturevalue("transactional_db")
    try:
        yield
    finally:
        _restore_or_stop_session(request.session, request.node.nodeid)


class _FakeSession:
    shouldstop = False


@pytest.mark.parametrize("broken", ["flush", "migrate", "verification"])
def test_restoration_failure_stops_the_session(broken):
    # Stubs are scoped to the `with` block (not monkeypatch): the fixture
    # teardown order of monkeypatch vs _restore_latest_schema is not
    # guaranteed, and a stub leaking into the real teardown stops the session.
    def boom(*args, **kwargs):
        raise RuntimeError(f"forced {broken} failure")

    if broken == "flush":
        stubs = [mock.patch(f"{__name__}.call_command", boom)]
    elif broken == "migrate":
        stubs = [mock.patch.object(MigrationExecutor, "migrate", boom)]
    else:
        stubs = [
            mock.patch.object(MigrationExecutor, "migrate", lambda self, targets: None),
            mock.patch.object(
                MigrationExecutor, "migration_plan", lambda self, targets: [("x", False)]
            ),
        ]
    session = _FakeSession()
    with contextlib.ExitStack() as stack:
        for stub in stubs:
            stack.enter_context(stub)
        with pytest.raises(RuntimeError):
            _restore_or_stop_session(session, "some::test")
    assert isinstance(session.shouldstop, str)
    assert "some::test" in session.shouldstop
    assert "schema restoration failed" in session.shouldstop


def test_restoration_success_leaves_the_session_running():
    session = _FakeSession()
    _restore_or_stop_session(session, "some::test")
    assert session.shouldstop is False


@pytest.fixture
def expanded_schema():
    executor = MigrationExecutor(connection)
    executor.migrate(EXPAND)
    yield executor.loader.project_state(EXPAND).apps
    MigrationExecutor(connection).migrate(CONTRACT)
    MigrationExecutor(connection).migrate([
        ("accounts", "0019_idempotency_record"),
        ("inboxes", "0005_message_usage_counted"),
        ("webhooks", "0001_initial"),
    ])


def _create_message(Message, inbox):
    raw = b"Subject: migration\r\n\r\nkept"
    return Message.objects.create(
        inbox_id=inbox.pk,
        dedupe_key="a" * 64,
        envelope_recipient="legacy@in.inboxpipe.net",
        raw_bytes=raw,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
    )


def test_backfill_owner_path_is_idempotent_preserves_messages_and_allows_contract(expanded_schema):
    User = expanded_schema.get_model("auth", "User")
    Inbox = expanded_schema.get_model("inboxes", "Inbox")
    Message = expanded_schema.get_model("inboxes", "Message")
    owner = User.objects.create(username="owner", email="Owner@Example.com")
    inbox = Inbox.objects.create(public_id="inbox_legacy_owner", routing_key="legacy_owner")
    message = _create_message(Message, inbox)

    output = StringIO()
    call_command("backfill_m3_accounts", owner_user_id=str(owner.pk), stdout=output)
    assert "inboxes_assigned=1 null_owned_remaining=0" in output.getvalue()
    assert "inbox_legacy_owner" in output.getvalue()
    assert Inbox.objects.filter(user_id=owner.pk).count() == 1
    assert Message.objects.get(pk=message.pk).inbox_id == inbox.pk

    second = StringIO()
    call_command("backfill_m3_accounts", owner_user_id=str(owner.pk), stdout=second)
    assert "inboxes_assigned=0 null_owned_remaining=0" in second.getvalue()

    MigrationExecutor(connection).migrate(CONTRACT)
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM inboxes_inbox WHERE user_id IS NULL")
        assert cursor.fetchone()[0] == 0


def test_backfill_quarantine_path_creates_inactive_unusable_owner(expanded_schema):
    Inbox = expanded_schema.get_model("inboxes", "Inbox")
    Inbox.objects.create(public_id="inbox_quarantine", routing_key="quarantine")
    output = StringIO()
    call_command(
        "backfill_m3_accounts",
        create_quarantine_owner="Quarantine@Example.com",
        stdout=output,
    )
    user = get_user_model().objects.get(email="quarantine@example.com")
    assert not user.is_active
    assert not user.has_usable_password()
    assert Inbox.objects.get().user_id == user.pk
    assert "inboxes_assigned=1 null_owned_remaining=0" in output.getvalue()


def test_backfill_refuses_missing_owner_and_normalized_collision(expanded_schema):
    User = expanded_schema.get_model("auth", "User")
    with pytest.raises(CommandError, match="does not exist"):
        call_command("backfill_m3_accounts", owner_user_id="999999")

    first = User.objects.create(username="first", email="duplicate@example.com")
    User.objects.create(username="second", email="DUPLICATE@EXAMPLE.COM")
    with pytest.raises(CommandError, match="collision"):
        call_command("backfill_m3_accounts", owner_user_id=str(first.pk))


def test_contract_migration_gate_refuses_null_owner(expanded_schema):
    Inbox = expanded_schema.get_model("inboxes", "Inbox")
    Inbox.objects.create(public_id="inbox_gate_null", routing_key="gate_null")
    with pytest.raises(RuntimeError, match="null_owned_inboxes=1"):
        MigrationExecutor(connection).migrate(CONTRACT)
    Inbox.objects.all().delete()


@override_settings(
    INGEST_SHARED_SECRET="expand-secret",
    INGEST_ALLOWED_CIDRS="172.30.0.0/24",
    MAIL_DOMAIN="in.inboxpipe.net",
)
def test_expand_entrypoint_command_keeps_ownership_contract_explicit(expanded_schema):
    Inbox = expanded_schema.get_model("inboxes", "Inbox")
    Inbox.objects.create(public_id="inbox_expand_live", routing_key="expand_live")

    call_command("migrate_expand", noinput=True, stdout=StringIO())
    executor = MigrationExecutor(connection)
    assert ("inboxes", "0001_initial") in executor.loader.applied_migrations
    assert ("inboxes", "0002_m3a_ownership_backfill") not in executor.loader.applied_migrations

    Inbox.objects.all().delete()


def test_expand_entrypoint_applies_safe_inbox_leaves_after_contract():
    MigrationExecutor(connection).migrate([
        ("accounts", "0019_idempotency_record"),
        ("inboxes", "0004_message_html_body"),
        ("webhooks", "0001_initial"),
    ])

    call_command("migrate_expand", noinput=True, stdout=StringIO())

    applied = MigrationExecutor(connection).loader.applied_migrations
    assert ("inboxes", "0005_message_usage_counted") in applied


def test_register_form_is_email_only():
    """Signup UX 2026-10: register asks for the email only; the password is
    chosen once, on the token-bound verification form."""
    from apps.accounts.forms import RegisterForm
    form = RegisterForm()
    assert list(form.fields) == ["email"]
    html = form.as_p()
    assert 'type="password"' not in html
    assert "We'll email you a link to set your password." in html
