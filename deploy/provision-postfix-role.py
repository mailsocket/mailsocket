#!/usr/bin/env python3
"""Idempotently provision the least-privilege Postfix recipient-map role."""

import os

import psycopg2
from psycopg2 import sql


role_name = os.environ["POSTFIX_DB_READER_USER"]
role_password = os.environ["POSTFIX_DB_READER_PASSWORD"]
database_name = os.environ["DB_NAME"]
connection = psycopg2.connect(
    dbname=database_name,
    user=os.environ["DB_USERNAME"],
    password=os.environ["DB_PASS"],
    host=os.environ["DB_HOST"],
    port=os.environ["DB_PORT"],
)
connection.autocommit = True
role = sql.Identifier(role_name)
database = sql.Identifier(database_name)

with connection, connection.cursor() as cursor:
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
    if cursor.fetchone() is None:
        cursor.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD %s NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION").format(role),
            (role_password,),
        )
    else:
        cursor.execute(
            sql.SQL("ALTER ROLE {} LOGIN PASSWORD %s NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION").format(role),
            (role_password,),
        )

    cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(database, role))
    cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, role))
    cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA public FROM {}").format(role))
    cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
    cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {}").format(role))
    cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {}").format(role))
    cursor.execute(
        sql.SQL(
            "GRANT SELECT (routing_key, is_enabled, deleted_at, domain_id, id) ON TABLE inboxes_inbox TO {}"
        ).format(role)
    )
    
    # recipient-map.cf / relay-domains.cf / transport.cf pgsql queries now
    # LEFT JOIN domains_domain and filter on domain_id/name/status. Without
    # this grant every custom-domain delivery would silently 550 with a
    # permission error at the acceptance gate. Least-privilege: only these
    # three columns, only SELECT, same idempotent REVOKE-ALL-then-GRANT
    # pattern as inboxes_inbox above.
    #
    
    # the recipient-map's new UNION ALL leg reads dc.catch_all,
    # dc.catch_all_inbox_id (domains_domain) and joins inboxes_inbox on
    # si.id — both grants below were widened accordingly (inboxes_inbox
    # gained `id`; domains_domain gained `catch_all, catch_all_inbox_id`).
    # P0-2 NON-NEGOTIABLE: this script MUST run AFTER the migration that adds
    # these columns and BEFORE the postfix container is restarted with the
    # new recipient-map.cf.template SQL — see scripts/deploy.sh ordering.
    cursor.execute(
        sql.SQL(
            "GRANT SELECT (id, name, status, catch_all, catch_all_inbox_id) ON TABLE domains_domain TO {}"
        ).format(role)
    )
