#!/usr/bin/env sh
set -eu

echo "[entrypoint] Waiting for database ${DB_HOST:-db}:${DB_PORT:-5432} ..."
python - <<'PY'
import os
import socket
import time

host = os.environ.get("DB_HOST", "db")
port = int(os.environ.get("DB_PORT", "5432"))
for _ in range(60):
    try:
        socket.create_connection((host, port), timeout=2).close()
        print("[entrypoint] Database is reachable")
        break
    except OSError:
        time.sleep(1)
else:
    raise SystemExit(f"Database {host}:{port} not reachable after 60 seconds")
PY

# Fail closed before anything else touches the DB: refuse to start when the
# env-driven runtime sizing is unsafe (apps.accounts.checks, mailsocket.E00x),
# e.g. a half-applied rollback with persistent connections on 96 threads.
python manage.py check --fail-level ERROR
python manage.py migrate_expand --noinput
python /usr/local/bin/provision-postfix-role
python manage.py collectstatic --noinput
exec "$@"
