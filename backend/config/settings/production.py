import os

from .base import *

DEBUG = False
SECRET_KEY = get_required_env("SECRET_KEY")

# (checkout user_sig, sessions, signed cursors) during a SECRET_KEY rotation.
SECRET_KEY_FALLBACKS = [
    key.strip() for key in os.environ.get("SECRET_KEY_FALLBACKS", "").split(",") if key.strip()
]
ALLOWED_HOSTS = [host.strip() for host in get_required_env("ALLOWED_HOSTS").split(",") if host.strip()]
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must contain at least one host")
# The MTA-STS policy host (apps.accounts.mta_sts) is accepted without an
# .env edit; the middleware answers it before anything else of the site.
if MTA_STS_HOST and MTA_STS_HOST not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(MTA_STS_HOST)

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
INGEST_SHARED_SECRET = get_required_env("INGEST_SHARED_SECRET")
AUTH_RATE_LIMIT_SECRET = get_required_env("AUTH_RATE_LIMIT_SECRET")
API_KEY_PEPPER = get_required_env("API_KEY_PEPPER")
WEBHOOK_SIGNING_MASTER_KEY = get_required_env("WEBHOOK_SIGNING_MASTER_KEY")

# Production incident: OUTBOUND_DKIM_MASTER_KEY is an OUTBOUND-ONLY
# feature secret (apps/outbound/dkim_keys.py derives a Fernet key from it to
# encrypt Domain.dkim_private_key_enc at rest). ENABLE_OUTBOUND defaults False
# and was False in prod, yet this line demanded the var unconditionally —
# absent from prod .env, Django settings failed to import, web crash-looped,
# and the deploy migrate step was killed. A flag-OFF feature must not require
# new infrastructure. Require it ONLY when the flag that consumes it is on;
# otherwise fall back to base.py's os.environ.get(..., dev-only-default),
# which is safe because the key is never read while outbound is off.
if ENABLE_OUTBOUND:
    OUTBOUND_DKIM_MASTER_KEY = get_required_env("OUTBOUND_DKIM_MASTER_KEY")

# D3 (N1): active only in production. Not an
# env var — deploys inert on tests/dev by construction.
EDGE_HEADER_MONITOR = True


# default in production; CANONICAL_HOST_REDIRECTS=false in .env switches the
# redirects AND the absolute cross-host links off together.
CANONICAL_HOST_REDIRECTS = os.environ.get("CANONICAL_HOST_REDIRECTS", "true").lower() == "true"

# BILLING (Lemon Squeezy) — exact OUTBOUND_DKIM_MASTER_KEY idiom above (the
# 2026-09-28 incident lesson): a flag-OFF feature must not require new
# infrastructure. Require the two LS secrets ONLY when ENABLE_BILLING is on;
# otherwise fall back to base.py's os.environ.get(..., dev-only-default),
# which is safe because neither is ever read while billing is off.
if ENABLE_BILLING:
    LS_API_KEY = get_required_env("LS_API_KEY")
    LS_WEBHOOK_SECRET = get_required_env("LS_WEBHOOK_SECRET")

EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend"
)

DATABASES["default"].update(
    {
        "NAME": get_required_env("DB_NAME"),
        "USER": get_required_env("DB_USERNAME"),
        "PASSWORD": get_required_env("DB_PASS"),
        "HOST": get_required_env("DB_HOST"),
        "PORT": get_required_env("DB_PORT"),
    }
)


# connection pool, per gunicorn worker process. With 32 gthread threads per
# worker, per-thread persistent connections (CONN_MAX_AGE) pinned one Postgres
# backend per held long-poll wait (measured 84-96 of max_connections=100 at 64
# waits). With the pool, waits hand their connection back between polls
# (apps.api.views._release_db_connection_while_idle), so backends are bounded
# by DB_POOL_MAX_SIZE x workers (default 16 x 3 = 48 < 60). The pool and
# persistent connections are mutually exclusive in Django, hence
# CONN_MAX_AGE=0. DB_POOL_MAX_SIZE=0 reverts to the old persistent mode, which
# is only safe together with GUNICORN_THREADS=8 and the old wait caps — use
# scripts/rollback-wait-capacity.sh; apps.accounts.checks (mailsocket.E001)
# refuses to start the half-applied combination.
DB_POOL_MAX_SIZE = int(os.environ.get("DB_POOL_MAX_SIZE", "16"))
if DB_POOL_MAX_SIZE > 0:
    DATABASES["default"]["CONN_MAX_AGE"] = 0
    DATABASES["default"].setdefault("OPTIONS", {})["pool"] = {
        # Small floor: every `manage.py` cron process builds its own pool too.
        "min_size": min(2, DB_POOL_MAX_SIZE),
        "max_size": DB_POOL_MAX_SIZE,
        # A request that cannot get a connection within this many seconds
        # fails (500) instead of queueing past gunicorn's timeout.
        "timeout": float(os.environ.get("DB_POOL_TIMEOUT", "10")),
    }

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
# The Postfix pipe reaches this network-hidden endpoint over the private Docker
# network. It must not be redirected to an HTTPS listener that does not exist.
SECURE_REDIRECT_EXEMPT = [r"^internal/ingest$"]
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
