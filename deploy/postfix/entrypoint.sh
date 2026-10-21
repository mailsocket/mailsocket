#!/usr/bin/env bash
set -euo pipefail
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077

readonly MAP_FILE=/etc/postfix/recipient-map.cf
#  (BYOD): two more proxy:pgsql maps, same env-substituted
# credential pattern as MAP_FILE. relay_domains + transport_maps in main.cf
# now include these; the shared apex keeps its static literal/hash fallback.
readonly RELAY_DOMAINS_MAP_FILE=/etc/postfix/relay-domains.cf
readonly TRANSPORT_MAP_FILE=/etc/postfix/transport.cf
readonly SIGNING_KEY=/run/secrets/ms1.private
: "${POSTFIX_DB_READER_USER:?POSTFIX_DB_READER_USER is required}"
: "${POSTFIX_DB_READER_PASSWORD:?POSTFIX_DB_READER_PASSWORD is required}"
: "${POSTFIX_DB_NAME:?POSTFIX_DB_NAME is required}"
: "${POSTFIX_QUEUE_MAX_DAYS:=5}"

[[ "$POSTFIX_QUEUE_MAX_DAYS" =~ ^[1-9][0-9]*$ ]] || {
    echo "POSTFIX_QUEUE_MAX_DAYS must be a positive integer" >&2
    exit 1
}
for value in "$POSTFIX_DB_READER_USER" "$POSTFIX_DB_READER_PASSWORD" "$POSTFIX_DB_NAME"; do
    [[ "$value" =~ ^[A-Za-z0-9_.-]+$ ]] || {
        echo "Postfix database settings must use only letters, digits, dot, underscore, and hyphen" >&2
        exit 1
    }
done

{
    printf 'hosts = db\n'
    printf 'user = %s\n' "$POSTFIX_DB_READER_USER"
    printf 'password = %s\n' "$POSTFIX_DB_READER_PASSWORD"
    printf 'dbname = %s\n' "$POSTFIX_DB_NAME"
    printf '%s\n' "query = SELECT i.routing_key FROM inboxes_inbox i LEFT JOIN domains_domain d ON i.domain_id = d.id WHERE i.routing_key = '%u' AND i.routing_key = lower(i.routing_key) AND i.is_enabled IS TRUE AND i.deleted_at IS NULL AND ( ( '%d' = 'in.inboxpipe.net' AND i.domain_id IS NULL ) OR ( i.domain_id = d.id AND d.name = '%d' AND d.status = 'verified' ) ) UNION ALL SELECT '%u' FROM domains_domain dc JOIN inboxes_inbox si ON si.id = dc.catch_all_inbox_id AND si.domain_id = dc.id WHERE dc.name = '%d' AND dc.name <> 'in.inboxpipe.net' AND dc.status = 'verified' AND dc.catch_all IS TRUE AND si.is_enabled IS TRUE AND si.deleted_at IS NULL AND '%u' ~ '^[a-z0-9][a-z0-9._+=-]{0,63}$'"
} >"$MAP_FILE"

chmod 0640 "$MAP_FILE"
chown root:postfix "$MAP_FILE"

#  (BYOD): relay_domains pgsql map — accepts a verified
# custom domain at the SMTP RELAY stage (before recipient-map is even
# consulted). The shared apex is a static literal in main.cf, not here.
{
    printf 'hosts = db\n'
    printf 'user = %s\n' "$POSTFIX_DB_READER_USER"
    printf 'password = %s\n' "$POSTFIX_DB_READER_PASSWORD"
    printf 'dbname = %s\n' "$POSTFIX_DB_NAME"
    printf '%s\n' "query = SELECT name FROM domains_domain WHERE name = '%s' AND status = 'verified'"
} >"$RELAY_DOMAINS_MAP_FILE"
chmod 0640 "$RELAY_DOMAINS_MAP_FILE"
chown root:postfix "$RELAY_DOMAINS_MAP_FILE"

#  (BYOD): transport_maps pgsql map — routes verified custom
# domains to the same mailsocket: pipe transport as the shared apex (whose
# static hash:/etc/postfix/transport fallback entry stays untouched).
{
    printf 'hosts = db\n'
    printf 'user = %s\n' "$POSTFIX_DB_READER_USER"
    printf 'password = %s\n' "$POSTFIX_DB_READER_PASSWORD"
    printf 'dbname = %s\n' "$POSTFIX_DB_NAME"
    printf '%s\n' "query = SELECT 'mailsocket:' FROM domains_domain WHERE name = '%s' AND status = 'verified'"
} >"$TRANSPORT_MAP_FILE"
chmod 0640 "$TRANSPORT_MAP_FILE"
chown root:postfix "$TRANSPORT_MAP_FILE"

if [[ ! -f "$SIGNING_KEY" ]]; then
    echo "DKIM signing key is missing: $SIGNING_KEY" >&2
    exit 1
fi
key_mode=$(stat -c '%a' "$SIGNING_KEY")
key_uid=$(stat -c '%u' "$SIGNING_KEY")
key_gid=$(stat -c '%g' "$SIGNING_KEY")
if [[ "$key_mode" != 640 || "$key_uid" != 0 || "$key_gid" != 2001 ]]; then
    echo "DKIM signing key must be mode 0640 and owned by 0:2001" >&2
    exit 1
fi

# The ingest shared secret is read by the mailsocket-pipe transport (uid/gid
# 2000) for EVERY inbound message. If its group is wrong (e.g. restored as
# root:2001 like the DKIM key), the pipe user cannot read it, every delivery
# exits EX_TEMPFAIL, and inbound mail silently piles up deferred with NO bounce
# and NO app error — the inbox just looks empty. Fail-fast here instead. Per
# operations notes the file must be 0440 root:2000 (group 2000 = mailsocket-pipe).
readonly INGEST_SECRET=/run/secrets/ingest_shared_secret
if [[ ! -s "$INGEST_SECRET" ]]; then
    echo "Ingest shared secret is missing or empty: $INGEST_SECRET" >&2
    exit 1
fi
ingest_gid=$(stat -c '%g' "$INGEST_SECRET")
if [[ "$ingest_gid" != 2000 ]]; then
    echo "Ingest secret must be group 2000 (mailsocket-pipe) so the pipe can read it; got gid $ingest_gid." >&2
    exit 1
fi
if ! su -s /bin/sh mailsocket-pipe -c "test -r '$INGEST_SECRET'"; then
    echo "Ingest secret is not readable by mailsocket-pipe — inbound mail would defer forever." >&2
    exit 1
fi

install -d -m 0750 -o opendkim -g opendkim /run/opendkim

# Postfix runs the smtp(8) client chrooted in /var/spool/postfix, so it cannot
# see the container's /etc/resolv.conf. Without a resolver inside the chroot,
# every recipient MX lookup fails ("Name service error ... Host not found") and
# ALL outbound mail sits deferred. Docker's embedded resolver (127.0.0.11) is
# also unreachable from the chroot, so seed public resolvers directly. The
# compose `dns:` entry additionally fixes the non-chroot path (e.g. bounce).
install -d -m 0755 /var/spool/postfix/etc
printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > /var/spool/postfix/etc/resolv.conf
for f in services host.conf; do
    [[ -f "/etc/$f" ]] && cp -f "/etc/$f" "/var/spool/postfix/etc/$f"
done

/usr/local/libexec/mailsocket-postfix-tls

postconf -e "maximal_queue_lifetime=${POSTFIX_QUEUE_MAX_DAYS}d"
postconf -e "bounce_queue_lifetime=${POSTFIX_QUEUE_MAX_DAYS}d"
opendkim -x /etc/opendkim.conf
postfix check
exec "$@"
