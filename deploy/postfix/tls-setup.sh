#!/usr/bin/env bash
# and FAIL-SAFE. Run by entrypoint.sh on every container start.
#
#   cert present  -> smtpd_tls_security_level = may  (STARTTLS offered)
#   cert missing  -> smtpd_tls_security_level = none (plaintext, one log line)
#
# Mail must never stop because of TLS: this script always exits 0, and a
# missing/empty/unreadable key or chain keeps the listener plaintext exactly as
# before. The static TLS policy (protocols, ciphers, logging) lives in main.cf.
# Both settings are written EVERY start (not only when enabling) because
# `postconf -e` edits the container's writable layer, which survives a plain
# restart: a cert removed since the last start must turn TLS back off.
#
# Cert files come from the host bind mount ./secrets/postfix-tls (compose,
# read-only). Issuance/renewal: operations notes "Inbound TLS cert".
set -uo pipefail

TLS_DIR="${POSTFIX_TLS_DIR:-/etc/postfix/tls}"
KEY="$TLS_DIR/privkey.pem"
CHAIN="$TLS_DIR/fullchain.pem"

usable() { [[ -f "$1" && -s "$1" && -r "$1" ]]; }

# parseable, unexpired cert whose public key matches the private key; anything
# else keeps inbound plaintext (mail still flows) and says why in one line.
valid_pair() {
    command -v openssl >/dev/null 2>&1 || { REASON="openssl not available to validate the certificate"; return 1; }
    openssl x509 -in "$CHAIN" -noout -checkend 0 >/dev/null 2>&1 || { REASON="certificate unparseable or expired"; return 1; }
    local cert_pub key_pub
    cert_pub=$(openssl x509 -in "$CHAIN" -noout -pubkey 2>/dev/null) || { REASON="certificate unparseable"; return 1; }
    key_pub=$(openssl pkey -in "$KEY" -pubout 2>/dev/null) || { REASON="private key unparseable"; return 1; }
    [[ -n "$cert_pub" && "$cert_pub" == "$key_pub" ]] || { REASON="private key does not match certificate"; return 1; }
}

REASON=""
if usable "$KEY" && usable "$CHAIN" && ! valid_pair; then
    postconf -e "smtpd_tls_security_level = none" || true
    postconf -X smtpd_tls_chain_files || true
    echo "postfix-tls: STARTTLS disabled: $REASON; inbound stays plaintext" >&2
    exit 0
fi

if usable "$KEY" && usable "$CHAIN"; then
    if postconf -e "smtpd_tls_chain_files = $KEY, $CHAIN" \
        && postconf -e "smtpd_tls_security_level = may"; then
        echo "postfix-tls: STARTTLS enabled (opportunistic) with $CHAIN"
        exit 0
    fi
    # Half-applied config would make smtpd fail every TLS session; undo it.
    postconf -e "smtpd_tls_security_level = none" || true
    postconf -X smtpd_tls_chain_files || true
    echo "postfix-tls: STARTTLS disabled: postconf failed; inbound stays plaintext" >&2
    exit 0
fi

postconf -e "smtpd_tls_security_level = none" || true
postconf -X smtpd_tls_chain_files || true
echo "postfix-tls: STARTTLS disabled: $KEY or $CHAIN missing, empty or unreadable; inbound stays plaintext" >&2
exit 0
