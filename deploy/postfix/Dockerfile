# syntax=docker/dockerfile:1
FROM ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3

ARG DEBIAN_FRONTEND=noninteractive
# Keep Postfix's historical UID 100 stable so the persistent queue remains
# readable across the OpenDKIM package addition.
RUN apt-get update \
    && echo "postfix postfix/mailname string mx.inboxpipe.net" | debconf-set-selections \
    && echo "postfix postfix/main_mailer_type select Internet Site" | debconf-set-selections \
    && apt-get install -y --no-install-recommends \
        postfix=3.8.6-1ubuntu0.1 \
        postfix-pgsql=3.8.6-1ubuntu0.1 \
        opendkim=2.11.0~beta2-9build4 \
        opendkim-tools=2.11.0~beta2-9build4 \
        curl \
        ca-certificates \
        netcat-openbsd \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 2000 mailsocket-pipe \
    && useradd --system --uid 2000 --gid mailsocket-pipe \
        --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin mailsocket-pipe \
    && usermod --uid 2001 opendkim \
    && usermod --uid 100 postfix \
    && groupmod --gid 2002 postfix \
    && groupmod --gid 103 postdrop \
    && groupmod --gid 2001 opendkim \
    && chown -R 100 /var/spool/postfix /var/lib/postfix \
    && chgrp 103 /var/spool/postfix/maildrop /var/spool/postfix/public \
        /usr/sbin/postdrop /usr/sbin/postqueue \
    && chmod 2755 /usr/sbin/postdrop /usr/sbin/postqueue

COPY deploy/postfix/main.cf /etc/postfix/main.cf
COPY deploy/postfix/master.cf /etc/postfix/master.cf
COPY deploy/postfix/transport /etc/postfix/transport
COPY deploy/postfix/sender-canonical /etc/postfix/sender-canonical
COPY deploy/postfix/recipient-map.cf.template /etc/postfix/recipient-map.cf.template
COPY deploy/postfix/recipient-map.cf.template /etc/postfix/recipient-map.cf
COPY deploy/postfix/pipe.sh /usr/local/libexec/mailsocket-pipe
COPY deploy/postfix/tls-setup.sh /usr/local/libexec/mailsocket-postfix-tls
COPY deploy/postfix/entrypoint.sh /usr/local/sbin/mailsocket-postfix-entrypoint
COPY deploy/milter/signer.conf /etc/opendkim.conf
COPY deploy/milter/key-table /etc/opendkim/key-table
COPY deploy/milter/signing-table /etc/opendkim/signing-table
COPY deploy/milter/trusted-hosts /etc/opendkim/trusted-hosts

RUN chmod 0644 /etc/postfix/main.cf /etc/postfix/master.cf /etc/postfix/transport \
        /etc/postfix/sender-canonical /etc/opendkim.conf /etc/opendkim/key-table \
        /etc/opendkim/signing-table /etc/opendkim/trusted-hosts \
        /etc/postfix/recipient-map.cf.template \
    && chmod 0640 /etc/postfix/recipient-map.cf \
    && chown root:postfix /etc/postfix/recipient-map.cf \
    && chmod 0755 /usr/local/libexec/mailsocket-pipe /usr/local/libexec/mailsocket-postfix-tls \
        /usr/local/sbin/mailsocket-postfix-entrypoint \
    && postmap /etc/postfix/transport \
    && postmap /etc/postfix/sender-canonical \
    && postfix check

EXPOSE 25
ENTRYPOINT ["/usr/local/sbin/mailsocket-postfix-entrypoint"]
CMD ["postfix", "start-fg"]
