import os
from pathlib import Path
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR.parent / ".env")


def get_required_env(name: str, allow_empty: bool = False) -> str:
    value = os.environ.get(name)
    if value is None or (not allow_empty and not value):
        raise ImproperlyConfigured(f"Required environment variable not set: {name}")
    return value


SECRET_KEY = os.environ.get("SECRET_KEY", "development-only-not-for-production")
DEBUG = False
ALLOWED_HOSTS: list[str] = []

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]
THIRD_PARTY_APPS = ["rest_framework", "corsheaders"]
LOCAL_APPS = [
    "apps.accounts",
    "apps.inboxes",
    "apps.domains",
    "apps.ingest",
    "apps.webhooks",
    "apps.outbound",
    "apps.billing",
    "apps.status",
    "apps.dashboard",
    "apps.feedback",
]
INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    # D3 (N1): placed FIRST so the anomaly
    # counter sees every request (including ones later middleware might
    # short-circuit).
    "apps.accounts.middleware.EdgeHeaderMonitorMiddleware",
    # #15: X-Request-Id on EVERY response. Placed early
    # (right after the edge monitor) so it wraps everything downstream,
    # including the JSON 404 fallback and any middleware-level short
    # circuit; it only reads/writes headers, never touches the body, so it
    # is safe ahead of SecurityMiddleware/CommonMiddleware too.
    "apps.api.middleware.RequestIdMiddleware",
    # MTA-STS policy host (apps.accounts.mta_sts): answers every request on
    # MTA_STS_HOST itself (policy or 404) before any redirect, session or
    # cookie middleware. Inert for every other host.
    "apps.accounts.mta_sts.MtaStsMiddleware",
    # www.<APEX_HOST> -> apex 301. Before SecurityMiddleware/CommonMiddleware,
    # which call request.get_host() and would 400 a www host that is not in
    # ALLOWED_HOSTS. Inert for every other host.
    "apps.accounts.middleware.WwwRedirectMiddleware",
    # apex <-> dash canonical host split (CANONICAL_HOST_REDIRECTS). Same
    # placement reasoning as above; inert for every other host (web:8000,
    # localhost, testserver).
    "apps.accounts.middleware.CanonicalHostRedirectMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Signup attribution is captured at signup only (no cookie). This only
    # EXPIRES a leftover ms_src cookie from the old design, until
    # LEGACY_MS_SRC_SWEEP_UNTIL; it never sets one.
    "apps.accounts.attribution.LegacyAttributionCookieSweepMiddleware",
    # Funnel commercial-intent flags (activation item 2c): first pricing /
    # billing view and first checkout start per signed-in account. Acts only
    # on those three routes, after the view; never alters the response.
    "apps.accounts.funnel_signals.IntentMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.accounts.middleware.BrowserSecurityHeadersMiddleware",
    "apps.ingest.middleware.InternalIngestNetworkMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.dashboard.context_processors.multi_account",
                "apps.dashboard.context_processors.feature_flags",
                "apps.dashboard.context_processors.site_origins",
            ],
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES: dict[str, dict[str, Any]] = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME", "mailsocket"),
        "USER": os.environ.get("DB_USERNAME", "mailsocket"),
        "PASSWORD": os.environ.get("DB_PASS", "mailsocket"),
        "HOST": os.environ.get("DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("DB_PORT", "5432"),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "postfix")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "25"))
EMAIL_HOST_USER = ""
EMAIL_HOST_PASSWORD = ""
EMAIL_USE_TLS = False
EMAIL_USE_SSL = False
DEFAULT_FROM_EMAIL = "mailsocket <noreply@mailsocket.app>"
# Ops address for operational alerts (e.g. retention purge liveness). Optional:
# when unset the alert command still prints its summary and exits non-zero, it
# just skips the email.
OPS_ALERT_EMAIL = os.environ.get("OPS_ALERT_EMAIL", "")
# Ops alerts also go to Telegram (apps.ops.notify) when BOTH are set; empty
# disables the Telegram channel silently. The token is a secret (it sits in
# the Bot API URL path): .env only, never logged or committed.
OPS_TELEGRAM_BOT_TOKEN = os.environ.get("OPS_TELEGRAM_BOT_TOKEN", "")
OPS_TELEGRAM_CHAT_ID = os.environ.get("OPS_TELEGRAM_CHAT_ID", "")
# Sink address (domain we control) for enumeration-safe auth mail on the
# no-account branch; keeps the submission path identical to the real branch.
AUTH_DISCARD_EMAIL = os.environ.get("AUTH_DISCARD_EMAIL", "auth-discard@mailsocket.app")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://mailsocket.app")
# Host that serves the app/dashboard (vs the marketing landing host). When an
# anonymous visitor hits the root of this host, send them to login instead of
# re-rendering the marketing landing. Empty in dev = behave as marketing host.
DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "dash.mailsocket.app")
# Marketing apex. Requests for www.<APEX_HOST> get a 301 to https://<APEX_HOST>
# (apps.accounts.middleware.WwwRedirectMiddleware). Empty disables it.
APEX_HOST = os.environ.get("APEX_HOST", "mailsocket.app")
# One canonical host per page: product (/app, /login, /register, /admin, ...)
# on DASHBOARD_HOST, public site (/docs, /pricing, /legal, /status, ...) on
# APEX_HOST; GET/HEAD on the wrong one get a 301
# (apps.accounts.middleware.CanonicalHostRedirectMiddleware) and cross-host
# template links become absolute (apps.dashboard.context_processors.site_origins).
# /api/ is served on both hosts. Off here and in tests; production.py turns it
# on by default. Kill switch: CANONICAL_HOST_REDIRECTS=false in .env.
CANONICAL_HOST_REDIRECTS = os.environ.get("CANONICAL_HOST_REDIRECTS", "false").lower() == "true"
# Signup attribution no longer sets any cookie (apps.accounts.attribution:
# captured at signup from ?ref= / Referer only). The old signed 30-day ms_src
# cookie was set on this parent domain; the sweep middleware expires a
# leftover one (same domain, or the browser keeps it) on every response until
# LEGACY_MS_SRC_SWEEP_UNTIL (ISO date; empty = off). The last cookie was set
# before the 2026-10 deploy and lives 30 days, so the default leaves margin.
ATTRIBUTION_COOKIE_DOMAIN = os.environ.get("ATTRIBUTION_COOKIE_DOMAIN", "mailsocket.app")
LEGACY_MS_SRC_SWEEP_UNTIL = os.environ.get("LEGACY_MS_SRC_SWEEP_UNTIL", "2026-11-15")
# MTA-STS (RFC 8461) policy for the brand domain, served on MTA_STS_HOST at
# /.well-known/mta-sts.txt (apps.accounts.mta_sts). MX = Cloudflare Email
# Routing (mailsocket.app's inbound MX). Flip MTA_STS_MODE to "enforce" (and
# raise MTA_STS_MAX_AGE) once TLS-RPT shows clean reports; bump the
# _mta-sts TXT id at the same time. Empty MTA_STS_HOST disables it.
MTA_STS_HOST = os.environ.get("MTA_STS_HOST", "mta-sts.mailsocket.app").strip().lower()
MTA_STS_MODE = os.environ.get("MTA_STS_MODE", "testing").strip().lower()
MTA_STS_MAX_AGE = int(os.environ.get("MTA_STS_MAX_AGE", "86400"))
MTA_STS_MX = [
    mx.strip().lower()
    for mx in os.environ.get(
        "MTA_STS_MX", "route1.mx.cloudflare.net,route2.mx.cloudflare.net,route3.mx.cloudflare.net"
    ).split(",")
    if mx.strip()
]
EMAIL_VERIFICATION_TIMEOUT = 86400
PASSWORD_RESET_TIMEOUT = 3600
AUTH_RATE_LIMIT_SECRET = os.environ.get("AUTH_RATE_LIMIT_SECRET", SECRET_KEY)
API_KEY_PEPPER = os.environ.get("API_KEY_PEPPER", "development-only-api-key-pepper")
WEBHOOK_SIGNING_MASTER_KEY = os.environ.get(
    "WEBHOOK_SIGNING_MASTER_KEY", "development-only-webhook-signing-master-key"
)
WEBHOOK_DNS_TIMEOUT_SECONDS = float(os.environ.get("WEBHOOK_DNS_TIMEOUT_SECONDS", "2"))

WEBHOOK_DNS_MAX_THREADS = 32
WEBHOOK_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("WEBHOOK_CONNECT_TIMEOUT_SECONDS", "3"))
WEBHOOK_TOTAL_TIMEOUT_SECONDS = float(os.environ.get("WEBHOOK_TOTAL_TIMEOUT_SECONDS", "10"))
WEBHOOK_RESPONSE_MAX_BYTES = int(os.environ.get("WEBHOOK_RESPONSE_MAX_BYTES", "262144"))
WEBHOOK_RESPONSE_HEADER_MAX_BYTES = int(
    os.environ.get("WEBHOOK_RESPONSE_HEADER_MAX_BYTES", "32768")
)
WEBHOOK_LEASE_SECONDS = int(os.environ.get("WEBHOOK_LEASE_SECONDS", "30"))
WEBHOOK_MAX_ATTEMPTS = int(os.environ.get("WEBHOOK_MAX_ATTEMPTS", "4"))
WEBHOOK_RETRY_SCHEDULE_SECONDS = (0, 60, 300, 1800)
WEBHOOK_RETRY_JITTER_SECONDS = int(os.environ.get("WEBHOOK_RETRY_JITTER_SECONDS", "5"))
# Web runtime sizing — ONE place (cap r2). The SAME env vars drive the gunicorn
# command line (docker-compose.yml `${GUNICORN_WORKERS:-3}` /
# `${GUNICORN_THREADS:-32}`, Dockerfile CMD) and these settings, so the
# capacity math below can never drift from the real process. Defaults are the

# is enforced at startup by apps.accounts.checks (mailsocket.E001-E005), and
# scripts/rollback-wait-capacity.sh flips them together.
GUNICORN_WORKERS = int(os.environ.get("GUNICORN_WORKERS", "3"))
GUNICORN_THREADS = int(os.environ.get("GUNICORN_THREADS", "32"))
# Global cap on in-flight waits across all accounts, and the Free/agent/demo
# sub-pool inside it (apps.accounts.services imports both from here).
WAIT_CAPACITY_LIMIT = int(os.environ.get("WAIT_CAPACITY_LIMIT", "64"))
WAIT_CAPACITY_FREE_LIMIT = int(os.environ.get("WAIT_CAPACITY_FREE_LIMIT", "12"))
# Request slots (workers x threads) that must stay free of waits for
# dashboard, login, ingest and normal API calls. 24 for the raised runtime;
# the rollback script sets 8, the pre-raise production value (3x8 slots, 16
# waits) that ran for weeks.
WAIT_NON_WAIT_HEADROOM_MIN = int(os.environ.get("WAIT_NON_WAIT_HEADROOM_MIN", "24"))
# PostgreSQL max_connections (the compose db service runs the image default)
# and the connections reserved for everything that is NOT a gunicorn thread:
# cron `manage.py` one-shots, Postfix pgsql lookups, the entrypoint, backups,
# psql, the deploy's exec'd commands.
DB_MAX_CONNECTIONS = int(os.environ.get("DB_MAX_CONNECTIONS", "100"))
RESERVED_NON_WEB_DB_CONNECTIONS = int(os.environ.get("RESERVED_NON_WEB_DB_CONNECTIONS", "20"))
# The DB-connection budget (E001/E002) describes the gunicorn deployment;
# testing/development settings (pytest, runserver) switch it off.
DB_CONNECTION_BUDGET_CHECK = True
# Single source of truth for every billing tier: inboxes / messages_per_month /
# webhooks / retention_days / wait_concurrency / is_paid_tier all live here so a
# tier is defined in exactly one place. RETENTION_DAYS below is DERIVED from
# this dict — never duplicate a tier's numbers elsewhere. See operations notes


# 2026-10-02): free 2 / pro 10 / scale 20 / business 40. Every tier must
# stay <= WAIT_CAPACITY_LIMIT below (64, the global pool) — enforced at
# startup by the mailsocket.E003 system check (apps.accounts.checks).
BILLING_PLAN_LIMITS = {
    "free": {
        
        # §1): Free raised 1 -> 3 inboxes, 100 -> 1,000 messages/month.
        # wait_concurrency, the free wait pool, retention and webhooks are
        # deliberately unchanged. A FREE_* env override masks these defaults.
        "inboxes": int(os.environ.get("FREE_INBOX_LIMIT", "3")),
        "messages_per_month": int(os.environ.get("FREE_MONTHLY_MESSAGE_LIMIT", "1000")),
        "webhooks": False,
        "retention_days": int(os.environ.get("FREE_RETENTION_DAYS", "1")),
        "wait_concurrency": int(os.environ.get("FREE_WAIT_CONCURRENCY", "2")),
        "is_paid_tier": False,
        "custom_domains": 0,
        "seats": 1,
        "catch_all": False,
        "outbound_per_day": 0,
        "outbound_per_hour": 0,
        "outbound_recipients_per_message": 0,
        "outbound_allowlist": False,
        "verify_full": False,
        
        # decision: tiered cap): bounds free-quota multiplication (each
        # owned account carries its own FREE-plan quotas).
        "owned_accounts": int(os.environ.get("FREE_OWNED_ACCOUNTS_LIMIT", "1")),
    },
    "paid": {
        "inboxes": int(os.environ.get("PAID_INBOX_LIMIT", "10")),
        "messages_per_month": int(os.environ.get("PAID_MONTHLY_MESSAGE_LIMIT", "10000")),
        "webhooks": True,
        "retention_days": int(os.environ.get("PAID_RETENTION_DAYS", "7")),
        "wait_concurrency": int(os.environ.get("PAID_WAIT_CONCURRENCY", "10")),
        "is_paid_tier": True,
        "custom_domains": 0,
        "seats": 1,
        "catch_all": False,
        "outbound_per_day": 0,
        "outbound_per_hour": 0,
        "outbound_recipients_per_message": 0,
        "outbound_allowlist": False,
        "verify_full": False,
        "owned_accounts": int(os.environ.get("PAID_OWNED_ACCOUNTS_LIMIT", "3")),
    },
    "scale": {
        "inboxes": int(os.environ.get("SCALE_INBOX_LIMIT", "50")),
        "messages_per_month": int(os.environ.get("SCALE_MONTHLY_MESSAGE_LIMIT", "50000")),
        "webhooks": True,
        "retention_days": int(os.environ.get("SCALE_RETENTION_DAYS", "30")),
        "wait_concurrency": int(os.environ.get("SCALE_WAIT_CONCURRENCY", "20")),
        "is_paid_tier": True,
        
        # Scale. Business keeps 10 and stays the only catch-all tier.
        "custom_domains": int(os.environ.get("SCALE_DOMAIN_LIMIT", "3")),
        "seats": 1,
        "catch_all": False,
        "outbound_per_day": 0,
        "outbound_per_hour": 0,
        "outbound_recipients_per_message": 0,
        "outbound_allowlist": False,
        "verify_full": False,
        "owned_accounts": int(os.environ.get("SCALE_OWNED_ACCOUNTS_LIMIT", "5")),
    },
    
    
    # live /pricing/ Business row (dashboard/views.py _pricing_context).
    # custom_domains is the NEW capability key gating apps.domains — see
    # apps.accounts.entitlements.can_add_domain. Business is sold self-serve
    # via Lemon Squeezy (LS_VARIANT_BUSINESS, 2026-09-30) whenever
    # ENABLE_BILLING is on, exactly like Scale.
    "business": {
        "inboxes": int(os.environ.get("BUSINESS_INBOX_LIMIT", "200")),
        "messages_per_month": int(os.environ.get("BUSINESS_MONTHLY_MESSAGE_LIMIT", "250000")),
        "webhooks": True,
        "retention_days": int(os.environ.get("BUSINESS_RETENTION_DAYS", "90")),
        "wait_concurrency": int(os.environ.get("BUSINESS_WAIT_CONCURRENCY", "40")),
        "is_paid_tier": True,
        "custom_domains": int(os.environ.get("BUSINESS_DOMAIN_LIMIT", "10")),
        #  (Team Seats): added via the exact custom_domains
        
        
        # them) — see apps.accounts.entitlements.can_invite_member.
        "seats": int(os.environ.get("BUSINESS_SEATS", "5")),
        
        # §G): new capability key gating apps.api.views.DomainCatchAllView —
        # Business only, mirrors the custom_domains fold. False on every
        # other tier -> 403 catch_all_entitlement_required.
        "catch_all": True,
        
        
        # 0 for all three below regardless of status (see the fold entries
        # under those tiers) -> can_send_outbound is False unconditionally,
        # one of the two independent walls against demo/agent/free abuse.
        "outbound_per_day": int(os.environ.get("OUTBOUND_BUSINESS_PER_DAY", "500")),
        "outbound_per_hour": int(os.environ.get("OUTBOUND_BUSINESS_PER_HOUR", "100")),
        "outbound_recipients_per_message": int(
            os.environ.get("OUTBOUND_BUSINESS_RECIPIENTS_PER_MESSAGE", "10")
        ),
        
        # CONFIGURATION capability key — gates who may configure the allowlist
        # (apps.accounts.entitlements.can_configure_outbound_allowlist),
        # Business-only. Separate from Account.outbound_allowlist_enabled,
        # which is the ENFORCEMENT flag a plan lapse must never touch.
        "outbound_allowlist": True,
        
        # step b): entitlement key gating mode="full" — Business only,
        # mirrors the catch_all/outbound_allowlist fold exactly. False on
        # every other tier -> 403 verify_full_entitlement_required.
        "verify_full": True,
        
        "owned_accounts": int(os.environ.get("BUSINESS_OWNED_ACCOUNTS_LIMIT", "5")),
    },
}

# ops alert per UTC day when messages received today (UTC) by Free-plan
# accounts exceed this (apps.accounts.abuse_alerts).
FREE_DAILY_MESSAGE_ALERT = int(os.environ.get("FREE_DAILY_MESSAGE_ALERT", "20000"))
# Launch-traffic abuse alerts (apps.accounts.abuse_launch, alert only, 1/kind/UTC day):
# accounts created per hour, inboxes one account created per hour, distinct
# sender domains one Free account received mail from today.
SIGNUP_BURST_ALERT = int(os.environ.get("SIGNUP_BURST_ALERT", "50"))
INBOX_BURST_ALERT = int(os.environ.get("INBOX_BURST_ALERT", "30"))
SENDER_DOMAINS_ALERT = int(os.environ.get("SENDER_DOMAINS_ALERT", "25"))
# Derived, not duplicated — keeps RETENTION_DAYS (consumed by retention.py,
# the two retention management commands, and the pricing page) permanently in
# sync with BILLING_PLAN_LIMITS.
RETENTION_DAYS = {
    plan: limits["retention_days"] for plan, limits in BILLING_PLAN_LIMITS.items()
}
SESSION_COOKIE_AGE = 14 * 24 * 60 * 60
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"

CSRF_FAILURE_VIEW = "config.views.csrf_failure"
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/app/"

EDGE_HEADER_MONITOR = False
# D3 (N1): default URL for the hourly
# check_edge_health command's probe. Derived, not a required env — flag-off
# and production-default paths both fall back to PUBLIC_BASE_URL, so no new
# required env is introduced.
EDGE_PROBE_URL = os.environ.get(
    "EDGE_PROBE_URL", PUBLIC_BASE_URL.rstrip("/") + "/healthz/edge"
)
EDGE_MISSING_ALERT_THRESHOLD = 2
# Public /status page probes (apps.status). Container names on the compose
# networks web already joins: postfix on mail_internal, mcp on app_public.
STATUS_PROBE_SMTP_HOST = os.environ.get("STATUS_PROBE_SMTP_HOST", "postfix")
STATUS_PROBE_SMTP_PORT = int(os.environ.get("STATUS_PROBE_SMTP_PORT", "25"))
STATUS_PROBE_MCP_URL = os.environ.get("STATUS_PROBE_MCP_URL", "http://mcp:8080/healthz")
# Comma list of CIDRs whose requests legitimately arrive WITHOUT
# CF-Connecting-IP (internal peers on app_public, e.g. the remote-MCP container
# calling web:8000 directly). Exempts ONLY EdgeHeaderMonitorMiddleware's
# anomaly counter — never any _client_ip / rate-limit IP logic. Default empty;
# docker-compose.yml sets the mcp container's static /32.
EDGE_MONITOR_EXEMPT_CIDRS = os.environ.get("EDGE_MONITOR_EXEMPT_CIDRS", "")

# trusted (apps.accounts.client_ip). Loopback is always trusted. Production:
# the cloudflared container's pinned /32 (docker-compose.yml). From any other
# peer the header is ignored and REMOTE_ADDR is the client IP. Malformed entry
# -> nothing trusted (fail toward REMOTE_ADDR).
TRUSTED_PROXY_CIDRS = os.environ.get("TRUSTED_PROXY_CIDRS", "")

INGEST_SHARED_SECRET = os.environ.get("INGEST_SHARED_SECRET", "development-ingest-secret")
INGEST_ALLOWED_CIDRS = os.environ.get("INGEST_ALLOWED_CIDRS", "127.0.0.0/8,::1/128")
MAIL_DOMAIN = os.environ.get("MAIL_DOMAIN", "in.inboxpipe.net")
MAX_INBOUND_BYTES = int(os.environ.get("MAX_INBOUND_BYTES", "10485760"))
MAX_PARSE_HEADER_BYTES = int(os.environ.get("MAX_PARSE_HEADER_BYTES", "65536"))
MAX_PARSE_HEADERS = int(os.environ.get("MAX_PARSE_HEADERS", "200"))
MAX_PARSE_MIME_DEPTH = int(os.environ.get("MAX_PARSE_MIME_DEPTH", "20"))
MAX_PARSE_MIME_PARTS = int(os.environ.get("MAX_PARSE_MIME_PARTS", "200"))
MAX_PARSE_TEXT_BYTES = int(os.environ.get("MAX_PARSE_TEXT_BYTES", "100000"))
MAX_PARSE_HTML_BYTES = int(os.environ.get("MAX_PARSE_HTML_BYTES", "200000"))
MAX_EXTRACT_BYTES = int(os.environ.get("MAX_EXTRACT_BYTES", "100000"))
MAX_PARSE_ATTEMPTS = int(os.environ.get("MAX_PARSE_ATTEMPTS", "5"))
PARSE_LEASE_SECONDS = int(os.environ.get("PARSE_LEASE_SECONDS", "300"))
PARSE_RETRY_BASE_SECONDS = int(os.environ.get("PARSE_RETRY_BASE_SECONDS", "60"))
PARSE_RETRY_MAX_SECONDS = int(os.environ.get("PARSE_RETRY_MAX_SECONDS", "3600"))
# Django otherwise applies its 2.5 MiB form-oriented request cap before the
# ingest view can enforce the authoritative raw-message limit.
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_INBOUND_BYTES + 1

#  (BYOD) master switch. Default False: the domains app model
# exists and migrations apply, but the CRUD/verify API + dashboard UI are
# hidden/403 until this is flipped — and even then, no mail actually routes
# to a custom domain until Stage B's Postfix wiring ships (see

# resolve_inbox/parse_envelope_recipient/store_message either way.
ENABLE_BYOD = os.environ.get("ENABLE_BYOD", "false").lower() == "true"

#  (Team Seats) master switch. Default False: TeamMembership/
# TeamInvite models exist and migrations apply, but the invite/accept/revoke
# dashboard UI + mail are hidden/404 until this is flipped — and even then, a
# membership row grants NO access to anything: tenant(request) does not

# Zero effect on any of the 51 apps.api.views request.user sites or the 23
# apps.dashboard.views request.user sites either way.
ENABLE_TEAMS = os.environ.get("ENABLE_TEAMS", "false").lower() == "true"


# Default False: AccountOwnership rows and migrations exist, but tenant()
# never consults the session key, and the switch/new dashboard endpoints are
# a uniform 404 (mirrors ENABLE_TEAMS's flag-off-identity idiom exactly) —
# see apps.accounts.tenancy.tenant()'s combined fast path. Deploys inert.
ENABLE_MULTI_ACCOUNT = os.environ.get("ENABLE_MULTI_ACCOUNT", "false").lower() == "true"


# from AGENT_ACCOUNT_DOMAIN (below) — reusing it would make
# is_demo_or_agent_user() true for workspace users and silently disable the

# sec-gap item 4: the default lives under the non-registrable `.internal`
# TLD (like agent./demo@mailsocket.internal) — the previous default sat on
# the public, unregistered mailsocket.dev, which a third party could buy and
# receive workspace-user mail (e.g. reset links) for.
WORKSPACE_ACCOUNT_DOMAIN = os.environ.get(
    "WORKSPACE_ACCOUNT_DOMAIN", "workspace.mailsocket.internal"
)

# Funnel report (manage.py funnel_report, apps.accounts.funnel): accounts that
# are never counted as signups/activations. Staff/superusers, the demo user

# accounts plus glob patterns (one "*" at most, matched case-insensitively on
# Account.email_normalized). FUNNEL_EXCLUDED_EMAILS_EXTRA (comma-separated)

# the repo.
FUNNEL_EXCLUDED_EMAILS = [
    *[
        email.strip().lower()
        for email in os.environ.get("FUNNEL_EXCLUDED_EMAILS_EXTRA", "").split(",")
        if email.strip()
    ],
]
FUNNEL_EXCLUDED_EMAIL_GLOBS = [f"e2e-*@{MAIL_DOMAIN}"]


# switch. Default False: apps.outbound models exist and migrations apply,
# and the additive Domain.dkim_* fields exist, but the whole API surface
# (POST /outbound_emails, GET status/list, POST /domains/{id}/sending_setup)
# is a UNIFORM 404 until this flips (mirrors ENABLE_BYOD's _byod_disabled_
# response pattern) — see apps.api.views._outbound_disabled_response. The
# drain management command (send_outbound_emails) also no-ops when this is
# False, so even a stray cron invocation sends nothing. Zero effect on any
# existing mail path (milter noreply@ signing, resolve_inbox,
# parse_envelope_recipient, store_message) either way.
ENABLE_OUTBOUND = os.environ.get("ENABLE_OUTBOUND", "false").lower() == "true"

# Encrypts Domain.dkim_private_key_enc at rest — same required-env discipline
# as WEBHOOK_SIGNING_MASTER_KEY (production.py get_required_env). A Fernet
# key derived from this value; rotating it invalidates every stored DKIM
# private key (customers would need to re-run sending_setup).
OUTBOUND_DKIM_MASTER_KEY = os.environ.get(
    "OUTBOUND_DKIM_MASTER_KEY", "development-only-outbound-dkim-master-key"
)
# DKIM selector for all customer outbound signing (design §2): the DNS TXT
# record customers publish is always at <selector>._domainkey.<domain>.
OUTBOUND_DKIM_SELECTOR = "msout1"


# for any single OutboundEmail. This is the FLOOR shared by every Business
# tenant regardless of BILLING_PLAN_LIMITS[plan]["outbound_recipients_per_message"]
# (the two are checked together — see apps.outbound.services).
OUTBOUND_MAX_RECIPIENTS = int(os.environ.get("OUTBOUND_MAX_RECIPIENTS", "10"))
# Body size cap (design §1): body_text/body_html combined, bytes.
OUTBOUND_MAX_BODY_BYTES = int(os.environ.get("OUTBOUND_MAX_BODY_BYTES", "262144"))
# Per-tenant queue-depth guard (design §3 step 8): COUNT(status IN
# queued,sending) >= this -> 429. Default 2x the daily recipient cap, a
# generous but bounded backlog ceiling for a compromised key (design §5.6).
OUTBOUND_MAX_QUEUE_DEPTH = int(os.environ.get("OUTBOUND_MAX_QUEUE_DEPTH", "1000"))
# Global fleet cap enforced AT DRAIN (design §4), NOT at enqueue — the IP's
# total output is bounded per hour no matter how many tenants misbehave
# simultaneously. Deliberately small initially (IP warm-up posture, Q1).
OUTBOUND_GLOBAL_HOURLY_CAP = int(os.environ.get("OUTBOUND_GLOBAL_HOURLY_CAP", "200"))
# Drain batch size per management-command pass (design §4): hard ceiling on
# how many mails can leave the IP per invocation (run every minute by cron,
# operations notes pattern) — smooth-by-construction, never a burst spike.
OUTBOUND_DRAIN_BATCH_SIZE = int(os.environ.get("OUTBOUND_DRAIN_BATCH_SIZE", "20"))
# Retry schedule + max attempts (design §4, clones WEBHOOK_RETRY_SCHEDULE_SECONDS
# / WEBHOOK_MAX_ATTEMPTS / WEBHOOK_LEASE_SECONDS / WEBHOOK_RETRY_JITTER_SECONDS
# exactly — the proven webhooks/engine.py shape).
OUTBOUND_RETRY_SCHEDULE_SECONDS = (0, 60, 300, 1800)
OUTBOUND_MAX_ATTEMPTS = int(os.environ.get("OUTBOUND_MAX_ATTEMPTS", "4"))
OUTBOUND_LEASE_SECONDS = int(os.environ.get("OUTBOUND_LEASE_SECONDS", "30"))
OUTBOUND_RETRY_JITTER_SECONDS = int(os.environ.get("OUTBOUND_RETRY_JITTER_SECONDS", "5"))

# whose bounce ratio over its last OUTBOUND_AUTO_PAUSE_WINDOW sent emails
# exceeds this fraction gets Account.outbound_paused_at set; the drain

OUTBOUND_AUTO_PAUSE_BOUNCE_RATIO = float(os.environ.get("OUTBOUND_AUTO_PAUSE_BOUNCE_RATIO", "0.2"))
OUTBOUND_AUTO_PAUSE_WINDOW = int(os.environ.get("OUTBOUND_AUTO_PAUSE_WINDOW", "50"))

# decision): per-tenant cap on ApprovedRecipient rows — bounds unbounded list
# growth / enumeration hammering (design R7).
OUTBOUND_ALLOWLIST_MAX_ENTRIES = int(os.environ.get("OUTBOUND_ALLOWLIST_MAX_ENTRIES", "1000"))

# to keep mailsocket.app's DMARC posture untouched). VERP envelope sender is
# obe_<public_id>@<this>.
# Canonical form (lower-case, no surrounding space, no trailing dot): the VERP
# sender (apps.outbound.engine) and the funnel's "not external" filter

OUTBOUND_BOUNCE_DOMAIN = (
    os.environ.get("OUTBOUND_BOUNCE_DOMAIN", "bounces.inboxpipe.net").strip().lower().rstrip(".")
)
# Belt-and-braces denylist (design §2 point 1): explicit refusal even though
# a Domain row can never legitimately be one of these (Domain._validate_not_
# reserved_apex already blocks it at create time) — defense in depth so a
# monkeypatched/bugged Domain lookup can never make the invariant pass for
# our own infra domains. RESERVED_APEX_SUFFIXES is imported from
# apps.domains.models at use time (see apps.outbound.services) to avoid a
# load-order dependency (apps.outbound loads after apps.domains, so this is
# actually safe either way, but the check stays local to the function that
# needs it, matching can_add_domain's own local-import discipline).


# Master switch, exact ENABLE_OUTBOUND idiom (design §4a): default False, the
# whole mode="full" surface is 403 smtp_verify_disabled until this flips.
# Deploys inert. Re-checked a second time INSIDE apps.api.smtp_probe
# (defense in depth, mirrors apps.outbound.engine.drain's flag check at the
# point of any real I/O).
ENABLE_SMTP_VERIFY = os.environ.get("ENABLE_SMTP_VERIFY", "false").lower() == "true"
# HELO/EHLO identity for the probe (design §7) — matches the PTR of the

SMTP_PROBE_HELO_HOST = os.environ.get("SMTP_PROBE_HELO_HOST", "mx.inboxpipe.net")
# Timeout budget (design §4g): total wall-clock per probe request, and the
# per-connect timeout (never larger than the remaining total budget).
SMTP_PROBE_TOTAL_BUDGET_SECONDS = float(os.environ.get("SMTP_PROBE_TOTAL_BUDGET_SECONDS", "12"))
SMTP_PROBE_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("SMTP_PROBE_CONNECT_TIMEOUT_SECONDS", "5"))
# Full-mode rate limits (design §4b) — separate buckets, deliberately harder
# than safe mode's 500/key/1500/account per hour. All env-overridable

VERIFY_FULL_KEY_RATE_LIMIT = int(os.environ.get("VERIFY_FULL_KEY_RATE_LIMIT", "50"))
VERIFY_FULL_KEY_RATE_WINDOW = int(os.environ.get("VERIFY_FULL_KEY_RATE_WINDOW", "3600"))
VERIFY_FULL_ACCOUNT_RATE_LIMIT = int(os.environ.get("VERIFY_FULL_ACCOUNT_RATE_LIMIT", "100"))
VERIFY_FULL_ACCOUNT_RATE_WINDOW = int(os.environ.get("VERIFY_FULL_ACCOUNT_RATE_WINDOW", "3600"))
# Per-target-domain cooldown (design §4d) — GLOBAL across all accounts (the
# remote MX sees one probing IP; reputation is shared, so the bucket subject
# is the domain, not the tenant).
VERIFY_FULL_PER_DOMAIN_LIMIT = int(os.environ.get("VERIFY_FULL_PER_DOMAIN_LIMIT", "10"))
VERIFY_FULL_PER_DOMAIN_WINDOW = int(os.environ.get("VERIFY_FULL_PER_DOMAIN_WINDOW", "600"))

# fleet cap: the IP's total probe output is bounded per hour no matter how
# many tenants misbehave simultaneously.
VERIFY_FULL_GLOBAL_HOURLY_CAP = int(os.environ.get("VERIFY_FULL_GLOBAL_HOURLY_CAP", "500"))
# Global in-flight cap (design §4c) — separate pool from VERIFY_CAPACITY_*

VERIFY_FULL_CAPACITY_LIMIT = int(os.environ.get("VERIFY_FULL_CAPACITY_LIMIT", "2"))



# for the whole loop; FULL holds one verify-full slot (of 2) for the whole
# loop — keep both hold times short, hence the smaller FULL cap.
VERIFY_BATCH_MAX_SAFE = int(os.environ.get("VERIFY_BATCH_MAX_SAFE", "100"))
VERIFY_BATCH_MAX_FULL = int(os.environ.get("VERIFY_BATCH_MAX_FULL", "25"))
# Wall-clock budget for the WHOLE batch loop (design §3f), under the
# gunicorn --timeout 120 / Cloudflare ~100s envelope (design §0). Rows not
# started before this deadline degrade to deliverable="unknown",
# reason="batch_budget_exhausted" — charge/meter still count them (no
# refund race, design §3f).
VERIFY_BATCH_DEADLINE_SECONDS = int(os.environ.get("VERIFY_BATCH_DEADLINE_SECONDS", "60"))
# 64KB (design §6b): 100 rows at up to 320 chars/token is ~32KB even with
# padding — deliberately smaller than a naive "1MB" guess, the cap the
# actual row cap can ever use.
VERIFY_BATCH_FILE_MAX_BYTES = int(os.environ.get("VERIFY_BATCH_FILE_MAX_BYTES", "65536"))

# --- Billing (Lemon Squeezy) master switch ------------------------------------

# False: apps.billing (BillingEvent) exists and migrations apply, but the
# checkout/billing-page/portal dashboard routes and the LS webhook route are
# all a uniform Http404 until this flips (exact ENABLE_OUTBOUND/ENABLE_TEAMS
# flag-off-identity idiom). Zero effect on any existing request.user/tenant()
# site either way. TEST-MODE defaults below are env-overridable and NONE are
# required off-flag — production.py gates the two secrets behind the flag
# (the exact OUTBOUND_DKIM_MASTER_KEY 2026-09-28 incident lesson).
ENABLE_BILLING = os.environ.get("ENABLE_BILLING", "false").lower() == "true"
LS_API_KEY = os.environ.get("LS_API_KEY", "")
LS_WEBHOOK_SECRET = os.environ.get("LS_WEBHOOK_SECRET", "development-only-ls-webhook-secret")
LS_STORE_ID = os.environ.get("LS_STORE_ID", "482773")
LS_VARIANT_PAID = os.environ.get("LS_VARIANT_PAID", "2176466")
LS_VARIANT_SCALE = os.environ.get("LS_VARIANT_SCALE", "2176478")
LS_VARIANT_BUSINESS = os.environ.get("LS_VARIANT_BUSINESS", "2187025")
LS_WEBHOOK_MAX_BYTES = int(os.environ.get("LS_WEBHOOK_MAX_BYTES", "262144"))
# HOTFIX P0 (checkout-form-action): checkout_create/portal 303-redirect a plain
# POST form to this exact origin (LS-hosted checkout AND, per LS docs, the
# store-subdomain customer portal — both live under the store's own
# subdomain, never app.lemonsqueezy.com). CSP form-action must allow it or
# Chrome blocks the cross-origin form-submission redirect and checkout is
# dead (0 orders). No wildcard: see apps.accounts.middleware._default_csp().
LS_CHECKOUT_ORIGIN = os.environ.get("LS_CHECKOUT_ORIGIN", "https://mailsocket.lemonsqueezy.com")
# BL-21 dead-man: manage.py billing_reconcile GETs this Healthchecks.io ping URL
# after every completed run. Empty (default) = no ping, logged as skipped.
HEALTHCHECKS_RECONCILE_URL = os.environ.get("HEALTHCHECKS_RECONCILE_URL", "")


def strict_bool_env(name: str, default: bool) -> tuple[bool | None, str | None]:
    """(value, raw) for a boolean env var parsed STRICTLY: exactly "true"/"1"
    -> True, "false"/"0" -> False, unset -> ``default``. Anything else (incl.
    "True", "yes", "" or surrounding spaces) -> None, which a system check
    turns into a refusal to start (never a silent guess)."""
    raw = os.environ.get(name)
    if raw is None:
        return default, None
    return {"true": True, "1": True, "false": False, "0": False}.get(raw), raw


# --- Billing kill switch (operations notes) -----------------
# false/0 = checkout + plan changes refused before any Lemon Squeezy call
# (apps.billing.kill_switch); the LS webhook and the customer portal keep
# working. Default true. An invalid value refuses to start (mailsocket.E008).
BILLING_CHANGES_ENABLED, BILLING_CHANGES_ENABLED_RAW = strict_bool_env("BILLING_CHANGES_ENABLED", True)

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.api.authentication.BearerAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": [],
    "EXCEPTION_HANDLER": "apps.api.errors.api_exception_handler",
    "UNAUTHENTICATED_USER": None,
    "DATETIME_FORMAT": "%Y-%m-%dT%H:%M:%SZ",
}
