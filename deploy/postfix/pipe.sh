#!/usr/bin/env bash
set -Eeuo pipefail
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077

readonly EX_TEMPFAIL=75
readonly SECRET_FILE=/run/secrets/ingest_shared_secret
readonly INGEST_URL=http://web:8000/internal/ingest
readonly MAX_BYTES="${MAX_INBOUND_BYTES:-10485760}"
readonly CONNECT_TIMEOUT="${INGEST_CONNECT_TIMEOUT_SECONDS:-5}"
readonly TOTAL_TIMEOUT="${INGEST_TOTAL_TIMEOUT_SECONDS:-30}"
recipient=""
sender=""
queue_id=""
intake_id=""
tmpfile=""
response_headers=""
curl_config=""

log_metadata() {
    local priority="$1"
    local status="$2"
    local outcome="$3"
    logger -p "mail.${priority}" -t mailsocket-pipe -- \
        "queue_id=${queue_id:--} recipient=${recipient:--} status=${status} outcome=${outcome}"
}

tempfail() {
    log_metadata "$1" "$2" "$3"
    exit "$EX_TEMPFAIL"
}

# Fail-safe for the no-loss guarantee: ANY unexpected error (mktemp/chmod/wc/
# out-of-disk/inode exhaustion/filesystem faults, or any command not already
# routed through tempfail) must become EX_TEMPFAIL (75) so Postfix REQUEUES the
# mail instead of treating a bash exit 1 as a permanent 5.x.x bounce (which would
# lose a message already accepted at SMTP but not yet confirmed stored).
# The only path that returns 0 is the explicit `exit 0` after a 200/201/202.
on_err() {
    local rc=$?
    log_metadata crit "exit-${rc}" unexpected-error
    exit "$EX_TEMPFAIL"
}
trap on_err ERR

# Invoked by the EXIT/signal trap below.
# shellcheck disable=SC2329
cleanup() {
    [[ -z "$tmpfile" ]] || rm -f -- "$tmpfile"
    [[ -z "$response_headers" ]] || rm -f -- "$response_headers"
    [[ -z "${curl_config:-}" ]] || rm -f -- "$curl_config"
}
trap cleanup EXIT HUP INT TERM

while (($#)); do
    case "$1" in
        --recipient)
            (($# >= 2)) || tempfail crit config-error missing-recipient-value
            recipient=$2
            shift 2
            ;;
        --sender)
            (($# >= 2)) || tempfail crit config-error missing-sender-value
            sender=$2
            shift 2
            ;;
        --queue-id)
            (($# >= 2)) || tempfail crit config-error missing-queue-id-value
            queue_id=$2
            shift 2
            ;;
        --intake-id)
            (($# >= 2)) || tempfail crit config-error missing-intake-id-value
            intake_id=$2
            shift 2
            ;;
        *)
            tempfail crit config-error unexpected-argument
            ;;
    esac
done

[[ -n "$recipient" ]] || tempfail crit config-error missing-recipient
[[ "$MAX_BYTES" =~ ^[0-9]+$ ]] || tempfail crit config-error invalid-max-bytes
[[ -r "$SECRET_FILE" ]] || tempfail crit config-error secret-unreadable

secret=$(<"$SECRET_FILE")
[[ -n "$secret" ]] || tempfail crit config-error secret-empty

response_headers=$(mktemp /tmp/mailsocket-response.XXXXXX)
tmpfile=$(mktemp /tmp/mailsocket-message.XXXXXX)
chmod 0600 "$response_headers" "$tmpfile"

if ! head -c "$((MAX_BYTES + 1))" >"$tmpfile"; then
    tempfail err read-error stdin-read-failed
fi
size=$(wc -c < "$tmpfile")
if ((size > MAX_BYTES)); then
    tempfail warning too-large deferred
fi

# The ingest secret is passed via a curl --config file on a 0600 fd-backed temp
# file, NOT on argv, so it never appears in the process command line (defence
# against local process-list snooping; audit P2 @7b60bc4).
curl_config=$(mktemp /tmp/mailsocket-curlcfg.XXXXXX)
chmod 0600 "$curl_config"
printf 'header = "X-Mailsocket-Ingest-Secret: %s"\n' "$secret" >"$curl_config"

curl_args=(
    --silent
    --show-error
    --fail-with-body
    --noproxy '*'
    --config "$curl_config"
    --connect-timeout "$CONNECT_TIMEOUT"
    --max-time "$TOTAL_TIMEOUT"
    --request POST
    --header 'Content-Type: application/octet-stream'
    --header "X-Envelope-To: ${recipient}"
    --header "X-Envelope-From: ${sender}"
    --header "X-Postfix-Queue-Id: ${queue_id}"
    --data-binary "@${tmpfile}"
    --dump-header "$response_headers"
    --output /dev/null
    --write-out '%{http_code}'
)
if [[ -n "$intake_id" ]]; then
    curl_args+=(--header "X-Intake-Id: ${intake_id}")
fi
curl_args+=("$INGEST_URL")

set +e
http_status=$(curl "${curl_args[@]}" 2>/dev/null)
curl_exit=$?
set -e
http_status=${http_status:--}

if ((curl_exit != 0)); then
    case "$http_status" in
        401|403)
            tempfail crit "$http_status" auth-config-failure
            ;;
        *)
            tempfail warning "$http_status" "deferred-curl-${curl_exit}"
            ;;
    esac
fi

case "$http_status" in
    200|201|202)
        log_metadata info "$http_status" delivered
        exit 0
        ;;
    401|403)
        tempfail crit "$http_status" auth-config-failure
        ;;
    *)
        tempfail warning "$http_status" deferred
        ;;
esac
