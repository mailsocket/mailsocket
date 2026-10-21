"Public API."
import re
from pathlib import Path

import psycopg2
import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan
from apps.domains.models import Domain
from apps.inboxes.models import Inbox, Message
from apps.ingest.services import (
    IngestError,
    parse_envelope_recipient,
    resolve_inbox,
    store_message,
)

pytestmark = pytest.mark.django_db(transaction=True)

SHARED_DOMAIN = "in.inboxpipe.net"

REPO_ROOT = Path(__file__).resolve().parents[4]
RECIPIENT_MAP_TEMPLATE = REPO_ROOT / "deploy" / "postfix" / "recipient-map.cf.template"
POSTFIX_ENTRYPOINT = REPO_ROOT / "deploy" / "postfix" / "entrypoint.sh"


def make_user(name):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user, plan=AccountPlan.Plan.BUSINESS)
    return user


def make_domain(user, name, *, status=Domain.Status.VERIFIED):
    domain = Domain.objects.create(user=user, name=name)
    if status != Domain.Status.PENDING_DNS:
        domain.status = status
        domain.save(update_fields=["status"])
    return domain


# --- resolve_inbox composite -------------------------------------------------


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
def test_resolve_inbox_shared_resolves_for_null_domain_inbox():
    user = make_user("rb_shared_a")
    inbox = Inbox.objects.create(user=user, routing_key="sharedkey")
    resolved = resolve_inbox("sharedkey", SHARED_DOMAIN)
    assert resolved.pk == inbox.pk


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
def test_resolve_inbox_custom_resolves_for_verified_domain():
    user = make_user("rb_custom_a")
    domain = make_domain(user, "custom-rb-a.example.com")
    inbox = Inbox.objects.create(user=user, routing_key="customkey", domain=domain)
    resolved = resolve_inbox("customkey", "custom-rb-a.example.com")
    assert resolved.pk == inbox.pk


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
def test_resolve_inbox_same_routing_key_shared_vs_custom_resolves_correct_one():
    """The exact risk #6 scenario: SAME routing_key on shared and custom
    domains must resolve to the RIGHT inbox — no MultipleObjectsReturned."""
    user = make_user("rb_samekey_a")
    domain = make_domain(user, "samekey-a.example.com")
    shared_inbox = Inbox.objects.create(user=user, routing_key="dupkey")
    custom_inbox = Inbox.objects.create(user=user, routing_key="dupkey", domain=domain)

    resolved_shared = resolve_inbox("dupkey", SHARED_DOMAIN)
    resolved_custom = resolve_inbox("dupkey", "samekey-a.example.com")

    assert resolved_shared.pk == shared_inbox.pk
    assert resolved_custom.pk == custom_inbox.pk
    assert resolved_shared.pk != resolved_custom.pk


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
def test_resolve_inbox_foreign_domain_is_404_not_leaked():
    """Inbox bound to domain X; resolve with domain Y -> 404, never 500,
    never a cross-tenant match even though the routing_key exists."""
    user_a = make_user("rb_foreign_a")
    user_b = make_user("rb_foreign_b")
    domain_a = make_domain(user_a, "foreign-a.example.com")
    make_domain(user_b, "foreign-b.example.com")
    Inbox.objects.create(user=user_a, routing_key="foreignkey", domain=domain_a)

    with pytest.raises(IngestError) as exc_info:
        resolve_inbox("foreignkey", "foreign-b.example.com")
    assert exc_info.value.status_code == 404


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
@pytest.mark.parametrize(
    "status", [Domain.Status.PENDING_DNS, Domain.Status.FAILED, Domain.Status.DISABLED]
)
def test_resolve_inbox_unverified_or_disabled_domain_fails_closed(status):
    user = make_user(f"rb_unverified_{status}")
    domain = make_domain(user, f"unverified-{status}.example.com", status=status)
    Inbox.objects.create(user=user, routing_key="pendingkey", domain=domain)

    with pytest.raises(IngestError) as exc_info:
        resolve_inbox("pendingkey", f"unverified-{status}.example.com")
    assert exc_info.value.status_code in (404, 422)


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
def test_resolve_inbox_unknown_domain_is_404():
    with pytest.raises(IngestError) as exc_info:
        resolve_inbox("anykey", "totally-unknown.example.com")
    assert exc_info.value.status_code == 404


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
def test_resolve_inbox_disabled_custom_inbox_is_422():
    user = make_user("rb_disabled_inbox")
    domain = make_domain(user, "disabled-inbox.example.com")
    Inbox.objects.create(
        user=user, routing_key="offkey", domain=domain, is_enabled=False
    )
    with pytest.raises(IngestError) as exc_info:
        resolve_inbox("offkey", "disabled-inbox.example.com")
    assert exc_info.value.status_code == 422


# --- parse_envelope_recipient -------------------------------------------------


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
def test_parse_envelope_recipient_accepts_custom_domain():
    recipient, localpart, domain = parse_envelope_recipient("mykey@custom.example.com")
    assert recipient == "mykey@custom.example.com"
    assert localpart == "mykey"
    assert domain == "custom.example.com"


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
def test_parse_envelope_recipient_accepts_shared_domain_unchanged():
    recipient, localpart, domain = parse_envelope_recipient("mykey@in.inboxpipe.net")
    assert recipient == "mykey@in.inboxpipe.net"
    assert localpart == "mykey"
    assert domain == "in.inboxpipe.net"


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
@pytest.mark.parametrize(
    "value",
    [
        "not-an-address",
        "mykey@custom.example.com\r\nX-Evil: yes",  # control char / header inject
        "Display <mykey@custom.example.com>",  # malformed
        "mykey@custom.example.com,other@custom.example.com",  # multi-recipient
        "mykey@custom.example.com,other@x.test",  # multi-@ variant
        "@custom.example.com",  # empty localpart
        "mykey@",  # empty domain
        "mykey@.custom.example.com",  # leading dot domain
        "mykey@custom.example.cóm",  # non-ascii
    ],
)
def test_parse_envelope_recipient_still_rejects_malformed(value):
    with pytest.raises(IngestError) as exc_info:
        parse_envelope_recipient(value)
    assert exc_info.value.status_code == 400


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
@pytest.mark.parametrize("value", ["MyKey@custom.example.com", "mykey@Custom.example.com"])
def test_parse_envelope_recipient_folds_case_instead_of_rejecting(value):
    
    # accepts RCPT case-insensitively; ingest folds to the canonical form.
    assert parse_envelope_recipient(value) == (
        "mykey@custom.example.com",
        "mykey",
        "custom.example.com",
    )


# --- store_message end-to-end -------------------------------------------------

RAW = (
    b"From: Sender <sender@example.test>\r\n"
    b"To: visible@example.test\r\n"
    b"Message-ID: <byod-case@example.test>\r\n"
    b"Subject: byod stage b\r\n"
    b"\r\n"
    b"body\r\n"
)


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
def test_store_message_routes_custom_domain_inbox_like_shared():
    user = make_user("store_custom_a")
    domain = make_domain(user, "store-custom-a.example.com")
    inbox = Inbox.objects.create(user=user, routing_key="storekey", domain=domain)

    result = store_message(
        raw_bytes=RAW,
        headers={
            "X-Envelope-To": "storekey@store-custom-a.example.com",
            "X-Envelope-From": "bounce@example.test",
        },
    )
    assert result.deduplicated is False
    assert result.message.inbox_id == inbox.pk
    assert bytes(result.message.raw_bytes) == RAW
    assert result.message.envelope_recipient == "storekey@store-custom-a.example.com"


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
def test_store_message_rejects_foreign_domain():
    user_a = make_user("store_foreign_a")
    user_b = make_user("store_foreign_b")
    domain_a = make_domain(user_a, "store-foreign-a.example.com")
    make_domain(user_b, "store-foreign-b.example.com")
    Inbox.objects.create(user=user_a, routing_key="storeforeignkey", domain=domain_a)

    with pytest.raises(IngestError) as exc_info:
        store_message(
            raw_bytes=RAW,
            headers={
                "X-Envelope-To": "storeforeignkey@store-foreign-b.example.com",
                "X-Envelope-From": "bounce@example.test",
            },
        )
    assert exc_info.value.status_code == 404
    assert Message.objects.count() == 0


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
def test_store_message_rejects_unverified_domain():
    user = make_user("store_unverified_a")
    domain = make_domain(user, "store-unverified-a.example.com", status=Domain.Status.PENDING_DNS)
    Inbox.objects.create(user=user, routing_key="storependingkey", domain=domain)

    with pytest.raises(IngestError) as exc_info:
        store_message(
            raw_bytes=RAW,
            headers={
                "X-Envelope-To": "storependingkey@store-unverified-a.example.com",
                "X-Envelope-From": "bounce@example.test",
            },
        )
    assert exc_info.value.status_code == 404
    assert Message.objects.count() == 0


# --- recipient-map.cf SQL correctness (real Postgres, exact deployed query) -
#

# from the same template file Postfix reads in production, and convert ONLY
# the Postfix map placeholders (%u/%d/%s) to psycopg bound params. This means
# a future edit to the template's WHERE clause is picked up here automatically
# -- if it silently weakens the tenant-isolation gate, these tests break
# instead of staying green against a stale hand-copy.


def _load_recipient_map_query_from_template() -> str:
    """Parse `query = ...` out of deploy/postfix/recipient-map.cf.template and
    convert the Postfix pgsql-map placeholders ('%u', '%d', '%s') to psycopg
    bound-parameter placeholders (%(u)s, %(d)s, %(s)s). Postfix always quotes
    its placeholders as SQL string literals ('%u'), so we look for the quoted
    forms specifically -- this is not a blind %u->%(u)s text replace, it only
    touches the placeholder tokens Postfix itself would substitute into.
    """
    template_text = RECIPIENT_MAP_TEMPLATE.read_text()
    match = re.search(r"^query\s*=\s*(.+)$", template_text, re.MULTILINE)
    assert match, f"no 'query = ...' line found in {RECIPIENT_MAP_TEMPLATE}"
    query = match.group(1).strip()
    for placeholder in ("u", "d", "s"):
        query = query.replace(f"'%{placeholder}'", f"%({placeholder})s")
    # Fail loudly if an un-converted Postfix placeholder slipped through --
    # otherwise a template change using a form we don't handle here would
    # silently turn every "acceptance" assertion into a syntax error caught by
    # nothing but a stack trace, or worse, an always-empty result set.
    assert "%u" not in query and "%d" not in query and "%s" not in query, (
        "unconverted Postfix placeholder remains in derived query: " + query
    )
    return query


RECIPIENT_MAP_SQL = _load_recipient_map_query_from_template()


def test_recipient_map_template_query_matches_entrypoint_emitted_query():
    "Public API."
    entrypoint_text = POSTFIX_ENTRYPOINT.read_text()
    # The recipient-map printf is the one whose query mentions inboxes_inbox
    # (entrypoint.sh also emits relay-domains.cf / transport.cf queries from
    # neighbouring printf blocks -- we want specifically the recipient-map one).
    printf_match = re.search(
        r'printf \'%s\\n\' "(query = [^\n]*inboxes_inbox[^\n]*)"',
        entrypoint_text,
    )
    assert printf_match, (
        "could not find the recipient-map query printf in "
        f"{POSTFIX_ENTRYPOINT} -- entrypoint.sh's emission format changed; "
        "update this test's regex alongside it"
    )
    entrypoint_query_line = printf_match.group(1)

    template_text = RECIPIENT_MAP_TEMPLATE.read_text()
    template_match = re.search(r"^(query\s*=\s*.+)$", template_text, re.MULTILINE)
    assert template_match
    template_query_line = template_match.group(1).strip()

    assert entrypoint_query_line == template_query_line, (
        "deploy/postfix/entrypoint.sh emits a DIFFERENT recipient-map query "
        "than deploy/postfix/recipient-map.cf.template documents -- template "
        "and runtime have drifted apart"
    )


def _run_recipient_map_sql(routing_key: str, domain: str) -> list:
    with connection.cursor() as cursor:
        cursor.execute(RECIPIENT_MAP_SQL, {"u": routing_key, "d": domain})
        return cursor.fetchall()



# material predicate (routing_key match, is_enabled, deleted_at, the
# domain/status branch) would flip at least one assertion here from green to
# red. Each test's docstring states which predicate its decoy proves.


def test_recipient_map_sql_accepts_verified_custom_bound_inbox():
    """Decoys: a same-domain inbox with a DIFFERENT routing_key (proves the
    routing_key predicate is load-bearing -- without it this query would
    return 2 rows, not 1), an is_enabled=False decoy (proves is_enabled), and
    a soft-deleted decoy (proves deleted_at). Also proves a non-existent key
    on the same domain, with decoys present, returns 0 -- not a false
    positive from a decoy match."""
    user = make_user("sql_accept_a")
    domain = make_domain(user, "sql-accept-a.example.com")
    Inbox.objects.create(user=user, routing_key="sqlkey", domain=domain)
    Inbox.objects.create(user=user, routing_key="sqlkey-decoy", domain=domain)
    Inbox.objects.create(
        user=user, routing_key="sqlkey-disabled-decoy", domain=domain, is_enabled=False
    )
    deleted_decoy = Inbox.objects.create(
        user=user, routing_key="sqlkey-deleted-decoy", domain=domain
    )
    Inbox.objects.filter(pk=deleted_decoy.pk).update(deleted_at=timezone.now())

    rows = _run_recipient_map_sql("sqlkey", "sql-accept-a.example.com")
    assert [r[0] for r in rows] == ["sqlkey"]
    assert len(_run_recipient_map_sql("sqlkey-nonexistent", "sql-accept-a.example.com")) == 0


def test_recipient_map_sql_accepts_shared_unchanged():
    """Regression guard: NULL-domain inbox + '%d'=in.inboxpipe.net -> 1 row,
    exactly as the legacy pre-Stage-B query. Decoys: a different-routing_key
    shared inbox (routing_key predicate), a disabled shared inbox
    (is_enabled), a soft-deleted shared inbox (deleted_at)."""
    user = make_user("sql_shared_a")
    Inbox.objects.create(user=user, routing_key="sqlsharedkey")
    Inbox.objects.create(user=user, routing_key="sqlsharedkey-decoy")
    Inbox.objects.create(user=user, routing_key="sqlsharedkey-off", is_enabled=False)
    deleted_decoy = Inbox.objects.create(user=user, routing_key="sqlsharedkey-deleted")
    Inbox.objects.filter(pk=deleted_decoy.pk).update(deleted_at=timezone.now())

    rows = _run_recipient_map_sql("sqlsharedkey", SHARED_DOMAIN)
    assert [r[0] for r in rows] == ["sqlsharedkey"]


def test_recipient_map_sql_rejects_disabled_inbox():
    """Direct proof the is_enabled predicate is load-bearing: an otherwise
    fully-matching (verified domain, correct routing_key, not deleted) but
    is_enabled=False inbox must return 0 rows. Decoy: a sibling enabled
    inbox on the same domain with a different routing_key stays out of the
    result regardless, proving the 0-row result isn't just an empty table."""
    user = make_user("sql_inbox_disabled_a")
    domain = make_domain(user, "sql-inbox-disabled-a.example.com")
    Inbox.objects.create(
        user=user, routing_key="sqlinboxoffkey", domain=domain, is_enabled=False
    )
    Inbox.objects.create(user=user, routing_key="sqlinboxoffkey-decoy", domain=domain)
    assert len(_run_recipient_map_sql("sqlinboxoffkey", "sql-inbox-disabled-a.example.com")) == 0


def test_recipient_map_sql_rejects_soft_deleted_inbox():
    """Direct proof the deleted_at predicate is load-bearing: an otherwise
    fully-matching, enabled inbox that has been soft-deleted must return 0
    rows. Decoy: a sibling non-deleted inbox on the same domain with a
    different routing_key, proving the 0-row result isn't just an empty
    table."""
    user = make_user("sql_inbox_deleted_a")
    domain = make_domain(user, "sql-inbox-deleted-a.example.com")
    target = Inbox.objects.create(
        user=user, routing_key="sqlinboxdelkey", domain=domain
    )
    Inbox.objects.filter(pk=target.pk).update(deleted_at=timezone.now())
    Inbox.objects.create(user=user, routing_key="sqlinboxdelkey-decoy", domain=domain)
    assert len(_run_recipient_map_sql("sqlinboxdelkey", "sql-inbox-deleted-a.example.com")) == 0


def test_recipient_map_sql_rejects_unverified_domain():
    """Decoys: an enabled, non-deleted, correctly-keyed inbox on this SAME
    domain -- if the status='verified' predicate (or the domain/status
    branch entirely) were dropped, this would return 1 row instead of 0."""
    user = make_user("sql_unverified_a")
    domain = make_domain(user, "sql-unverified-a.example.com", status=Domain.Status.PENDING_DNS)
    Inbox.objects.create(user=user, routing_key="sqlpendingkey", domain=domain)
    assert len(_run_recipient_map_sql("sqlpendingkey", "sql-unverified-a.example.com")) == 0


def test_recipient_map_sql_rejects_disabled_domain():
    """Decoys: same as unverified -- an otherwise-matching, enabled,
    non-deleted inbox proves the domain status='verified' predicate is what
    is rejecting this row, not an unrelated is_enabled/deleted_at mismatch."""
    user = make_user("sql_disabled_a")
    domain = make_domain(user, "sql-disabled-a.example.com", status=Domain.Status.DISABLED)
    Inbox.objects.create(user=user, routing_key="sqldisabledkey", domain=domain)
    assert len(_run_recipient_map_sql("sqldisabledkey", "sql-disabled-a.example.com")) == 0


def test_recipient_map_sql_rejects_foreign_domain():
    """Decoys: the target inbox is enabled and not deleted (isolates the
    tenant-isolation predicate: d.name = %(d)s AND i.domain_id = d.id), plus
    a same-domain-as-queried decoy inbox with a DIFFERENT routing_key owned
    by user_b, proving the routing_key predicate still gates even when the
    domain half of the WHERE would otherwise match a sibling row."""
    user_a = make_user("sql_foreign_a")
    user_b = make_user("sql_foreign_b")
    domain_a = make_domain(user_a, "sql-foreign-a.example.com")
    domain_b = make_domain(user_b, "sql-foreign-b.example.com")
    Inbox.objects.create(user=user_a, routing_key="sqlforeignkey", domain=domain_a)
    Inbox.objects.create(user=user_b, routing_key="sqlforeignkey-decoy", domain=domain_b)
    assert len(_run_recipient_map_sql("sqlforeignkey", "sql-foreign-b.example.com")) == 0


def test_recipient_map_sql_rejects_shared_key_on_custom_domain():
    """A shared (NULL-domain) inbox's routing_key queried with a '%d' that is
    a verified custom domain (owned by someone else, or nobody) -> 0 rows —
    the domain_id IS NULL branch only matches when '%d' is the shared apex.
    Decoy: the custom domain also has its OWN enabled, non-deleted inbox
    bound with a DIFFERENT routing_key, proving this isn't accidentally
    matching on domain alone."""
    user = make_user("sql_cross_a")
    other_user = make_user("sql_cross_a_domain_owner")
    other_domain = make_domain(other_user, "sql-cross-a.example.com")
    Inbox.objects.create(user=user, routing_key="sqlcrosskey")
    Inbox.objects.create(
        user=other_user, routing_key="sqlcrosskey-decoy", domain=other_domain
    )
    assert len(_run_recipient_map_sql("sqlcrosskey", "sql-cross-a.example.com")) == 0


def test_recipient_map_sql_rejects_custom_key_on_shared_domain():
    """A custom-domain-bound inbox's routing_key queried with
    '%d'=in.inboxpipe.net -> 0 rows (domain_id IS NOT NULL fails the shared
    branch; the custom branch requires d.name='%d' which is 'in.inboxpipe.net'
    and no Domain row is ever named that). Decoy: a shared-domain inbox with a
    DIFFERENT routing_key exists too, proving the 0-row result isn't a
    coincidence of an otherwise-empty shared namespace."""
    user = make_user("sql_cross_b")
    domain = make_domain(user, "sql-cross-b.example.com")
    Inbox.objects.create(user=user, routing_key="sqlcrosskey2", domain=domain)
    Inbox.objects.create(user=user, routing_key="sqlcrosskey2-decoy")
    assert len(_run_recipient_map_sql("sqlcrosskey2", SHARED_DOMAIN)) == 0



#

# provisioned reader role and showing (a) it succeeds for the granted
# columns and (b) it raises a permission error for an ungranted column. This
# proves the grant is both SUFFICIENT (the routing query works) and MINIMAL
# (nothing beyond the documented columns is readable).

LEAST_PRIV_ROLE = "test_postfix_reader_role_stageb"
LEAST_PRIV_PASSWORD = "test-only-not-a-real-secret"  # noqa: S105 -- disposable test-DB role


def _db_connection_kwargs():
    settings_dict = connection.settings_dict
    return {
        "dbname": settings_dict["NAME"],
        "user": settings_dict["USER"],
        "password": settings_dict["PASSWORD"],
        "host": settings_dict["HOST"],
        "port": settings_dict["PORT"],
    }


@pytest.fixture
def least_priv_reader():
    """Provision the exact least-privilege reader role against the CURRENT
    test database using raw psycopg2 (autocommit, own connection -- Django's
    transactional test wrapper would roll back GRANT/REVOKE DDL run through
    `connection`, and the role must be visible from a brand-new connection
    that authenticates AS that role, which the Django test transaction can't
    give us anyway). Mirrors deploy/provision-postfix-role.py's grants
    exactly: SELECT on (routing_key, is_enabled, deleted_at, domain_id, id) on
    inboxes_inbox and (id, name, status, catch_all, catch_all_inbox_id) on
    domains_domain, nothing else.
    """
    admin_conn = psycopg2.connect(**_db_connection_kwargs())
    admin_conn.autocommit = True
    try:
        with admin_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (LEAST_PRIV_ROLE,))
            if cur.fetchone() is None:
                cur.execute(
                    f"CREATE ROLE {LEAST_PRIV_ROLE} LOGIN PASSWORD %s "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION",
                    (LEAST_PRIV_PASSWORD,),
                )
            else:
                cur.execute(
                    f"ALTER ROLE {LEAST_PRIV_ROLE} LOGIN PASSWORD %s "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION",
                    (LEAST_PRIV_PASSWORD,),
                )
            dbname = _db_connection_kwargs()["dbname"]
            cur.execute(f'REVOKE ALL PRIVILEGES ON DATABASE "{dbname}" FROM {LEAST_PRIV_ROLE}')
            cur.execute(f'GRANT CONNECT ON DATABASE "{dbname}" TO {LEAST_PRIV_ROLE}')
            cur.execute(f"REVOKE ALL PRIVILEGES ON SCHEMA public FROM {LEAST_PRIV_ROLE}")
            cur.execute(f"GRANT USAGE ON SCHEMA public TO {LEAST_PRIV_ROLE}")
            cur.execute(
                f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {LEAST_PRIV_ROLE}"
            )
            cur.execute(
                f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {LEAST_PRIV_ROLE}"
            )
            cur.execute(
                "GRANT SELECT (routing_key, is_enabled, deleted_at, domain_id, id) "
                f"ON TABLE inboxes_inbox TO {LEAST_PRIV_ROLE}"
            )
            cur.execute(
                "GRANT SELECT (id, name, status, catch_all, catch_all_inbox_id) "
                f"ON TABLE domains_domain TO {LEAST_PRIV_ROLE}"
            )
        yield LEAST_PRIV_ROLE
    finally:
        admin_conn.close()


def test_recipient_map_sql_runs_as_least_priv_reader_role(least_priv_reader):
    "Public API."
    user = make_user("sql_least_priv_a")
    domain = make_domain(user, "sql-least-priv-a.example.com")
    Inbox.objects.create(user=user, routing_key="leastprivkey", domain=domain)

    reader_kwargs = dict(_db_connection_kwargs())
    reader_kwargs["user"] = least_priv_reader
    reader_kwargs["password"] = LEAST_PRIV_PASSWORD
    reader_conn = psycopg2.connect(**reader_kwargs)
    try:
        with reader_conn.cursor() as cur:
            cur.execute(
                RECIPIENT_MAP_SQL,
                {"u": "leastprivkey", "d": "sql-least-priv-a.example.com"},
            )
            assert [r[0] for r in cur.fetchall()] == ["leastprivkey"]
    finally:
        reader_conn.close()


@pytest.mark.parametrize(
    "forbidden_query",
    [
        "SELECT user_id FROM inboxes_inbox LIMIT 1",
        "SELECT verification_token FROM domains_domain LIMIT 1",
    ],
)
def test_recipient_map_sql_least_priv_reader_cannot_read_ungranted_columns(
    least_priv_reader, forbidden_query
):
    "Public API."
    reader_kwargs = dict(_db_connection_kwargs())
    reader_kwargs["user"] = least_priv_reader
    reader_kwargs["password"] = LEAST_PRIV_PASSWORD
    reader_conn = psycopg2.connect(**reader_kwargs)
    try:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            with reader_conn.cursor() as cur:
                cur.execute(forbidden_query)
    finally:
        reader_conn.close()
