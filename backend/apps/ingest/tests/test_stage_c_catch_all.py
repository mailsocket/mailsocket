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
from apps.inboxes.models import Inbox

pytestmark = pytest.mark.django_db(transaction=True)

SHARED_DOMAIN = "in.inboxpipe.net"

REPO_ROOT = Path(__file__).resolve().parents[4]
RECIPIENT_MAP_TEMPLATE = REPO_ROOT / "deploy" / "postfix" / "recipient-map.cf.template"


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


def make_catch_all_domain(user, name, *, sentinel_routing_key="sentinel", status=Domain.Status.VERIFIED):
    domain = make_domain(user, name, status=status)
    sentinel = Inbox.objects.create(user=user, routing_key=sentinel_routing_key, domain=domain)
    domain.catch_all = True
    domain.catch_all_inbox = sentinel
    domain.save(update_fields=["catch_all", "catch_all_inbox"])
    return domain, sentinel


def _load_recipient_map_query_from_template() -> str:
    template_text = RECIPIENT_MAP_TEMPLATE.read_text()
    match = re.search(r"^query\s*=\s*(.+)$", template_text, re.MULTILINE)
    assert match, f"no 'query = ...' line found in {RECIPIENT_MAP_TEMPLATE}"
    query = match.group(1).strip()
    for placeholder in ("u", "d", "s"):
        query = query.replace(f"'%{placeholder}'", f"%({placeholder})s")
    assert "%u" not in query and "%d" not in query and "%s" not in query, (
        "unconverted Postfix placeholder remains in derived query: " + query
    )
    return query


RECIPIENT_MAP_SQL = _load_recipient_map_query_from_template()


def _run_recipient_map_sql(routing_key: str, domain: str) -> list:
    with connection.cursor() as cursor:
        cursor.execute(RECIPIENT_MAP_SQL, {"u": routing_key, "d": domain})
        return cursor.fetchall()


# --- #1 P0: hostile raw-SQL row named 'in.inboxpipe.net' ---------------------





def _extract_leg2_regex_literal_from_template() -> str:
    """Mechanically pull the `'%u' ~ '...'` regex literal out of the leg2
    SQL in the raw template text (NOT the substituted RECIPIENT_MAP_SQL,
    which has already had '%u'/'%d' rewritten to psycopg2 placeholders — we
    want the literal as Postfix/Postgres actually sees it)."""
    template_text = RECIPIENT_MAP_TEMPLATE.read_text()
    match = re.search(r"'%u'\s*~\s*'([^']+)'", template_text)
    assert match, (
        f"could not find the `'%u' ~ '...'` regex literal in {RECIPIENT_MAP_TEMPLATE}"
    )
    return match.group(1)


def test_leg2_sql_regex_literal_equals_catchall_localpart_pattern():
    """The single most important drift guard for TEST FIX 5: the Postfix
    RCPT-accept regex (SQL, leg2) and the Python ingest-store regex
    (CATCHALL_LOCALPART_PATTERN) MUST be the exact same string, or a
    localpart can be accepted by one stage and rejected by the other."""
    from apps.ingest.services import CATCHALL_LOCALPART_PATTERN

    sql_regex_literal = _extract_leg2_regex_literal_from_template()
    assert sql_regex_literal == CATCHALL_LOCALPART_PATTERN, (
        f"leg2 SQL regex {sql_regex_literal!r} != "
        f"CATCHALL_LOCALPART_PATTERN {CATCHALL_LOCALPART_PATTERN!r} — "
        "RCPT-accept (SQL) and ingest-store (Python) have drifted apart."
    )


@pytest.mark.parametrize(
    "localpart",
    [
        "",  # empty
        "a",  # 1-char
        "a" * 64,  # 64-char (max allowed: 1 + {0,63} = 64 total)
        "a" * 65,  # 65-char (over the limit)
        ".leadingdot",  # leading dot (first char must be [a-z0-9])
        "UPPERCASE",  # uppercase (regex is lowercase-only)
        "quo'te",  # quote
        "a b",  # space
        "has%percent",  # percent
    ],
)
def test_leg2_sql_regex_boundary_table_matches_python_regex(localpart):
    """Boundary table: SQL-match must equal Python-match for every case."""
    from apps.ingest.services import CATCHALL_LOCALPART_RE

    sql_regex_literal = _extract_leg2_regex_literal_from_template()
    sql_match = re.fullmatch(sql_regex_literal, localpart) is not None
    python_match = CATCHALL_LOCALPART_RE.fullmatch(localpart) is not None
    assert sql_match == python_match, (
        f"localpart={localpart!r}: SQL regex match={sql_match} but Python "
        f"regex match={python_match} — SQL/Python regex have drifted apart "
        f"for this input."
    )


def test_recipient_map_sql_p0_hostile_shared_domain_row_returns_zero_rows():
    """THE P0 TEST. Force-insert a domains_domain row named 'in.inboxpipe.net'
    directly via raw SQL — bypassing Domain.save()'s reserved-apex Python
    validation entirely (Domain.objects.create() would refuse this; raw SQL
    does not) — with status='verified', catch_all=true, and a LIVE sentinel
    inbox already bound to it (also inserted via raw SQL, so the ORM's own
    guards are fully out of the picture). Assert the query still returns
    ZERO rows for ANY localpart: the SQL-level contradiction
    `dc.name <> 'in.inboxpipe.net'` in the catch-all leg is what closes this,
    independent of any Python-level validation.
    """
    owner = make_user("p0_hostile_owner")
    with connection.cursor() as cursor:
        # Raw-insert a "sentinel" inbox with domain_id NULL isn't enough (it
        # must be joined via catch_all_inbox_id on the hostile domain row),
        # so insert the domain row FIRST, then an inbox bound to it, both via
        # raw SQL to fully bypass every ORM-level guard (Domain.save's
        # reserved-apex check, Inbox's routing_key format validators, etc).
        cursor.execute(
            """
            INSERT INTO domains_domain
                (public_id, user_id, name, status, verification_token,
                 verification_txt_host, consecutive_failures, catch_all,
                 last_error, created_at, updated_at,
                 dkim_consecutive_failures, dkim_status, dkim_selector,
                 dkim_public_key_txt)
            VALUES
                (%s, %s, %s, 'verified', 'hostile-token',
                 '_mailsocket-verify.in.inboxpipe.net', 0, FALSE, '', now(), now(),
                 0, 'none', 'msout1', '')
            RETURNING id
            """,
            ["dom_hostile_p0_test", owner.pk, SHARED_DOMAIN],
        )
        hostile_domain_id = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO inboxes_inbox
                (public_id, user_id, label, routing_key, domain_id,
                 is_enabled, is_public, metadata, created_at, updated_at,
                 deleted_at)
            VALUES
                (%s, %s, '', %s, %s, TRUE, FALSE, '{}'::jsonb, now(), now(), NULL)
            RETURNING id
            """,
            ["inbox_hostile_p0_test", owner.pk, "hostilesentinelkey", hostile_domain_id],
        )
        hostile_sentinel_id = cursor.fetchone()[0]
        # Flip catch_all=TRUE and set catch_all_inbox_id together so the
        # domain_catch_all_requires_inbox CheckConstraint is satisfied at
        # every point — this is still 100% raw SQL, ORM/service-layer
        # validation never runs.
        cursor.execute(
            "UPDATE domains_domain SET catch_all = TRUE, catch_all_inbox_id = %s WHERE id = %s",
            [hostile_sentinel_id, hostile_domain_id],
        )

    # Any localpart at all — including ones that would pass the regex guard
    # cleanly — must return ZERO rows against the shared domain via the
    # catch-all leg. (We deliberately exclude the sentinel's own exact
    # routing_key here: an exact match would also satisfy leg 1's normal
    # domain-scoped equality branch, which is Stage B's pre-existing "a
    # domains_domain row literally named in.inboxpipe.net, inserted via raw
    # SQL, behaves like a normal verified custom domain for exact matches"
    # gap — real, but a different concern from THIS test's target: the
    # catch-all leg's own contradiction guard.)
    for localpart in ("probe", "john.doe", "info+tag", "a"):
        rows = _run_recipient_map_sql(localpart, SHARED_DOMAIN)
        assert rows == [], (
            f"P0 VIOLATION: catch-all leg matched localpart={localpart!r} "
            f"against the shared domain via a hostile raw-SQL domains_domain "
            f"row — the dc.name <> 'in.inboxpipe.net' contradiction guard is "
            f"not doing its job."
        )





def test_recipient_map_sql_catch_all_sentinel_wrong_domain_via_raw_sql_returns_zero_rows():
    "Public API."
    user = make_user("p2_wrongdomain_owner")
    catch_all_domain = make_domain(user, "p2-wrongdomain-catchall.example.com")
    other_domain = make_domain(user, "p2-wrongdomain-other.example.com")
    sentinel = Inbox.objects.create(
        user=user, routing_key="wrongdomainsentinel", domain=other_domain
    )
    with connection.cursor() as cursor:
        # Bypass set_catch_all entirely: raw-wire catch_all_inbox_id to a
        # sentinel that lives on a DIFFERENT domain than catch_all_domain.
        cursor.execute(
            "UPDATE domains_domain SET catch_all = TRUE, catch_all_inbox_id = %s "
            "WHERE id = %s",
            [sentinel.pk, catch_all_domain.pk],
        )
    rows = _run_recipient_map_sql("anylocalpart", catch_all_domain.name)
    assert rows == [], (
        "leg2 matched a sentinel bound to a DIFFERENT domain via "
        "catch_all_inbox_id — the `si.domain_id = dc.id` predicate is "
        "missing or not doing its job."
    )


# --- #2 shared regression -----------------------------------------------------


def test_recipient_map_sql_shared_domain_still_resolves_null_domain_inbox():
    """Regression: adding the catch-all UNION-ALL leg must not change the
    shared apex's existing behaviour at all."""
    user = make_user("stagec_shared_a")
    Inbox.objects.create(user=user, routing_key="stagecsharedkey")
    rows = _run_recipient_map_sql("stagecsharedkey", SHARED_DOMAIN)
    assert [r[0] for r in rows] == ["stagecsharedkey"]

    assert _run_recipient_map_sql("stagecsharedkey-unknown", SHARED_DOMAIN) == []


# --- #3 non-catch-all custom domain -------------------------------------------


def test_recipient_map_sql_non_catch_all_domain_unknown_localpart_empty():
    user = make_user("stagec_noncatchall_a")
    domain = make_domain(user, "stagec-noncatchall-a.example.com")
    Inbox.objects.create(user=user, routing_key="knownkey", domain=domain)

    assert _run_recipient_map_sql("unknownkey", "stagec-noncatchall-a.example.com") == []
    rows = _run_recipient_map_sql("knownkey", "stagec-noncatchall-a.example.com")
    assert [r[0] for r in rows] == ["knownkey"]


# --- #4 catch-all matches / rejects -------------------------------------------


@pytest.mark.parametrize("localpart", ["john.doe", "info+tag", "a"])
def test_recipient_map_sql_catch_all_matches_realworld_localparts(localpart):
    user = make_user(f"stagec_realworld_{localpart.replace('.', '_').replace('+', '_')}")
    domain, _sentinel = make_catch_all_domain(
        user, f"stagec-realworld-{localpart.replace('.', '_').replace('+', '_')}.example.com"
    )
    rows = _run_recipient_map_sql(localpart, domain.name)
    assert [r[0] for r in rows] == [localpart]


def test_recipient_map_sql_catch_all_domain_status_failed_returns_empty():
    "Public API."
    user = make_user("stagec_failed_a")
    domain, _sentinel = make_catch_all_domain(user, "stagec-failed-a.example.com")

    rows = _run_recipient_map_sql("anything", domain.name)
    assert [r[0] for r in rows] == ["anything"]

    domain.status = Domain.Status.FAILED
    domain.save(update_fields=["status"])
    assert _run_recipient_map_sql("anything", domain.name) == []


def test_recipient_map_sql_catch_all_off_returns_empty():
    "Public API."
    user = make_user("stagec_off_a")
    domain, _sentinel = make_catch_all_domain(user, "stagec-off-a.example.com")

    rows = _run_recipient_map_sql("anything", domain.name)
    assert [r[0] for r in rows] == ["anything"]

    domain.catch_all = False
    domain.save(update_fields=["catch_all"])
    assert _run_recipient_map_sql("anything", domain.name) == []


def test_recipient_map_sql_catch_all_sentinel_disabled_returns_empty():
    user = make_user("stagec_sentineloff_a")
    domain, sentinel = make_catch_all_domain(user, "stagec-sentineloff-a.example.com")
    sentinel.is_enabled = False
    sentinel.save(update_fields=["is_enabled"])
    assert _run_recipient_map_sql("anything", domain.name) == []


def test_recipient_map_sql_catch_all_sentinel_soft_deleted_returns_empty():
    user = make_user("stagec_sentineldel_a")
    domain, sentinel = make_catch_all_domain(user, "stagec-sentineldel-a.example.com")
    Inbox.objects.filter(pk=sentinel.pk).update(deleted_at=timezone.now())
    assert _run_recipient_map_sql("anything", domain.name) == []


# --- #5 SQL-hostile chars ------------------------------------------------------


@pytest.mark.parametrize(
    "localpart",
    [
        "quo'te",
        "a b",
        "has%percent",
        "; DROP TABLE inboxes_inbox; --",
        "",
    ],
)
def test_recipient_map_sql_hostile_localpart_chars_return_empty_no_error(localpart):
    user = make_user(f"stagec_hostile_{abs(hash(localpart))}")
    domain, _sentinel = make_catch_all_domain(
        user, f"stagec-hostile-{abs(hash(localpart))}.example.com"
    )
    # Must not raise — psycopg2 bound params make this injection-safe
    # regardless; the regex guard is what rejects these localparts.
    assert _run_recipient_map_sql(localpart, domain.name) == []


# --- #6 postfix reader role permission ----------------------------------------

LEAST_PRIV_ROLE = "test_postfix_reader_role_stagec"
LEAST_PRIV_PASSWORD = "test-only-not-a-real-secret"  # noqa: S105


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
    """Mirrors deploy/provision-postfix-role.py's Stage C grants exactly:
    SELECT (routing_key, is_enabled, deleted_at, domain_id, id) on
    inboxes_inbox, SELECT (id, name, status, catch_all, catch_all_inbox_id)
    on domains_domain, nothing else."""
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


def test_recipient_map_sql_runs_as_least_priv_reader_role_with_catch_all(least_priv_reader):
    """Proves the Stage-C-widened grant is SUFFICIENT: the full query
    (including the new UNION ALL leg) runs end-to-end through the
    provisioned least-privilege reader role for both a normal-match and a
    catch-all-match row."""
    user = make_user("stagec_leastpriv_a")
    domain, _sentinel = make_catch_all_domain(user, "stagec-leastpriv-a.example.com")

    reader_kwargs = dict(_db_connection_kwargs())
    reader_kwargs["user"] = least_priv_reader
    reader_kwargs["password"] = LEAST_PRIV_PASSWORD
    reader_conn = psycopg2.connect(**reader_kwargs)
    try:
        with reader_conn.cursor() as cur:
            cur.execute(RECIPIENT_MAP_SQL, {"u": "john.doe", "d": domain.name})
            assert [r[0] for r in cur.fetchall()] == ["john.doe"]
    finally:
        reader_conn.close()


def test_recipient_map_sql_least_priv_reader_cannot_read_ungranted_catch_all_columns(
    least_priv_reader,
):
    """Proves the grant is MINIMAL: selecting domains_domain.verification_token
    (an ungranted column, unrelated to catch_all) still raises a permission
    error through the reader role even after the Stage C grant widening."""
    reader_kwargs = dict(_db_connection_kwargs())
    reader_kwargs["user"] = least_priv_reader
    reader_kwargs["password"] = LEAST_PRIV_PASSWORD
    reader_conn = psycopg2.connect(**reader_kwargs)
    try:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            with reader_conn.cursor() as cur:
                cur.execute("SELECT verification_token FROM domains_domain LIMIT 1")
    finally:
        reader_conn.close()


# --- #11 resolve_inbox / ingest precedence ------------------------------------


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
def test_resolve_inbox_catch_all_unknown_localpart_resolves_sentinel():
    from apps.ingest.services import resolve_inbox

    user = make_user("ri_unknown_a")
    domain, sentinel = make_catch_all_domain(user, "ri-unknown-a.example.com")
    resolved = resolve_inbox("john.doe", domain.name)
    assert resolved.pk == sentinel.pk


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
def test_resolve_inbox_catch_all_exact_live_inbox_wins_over_sentinel():
    from apps.ingest.services import resolve_inbox

    user = make_user("ri_exact_a")
    domain, sentinel = make_catch_all_domain(user, "ri-exact-a.example.com")
    exact = Inbox.objects.create(user=user, routing_key="exactkey", domain=domain)
    resolved = resolve_inbox("exactkey", domain.name)
    assert resolved.pk == exact.pk
    assert resolved.pk != sentinel.pk


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
def test_resolve_inbox_catch_all_exact_disabled_inbox_falls_to_sentinel():
    from apps.ingest.services import resolve_inbox

    user = make_user("ri_exactdead_a")
    domain, sentinel = make_catch_all_domain(user, "ri-exactdead-a.example.com")
    Inbox.objects.create(
        user=user, routing_key="deadexactkey", domain=domain, is_enabled=False
    )
    resolved = resolve_inbox("deadexactkey", domain.name)
    assert resolved.pk == sentinel.pk


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
def test_resolve_inbox_catch_all_sentinel_disabled_is_422():
    from apps.ingest.services import IngestError, resolve_inbox

    user = make_user("ri_sentineldead_a")
    domain, sentinel = make_catch_all_domain(user, "ri-sentineldead-a.example.com")
    sentinel.is_enabled = False
    sentinel.save(update_fields=["is_enabled"])

    with pytest.raises(IngestError) as exc_info:
        resolve_inbox("anyunknownkey", domain.name)
    assert exc_info.value.status_code == 422


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
def test_resolve_inbox_catch_all_off_is_404_today_behavior():
    from apps.ingest.services import IngestError, resolve_inbox

    user = make_user("ri_catchoff_a")
    domain = make_domain(user, "ri-catchoff-a.example.com")
    Inbox.objects.create(user=user, routing_key="somekey", domain=domain)

    with pytest.raises(IngestError) as exc_info:
        resolve_inbox("unknownkey", domain.name)
    assert exc_info.value.status_code == 404


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
def test_resolve_inbox_shared_domain_branch_byte_for_byte_unaffected():
    """Regression: the shared branch must be completely untouched by
    catch-all logic, even when the account also owns a catch-all domain."""
    from apps.ingest.services import resolve_inbox

    user = make_user("ri_sharedunaffected_a")
    make_catch_all_domain(user, "ri-sharedunaffected-a.example.com")
    shared_inbox = Inbox.objects.create(user=user, routing_key="sharedonlykey")
    resolved = resolve_inbox("sharedonlykey", SHARED_DOMAIN)
    assert resolved.pk == shared_inbox.pk


# --- #12 parse_envelope_recipient permissive charset --------------------------


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
def test_parse_envelope_recipient_shared_domain_dotty_localpart_still_400s():
    from apps.ingest.services import IngestError, parse_envelope_recipient

    with pytest.raises(IngestError) as exc_info:
        parse_envelope_recipient("john.doe@in.inboxpipe.net")
    assert exc_info.value.status_code == 400


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
@pytest.mark.parametrize("localpart", ["john.doe", "info+tag", "a"])
def test_parse_envelope_recipient_custom_domain_realworld_localpart_parses(localpart):
    from apps.ingest.services import parse_envelope_recipient

    recipient, parsed_localpart, domain = parse_envelope_recipient(
        f"{localpart}@custom.example.com"
    )
    assert parsed_localpart == localpart
    assert domain == "custom.example.com"
    assert recipient == f"{localpart}@custom.example.com"


# --- #13 store_message end-to-end ---------------------------------------------

RAW_TEMPLATE = (
    b"From: Sender <sender@example.test>\r\n"
    b"To: visible@example.test\r\n"
    b"Message-ID: <%s@example.test>\r\n"
    b"Subject: stage c catch-all\r\n"
    b"\r\n"
    b"body\r\n"
)


@override_settings(MAIL_DOMAIN=SHARED_DOMAIN)
def test_store_message_two_localparts_land_in_same_sentinel_distinct_envelope():
    from apps.ingest.services import store_message

    user = make_user("store_catchall_a")
    domain, sentinel = make_catch_all_domain(user, "store-catchall-a.example.com")

    result_a = store_message(
        raw_bytes=RAW_TEMPLATE.replace(b"%s", b"stagec-case-a"),
        headers={
            "X-Envelope-To": f"john.doe@{domain.name}",
            "X-Envelope-From": "bounce@example.test",
        },
    )
    result_b = store_message(
        raw_bytes=RAW_TEMPLATE.replace(b"%s", b"stagec-case-b"),
        headers={
            "X-Envelope-To": f"info+tag@{domain.name}",
            "X-Envelope-From": "bounce@example.test",
        },
    )

    assert result_a.message.inbox_id == sentinel.pk
    assert result_b.message.inbox_id == sentinel.pk
    assert result_a.message.envelope_recipient == f"john.doe@{domain.name}"
    assert result_b.message.envelope_recipient == f"info+tag@{domain.name}"
    assert result_a.deduplicated is False
    assert result_b.deduplicated is False
