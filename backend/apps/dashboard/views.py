import json
import math
from datetime import timedelta as _timedelta
from pathlib import Path

from django.conf import settings
from django.contrib import messages as messages_framework
from django.contrib.auth.views import redirect_to_login
from django.core import signing
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET, require_http_methods

from apps.accounts.entitlements import can_configure_webhook, can_create_inbox, usage_summary
from apps.accounts.models import Account, AccountPlan, ApiKey, MonthlyUsage
from apps.accounts.services import (
    AGENT_REGISTER_RATE_LIMIT,
    VERIFY_ACCOUNT_RATE_LIMIT,
    WAIT_LEASE_LIMIT_FREE,
    WAIT_LEASE_LIMIT_PAID,
    ApiKeyLimitExceeded,
    RateLimitExceeded,
    create_api_key,
    increment_rate_limit,
)
from apps.accounts.tenancy import require_owner, tenant
from apps.accounts.views import _is_relative_path
from apps.inboxes.filters import SUBJECT_CONTAINS_MAX, apply_message_filters, parse_bool
from apps.inboxes.models import Inbox, Message
from apps.inboxes.services import random_routing_key
from apps.webhooks.models import Webhook, WebhookDelivery
from apps.webhooks.security import SSRFValidationError
from apps.webhooks.services import create_webhook, delete_webhook, rotate_signing_secret, update_webhook

from .context_processors import site_origins
from .decorators import post_only
from .forms import ApiKeyCreateForm, InboxCreateForm, InboxRenameForm, WebhookForm
from .partials import render_partial

CURSOR_SALT = "dashboard.messages.cursor.v1"
PAGE_SIZE = 25
PREVIEW_CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"


# --- API reference data (A2) --------------------------------------------------
# A declarative, hand-verified mirror of docs/openapi.yaml for the browsable
# HTML page. Every path, method, param, default and status code here is read
# from that spec (which CI validates against the real serializers/views), so
# the two stay consistent. This page never invents an endpoint or a parameter.
#
# PUBLIC copy only: every string below is rendered verbatim on /docs/api/ and
# /llms.txt, so no internal ticket names, flag names or file paths
# (test_public_copy_has_no_internal_jargon enforces this).

# charged before any message is written: per inbox and per account (tenant).
# Shared by the dashboard button and POST /api/v1/inboxes/{id}/test-code
# (same bucket names, so both paths draw from one quota).
TEST_CODE_INBOX_RATE_NAME = "dashboard:test-code:inbox:hour"
TEST_CODE_INBOX_RATE_LIMIT = 5
TEST_CODE_ACCOUNT_RATE_NAME = "dashboard:test-code:account:hour"
TEST_CODE_ACCOUNT_RATE_LIMIT = 20
TEST_CODE_RATE_WINDOW = 3600

API_ENDPOINTS = [
    {
        "method": "POST",
        "path": "/api/v1/agents/register",
        "summary": "Create an API key and a first inbox with no signup (no auth, no email). Idempotency-Key is not supported here (no account to scope it to); a retry creates a new key",
        "auth": None,
        "params": [],
        "body": '{"name": "optional label, max 60 chars"}',
        "response_schema": None,
        "statuses": [
            ("201", "api_key (shown once), key_prefix, plan, and the new inbox."),
            ("429", "Registration rate limit exceeded (10 per hour per IP, plus a service-wide hourly cap)."),
        ],
    },
    {
        "method": "POST",
        "path": "/api/v1/verify",
        "summary": "Check whether an email address looks deliverable (syntax, MX and heuristics; never an SMTP probe)",
        "auth": "Bearer",
        "params": [],
        "body": '{"email": "someone@example.com"}',
        "response_schema": None,
        "statuses": [
            ("200", "deliverable (deliverable | risky | undeliverable | unknown), syntax_valid, has_mx, is_disposable, is_role, reason."),
            ("401", "Missing or invalid API key."),
            ("403", "Account is not permitted to use the API."),
            ("422", "validation_error: email missing or blank, or bad mode (bad email = 200 verdict)."),
            ("429", "Rate limit exceeded (per key, per account, or service-wide verify capacity)."),
        ],
    },
    {
        "method": "POST",
        "path": "/api/v1/verify/batch",
        "summary": "Check up to 100 addresses in one call; each row is charged like a single /verify call",
        "auth": "Bearer",
        "params": [],
        "body": '{"emails": ["a@x.com", ...]} or {"text": "pasted list"} or multipart file=<.csv|.txt>. Send exactly one of the three, plus optional "mode": "safe" (default) | "full".',
        "response_schema": None,
        "statuses": [
            ("200", "data.results (one row per address, same shape as POST /verify, in input order) and data.summary (total, deliverable, risky, undeliverable, unknown)."),
            ("401", "Missing or invalid API key."),
            ("403", "Account is not permitted, or mode \"full\" was requested (smtp_verify_disabled; full mode is not generally available)."),
            ("422", "validation_error (send exactly one input, with at least one address), batch_too_large (over 100 unique rows, 25 in full mode), unsupported_file_type, malformed_file, or file_too_large (64 KB cap)."),
            ("429", "The whole batch did not fit your remaining /verify budget. Nothing was charged."),
        ],
    },
    {
        "method": "GET",
        "path": "/api/v1/inboxes/{id}/stream",
        "summary": "Server-Sent Events stream of new messages (an EventSource-compatible alternative to /wait)",
        "auth": "Bearer",
        "params": [
            {"name": "id", "type": "string (path)", "default": "required", "constraint": "inbox public id"},
            {"name": "since", "type": "string", "default": "request start time", "constraint": "ISO-8601 datetime | message public_id | unix timestamp; messages strictly after this match"},
            {"name": "require", "type": "string", "default": "any", "constraint": "otp | link | any (anything else returns 422)"},
            {"name": "min_confidence", "type": "number", "default": "0.0", "constraint": "OTP must have otp_confidence ≥ this; clamped to [0, 1]; ignored for link"},
        ],
        "body": None,
        "response_schema": None,
        "statuses": [
            ("200", "text/event-stream: `message` events (Message JSON), periodic heartbeats, and a final `bye` before the connection is recycled (about every 25s) so EventSource reconnects."),
            ("401", "Missing or invalid API key."),
            ("403", "Account is not permitted to use the API."),
            ("404", "Unknown or not owned."),
            ("422", "validation_error: `require` is not one of otp | link | any."),
            ("429", "too_many_wait_requests (your plan's concurrent-wait cap) or wait_capacity (service-wide cap). Honour Retry-After."),
        ],
    },
    {
        "method": "GET",
        "path": "/api/v1/inboxes",
        "summary": "List inboxes",
        "auth": "Bearer",
        "params": [
            {"name": "limit", "type": "integer", "default": "25", "constraint": "1–100"},
            {"name": "cursor", "type": "string", "default": "none", "constraint": "opaque, signed, bound to the key owner and filters"},
        ],
        "body": None,
        "response_schema": "InboxListResponse",
        "statuses": [
            ("200", "A page of inboxes."),
            ("401", "Missing or invalid API key."),
            ("403", "Account is not permitted to use the API."),
            ("422", "Invalid parameter."),
            ("429", "Rate limit exceeded."),
        ],
    },
    {
        "method": "POST",
        "path": "/api/v1/inboxes",
        "summary": "Create an inbox",
        "auth": "Bearer",
        "params": [
            {"name": "Idempotency-Key", "type": "string (header)", "default": "none", "constraint": "optional; 1-255 printable ASCII chars, scoped to the API key that sends it. A retry with the same key and body within 24h replays the first response (Idempotent-Replayed: true) instead of running again. Stored: 2xx and 400/404/422 validation errors. Not stored, so a retry runs again: outcomes you can fix and retry (402, 403, 409, and 422 domain_not_found_or_unverified), 429 and 5xx"},
        ],
        "body": '{"label": "optional label, max 120 chars", "routing_key": "optional custom local part, 3-64 chars, a-z/A-Z/0-9/_/- (stored lowercase)", "public": "optional bool, default false; opt-in anonymous read (shared domain only)", "metadata": "optional JSON object, <=4096 bytes compact UTF-8, <=50 top-level keys"}',
        "response_schema": "Inbox",
        "statuses": [
            ("201", "Inbox created; the Location header points at the new inbox."),
            ("400", "validation_error: the Idempotency-Key header is empty, over 255 chars, or not printable ASCII, or the body is too large to read with an Idempotency-Key."),
            ("401", "Missing or invalid API key."),
            ("403", "Account is not permitted to use the API."),
            ("409", "plan_limit_reached (plan inbox limit), slug_taken (routing_key already claimed), conflict (address allocation failed; retry), or idempotency_in_progress (a request with the same Idempotency-Key is still running)."),
            ("415", "Request media type not supported."),
            ("422", "validation_error, public_not_allowed_on_custom_domain, metadata_must_be_object, metadata_too_large, or idempotency_key_reused (same Idempotency-Key, different body)."),
            ("429", "Rate limit exceeded."),
        ],
    },
    {
        "method": "GET",
        "path": "/api/v1/inboxes/{id}",
        "summary": "Get an inbox",
        "auth": "Bearer",
        "params": [
            {"name": "id", "type": "string (path)", "default": "required", "constraint": "inbox public id, e.g. inbox_..."},
        ],
        "body": None,
        "response_schema": "InboxDetail",
        "statuses": [
            ("200", "The inbox, plus message_count_month and webhook_configured."),
            ("401", "Missing or invalid API key."),
            ("403", "Account is not permitted to use the API."),
            ("404", "Unknown or not owned."),
            ("429", "Rate limit exceeded."),
        ],
    },
    {
        "method": "DELETE",
        "path": "/api/v1/inboxes/{id}",
        "summary": "Delete an inbox",
        "auth": "Bearer",
        "params": [
            {"name": "id", "type": "string (path)", "default": "required", "constraint": "inbox public id, e.g. inbox_..."},
        ],
        "body": None,
        "response_schema": None,
        "statuses": [
            ("204", "Inbox deleted. A second delete returns 404."),
            ("401", "Missing or invalid API key."),
            ("403", "Account is not permitted to use the API."),
            ("404", "Unknown or not owned."),
            ("429", "Rate limit exceeded."),
        ],
    },
    {
        "method": "GET",
        "path": "/api/v1/inboxes/{id}/messages",
        "summary": "List messages in an inbox",
        "auth": "Bearer",
        "params": [
            {"name": "id", "type": "string (path)", "default": "required", "constraint": "inbox public id"},
            {"name": "limit", "type": "integer", "default": "25", "constraint": "1–100"},
            {"name": "cursor", "type": "string", "default": "none", "constraint": "opaque, signed, bound to the key owner and filters"},
            {"name": "has_otp", "type": "boolean", "default": "none", "constraint": "keep only messages with an OTP"},
            {"name": "subject_contains", "type": "string", "default": "none", "constraint": "case-insensitive substring, capped at 200 chars"},
            {"name": "from", "type": "string", "default": "none", "constraint": "case-insensitive substring of the From header"},
        ],
        "body": None,
        "response_schema": "MessageSummaryListResponse",
        "statuses": [
            ("200", "A page of message summaries."),
            ("401", "Missing or invalid API key."),
            ("403", "Account is not permitted to use the API."),
            ("404", "Unknown or not owned."),
            ("422", "Invalid parameter."),
            ("429", "Rate limit exceeded."),
        ],
    },
    {
        "method": "GET",
        "path": "/api/v1/inboxes/{id}/messages/latest",
        "summary": "Get the latest message in an inbox",
        "auth": "Bearer",
        "params": [
            {"name": "id", "type": "string (path)", "default": "required", "constraint": "inbox public id"},
        ],
        "body": None,
        "response_schema": "Message",
        "statuses": [
            ("200", "The newest message (full representation)."),
            ("401", "Missing or invalid API key."),
            ("403", "Account is not permitted to use the API."),
            ("404", "Unknown inbox, or no messages yet."),
            ("429", "Rate limit exceeded."),
        ],
    },
    {
        "method": "GET",
        "path": "/api/v1/inboxes/{id}/messages/wait",
        "summary": "Wait for an OTP or magic link",
        "auth": "Bearer",
        "params": [
            {"name": "id", "type": "string (path)", "default": "required", "constraint": "inbox public id"},
            {"name": "timeout", "type": "number (seconds)", "default": "20", "constraint": "clamped to [1, 25]"},
            {"name": "since", "type": "string", "default": "request start time", "constraint": "ISO-8601 datetime | message public_id | unix timestamp; messages strictly after this match"},
            {"name": "require", "type": "string", "default": "otp", "constraint": "otp | link | any (anything else returns 422)"},
            {"name": "min_confidence", "type": "number", "default": "0.0", "constraint": "OTP must have otp_confidence ≥ this; clamped to [0, 1]; ignored for link"},
        ],
        "body": None,
        "response_schema": "Message",
        "statuses": [
            ("200", "A matching parsed message arrived."),
            ("204", "No match yet. Call again with since=<the Mailsocket-Wait-Since response header>."),
            ("401", "Missing or invalid API key."),
            ("403", "Account is not permitted to use the API."),
            ("404", "Unknown or not owned."),
            ("422", "validation_error: `require` is not one of otp | link | any (fields.require lists the allowed values)."),
            ("429", "too_many_wait_requests (your plan's concurrent-wait cap) or wait_capacity (service-wide cap). Honour Retry-After."),
        ],
    },
    {
        "method": "POST",
        "path": "/api/v1/inboxes/{id}/test-code",
        "summary": "Send yourself a test code: puts one sample OTP email straight into your own inbox (it does not go through the mail server), so you can see wait return 200 without sending real mail. Same feature and same limits as the dashboard \"Send a test code\" button",
        "auth": "Bearer",
        "params": [
            {"name": "id", "type": "string (path)", "default": "required", "constraint": "inbox public id"},
            {"name": "Idempotency-Key", "type": "string (header)", "default": "none", "constraint": "optional; 1-255 printable ASCII chars, scoped to the API key that sends it. A retry with the same key within 24h replays the first response (Idempotent-Replayed: true) instead of sending a second code. Stored: 2xx and 400/404 errors. Not stored, so a retry runs again: 409, 429 and 5xx"},
        ],
        "body": None,
        "response_schema": None,
        "statuses": [
            ("201", "data.message_id (msg_...) and data.sent_at. The code is parsed like real mail: it counts toward your monthly messages and fires the inbox's webhook. The Location header points at the message."),
            ("400", "validation_error: the Idempotency-Key header is empty, over 255 chars, or not printable ASCII."),
            ("401", "Missing or invalid API key."),
            ("403", "Account is not permitted to use the API."),
            ("404", "Unknown, deleted or not owned. All cases return the same response."),
            ("409", "inbox_disabled: enable the inbox first; or idempotency_in_progress (a request with the same Idempotency-Key is still running)."),
            ("422", "idempotency_key_reused: the same Idempotency-Key was already used on another endpoint."),
            ("429", "rate_limited: at most " + str(TEST_CODE_INBOX_RATE_LIMIT) + " test codes per inbox and " + str(TEST_CODE_ACCOUNT_RATE_LIMIT) + " per account per hour, shared with the dashboard button. error.message says how many minutes to wait; Retry-After gives the seconds."),
        ],
    },
    {
        "method": "GET",
        "path": "/api/v1/messages/{id}",
        "summary": "Get a message",
        "auth": "Bearer",
        "params": [
            {"name": "id", "type": "string (path)", "default": "required", "constraint": "message public id, e.g. msg_..."},
        ],
        "body": None,
        "response_schema": "Message",
        "statuses": [
            ("200", "The message (full representation)."),
            ("401", "Missing or invalid API key."),
            ("403", "Account is not permitted to use the API."),
            ("404", "Unknown or not owned."),
            ("429", "Rate limit exceeded."),
        ],
    },
    {
        "method": "PATCH",
        "path": "/api/v1/messages/{id}",
        "summary": "Mark a message read or unread",
        "auth": "Bearer",
        "params": [
            {"name": "id", "type": "string (path)", "default": "required", "constraint": "message public id, e.g. msg_..."},
        ],
        "body": '{"read": true}. `read` (boolean) is the only accepted field; `read: false` marks the message unread.',
        "response_schema": "Message",
        "statuses": [
            ("200", "The updated message. Idempotent: repeating `read: true` keeps the first read_at."),
            ("401", "Missing or invalid API key."),
            ("403", "Account is not permitted to use the API."),
            ("404", "Unknown or not owned."),
            ("422", "validation_error: the body must be {\"read\": true|false}. Any other key, even alongside `read`, is rejected and named in fields.<key>."),
            ("429", "Rate limit exceeded."),
        ],
    },
    {
        "method": "POST",
        "path": "/api/v1/messages/batch",
        "summary": "Apply one action (mark_read | delete | fetch) to up to 100 of your messages (paid plans)",
        "auth": "Bearer",
        "params": [
            {"name": "Idempotency-Key", "type": "string (header)", "default": "none", "constraint": "optional; 1-255 printable ASCII chars, scoped to the API key that sends it. A retry with the same key and body within 24h replays the first response (Idempotent-Replayed: true) instead of running again. Stored: 2xx and 400/404/422 validation errors. Not stored, so a retry runs again: outcomes you can fix and retry (402, 403, 409, and 422 domain_not_found_or_unverified), 429 and 5xx"},
        ],
        "body": '{"action": "mark_read | delete | fetch", "ids": ["msg_...", "... up to 100 ids"]}',
        "response_schema": None,
        "statuses": [
            ("200", "data.results: one {id, status} per input id, in input order. status is ok | deleted | not_found (fetch also returns message). Ids you do not own are reported as not_found."),
            ("400", "validation_error: the Idempotency-Key header is empty, over 255 chars, or not printable ASCII, or the body is too large to read with an Idempotency-Key."),
            ("401", "Missing or invalid API key."),
            ("402", "plan_upgrade_required: batch operations need a paid plan."),
            ("403", "Account is not permitted to use the API."),
            ("409", "idempotency_in_progress: a request with the same Idempotency-Key is still running."),
            ("422", "validation_error: unknown action, or ids missing, empty or over 100; or idempotency_key_reused (same Idempotency-Key, different body). Idempotency-Key is ignored for fetch."),
            ("429", "Rate limit exceeded (60 batch calls per hour per account, plus the per-key limit)."),
        ],
    },
    {
        "method": "POST",
        "path": "/api/v1/inboxes/{id}/webhook/test",
        "summary": "Send one signed test delivery to the inbox's webhook (paid plans)",
        "auth": "Bearer",
        "params": [
            {"name": "id", "type": "string (path)", "default": "required", "constraint": "inbox public id"},
        ],
        "body": None,
        "response_schema": None,
        "statuses": [
            ("200", "data.delivered, data.response_status (your receiver's HTTP status), data.signature_version, data.event_id (evt_test_...). Not recorded in delivery history and never retried."),
            ("400", "webhook_ssrf_rejected: the webhook URL resolves to a private, loopback or link-local address."),
            ("401", "Missing or invalid API key."),
            ("403", "account_forbidden, or webhook_entitlement_required (the plan does not include webhooks)."),
            ("404", "not_found (unknown or not owned inbox) or webhook_not_configured."),
            ("429", "rate_limited: at most 10 test sends per inbox and 30 per account per hour."),
            ("502", "webhook_unreachable: timeout, connection or TLS failure reaching your receiver."),
        ],
    },
    {
        "method": "GET",
        "path": "/api/v1/public/emails/{email}/messages",
        "summary": "Read a public inbox's recent messages without auth (inboxes are private unless created with public: true)",
        "auth": None,
        "params": [
            {"name": "email", "type": "string (path)", "default": "required", "constraint": "a shared-domain address, e.g. someone@in.inboxpipe.net"},
            {"name": "limit", "type": "integer", "default": "20", "constraint": "1–20"},
        ],
        "body": None,
        "response_schema": None,
        "statuses": [
            ("200", "Up to `limit` newest messages. Includes otp and magic_link; excludes text, html, links, otp_context, otp_candidates and metadata. See Public inboxes above."),
            ("404", "Address malformed, unknown, private (the default), disabled, deleted, or on a custom domain. All cases return the same response."),
            ("429", "Per-IP rate limit exceeded (30 per minute)."),
        ],
    },
    # Custom domains. Creating a domain needs Scale or Business. List/get/delete/verify
    # stay available on domains the account already owns so a downgrade never
    # strands existing data.
    {
        "method": "GET",
        "path": "/api/v1/domains",
        "summary": "List your custom domains (a plan without custom domains gets an empty list)",
        "auth": "Bearer",
        "params": [
            {"name": "limit", "type": "integer", "default": "25", "constraint": "1–100"},
            {"name": "cursor", "type": "string", "default": "none", "constraint": "opaque, signed, bound to the key owner"},
        ],
        "body": None,
        "response_schema": "Domain",
        "statuses": [
            ("200", "A page of domains, newest first."),
            ("401", "Missing or invalid API key."),
            ("403", "account_forbidden, or byod_disabled (custom domains are not enabled for this account)."),
            ("429", "Rate limit exceeded."),
        ],
    },
    {
        "method": "POST",
        "path": "/api/v1/domains",
        "summary": "Register a custom domain (Scale or Business plan)",
        "auth": "Bearer",
        "params": [
            {"name": "Idempotency-Key", "type": "string (header)", "default": "none", "constraint": "optional; 1-255 printable ASCII chars, scoped to the API key that sends it. A retry with the same key and body within 24h replays the first response (Idempotent-Replayed: true) instead of running again. Stored: 2xx and 400/404/422 validation errors. Not stored, so a retry runs again: outcomes you can fix and retry (402, 403, 409, and 422 domain_not_found_or_unverified), 429 and 5xx"},
        ],
        "body": '{"name": "mail.yourcompany.com"}. `name` is a bare hostname, max 253 chars.',
        "response_schema": "Domain",
        "statuses": [
            ("201", "Domain created with status=pending_dns. Publish verification_token as a TXT record at verification_txt_host, and mx_target as MX. The Location header points at the domain."),
            ("400", "validation_error: the Idempotency-Key header is empty, over 255 chars, or not printable ASCII, or the body is too large to read with an Idempotency-Key."),
            ("401", "Missing or invalid API key."),
            ("403", "domain_entitlement_required (checked before the body), domain_limit_reached, byod_disabled, or account_forbidden (only keys created by the account owner)."),
            ("409", "domain_taken: the name is already registered, or idempotency_in_progress (a request with the same Idempotency-Key is still running)."),
            ("422", "validation_error: `name` missing, malformed, or a reserved mailsocket domain; or idempotency_key_reused (same Idempotency-Key, different body)."),
            ("429", "Rate limit exceeded."),
        ],
    },
    {
        "method": "GET",
        "path": "/api/v1/domains/{id}",
        "summary": "Get a custom domain and its DNS verification status",
        "auth": "Bearer",
        "params": [
            {"name": "id", "type": "string (path)", "default": "required", "constraint": "domain public id, e.g. dom_..."},
        ],
        "body": None,
        "response_schema": "Domain",
        "statuses": [
            ("200", "The domain (status: pending_dns | verified | failed | disabled; last_checked_at, last_error, catch_all)."),
            ("401", "Missing or invalid API key."),
            ("403", "account_forbidden, or byod_disabled."),
            ("404", "Unknown or not owned."),
            ("429", "Rate limit exceeded."),
        ],
    },
    {
        "method": "DELETE",
        "path": "/api/v1/domains/{id}",
        "summary": "Delete a custom domain",
        "auth": "Bearer",
        "params": [
            {"name": "id", "type": "string (path)", "default": "required", "constraint": "domain public id"},
        ],
        "body": None,
        "response_schema": None,
        "statuses": [
            ("204", "Domain deleted and its name released."),
            ("401", "Missing or invalid API key."),
            ("403", "account_forbidden (only keys created by the account owner), or byod_disabled."),
            ("404", "Unknown or not owned."),
            ("409", "domain_has_inboxes: remove the inboxes bound to it first."),
            ("429", "Rate limit exceeded."),
        ],
    },
    {
        "method": "POST",
        "path": "/api/v1/domains/{id}/verify",
        "summary": "Re-check the domain's TXT and MX records now",
        "auth": "Bearer",
        "params": [
            {"name": "id", "type": "string (path)", "default": "required", "constraint": "domain public id"},
        ],
        "body": None,
        "response_schema": "Domain",
        "statuses": [
            ("200", "The domain after the check. status becomes verified once both TXT and MX match. Idempotent on a verified domain."),
            ("401", "Missing or invalid API key."),
            ("403", "account_forbidden (only keys created by the account owner), or byod_disabled."),
            ("404", "Unknown or not owned."),
            ("429", "domain_verify_rate_limited: at most 1 per domain per 30s and 20 per account per hour (see Retry-After)."),
        ],
    },
    {
        "method": "PATCH",
        "path": "/api/v1/domains/{id}/catch_all",
        "summary": "Enable or disable catch-all on a verified custom domain (Business plan)",
        "auth": "Bearer",
        "params": [
            {"name": "id", "type": "string (path)", "default": "required", "constraint": "domain public id"},
        ],
        "body": '{"catch_all": true, "inbox_id": "optional inbox_... already bound to this domain; omit to create a catch-all inbox"}',
        "response_schema": "Domain",
        "statuses": [
            ("200", "The domain with the new catch_all state and catch_all_inbox_id."),
            ("401", "Missing or invalid API key."),
            ("403", "catch_all_entitlement_required, inbox_limit_reached (creating the catch-all inbox would exceed the plan's inbox limit), byod_disabled, or account_forbidden."),
            ("404", "Unknown or not owned domain, or domain_not_found_or_unverified for inbox_id."),
            ("409", "domain_not_verified: verify the domain first."),
            ("422", "validation_error (catch_all missing or not boolean) or catch_all_target_invalid (inbox_id is not a live inbox bound to this domain)."),
            ("429", "catch_all_rate_limited: at most 6 changes per domain per hour."),
        ],
    },
]

# INTERNAL, NOT PUBLISHED. Outbound sending (send, reply, forward, compose,
# recipient allowlist, DKIM setup) is built but switched off in production,
# where every route below returns 404. These entries are kept so the reference
# can be restored the day outbound ships. They are deliberately excluded from
# /docs/api/, /llms.txt, /docs/errors/ and the public openapi.yaml; the
# matching OpenAPI fragment lives in docs/internal/openapi-outbound.yaml.
UNRELEASED_API_ENDPOINTS = [
    {"method": "POST", "path": "/api/v1/domains/{id}/sending_setup", "summary": "Provision a per-domain DKIM keypair for outbound sending"},
    {"method": "POST", "path": "/api/v1/outbound_emails", "summary": "Send an email from a verified, DKIM-verified custom domain"},
    {"method": "GET", "path": "/api/v1/outbound_emails/{id}", "summary": "Get the status of an outbound email"},
    {"method": "GET", "path": "/api/v1/outbound_allowlist", "summary": "List approved outbound recipients"},
    {"method": "POST", "path": "/api/v1/outbound_allowlist", "summary": "Approve an outbound recipient"},
    {"method": "DELETE", "path": "/api/v1/outbound_allowlist/{id}", "summary": "Revoke an approved recipient"},
    {"method": "POST", "path": "/api/v1/outbound_allowlist/mode", "summary": "Turn allowlist enforcement on or off"},
    {"method": "POST", "path": "/api/v1/inboxes/{inbox_id}/messages/{message_id}/reply", "summary": "Reply to an inbound message"},
    {"method": "POST", "path": "/api/v1/inboxes/{inbox_id}/messages/{message_id}/forward", "summary": "Forward an inbound message"},
    {"method": "POST", "path": "/api/v1/inboxes/{inbox_id}/compose", "summary": "Send a new email from an inbox"},
]

# Error codes only the unreleased outbound endpoints can emit. They stay in
# apps.api.errors.MESSAGES (the code paths exist) but are left out of the
# public error reference and openapi enum until outbound ships.
UNRELEASED_ERROR_CODES = frozenset({
    "outbound_disabled",
    "outbound_entitlement_required",
    "sending_domain_not_eligible",
    "recipient_rejected",
    "outbound_quota_exceeded",
    "outbound_rate_limited",
    "recipient_not_approved",
    "outbound_allowlist_entitlement_required",
    "approved_recipient_exists",
    "outbound_allowlist_limit_reached",
    "outbound_allowlist_rate_limited",
    "reply_target_invalid",
})

# Per-key request limits applied by BearerAPIView.initial (apps/api/views.py).
# The same two buckets apply on every plan; plans differ in concurrent waits.
API_KEY_RATE_LIMIT_PER_MINUTE = 120
API_KEY_RATE_LIMIT_PER_HOUR = 600


def _wait_caps_display():
    """QA #7: "2 Free / 3 Pro / 8 Scale / 16 Business" from BILLING_PLAN_LIMITS
    — the same wait_concurrency values apps.accounts.services.
    wait_lease_limit_for enforces — so no page hard-codes the per-plan wait
    caps. Business is listed since it became self-serve (2026-09-30)."""
    limits = settings.BILLING_PLAN_LIMITS
    return " / ".join(
        f'{limits[key]["wait_concurrency"]} {AccountPlan.DISPLAY_NAMES[key]}'
        for key in ("free", "paid", "scale", "business")
    )


# Error reference rows (A2). Mirrors apps.api.errors.MESSAGES minus
# UNRELEASED_ERROR_CODES, so the public table lists every code a shipped
# endpoint can emit and nothing else. Public copy: no internal names.
_ERROR_ROWS = [
    ("authentication_required", "401", "Missing or invalid API key.", "Send a valid Authorization: Bearer <key> header."),
    ("account_forbidden", "403", "The account is not permitted to use the API (unverified, suspended, or inactive).", "Verify your email, or contact support."),
    ("not_found", "404", "The resource does not exist or is not owned by this key.", "Check the id. Ids that belong to another account return the same 404."),
    ("method_not_allowed", "405", "The HTTP method is not supported for this path.", "Use the documented method for the endpoint."),
    ("not_acceptable", "406", "The requested response format is not acceptable.", "Request application/json."),
    ("conflict", "409", "The request conflicts with the current state (for example, address allocation failed).", "Retry the request."),
    ("plan_limit_reached", "409", "The plan limit for this action has been reached.", "Upgrade your plan, or free capacity first."),
    ("slug_taken", "409", "The requested inbox routing_key is already in use.", "Choose a different routing_key, or omit it for a random one."),
    ("unsupported_media_type", "415", "The request media type is not supported.", "Send a JSON request body."),
    ("validation_error", "422", "The request body or a parameter failed validation.", "Read the fields object for per-field messages."),
    ("rate_limited", "429", "The per-key request rate limit was exceeded.", "Wait for the number of seconds in Retry-After, then retry."),
    ("too_many_wait_requests", "429", "Your plan's concurrent-wait cap was reached (" + _wait_caps_display() + ").", "Wait for Retry-After seconds, or for one of your open waits to return."),
    ("wait_capacity", "429", "The service-wide concurrent-wait limit was reached.", "Wait for Retry-After seconds, then retry."),
    ("plan_upgrade_required", "402", "Batch message operations require a paid plan.", "Upgrade to Pro or higher to use POST /messages/batch."),
    ("webhook_entitlement_required", "403", "Webhooks require a paid plan (Pro or higher).", "Upgrade your plan to configure or test a webhook."),
    ("webhook_not_configured", "404", "No webhook is configured for this inbox.", "Create a webhook for the inbox before testing it."),
    ("webhook_ssrf_rejected", "400", "The webhook URL resolves to a private, loopback or link-local address.", "Use a public HTTPS URL."),
    ("webhook_unreachable", "502", "The webhook receiver could not be reached (timeout, TLS, or network error).", "Check the receiver is up and reachable over HTTPS."),
    ("server_error", "500", "An internal error occurred.", "Retry. If it persists, send us the request_id."),
    ("byod_disabled", "403", "Custom domains are not enabled for this account.", "Contact support to enable custom domains."),
    ("domain_entitlement_required", "403", "Custom domains require a Scale or Business plan.", "Upgrade to Scale or Business to add a custom domain."),
    ("domain_limit_reached", "403", "The custom-domain limit for this plan has been reached.", "Remove an existing domain."),
    ("domain_taken", "409", "The domain name is already registered.", "Choose a different domain, or contact support if you own it."),
    ("domain_has_inboxes", "409", "The domain still has inboxes bound to it.", "Remove the bound inboxes before deleting the domain."),
    ("domain_verify_rate_limited", "429", "Too many manual domain-verify attempts.", "Wait for Retry-After seconds, then retry."),
    ("domain_not_found_or_unverified", "422", "The domain id was not found, is not owned by this account, or is not verified.", "Create and verify the domain first, or omit `domain` to use the shared domain."),
    ("public_not_allowed_on_custom_domain", "422", "Only shared-domain inboxes can be public.", "Omit `public`, or omit `domain`."),
    ("metadata_must_be_object", "422", "`metadata` must be a JSON object.", "Send `metadata` as an object, e.g. {\"run\": \"ci-123\"}."),
    ("metadata_too_large", "422", "`metadata` exceeds 4096 bytes (compact UTF-8 JSON) or 50 top-level keys.", "Keep metadata to correlation ids."),
    ("catch_all_entitlement_required", "403", "Catch-all requires a Business plan.", "Upgrade to Business to enable catch-all."),
    ("domain_not_verified", "409", "The domain must be verified before catch-all can be enabled.", "Verify the domain first (`POST /domains/{id}/verify`)."),
    ("catch_all_target", "409", "This inbox receives the domain's catch-all mail, so it cannot be deleted or disabled.", "Disable catch-all on the domain first (`PATCH /domains/{id}/catch_all` with `catch_all: false`)."),
    ("catch_all_target_invalid", "422", "The `inbox_id` is not a live inbox bound to this domain.", "Pass an enabled inbox bound to this domain, or omit inbox_id to create one."),
    ("inbox_limit_reached", "403", "Creating the catch-all inbox would exceed the plan's inbox limit.", "Remove an inbox, or pass an existing inbox_id."),
    ("catch_all_rate_limited", "429", "Too many catch-all changes for this domain.", "Wait for Retry-After seconds, then retry."),
    ("smtp_verify_disabled", "403", "Full SMTP verification (`mode: \"full\"`) is not available.", "Use `mode: \"safe\"` (the default)."),
    ("verify_full_entitlement_required", "403", "Full SMTP verification requires a Business plan.", "Use `mode: \"safe\"` (the default)."),
    ("batch_too_large", "422", "The batch has more unique addresses than allowed (100).", "Split the batch into smaller requests."),
    ("file_too_large", "422", "The uploaded file is larger than 64 KB.", "Split the file, or send the addresses as `text` or `emails`."),
    ("unsupported_file_type", "422", "The uploaded file must be a `.csv` or `.txt` file.", "Re-export the file as .csv or .txt."),
    ("malformed_file", "422", "The uploaded file is not UTF-8 text.", "Re-save the file as plain UTF-8 text."),
    ("idempotency_key_reused", "422", "The Idempotency-Key was already used in the last 24 hours with a different body or endpoint.", "Use a new key for a new request; reuse a key only to retry the exact same request."),
    ("idempotency_in_progress", "409", "A request with the same Idempotency-Key is still running.", "Retry after a short wait; you will get the stored result once it finishes."),
    ("inbox_disabled", "409", "The inbox is disabled, so it cannot receive a test code.", "Enable the inbox in the dashboard, then send the test code again."),
]
ERROR_ROWS = [{"code": c, "status": s, "meaning": m, "handle": h} for c, s, m, h in _ERROR_ROWS]


def _endpoints_for_display():
    """API_ENDPOINTS as rendered on /docs/api/ and /llms.txt.

    Only shipped endpoints are listed (unreleased ones live in
    UNRELEASED_API_ENDPOINTS and are never rendered), so every row is live.
    """
    return [dict(ep) for ep in API_ENDPOINTS]


# --- guide snippets (A5) ------------------------------------------------------
# Each mirrors examples/playwright/otp-signup.spec.ts: create the inbox address,
# trigger the signup, block on the wait endpoint, assert the OTP. These are
# HTTP-call walkthroughs against the real endpoint. We ship no official
# framework plugins, and these don't claim to be one.
PYTEST_GUIDE_SNIPPET = '''import os
import time

import requests

BASE = os.environ.get("MAILSOCKET_API_BASE", "https://dash.mailsocket.app")
API_KEY = os.environ["MAILSOCKET_API_KEY"]
INBOX_ADDRESS = os.environ["MAILSOCKET_INBOX_ADDRESS"]  # e.g. ab12cd34@in.inboxpipe.net


def wait_for_otp(inbox_id, since, total_timeout=60):
    """Re-call the wait endpoint until an OTP arrives or total_timeout passes.
    `since` is the time taken just before the signup was submitted."""
    deadline = time.monotonic() + total_timeout
    while time.monotonic() < deadline:
        resp = requests.get(
            f"{BASE}/api/v1/inboxes/{inbox_id}/messages/wait",
            params={"require": "otp", "since": since, "timeout": 25},  # 25 = server max per call
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=35,
        )
        if resp.status_code == 204:
            continue  # no code yet: call again
        resp.raise_for_status()
        return resp.json()["data"]["otp"]
    return None


def test_signup_delivers_otp():
    inbox_id = os.environ["MAILSOCKET_INBOX_ID"]
    # 1. Note the time BEFORE the submit, so a fast email still matches.
    started = time.time()
    # 2. Trigger the signup with a mailsocket inbox address.
    requests.post("https://app.example.com/signup", json={"email": INBOX_ADDRESS})
    # 3. Wait for the OTP (each call blocks up to 25s server-side).
    otp = wait_for_otp(inbox_id, since=started)
    # 4. Assert the code arrived.
    assert otp is not None
    assert otp.isdigit() and 4 <= len(otp) <= 8
'''

CYPRESS_GUIDE_SNIPPET = '''// cypress/e2e/signup.cy.js
// Set MAILSOCKET_API_KEY, MAILSOCKET_INBOX_ID and MAILSOCKET_INBOX_ADDRESS
// in your Cypress environment (cypress.env.json or process.env).
const API_BASE = Cypress.env("MAILSOCKET_API_BASE") || "https://dash.mailsocket.app";

it("signup delivers an OTP to the mailsocket inbox", () => {
  const apiKey = Cypress.env("MAILSOCKET_API_KEY");
  const inboxId = Cypress.env("MAILSOCKET_INBOX_ID");
  const address = Cypress.env("MAILSOCKET_INBOX_ADDRESS");

  // 1. Note the time BEFORE the submit, so a fast email still matches.
  //    (Cypress commands are queued, so take it inside the chain.)
  let started;
  cy.then(() => { started = Date.now() / 1000; });

  // 2. Trigger the signup with a mailsocket inbox address.
  cy.visit("https://app.example.com/signup");
  cy.get('input[name="email"]').type(address);
  cy.get('button[type="submit"]').click();

  // 3. Block on the wait endpoint (up to 25s per call). A 204 means no code
  //    yet; for slower senders, repeat the request until it returns 200.
  cy.then(() => cy.request({
    method: "GET",
    url: `${API_BASE}/api/v1/inboxes/${inboxId}/messages/wait`,
    qs: { require: "otp", since: started, timeout: 25 },
    headers: { Authorization: `Bearer ${apiKey}` },
  })).then((resp) => {
    // 4. Assert the code arrived.
    expect(resp.status).to.eq(200);
    expect(resp.body.data.otp).to.match(/^\\d{4,8}$/);
  });
});
'''

PUPPETEER_GUIDE_SNIPPET = '''const puppeteer = require("puppeteer");

const API_BASE = process.env.MAILSOCKET_API_BASE || "https://dash.mailsocket.app";
const API_KEY = process.env.MAILSOCKET_API_KEY;
const INBOX_ID = process.env.MAILSOCKET_INBOX_ID;
const INBOX_ADDRESS = process.env.MAILSOCKET_INBOX_ADDRESS;

async function waitForOtp(since, totalMs = 60_000) {
  const deadline = Date.now() + totalMs;
  const url = new URL(`${API_BASE}/api/v1/inboxes/${INBOX_ID}/messages/wait`);
  url.searchParams.set("require", "otp");
  url.searchParams.set("since", String(since)); // time taken before the submit
  url.searchParams.set("timeout", "25"); // server max per call
  while (Date.now() < deadline) {
    const res = await fetch(url, { headers: { Authorization: `Bearer ${API_KEY}` } });
    if (res.status === 204) continue; // no code yet: call again
    if (!res.ok) throw new Error(`wait failed: ${res.status}`);
    return (await res.json()).data.otp;
  }
  return null;
}

(async () => {
  const browser = await puppeteer.launch();
  const page = await browser.newPage();

  // 1. Note the time BEFORE the submit, so a fast email still matches.
  const started = Date.now() / 1000;

  // 2. Trigger the signup with a mailsocket inbox address.
  await page.goto("https://app.example.com/signup");
  await page.type('input[name="email"]', INBOX_ADDRESS);
  await page.click('button[type="submit"]');

  // 3. Wait for the OTP (each call blocks up to 25s server-side).
  const otp = await waitForOtp(started);

  // 4. Assert the code arrived (never log it: CI logs outlive the run).
  if (!otp || !/^\\d{4,8}$/.test(otp)) {
    throw new Error("expected a 4-8 digit OTP");
  }
  await page.type('input[name="code"]', otp);
  await browser.close();
})();
'''

PLAYWRIGHT_GUIDE_SNIPPET = '''// otp-signup.spec.ts
//
// Fill a signup form with a mailsocket inbox address, then block on the wait
// endpoint until the OTP arrives, using the TypeScript SDK. See the full
// reference spec (no SDK, raw APIRequestContext calls) at
// examples/playwright/otp-signup.spec.ts in the mailsocket-sdk repo.
//
// Required environment variables (never hardcode a secret here):
//   MAILSOCKET_API_KEY       a Bearer API key minted in the dashboard
//   MAILSOCKET_INBOX_ID      the inbox public id (e.g. "inbox_abc123")
//   MAILSOCKET_INBOX_ADDRESS the full inbox address shown in the dashboard
//                            (e.g. "ab12cd34@in.inboxpipe.net")
//
// npm i mailsocket-sdk, then: npx playwright test

import { test, expect } from "@playwright/test";
import { MailsocketClient } from "mailsocket-sdk";

function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

test("signup delivers an OTP to the mailsocket inbox", async ({ page }) => {
  const client = new MailsocketClient(requiredEnv("MAILSOCKET_API_KEY"));
  const inboxId = requiredEnv("MAILSOCKET_INBOX_ID");
  const email = requiredEnv("MAILSOCKET_INBOX_ADDRESS");

  // 1. Note the time BEFORE the submit, so a fast email still matches.
  const started = Date.now() / 1000;

  // 2. Kick off the signup with a mailsocket inbox address.
  await page.goto("https://app.example.com/signup");
  await page.fill('input[name="email"]', email);
  await page.click('button[type="submit"]');

  // 3. Wait for the OTP. waitForOtp repeats the call on 204 until the code
  //    arrives or its own timeout elapses (default 60s).
  const result = await client.waitForOtp(inboxId, { since: started });

  // 4. Assert the code arrived (never log it: CI logs outlive the run).
  expect(result.otp).not.toBeNull();
  expect(result.otp!).toMatch(/^\\d{4,8}$/);
});
'''

GITHUB_ACTIONS_GUIDE_SNIPPET = '''# .github/workflows/otp-test.yml
# Store MAILSOCKET_API_KEY, MAILSOCKET_INBOX_ID and MAILSOCKET_INBOX_ADDRESS
# as encrypted repository secrets (Settings -> Secrets and variables -> Actions).
name: otp-signup
on: [push]
jobs:
  otp:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0
        with:
          python-version: "3.12"
      - run: pip install pytest requests
      # test_signup.py is the pytest guide's test file.
      - name: Wait for the signup OTP
        env:
          MAILSOCKET_API_KEY: ${{ secrets.MAILSOCKET_API_KEY }}
          MAILSOCKET_INBOX_ID: ${{ secrets.MAILSOCKET_INBOX_ID }}
          MAILSOCKET_INBOX_ADDRESS: ${{ secrets.MAILSOCKET_INBOX_ADDRESS }}
        run: pytest -q test_signup.py
'''


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"



_PLAN_LADDER = ("free", "paid", "scale", "business")


def lowest_plan_name_with(predicate) -> str:
    """Display name of the cheapest plan whose BILLING_PLAN_LIMITS satisfy
    ``predicate`` — so upsell copy follows settings, never a literal."""
    for key in _PLAN_LADDER:
        limits = settings.BILLING_PLAN_LIMITS.get(key)
        if limits and predicate(limits):
            return AccountPlan.DISPLAY_NAMES.get(key, key.title())
    return AccountPlan.DISPLAY_NAMES["business"]


def _snippet_api_base():
    "Public API."
    return settings.PUBLIC_BASE_URL.rstrip("/")


def dashboard_required(view):
    def wrapped(request, *args, **kwargs):
        allowed = False
        if request.user.is_authenticated:
            acting_tenant = tenant(request)
            allowed = Account.objects.filter(
                user=acting_tenant,
                email_verified_at__isnull=False,
                is_suspended=False,
                user__is_active=True,
                user__account_plan__status__in=(
                    AccountPlan.Status.ACTIVE,
                    AccountPlan.Status.PAST_DUE,
                ),
            ).exists()
            # sec-gap P2-1: suspension follows the HUMAN. A suspended team
            
            
            # the tenant IS request.user the check above already covers it,
            # so the extra query only runs for member/workspace sessions.
            # Fail-closed: a human with no Account row is refused as well.
            if allowed and acting_tenant.pk != request.user.pk:
                allowed = Account.objects.filter(
                    user_id=request.user.pk, is_suspended=False
                ).exists()
        if allowed:
            return view(request, *args, **kwargs)
        if _is_htmx(request):
            response = HttpResponse(status=401)
            response.headers["HX-Redirect"] = "/login/"
            return response
        return redirect_to_login(request.get_full_path())

    return wrapped


def _owned_inboxes(request, *, include_deleted=False):
    queryset = Inbox.objects.filter(user=tenant(request))
    return queryset if include_deleted else queryset.filter(deleted_at__isnull=True)


def _owned_messages(request):
    return Message.objects.filter(inbox__user=tenant(request), inbox__deleted_at__isnull=True)


@require_GET
def root(request):
    if request.user.is_authenticated:
        return redirect("dashboard:overview")
    # On the dashboard host, an anonymous visitor hitting "/" wants to sign in
    # (e.g. clicked "open dashboard"), not the marketing landing again.
    dashboard_host = getattr(settings, "DASHBOARD_HOST", "")
    if dashboard_host and request.get_host().split(":")[0] == dashboard_host:
        return redirect("accounts:login")
    tiers = _pricing_context()["tiers"]
    return render(request, "dashboard/landing.html", {
        "mail_domain": settings.MAIL_DOMAIN,
        "paid_from_price": _lowest_paid_price(tiers),
        # Free-plan limits strip: the very tier dict /pricing/ renders.
        "free_tier": next((t for t in tiers if t["key"] == "free"), None),
    })


def _lowest_paid_price(tiers=None):
    """Cheapest self-serve paid price for the landing teaser ("$12"), read
    from _pricing_context() so the landing can never drift from /pricing/."""
    prices = []
    for tier in tiers if tiers is not None else _pricing_context()["tiers"]:
        if tier["key"] == "free" or not tier["available"]:
            continue
        amount = tier["price"].split("/", 1)[0]
        if amount.startswith("$") and amount[1:].isdigit():
            prices.append((int(amount[1:]), amount))
    return min(prices)[1] if prices else None


@require_GET
def legal(request, document):
    templates = {
        "aup": ("Acceptable Use Policy", "dashboard/legal/aup.html"),
        "privacy": ("Privacy Policy", "dashboard/legal/privacy.html"),
        "terms": ("Terms of Service", "dashboard/legal/terms.html"),
    }
    title, template = templates[document]
    return render(request, template, {
        "title": title,
        "free_retention_days": settings.RETENTION_DAYS[AccountPlan.Plan.FREE],
        "paid_retention_days": settings.RETENTION_DAYS[AccountPlan.Plan.PAID],
    })


@require_GET
def docs(request):
    """Public, unauthenticated developer quickstart for the OTP/magic-link wait.

    Serves the same audience as the landing page (prospects evaluating the
    product before signup). Every claim and parameter below is sourced from
    ``docs/API-V1.md`` and the shipped ``WaitForMessageView`` contract — no
    invented params, no pricing, no auth.
    """
    free = settings.BILLING_PLAN_LIMITS["free"]
    return render(request, "dashboard/docs.html", {
        "public_base_url": _snippet_api_base(),
        "mail_domain": settings.MAIL_DOMAIN,
        "wait_caps": _wait_caps_display(),
        # 60-second path limits: the agent-register account is a Free account.
        "free_inboxes": free["inboxes"],
        "free_retention_days": settings.RETENTION_DAYS["free"],
        "free_wait_cap": free["wait_concurrency"],
        "agent_register_per_ip": AGENT_REGISTER_RATE_LIMIT,
        "test_code_per_inbox": TEST_CODE_INBOX_RATE_LIMIT,
        "test_code_per_account": TEST_CODE_ACCOUNT_RATE_LIMIT,
    })


@require_GET
def try_it(request):
    """Public, zero-signup "try it live" playground (sprint C1).

    Anonymous read-only page render. It ships NO api key, no inline script (the
    interactive JS lives in a static file so the strict CSP holds), and the two
    JSON endpoints it calls are IP-rate-limited and scoped to a dedicated demo
    user's throwaway inboxes only.
    """
    return render(request, "dashboard/try.html", {
        "public_base_url": _snippet_api_base(),
        "mail_domain": settings.MAIL_DOMAIN,
    })


# --- public reference pages (A2/A3/A5) ----------------------------------------
# All of these are PUBLIC, unauthenticated, read-only renders in the same anon
# tier as the landing page, /docs/ and the legal pages. No auth, no user data,
# no DB writes, no outbound fetch, no ``|safe`` — every snippet is rendered with
# Django auto-escaping so ``<``/``&`` display literally and nothing is ever
# marked safe.

# The raw OpenAPI spec is served from a copy baked into this app's static dir
# (see backend/apps/dashboard/static/dashboard/openapi.yaml) so it resolves in
# the container regardless of CWD. ``finders.find`` locates it via the same
# static-file discovery used for CSS/JS, in both dev and the collected image.
OPENAPI_STATIC_NAME = "dashboard/openapi.yaml"


def _openapi_spec_bytes():
    from django.contrib.staticfiles import finders

    path = finders.find(OPENAPI_STATIC_NAME)
    if path is None:
        raise Http404("OpenAPI spec is not available.")
    return Path(path).read_bytes()


@require_GET
def api_reference(request):
    """Public, browsable REST API reference for v1, mirroring docs/openapi.yaml."""
    tiers = [t for t in _pricing_context()["tiers"] if t["key"] != "enterprise"]
    return render(request, "dashboard/api_reference.html", {
        "title": "API reference",
        "endpoints": _endpoints_for_display(),
        "rate_per_minute": API_KEY_RATE_LIMIT_PER_MINUTE,
        "rate_per_hour": API_KEY_RATE_LIMIT_PER_HOUR,
        "wait_tiers": tiers,
    })


@require_GET
def error_reference(request):
    """Public canonical error-code table derived from apps.api.errors.MESSAGES."""
    return render(request, "dashboard/errors.html", {
        "title": "Error reference",
        "error_rows": ERROR_ROWS,
        "wait_caps": _wait_caps_display(),
        "error_body": '{\n  "error": {\n    "code": "not_found",\n    "message": "Resource not found.",\n    "fields": {},\n    "request_id": "req_3k9QxV2mT7bLw8RfY1aZ"\n  }\n}',
    })


@require_GET
def openapi_yaml(request):
    """Serve the raw OpenAPI spec as text/yaml for tools and devs to import."""
    return HttpResponse(_openapi_spec_bytes(), content_type="text/yaml; charset=utf-8")


@require_GET
def llms_txt(request):
    """Serve /llms.txt, a plain-text, AI-agent-readable map of the whole API.

    Generated from the SAME API_ENDPOINTS list that powers /docs/api/ and from
    settings, so it can never drift from the real contract. No auth, no user
    data, no DB. This is the "skip the UI" front door for autonomous agents:
    one GET returns everything an agent needs to authenticate and call the API.
    """
    api_base = getattr(settings, "MAILSOCKET_API_BASE", None) or (
        "https://" + getattr(settings, "DASHBOARD_HOST", "dash.mailsocket.app")
    )
    api_base = api_base.rstrip("/")
    site = settings.PUBLIC_BASE_URL.rstrip("/")
    # canon: public docs live on the apex; don't hand agents dash URLs that 301.
    apex_origin = site_origins(request)["APEX_ORIGIN"]
    if apex_origin:
        site = apex_origin
    mail_domain = settings.MAIL_DOMAIN

    lines = []
    add = lines.append
    add("# mailsocket")
    add("")
    add("> Programmable email inboxes for developers and AI agents. Create an")
    add("> inbox, send email to it, then read the parsed message, or block on one")
    add("> call until the OTP or magic link arrives. Email in, JSON out.")
    add("")
    add("## What it is")
    add("")
    add("- Create inbox addresses on " + mail_domain + " via the REST API.")
    add("- Every email is parsed to JSON (sender, subject, text, html, otp, links).")
    add("- The core call: `messages/wait` blocks until a matching OTP or link is parsed.")
    add("- Built for signup automation, end-to-end tests (pytest, Cypress, Puppeteer, CI) and AI agents.")
    add("")
    add("## Base URL and authentication")
    add("")
    add("- API base: `" + api_base + "/api/v1`")
    add("- Auth: send your API key as a Bearer token on EVERY request:")
    add("  `Authorization: Bearer ms_live_...`")
    add("- Keys are created in the dashboard (" + api_base + "/app/api-keys/), prefixed `ms_live_`,")
    add("  hashed at rest, individually revocable, and may carry an optional expiry.")
    add("  Dashboard session cookies are not accepted on the API.")
    add("- Missing or invalid key: 401. Account not permitted: 403. Rate limited: 429 with Retry-After.")
    add("- Every response carries an X-Request-Id header; error bodies repeat it as error.request_id.")
    add("- No-signup path: POST /api/v1/agents/register needs no key and returns one")
    add("  (step 0 below). Email verification is not required for this path.")
    add("")
    add("## Limits")
    add("")
    add("- Requests: " + str(API_KEY_RATE_LIMIT_PER_MINUTE) + " per minute and " + str(API_KEY_RATE_LIMIT_PER_HOUR)
        + " per hour per API key, on every plan. Authenticated responses carry")
    add("  X-RateLimit-Limit, X-RateLimit-Remaining and X-RateLimit-Reset.")
    add("- Concurrent waits per account: " + _wait_caps_display() + ".")
    add("- Wait timeout: at most 25s per call (default 20). On 204 call again, and pass the response's")
    add("  Mailsocket-Wait-Since header back as since to never miss a message (the SDKs do this).")
    add("")
    add("## Quick start (curl): register, then wait")
    add("")
    add("```bash")
    add("# Needs curl and jq. Paste the whole block into bash; it runs as is.")
    add("command -v jq >/dev/null || echo \"This block needs jq: https://jqlang.org/download/\" >&2")
    add("# 0. No signup: create an API key and a ready-to-use inbox in one call, and keep")
    add("#    both in shell variables. Already have a key from the dashboard? Instead set")
    add("#    MAILSOCKET_API_KEY yourself and INBOX=<your inbox id>.")
    add("RESP=$(curl -s -X POST \"" + api_base + "/api/v1/agents/register\" \\")
    add("  -H \"Content-Type: application/json\" \\")
    add("  -d '{\"name\": \"my-agent\"}')")
    add("# RESP = { \"data\": { \"api_key\": \"ms_live_...\", \"key_prefix\": \"...\", \"plan\": \"free\",")
    add("#        \"inbox\": { \"id\": \"inbox_...\", \"address\": \"...@" + mail_domain + "\", ... } } }")
    add("# api_key is shown once. Store it.")
    add("export MAILSOCKET_API_KEY=$(jq -r .data.api_key <<<\"$RESP\")")
    add("INBOX=$(jq -r .data.inbox.id <<<\"$RESP\")")
    add("echo \"Inbox address: $(jq -r .data.inbox.address <<<\"$RESP\")\"")
    add("")
    add("# 1. (optional, not run here) Create another inbox, optionally with a custom")
    add("#    routing_key (the local part of the address) instead of a random one:")
    add("#  curl -X POST \"" + api_base + "/api/v1/inboxes\" \\")
    add("#    -H \"Authorization: Bearer $MAILSOCKET_API_KEY\" \\")
    add("#    -H \"Content-Type: application/json\" \\")
    add("#    -d '{\"label\": \"signup-test\", \"routing_key\": \"my-custom-slug\"}'")
    add("#  -> { \"data\": { \"id\": \"inbox_...\", \"address\": \"my-custom-slug@" + mail_domain + "\", ... } }")
    add("")
    add("# 2. Remember the newest message id in the inbox (0 if it is empty), then")
    add("#    send an email to that address (from your app or signup flow):")
    add("SINCE=$(curl -s \"" + api_base + "/api/v1/inboxes/$INBOX/messages/latest\" \\")
    add("  -H \"Authorization: Bearer $MAILSOCKET_API_KEY\" | jq -r '.data.id // 0')")
    add("")
    add("# 2a. No email handy (or your sandbox cannot send mail)? Send yourself a test")
    add("#     code instead. It puts a sample OTP email straight into your own inbox")
    add("#     (it does not go through the mail server). Limit: " + str(TEST_CODE_INBOX_RATE_LIMIT) + " per inbox, " + str(TEST_CODE_ACCOUNT_RATE_LIMIT) + " per account per hour.")
    add("curl -s -X POST \"" + api_base + "/api/v1/inboxes/$INBOX/test-code\" \\")
    add("  -H \"Authorization: Bearer $MAILSOCKET_API_KEY\"")
    add("echo")
    add("# -> 201 { \"data\": { \"message_id\": \"msg_...\", \"sent_at\": \"...\" } }")
    add("")
    add("# 3. Wait for the OTP. since=$SINCE matches only mail newer than that message,")
    add("#    even if it arrived before this call started, so the steps work one call at")
    add("#    a time. 200 returns the code; 204 means none yet, so call again (same since).")
    add("#    An empty SINCE (no jq) stops here instead of matching older mail:")
    add("curl -s \"" + api_base + "/api/v1/inboxes/$INBOX/messages/wait?require=otp&since=${SINCE:?no cutoff, install jq and rerun step 2}&timeout=25\" \\")
    add("  -H \"Authorization: Bearer $MAILSOCKET_API_KEY\"")
    add("# -> { \"data\": { \"otp\": \"123456\", \"otp_context\": \"...\", ... } }")
    add("# Or skip SINCE: start the wait first, then send the email or the test code")
    add("# from another shell while the wait is running.")
    add("")
    add("# 3a. (optional, not run here) Or subscribe to a Server-Sent Events stream of new")
    add("#     messages (same auth, same require/min_confidence/since params). Each event's")
    add("#     `data:` payload is the same message JSON as GET /messages/{id}.")
    add("#  curl -N \"" + api_base + "/api/v1/inboxes/$INBOX/stream?require=otp\" \\")
    add("#    -H \"Authorization: Bearer $MAILSOCKET_API_KEY\" -H \"Accept: text/event-stream\"")
    add("```")
    add("")
    add("Success responses are `{\"data\": ...}`; errors are")
    add("`{\"error\": {\"code\": ..., \"message\": ..., \"fields\": {}, \"request_id\": ...}}`.")
    add("Match on error.code; messages may change.")
    add("")
    add("## Endpoints")
    add("")
    for ep in _endpoints_for_display():
        add("### " + ep["method"] + " " + ep["path"])
        add(ep["summary"] + ".")
        if ep.get("params"):
            add("Params: " + ", ".join(
                p["name"] + " (" + p["type"] + ")" for p in ep["params"]
            ))
        if ep.get("body"):
            add("Body: `" + ep["body"] + "`")
        if ep.get("statuses"):
            add("Status: " + ", ".join(code + " " + desc for code, desc in ep["statuses"]))
        add("")
    add("## Plans")
    add("")
    # QA #7: same names/numbers as /pricing/ (single source: _pricing_context).
    for tier in _pricing_context()["tiers"]:
        # Billing-gated tiers say "coming soon"; Enterprise says "contact sales".
        status = "available now" if tier["available"] else tier["badge"].lower()
        if tier["key"] == "enterprise":
            add(
                "- " + tier["name"] + " (" + tier["price"] + ", " + status + "): "
                + "Business limits and features with custom contract terms, set up by our team."
            )
            continue
        add(
            "- " + tier["name"] + " (" + tier["price"] + ", " + status + "): "
            + tier["inboxes_display"] + ", " + tier["messages_display"] + " messages, "
            + tier["retention_display"] + " retention, webhooks: " + tier["webhooks_display"]
            + ", custom domains: " + tier["domains_display"] + "."
        )
    add("- Plan gating: 402 plan_upgrade_required means a Free account called a paid-only")
    add("  operation (POST /messages/batch); 403 <feature>_entitlement_required means the")
    add("  plan lacks a named capability (webhooks, custom domains, catch-all).")
    add("")
    add("## SDKs and tools")
    add("")
    add("- Python: `pip install mailsocket`, then `started = time.time()` before the trigger and")
    add("  `Client(api_key).wait_for_otp(inbox_id, since=started)` (since = a time before the trigger)")
    add("  No email handy: `client.send_test_code(inbox_id)` (since mailsocket 0.3.0) as the trigger")
    add("- JavaScript/TypeScript: `npm i mailsocket-sdk`, then `new MailsocketClient(apiKey)`")
    add("  No email handy: `await client.sendTestCode(inboxId)` (since mailsocket-sdk 0.3.0) as the trigger")
    add("- MCP server (for AI agents): `uvx mailsocket-mcp` (or `pip install mailsocket-mcp`, Python 3.10+); set MAILSOCKET_API_KEY")
    add('  Config: {"mcpServers":{"mailsocket":{"command":"uvx","args":["mailsocket-mcp"],"env":{"MAILSOCKET_API_KEY":"ms_live_..."}}}}')
    add("  Official MCP Registry: app.mailsocket/mailsocket-mcp")
    add("  Tool send_test_code(inbox_id) (since mailsocket-mcp 0.4.0) puts a sample code in the inbox;")
    add("  take a cutoff first, then wait_for_otp(inbox_id, since=<cutoff>)")
    add("- Remote MCP (no install): https://mcp.mailsocket.app/mcp over Streamable HTTP. Send")
    add("  `Authorization: Bearer ms_live_...` on every request (never in the URL). Remote waits cap at 55s")
    add("  per call; stateless; per-key concurrency limits (10 waits / 8 other calls in flight).")
    add("  Claude.ai, Claude Desktop connectors and Smithery need OAuth, which is planned but not available yet.")
    add("  Use the local stdio server above in those clients until then.")
    add("- Full SDK and MCP docs: " + site + "/docs/#sdks")
    add("")
    add("## More documentation")
    add("")
    add("- Machine-readable OpenAPI 3.1 spec: " + api_base + "/docs/openapi.yaml")
    add("- API reference: " + site + "/docs/api/")
    add("- Error codes: " + site + "/docs/errors/")
    add("- Versioning and deprecation policy: " + site + "/docs/versioning")
    add("- Guides (pytest, Cypress, Puppeteer, GitHub Actions): " + site + "/docs/guides/")
    add("- Try it with no signup: " + site + "/docs/try/")
    add("- Comparisons (Mailpit, Mailosaur, AgentMail): " + site + "/docs/compare/")
    add("- Security: " + site + "/security")
    add("")

    body = "\n".join(lines)
    return HttpResponse(body, content_type="text/plain; charset=utf-8")


def _pricing_context():
    """Build the 5-tier pricing ladder.

    Free, Pro, and Scale numbers MUST come straight from
    settings.BILLING_PLAN_LIMITS / settings.RETENTION_DAYS / the
    WAIT_LEASE_LIMIT_*/wait_concurrency fields so the pricing page and the
    enforced limits cannot drift (drift-guard tests depend on this). Scale and
    Business are self-serve but availability-gated on settings.ENABLE_BILLING:
    with billing off they render available=False / "Coming soon" / "Contact
    us". Enterprise is always available=False / "Contact sales" / price
    "Custom" (provisioned manually as plan=business, operations notes).

    verify_display: the email-verification quota shown per tier. The ONLY
    verify limit enforced today is the flat per-account hourly rate limit in
    apps.api.views.VerifyEmailView (VERIFY_ACCOUNT_RATE_LIMIT /
    VERIFY_ACCOUNT_RATE_WINDOW, apps/accounts/services.py), applied
    identically to every account regardless of plan (verify is not
    plan-gated yet; full SMTP verify is off, ENABLE_SMTP_VERIFY). QA #8:
    every tier shows that real hourly figure — no planned quotas.

    cta_url_name / cta_next (QA #9, ux3 N2): self-serve paid tiers (Pro, Scale,
    Business) route an anonymous
    visitor to REGISTER (not the login-gated billing page) with ?next= set to
    billing so they land on checkout after signing up.
    """
    free = settings.BILLING_PLAN_LIMITS["free"]
    paid = settings.BILLING_PLAN_LIMITS["paid"]
    scale = settings.BILLING_PLAN_LIMITS["scale"]
    business = settings.BILLING_PLAN_LIMITS["business"]

    def _domains_display(limits):
        
        # literal (Scale 3 / Business 10 by default, 0 -> "Not included").
        n = limits.get("custom_domains", 0)
        return f"{n} domain{'' if n == 1 else 's'}" if n else "Not included"

    def _inboxes_display(n):
        return f"{n} inbox{'' if n == 1 else 'es'}"

    verify_display_real = f"{VERIFY_ACCOUNT_RATE_LIMIT:,} / hour"
    upgrade_next = reverse("dashboard:billing-page") if settings.ENABLE_BILLING else None

    return {
        "tiers": [
            {
                "key": "free",
                "name": AccountPlan.DISPLAY_NAMES["free"],
                "price": "$0",
                "available": True,
                "badge": "Available now",
                "inboxes_display": _inboxes_display(free["inboxes"]),
                "messages_display": f'{free["messages_per_month"]:,} / month',
                "verify_display": verify_display_real,
                "retention_display": f'{settings.RETENTION_DAYS["free"]} day',
                "concurrent_waits": WAIT_LEASE_LIMIT_FREE,
                "webhooks_display": "Included" if free["webhooks"] else "Not included",
                "domains_display": _domains_display(free),
                "extra": None,
                "cta_text": "Start free",
                "cta_url_name": "accounts:register",
                "cta_mailto": None,
                "cta_next": None,
            },
            {
                "key": "paid",
                "name": AccountPlan.DISPLAY_NAMES["paid"],
                "price": "$12/mo",
                "available": True,
                "badge": "Available now",
                "inboxes_display": _inboxes_display(paid["inboxes"]),
                "messages_display": f'{paid["messages_per_month"]:,} / month',
                "verify_display": verify_display_real,
                "retention_display": f'{settings.RETENTION_DAYS["paid"]} days',
                "concurrent_waits": WAIT_LEASE_LIMIT_PAID,
                "webhooks_display": "Included" if paid["webhooks"] else "Not included",
                "domains_display": _domains_display(paid),
                "extra": None,
                
                "cta_text": "Start free",
                "cta_url_name": "accounts:register",
                "cta_mailto": None,
                # ux3 N2: Pro carries the upgrade intent like Scale/Business.
                "cta_next": upgrade_next,
            },
            {
                "key": "scale",
                "name": AccountPlan.DISPLAY_NAMES["scale"],
                "price": "$49/mo",
                "available": settings.ENABLE_BILLING,
                "badge": "Available now" if settings.ENABLE_BILLING else "Coming soon",
                "inboxes_display": _inboxes_display(scale["inboxes"]),
                "messages_display": f'{scale["messages_per_month"]:,} / month',
                "verify_display": verify_display_real,
                "retention_display": f'{settings.RETENTION_DAYS["scale"]} days',
                "concurrent_waits": scale["wait_concurrency"],
                "webhooks_display": "Included" if scale["webhooks"] else "Not included",
                "domains_display": _domains_display(scale),
                "extra": None,
                "cta_text": "Start free" if settings.ENABLE_BILLING else "Contact us",
                # QA #9: anon visitors go to REGISTER (like Pro), not the
                # login-gated billing page; ?next= brings them to billing.
                "cta_url_name": "accounts:register" if settings.ENABLE_BILLING else None,
                "cta_mailto": None if settings.ENABLE_BILLING else "sales@mailsocket.app",
                "cta_next": upgrade_next,
            },
            {
                # Self-serve via Lemon Squeezy exactly like Scale (LS variant
                # settings.LS_VARIANT_BUSINESS). Only capabilities live on prod
                # are listed: outbound send (ENABLE_OUTBOUND) and full SMTP
                # verify (ENABLE_SMTP_VERIFY) are OFF system-wide, so neither
                # is promised here.
                "key": "business",
                "name": AccountPlan.DISPLAY_NAMES["business"],
                "price": "$149/mo",
                "available": settings.ENABLE_BILLING,
                "badge": "Available now" if settings.ENABLE_BILLING else "Coming soon",
                # QA #8: enforced numbers come from settings (the same dict
                # apps.accounts.entitlements enforces), never literals.
                "inboxes_display": _inboxes_display(business["inboxes"]),
                "messages_display": f'{business["messages_per_month"]:,} / month',
                "verify_display": verify_display_real,
                "retention_display": f'{settings.RETENTION_DAYS["business"]} days',
                "concurrent_waits": business["wait_concurrency"],
                "webhooks_display": "Included" if business["webhooks"] else "Not included",
                "domains_display": _domains_display(business),
                "extra": "Team seats, catch-all",
                "cta_text": "Start free" if settings.ENABLE_BILLING else "Contact us",
                "cta_url_name": "accounts:register" if settings.ENABLE_BILLING else None,
                "cta_mailto": None if settings.ENABLE_BILLING else "sales@mailsocket.app",
                "cta_next": upgrade_next,
            },
            {
                # Contact sales. There is no "enterprise" AccountPlan value and
                # no per-account limit override: an Enterprise customer is
                # provisioned by an admin as plan=business + custom contract
                # terms (operations notes). So the limit cells say "As Business"
                # rather than promising custom limits we cannot enforce. No SSO
                # (not built).
                "key": "enterprise",
                "name": AccountPlan.DISPLAY_NAMES["enterprise"],
                "price": "Custom",
                "available": False,
                "badge": "Contact sales",
                "inboxes_display": "As Business",
                "messages_display": "As Business",
                "verify_display": verify_display_real,
                "retention_display": "As Business",
                "concurrent_waits": "As Business",
                "webhooks_display": "Included",
                "domains_display": "As Business",
                "extra": "Everything in Business, custom contract terms",
                "cta_text": "Contact sales",
                "cta_url_name": None,
                "cta_mailto": "sales@mailsocket.app",
                "cta_next": None,
            },
        ],
    }


@require_GET
def pricing(request):
    """Public pricing page: 5-tier ladder, Free/Pro always live; Scale and Business live only when settings.ENABLE_BILLING is on; Enterprise is always contact-sales."""
    return render(request, "dashboard/pricing.html", {
        "title": "Pricing",
        "plans": _pricing_context(),
    })


GUIDES = {
    "pytest": {
        "title": "Waiting for an OTP in pytest",
        "tool": "pytest",
        "lead": (
            "A pytest test that submits a signup form with a mailsocket inbox "
            "address, then blocks on the wait endpoint until the OTP arrives."
        ),
        "code": PYTEST_GUIDE_SNIPPET,
    },
    "cypress": {
        "title": "Waiting for an OTP in Cypress",
        "tool": "Cypress",
        "lead": (
            "A Cypress test that drives a real signup form with a mailsocket "
            "address and awaits the OTP over Cypress's built-in HTTP client."
        ),
        "code": CYPRESS_GUIDE_SNIPPET,
    },
    "puppeteer": {
        "title": "Waiting for an OTP in Puppeteer",
        "tool": "Puppeteer",
        "lead": (
            "A Puppeteer script that submits a signup form, then blocks on the "
            "wait endpoint with fetch until the OTP arrives."
        ),
        "code": PUPPETEER_GUIDE_SNIPPET,
    },
    "github-actions": {
        "title": "Waiting for an OTP in GitHub Actions",
        "tool": "GitHub Actions",
        "lead": (
            "Wire a wait-for-OTP test into CI by storing your mailsocket key and "
            "inbox id as encrypted secrets and exporting them to your test run."
        ),
        "code": GITHUB_ACTIONS_GUIDE_SNIPPET,
    },
    "playwright": {
        "title": "Waiting for an OTP in Playwright",
        "tool": "Playwright",
        "lead": (
            "A Playwright test that fills a signup form with a mailsocket inbox "
            "address, then waits for the OTP using the TypeScript SDK."
        ),
        "code": PLAYWRIGHT_GUIDE_SNIPPET,
    },
}


@require_GET
def guide(request, slug):
    """Public framework guide rendered from one template keyed on ``slug``."""
    guide_data = GUIDES.get(slug)
    if guide_data is None:
        raise Http404("Unknown guide.")
    return render(request, "dashboard/guide.html", {
        "title": guide_data["title"],
        "slug": slug,
        "tool": guide_data["tool"],
        "lead": guide_data["lead"],
        "code": guide_data["code"],
        "mail_domain": settings.MAIL_DOMAIN,
    })


@require_GET
def guides(request):
    """Public index of the framework guides (#34)."""
    return render(request, "dashboard/guides.html", {
        "title": "Guides",
        "guides": [{"slug": slug, **data} for slug, data in GUIDES.items()],
    })


@require_GET
def versioning(request):
    """Public API versioning and deprecation policy (#9)."""
    return render(request, "dashboard/versioning.html", {"title": "Versioning"})


@require_GET
def security(request):
    """Public security page (#3): data handling, retention, subprocessors,
    disclosure. Retention numbers come from settings, never literals."""
    tiers = [t for t in _pricing_context()["tiers"] if t["key"] != "enterprise"]
    return render(request, "dashboard/security.html", {
        "title": "Security",
        "retention_tiers": tiers,
        "mail_domain": settings.MAIL_DOMAIN,
    })


SECURITY_CONTACT = "security@mailsocket.app"


@require_GET
def security_txt(request):
    """RFC 9116 /.well-known/security.txt. Expires is always one year out so
    the file never goes stale between deploys."""
    expires = (timezone.now() + _timedelta(days=365)).replace(microsecond=0)
    body = "\n".join([
        "Contact: mailto:" + SECURITY_CONTACT,
        "Expires: " + expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Preferred-Languages: en",
        "Canonical: https://mailsocket.app/.well-known/security.txt",
        "Policy: https://mailsocket.app/security#disclosure",
        "",
    ])
    return HttpResponse(body, content_type="text/plain; charset=utf-8")


# Public, indexable pages (#40). Order is the order they appear in sitemap.xml.
SITEMAP_URL_NAMES = (
    "dashboard:root",
    "dashboard:docs",
    "dashboard:guides",
    "dashboard:api-reference",
    "dashboard:error-reference",
    "dashboard:versioning",
    "dashboard:faq",
    "dashboard:changelog",
    "dashboard:compare",
    "dashboard:compare-mailpit",
    "dashboard:compare-mailosaur",
    "dashboard:compare-agentmail",
    "dashboard:compare-mailtrap",
    "dashboard:compare-mailslurp",
    "dashboard:pricing",
    "dashboard:try-it",
    "dashboard:security",
    "dashboard:legal-terms",
    "dashboard:legal-privacy",
    "dashboard:legal-aup",
)
CANONICAL_ORIGIN = "https://mailsocket.app"


def _sitemap_paths():
    paths = [reverse(name) for name in SITEMAP_URL_NAMES]
    paths += [reverse("dashboard:guide", kwargs={"slug": slug}) for slug in GUIDES]
    return paths


@require_GET
def sitemap_xml(request):
    from xml.sax.saxutils import escape

    rows = "".join(
        "  <url><loc>" + escape(CANONICAL_ORIGIN + path) + "</loc></url>\n"
        for path in _sitemap_paths()
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + rows
        + "</urlset>\n"
    )
    return HttpResponse(body, content_type="application/xml; charset=utf-8")


@require_GET
def robots_txt(request):
    """Our own robots.txt so the Sitemap line is always present (#40)."""
    body = "\n".join([
        "User-agent: *",
        "Disallow: /app/",
        "Disallow: /admin/",
        "Disallow: /api/",
        "Disallow: /internal/",
        "Allow: /",
        "",
        "Sitemap: " + CANONICAL_ORIGIN + "/sitemap.xml",
        "",
    ])
    return HttpResponse(body, content_type="text/plain; charset=utf-8")


@require_GET
def faq(request):
    """Public FAQ — truthful, sourced from the real limits and behaviour.

    QA #7: plan names + retention come from _pricing_context() (the single
    source for customer-facing tier names/numbers), so the FAQ can never say
    "Paid" while pricing says "Pro", or omit tiers the table shows.
    """
    tiers = [t for t in _pricing_context()["tiers"] if t["key"] != "enterprise"]
    return render(request, "dashboard/faq.html", {
        "title": "FAQ",
        "free_retention_days": settings.RETENTION_DAYS["free"],
        "paid_retention_days": settings.RETENTION_DAYS["paid"],
        "retention_tiers": tiers,
        "wait_tiers": [t for t in tiers if t["concurrent_waits"]],
        
        # enforced cap (BILLING_PLAN_LIMITS custom_domains), never literals.
        "domain_tiers": [
            t for t in tiers
            if t["key"] in settings.BILLING_PLAN_LIMITS
            and settings.BILLING_PLAN_LIMITS[t["key"]].get("custom_domains", 0)
        ],
        "rate_per_minute": API_KEY_RATE_LIMIT_PER_MINUTE,
        "rate_per_hour": API_KEY_RATE_LIMIT_PER_HOUR,
    })


@require_GET
def changelog(request):
    """Public changelog — a dated list of shipped milestones, newest first.

    Plan numbers quoted in entries come from the same settings the pricing
    page and enforcement use (BILLING_PLAN_LIMITS / RETENTION_DAYS), never
    literals, so a BUSINESS_* env override cannot leave a stale promise.
    """
    business = settings.BILLING_PLAN_LIMITS["business"]
    business_tier = next(t for t in _pricing_context()["tiers"] if t["key"] == "business")
    return render(request, "dashboard/changelog.html", {
        "title": "Changelog",
        "business_plan": {
            "price": business_tier["price"],
            "inboxes": business["inboxes"],
            "messages_per_month": f'{business["messages_per_month"]:,}',
            "retention_days": settings.RETENTION_DAYS["business"],
            "wait_concurrency": business["wait_concurrency"],
        },
    })


def _recent(request):
    return _owned_messages(request).select_related("inbox__domain").order_by("-received_at", "-id")[:10]


@dashboard_required
@require_GET
def overview(request):
    active_count = _owned_inboxes(request).filter(is_enabled=True).count()
    message_count = _owned_messages(request).count()
    has_api_key = ApiKey.objects.filter(user=tenant(request)).exists()
    primary_inbox = _owned_inboxes(request).order_by("-created_at", "-id").first()
    return render(request, "dashboard/overview.html", {
        "active_count": active_count,
        "message_count": message_count,
        "recent_messages": _recent(request),
        "usage": usage_summary(tenant(request)),
        "has_api_key": has_api_key,
        "primary_inbox": primary_inbox,
        "public_base_url": _snippet_api_base(),
    })


@dashboard_required
@require_GET
def recent_messages(request):
    return render_partial(request, "dashboard/partials/message_rows.html", {"messages": _recent(request)})


@dashboard_required
@require_GET
def inboxes(request):
    return render(request, "dashboard/inboxes.html", {
        "inboxes": _owned_inboxes(request).select_related("domain").order_by("-created_at", "-id"),
        "usage": usage_summary(tenant(request)),
        "public_base_url": _snippet_api_base(),
    })


@dashboard_required
@require_GET
def usage(request):
    "Public API."
    summary = usage_summary(tenant(request))
    message_limit = summary["message_limit"] or 0
    message_pct = min(100, round(summary["messages_received"] * 100 / message_limit)) if message_limit else 0
    return render(request, "dashboard/usage.html", {
        "usage": summary,
        "message_pct": message_pct,
        "message_warning": summary["limit_exceeded"] or message_pct >= 80,
    })


def _random_routing_key():
    return random_routing_key()


def _inbox_result(request, text, event="inboxesChanged", status=200):
    if not _is_htmx(request):
        return redirect("dashboard:inboxes")
    # qafix2 #6: the header counter and the Create button are refreshed
    # out-of-band with the rows, so they never go stale after a swap.
    response = render_partial(request, "dashboard/partials/inbox_rows.html", {
        "inboxes": _owned_inboxes(request).select_related("domain").order_by("-created_at", "-id"),
        "flash": text,
        "public_base_url": _snippet_api_base(),
        "usage": usage_summary(tenant(request)),
        "oob_usage": True,
    }, status=status)
    if status < 400:
        response.headers["HX-Trigger"] = json.dumps({event: {"message": text}})
    return response


@dashboard_required
@post_only("dashboard:inboxes")
def inbox_create(request):
    form = InboxCreateForm(request.POST)
    if not form.is_valid():
        if form.errors.get("routing_key"):
            return _inbox_result(request, form.errors["routing_key"][0], status=422)
        return _inbox_result(request, "Invalid label.", status=422)
    custom_key = form.cleaned_data.get("routing_key") or ""
    label = form.cleaned_data["label"]
    if custom_key:
        # A caller-chosen slug is used verbatim — no retry loop. A collision
        # is a real "taken" address, reported as 409, never silently
        # randomized (mirrors InboxListCreateView.post in apps/api/views.py).
        if Inbox.objects.filter(routing_key=custom_key).exists():
            return _inbox_result(request, "That address is already taken.", status=409)
        try:
            with transaction.atomic():
                if not can_create_inbox(tenant(request)):
                    return _inbox_result(request, "Your plan inbox limit has been reached.", status=409)
                Inbox.objects.create(user=tenant(request), label=label, routing_key=custom_key)
        except IntegrityError:
            return _inbox_result(request, "That address is already taken.", status=409)
        return _inbox_result(request, "Inbox created.")
    for _ in range(10):
        try:
            with transaction.atomic():
                if not can_create_inbox(tenant(request)):
                    return _inbox_result(request, "Your plan inbox limit has been reached.", status=409)
                Inbox.objects.create(user=tenant(request), label=label, routing_key=_random_routing_key())
            return _inbox_result(request, "Inbox created.")
        except IntegrityError:
            continue
    return _inbox_result(request, "Could not allocate an address.", status=422)


def _owned_inbox(request, public_id):
    return get_object_or_404(_owned_inboxes(request), public_id=public_id)


@dashboard_required
@post_only("dashboard:inboxes")
def inbox_rename(request, public_id):
    inbox = _owned_inbox(request, public_id)
    form = InboxRenameForm(request.POST)
    if not form.is_valid():
        return _inbox_result(request, "Label must be at most 120 characters.", status=422)
    inbox.label = form.cleaned_data["label"]
    inbox.save(update_fields=["label", "updated_at"])
    return _inbox_result(request, "Inbox renamed.")


def _set_enabled(request, public_id, enabled):
    from apps.domains.services import CatchAllToggleRefused, assert_not_active_catch_all_target

    inbox = _owned_inbox(request, public_id)
    if not enabled:
        try:
            assert_not_active_catch_all_target(inbox)
        except CatchAllToggleRefused:
            return _inbox_result(
                request,
                "This inbox is the active catch-all target for its domain; "
                "disable catch-all on the domain first.",
                status=409,
            )
    inbox.is_enabled = enabled
    inbox.save(update_fields=["is_enabled", "updated_at"])
    return _inbox_result(request, "Inbox enabled." if enabled else "Inbox disabled.")


@dashboard_required
@post_only("dashboard:inboxes")
def inbox_enable(request, public_id):
    return _set_enabled(request, public_id, True)


@dashboard_required
@post_only("dashboard:inboxes")
def inbox_disable(request, public_id):
    return _set_enabled(request, public_id, False)


@dashboard_required
@post_only("dashboard:inboxes")
def inbox_delete(request, public_id):
    from apps.domains.services import CatchAllToggleRefused, assert_not_active_catch_all_target

    inbox = _owned_inbox(request, public_id)
    try:
        assert_not_active_catch_all_target(inbox)
    except CatchAllToggleRefused:
        return _inbox_result(
            request,
            "This inbox is the active catch-all target for its domain; "
            "disable catch-all on the domain first.",
            status=409,
        )
    inbox.deleted_at = timezone.now()
    inbox.is_enabled = False
    inbox.save(update_fields=["deleted_at", "is_enabled", "updated_at"])
    return _inbox_result(request, "Inbox deleted.")



# module (API_ENDPOINTS quotes them); see send_test_code below.
TEST_CODE_SENDER = "test-code@mailsocket.app"
TEST_CODE_FROM_HEADER = "mailsocket test <test-code@mailsocket.app>"
TEST_CODE_SENT = "Test code sent — it appears in a few seconds."
TEST_CODE_DISABLED_MESSAGE = "This inbox is disabled. Enable it to receive a test code."


class InboxCannotTakeTestCode(Exception):
    """The inbox cannot receive a test code (disabled, or disabled/deleted
    between the check and the store). Dashboard: 409 flash; API: 409
    ``inbox_disabled``."""


def retry_minutes_until(reset_at, *, now=None) -> int:
    """Whole minutes (rounded up, at least 1) until the refusing bucket's
    window ends — the ``reset_at`` that RateLimitExceeded carries."""
    seconds = (reset_at - (now or timezone.now())).total_seconds() if reset_at else 0
    return max(1, math.ceil(seconds / 60))


def rate_limited_test_code_message(reset_at) -> str:
    """ux3 N8: the 429 text says when to retry (dashboard flash + API
    error.message), from the expiry of the bucket that refused."""
    return (
        "Too many test codes requested. You can send another test code in "
        f"{retry_minutes_until(reset_at)} min."
    )


def send_test_code(inbox, account_user):
    """Drop one sample OTP into ``inbox``. The SINGLE implementation behind
    the dashboard "Send a test code" button AND
    ``POST /api/v1/inboxes/{id}/test-code`` (ux3 N1): same deliver function,
    same buckets, same keys, so the two paths share one quota.

    The caller owns tenant scoping (it must pass an inbox it already
    resolved as owned and not deleted) and ``account_user`` is the tenant
    (resource-owning user), so the account bucket is the same whichever
    path is used.

    Order: disabled check (charges nothing) -> inbox bucket -> account
    bucket (an inbox refusal never burns the account budget) -> write.
    Raises InboxCannotTakeTestCode or RateLimitExceeded. Returns
    ``(message, rate_limit)`` where rate_limit is the tightest of the two
    buckets as ``(limit, remaining, reset_at)``.
    """
    from apps.ingest.services import IngestError

    from .demo import deliver_sample_code

    if not inbox.is_enabled:
        raise InboxCannotTakeTestCode()
    inbox_remaining, inbox_reset_at = increment_rate_limit(
        TEST_CODE_INBOX_RATE_NAME,
        str(inbox.pk),
        limit=TEST_CODE_INBOX_RATE_LIMIT,
        window_seconds=TEST_CODE_RATE_WINDOW,
    )
    account_remaining, account_reset_at = increment_rate_limit(
        TEST_CODE_ACCOUNT_RATE_NAME,
        str(account_user.pk),
        limit=TEST_CODE_ACCOUNT_RATE_LIMIT,
        window_seconds=TEST_CODE_RATE_WINDOW,
    )
    rate_limit = min(
        [
            (TEST_CODE_INBOX_RATE_LIMIT, inbox_remaining, inbox_reset_at),
            (TEST_CODE_ACCOUNT_RATE_LIMIT, account_remaining, account_reset_at),
        ],
        key=lambda row: row[1] / row[0],
    )
    try:
        result = deliver_sample_code(inbox, from_header=TEST_CODE_FROM_HEADER, sender=TEST_CODE_SENDER)
    except IngestError:
        # Disabled/deleted between the check above and the store.
        raise InboxCannotTakeTestCode() from None
    return result.message, rate_limit
# Where a plain (non-htmx) POST returns to: a fixed allowlist, never a URL
# taken from the request.
TEST_CODE_RETURN_TO = {
    "overview": "dashboard:overview",
    "onboarding": "dashboard:overview",
    "inboxes": "dashboard:inboxes",
}


def _test_code_result(request, text, *, status=200):
    if _is_htmx(request):
        if request.POST.get("return_to") == "onboarding":
            # : the just-created-key card swaps ONLY a flash, so the
            # one-time key and the pre-filled snippet stay on screen.
            return render_partial(
                request,
                "dashboard/partials/test_code_flash.html",
                {"flash": text, "is_error": status >= 400},
                status=status,
            )
        return _inbox_result(request, text, status=status)
    (messages_framework.success if status < 400 else messages_framework.error)(request, text)
    return redirect(TEST_CODE_RETURN_TO.get(request.POST.get("return_to"), "dashboard:inboxes"))


@dashboard_required
@post_only("dashboard:inboxes")
def inbox_send_test_code(request, public_id):
    """POST /app/inboxes/<id>/test-code/ — drop one sample OTP into the
    caller's own inbox.

    - Tenant-scoped exactly like rename/enable/disable (``_owned_inbox``):
      a foreign or unknown public_id is the same plain 404, so this is no
      existence oracle and can never write into someone else's inbox.
    - CSRF: a normal dashboard POST (the global CsrfViewMiddleware applies).
    - Disabled inboxes are refused (409); deleted ones are already 404.
    - Rate limits (per inbox, then per account) are charged before any
      message is written; 429 when either is exhausted.
    - The message goes through the SAME store_message + parse_message path as
      real inbound mail (shared with /docs/try's sample sender), so it counts
      toward the monthly message usage and fires a configured webhook exactly
      like any other inbound message.
    - ux3 N1: the work is ``send_test_code``, shared with the API endpoint.
    """
    inbox = _owned_inbox(request, public_id)
    try:
        send_test_code(inbox, tenant(request))
    except InboxCannotTakeTestCode:
        return _test_code_result(request, TEST_CODE_DISABLED_MESSAGE, status=409)
    except RateLimitExceeded as exc:
        return _test_code_result(request, rate_limited_test_code_message(exc.reset_at), status=429)
    return _test_code_result(request, TEST_CODE_SENT)


def _owned_api_keys(request):
    return ApiKey.objects.filter(user=tenant(request)).order_by("-created_at", "-id")


API_KEY_CREATE_RATE_LIMIT = 10
API_KEY_CREATE_RATE_WINDOW = 3600
API_KEY_CREATE_RATE_NAME = "dashboard:apikey-create:hour"
API_KEY_CAP_MESSAGE = (
    "You have reached the maximum of 20 active API keys. Revoke an unused key first."
)
API_KEY_RATE_MESSAGE = "Too many API keys created. Please wait and try again."
API_KEY_DUPLICATE_WINDOW = _timedelta(seconds=5)
API_KEY_DUPLICATE_MESSAGE = "A key with this name was just created."


def _mint_api_key(request, name: str, *, expires_at=None, dedupe_name=False):
    """Rate-limit and cap-check the shared key-mint for both dashboard paths.

    Returns ``(api_key, token)`` on success, or an ``HttpResponse`` flash
    partial when the per-user active-key cap or the hourly create rate limit is
    hit. ``request`` carries the authenticated user; both failure shapes are
    rendered so the caller can return them verbatim.

    QA-DASH #4: a double-clicked "Create key" minted two keys (the first an
    orphan whose secret was never shown). With ``dedupe_name=True`` (the
    user-named Create-key form) a second create with the SAME name for the
    same tenant within ``API_KEY_DUPLICATE_WINDOW`` is refused (409). The
    onboarding path (fixed name) keeps its pre-existing contract.
    The check runs under the tenant's AccountPlan row lock (the same lock
    ``create_api_key`` takes), so two near-simultaneous requests serialize and
    the second one sees the first key. It runs before the rate-limit charge so
    a double-click does not burn hourly budget.
    """
    owner = tenant(request)
    with transaction.atomic():
        AccountPlan.objects.select_for_update().filter(user=owner).first()
        # A key revoked right after creation (e.g. "wrong expiry, redo it")
        
        if dedupe_name and ApiKey.objects.filter(
            user=owner,
            name=name,
            revoked_at__isnull=True,
            created_at__gte=timezone.now() - API_KEY_DUPLICATE_WINDOW,
        ).exists():
            return render_partial(request, "dashboard/partials/flash.html", {
                "flash": API_KEY_DUPLICATE_MESSAGE,
            }, status=409)
        try:
            increment_rate_limit(
                API_KEY_CREATE_RATE_NAME,
                str(owner.pk),
                limit=API_KEY_CREATE_RATE_LIMIT,
                window_seconds=API_KEY_CREATE_RATE_WINDOW,
            )
        except RateLimitExceeded:
            return render_partial(request, "dashboard/partials/flash.html", {
                "flash": API_KEY_RATE_MESSAGE,
            }, status=429)
        try:
            return create_api_key(
                owner, name, created_by=request.user, expires_at=expires_at
            )
        except ApiKeyLimitExceeded:
            return render_partial(request, "dashboard/partials/flash.html", {
                "flash": API_KEY_CAP_MESSAGE,
            }, status=409)


@dashboard_required
@require_GET
def api_keys(request):
    api_base = (
        "https://" + getattr(settings, "DASHBOARD_HOST", "dash.mailsocket.app")
    ).rstrip("/")
    return render(request, "dashboard/api_keys.html", {
        "api_keys": _owned_api_keys(request),
        "api_base": api_base,
    })


@dashboard_required
@post_only("dashboard:api-keys")
def api_key_create(request):
    form = ApiKeyCreateForm(request.POST)
    if not form.is_valid():
        # QA-DASH #5: report the field that actually failed; an out-of-range
        # expiry used to be reported as a name error.
        if "name" in form.errors:
            message = "Name is required and must be at most 80 characters."
        else:
            message = "Expiry must be between 1 and 3650 days."
        return render_partial(request, "dashboard/partials/flash.html", {
            "flash": message,
        }, status=422)
    expires_in_days = form.cleaned_data.get("expires_in_days")
    expires_at = (
        timezone.now() + _timedelta(days=expires_in_days)
        if expires_in_days
        else None
    )
    result = _mint_api_key(
        request, form.cleaned_data["name"], expires_at=expires_at, dedupe_name=True
    )
    if isinstance(result, HttpResponse):
        # Cap or rate-limit flash partial — return it verbatim.
        return result
    api_key, token = result
    response = render_partial(request, "dashboard/partials/api_key_created.html", {
        "api_key": api_key,
        "token": token,
        "api_keys": _owned_api_keys(request),
    })
    response.headers["Cache-Control"] = "private, no-store"
    return response


@dashboard_required
@post_only("dashboard:overview")
def onboarding_api_key_create(request):
    """One-click API key for the Overview onboarding card.

    A thin wrapper over the same ``create_api_key`` service: it mints the key
    with a fixed label, reveals the plaintext ONCE via the existing
    ``api_key_created`` partial (plaintext never persisted/re-shown), and swaps
    the onboarding card to the now-authorized code-snippet state. No secret is
    logged or stored.
    """
    inbox = _owned_inboxes(request).order_by("-created_at", "-id").first()
    result = _mint_api_key(request, "Onboarding key")
    if isinstance(result, HttpResponse):
        # Cap or rate-limit flash partial — return it verbatim (never a 500).
        return result
    api_key, token = result
    response = render_partial(request, "dashboard/partials/onboarding_snippet.html", {
        "api_key": api_key,
        "token": token,
        "inbox": inbox,
        "public_base_url": _snippet_api_base(),
    })
    response.headers["Cache-Control"] = "private, no-store"
    return response


@dashboard_required
@post_only("dashboard:api-keys")
def api_key_revoke(request, public_id):
    "Public API."
    queryset = ApiKey.objects.filter(user=tenant(request))
    if not require_owner(request):
        queryset = queryset.filter(created_by_id=request.user.pk)
    api_key = get_object_or_404(queryset, public_id=public_id)
    if api_key.revoked_at is None:
        api_key.revoked_at = timezone.now()
        api_key.save(update_fields=["revoked_at"])
    return render_partial(request, "dashboard/partials/api_key_rows.html", {
        "api_keys": _owned_api_keys(request),
        "flash": "API key revoked.",
    })


def _owned_webhooks(request):
    return Webhook.objects.filter(inbox__user=tenant(request)).select_related("inbox").order_by("inbox__label", "id")


def _webhook_page_context(request, *, flash="", secret="", is_error=False):
    return {
        "inboxes": _owned_inboxes(request).select_related("domain").order_by("label", "id"),
        "webhooks": _owned_webhooks(request),
        "can_configure": can_configure_webhook(tenant(request)),
        "webhook_plan_name": lowest_plan_name_with(lambda limits: limits.get("webhooks")),
        "flash": flash,
        "flash_is_error": is_error,
        "secret": secret,
    }


def _webhook_result(request, *, flash, status=200, secret="", is_error=None):
    """Every webhook mutation's response (qafix2 #4/#9).

    - htmx 4xx: ONLY the error flash, swapped into ``#webhook-result`` (never
      a second copy of the card list).
    - htmx success: the flash (+ the one-time secret) into ``#webhook-result``
      and the card list refreshed out-of-band in ``#webhook-rows``.
    - plain POST (no JS / htmx failed to load): never a bare partial — a
      success or error redirects back to the full Webhooks page with the
      flash; a one-time secret is rendered on the full page instead (it is
      never put in the message store / a cookie).
    """
    if is_error is None:
        is_error = status >= 400
    if not _is_htmx(request):
        if secret:
            response = render(
                request, "dashboard/webhooks.html",
                _webhook_page_context(request, flash=flash, secret=secret), status=status,
            )
            response.headers["Cache-Control"] = "private, no-store"
            return response
        (messages_framework.error if is_error else messages_framework.success)(request, flash)
        return redirect("dashboard:webhooks")
    if status >= 400:
        return render_partial(request, "dashboard/partials/flash.html", {"flash": flash}, status=status)
    template = "dashboard/partials/webhook_secret.html"
    response = render_partial(request, template, _webhook_page_context(request, flash=flash, secret=secret, is_error=is_error), status=status)
    if secret:
        response.headers["Cache-Control"] = "private, no-store"
    return response


@dashboard_required
@require_GET
def webhooks(request):
    return render(request, "dashboard/webhooks.html", _webhook_page_context(request))


@dashboard_required
@post_only("dashboard:webhooks")
def webhook_save(request, public_id):
    inbox = _owned_inbox(request, public_id)
    form = WebhookForm(request.POST)
    if not form.is_valid():
        return _webhook_result(request, flash="Enter a valid HTTPS webhook URL.", status=422)
    existing = Webhook.objects.filter(inbox=inbox).first()
    enabling = form.cleaned_data["is_enabled"]
    if (existing is None or enabling) and not can_configure_webhook(tenant(request)):
        return _webhook_result(request, flash="Your plan cannot configure or enable webhooks.", status=422)
    try:
        if existing is None:
            webhook, secret = create_webhook(inbox, form.cleaned_data["url"], enabled=enabling)
            del webhook
            return _webhook_result(request, flash="Webhook created.", secret=secret)
        update_webhook(existing, form.cleaned_data["url"], enabled=enabling)
    except SSRFValidationError:
        return _webhook_result(request, flash="Webhook URL is not an allowed public HTTPS target.", status=422)
    return _webhook_result(request, flash="Webhook updated.")


@dashboard_required
@post_only("dashboard:webhooks")
def webhook_disable(request, public_id):
    inbox = _owned_inbox(request, public_id)
    webhook = get_object_or_404(Webhook.objects.filter(inbox=inbox), inbox=inbox)
    webhook.is_enabled = False
    webhook.save(update_fields=["is_enabled", "updated_at"])
    return _webhook_result(request, flash="Webhook disabled.")


@dashboard_required
@post_only("dashboard:webhooks")
def webhook_rotate(request, public_id):
    inbox = _owned_inbox(request, public_id)
    webhook = get_object_or_404(Webhook.objects.filter(inbox=inbox), inbox=inbox)
    secret = rotate_signing_secret(webhook)
    return _webhook_result(request, flash="Signing secret rotated.", secret=secret)


WEBHOOK_TEST_REFUSED_FLASH = {
    "webhook_entitlement_required": "Your plan cannot configure or enable webhooks.",
    "webhook_not_configured": "Configure a webhook for this inbox first.",
    "webhook_ssrf_rejected": "Webhook URL is not an allowed public HTTPS target.",
}


@dashboard_required
@post_only("dashboard:webhooks")
def webhook_test(request, public_id):
    """qafix2 #5: the dashboard "Send test" button.

    Reuses ``apps.api.views.send_webhook_test`` verbatim — the exact function
    behind POST /api/v1/inboxes/{id}/webhook/test — so the entitlement gate,
    rate-limit buckets, ``webhook_attempts`` metering and the SSRF-pinned
    transport are shared, not re-implemented. Tenant-scoped through
    ``_owned_inbox``; same permission rule as Disable (any dashboard user of
    the tenant).
    """
    from apps.api.views import WebhookTestRefused, send_webhook_test

    inbox = _owned_inbox(request, public_id)
    try:
        data, _rate_limit = send_webhook_test(inbox, tenant(request))
    except RateLimitExceeded:
        return _webhook_result(request, flash="Too many test sends. Please wait and try again.", status=429)
    except WebhookTestRefused as exc:
        if exc.code == "webhook_unreachable":
            # The request itself worked; the receiver did not answer. This is
            # a test RESULT, shown inline, not a dashboard request error.
            reason = (exc.fields or {}).get("error", ["error"])[0]
            return _webhook_result(
                request, flash=f"Test sent, but your endpoint could not be reached ({reason}).", is_error=True,
            )
        flash = WEBHOOK_TEST_REFUSED_FLASH.get(exc.code, "Could not send a test event.")
        return _webhook_result(request, flash=flash, status=exc.status)
    code = data["response_status"]
    if 200 <= code < 300:
        return _webhook_result(request, flash=f"Test event delivered — your endpoint answered HTTP {code}.")
    return _webhook_result(
        request, flash=f"Test event sent, but your endpoint answered HTTP {code}.", is_error=True,
    )


@dashboard_required
@post_only("dashboard:webhooks")
def webhook_delete(request, public_id):
    """qafix2 #5: delete the inbox's webhook and its delivery history.

    Same tenant scoping and permission rule as Disable. ``delete_webhook``
    removes the PROTECT-ed WebhookDelivery rows first, in one transaction.
    """
    inbox = _owned_inbox(request, public_id)
    webhook = get_object_or_404(Webhook.objects.filter(inbox=inbox), inbox=inbox)
    delete_webhook(webhook)
    return _webhook_result(request, flash="Webhook deleted.")


@dashboard_required
@require_GET
def webhook_deliveries(request, public_id):
    inbox = _owned_inbox(request, public_id)
    webhook = get_object_or_404(Webhook.objects.filter(inbox=inbox), inbox=inbox)
    deliveries = WebhookDelivery.objects.filter(webhook=webhook).order_by("-created_at", "-id")[:100]
    return render_partial(request, "dashboard/partials/webhook_delivery_rows.html", {
        "webhook": webhook,
        "deliveries": deliveries,
    })


def _encode_cursor(request, message, filter_signature):
    return signing.dumps({
        "user": tenant(request).pk,
        "received_at": message.received_at.isoformat(),
        "id": message.pk,
        "filter": filter_signature,
    }, salt=CURSOR_SALT, compress=True)


def _decode_cursor(request, value, filter_signature):
    try:
        data = signing.loads(value, salt=CURSOR_SALT, max_age=86400)
        received_at = parse_datetime(data["received_at"])
        if data["user"] != tenant(request).pk or received_at is None or data.get("filter") != filter_signature:
            raise ValueError
        return received_at, int(data["id"])
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        return None


def _resolve_domain_filter(request):
    "Public API."
    from apps.domains.models import Domain

    domain_id = request.GET.get("domain_id") or ""
    if not domain_id:
        return None, "all"
    if domain_id == "shared":
        return None, "shared"
    domain = Domain.objects.filter(user=tenant(request), public_id=domain_id).first()
    if domain is None:
        return None, "all"
    return domain, domain.public_id


def _group_key_sender(message):
    from email.utils import parseaddr

    addr = parseaddr(message.header_from or "")[1]
    return (addr or message.header_from or "").lower()


def _group_key_subject(message):
    return message.subject or ""


GROUPED_LIMIT = 200


@dashboard_required
@require_GET
def messages(request):
    from apps.accounts.entitlements import outbound_ui_visible
    from apps.domains.models import Domain

    queryset = _owned_messages(request).select_related("inbox__domain")
    selected_inbox = None
    inbox_id = request.GET.get("inbox")
    if inbox_id:
        # qafix2 #14: an unknown/foreign inbox id is ignored (all inboxes),
        # not a 404 page; foreign == nonexistent, so no existence oracle.
        selected_inbox = _owned_inboxes(request).filter(public_id=inbox_id).first()
        if selected_inbox is not None:
            queryset = queryset.filter(inbox=selected_inbox)

    selected_domain, domain_sig = _resolve_domain_filter(request)
    if selected_domain is not None:
        queryset = queryset.filter(inbox__domain=selected_domain)
    elif domain_sig == "shared":
        queryset = queryset.filter(inbox__domain__isnull=True)

    queryset, message_filter_signature = apply_message_filters(queryset, request.GET)
    subject_value = (request.GET.get("subject_contains") or "").strip()
    from_value = (request.GET.get("from") or "").strip()
    has_otp_value = parse_bool(request.GET.get("has_otp")) is True
    q_value = (request.GET.get("q") or "").strip()[: SUBJECT_CONTAINS_MAX]
    read_value = (request.GET.get("read") or "").strip().lower()
    if read_value not in ("true", "false"):
        read_value = ""

    filter_signature = f"inbox:{selected_inbox.public_id if selected_inbox else 'all'}|domain:{domain_sig}"
    if message_filter_signature:
        filter_signature = f"{filter_signature}|{message_filter_signature}"

    group_by = request.GET.get("group_by") or ""
    view_mode = request.GET.get("view") or ""
    grouped = group_by in ("sender", "subject") and view_mode == "grouped"

    filter_query = urlencode({
        **(dict(inbox=selected_inbox.public_id) if selected_inbox else {}),
        **(dict(domain_id=domain_sig) if domain_sig != "all" else {}),
        **(dict(subject_contains=subject_value) if subject_value else {}),
        **({"from": from_value} if from_value else {}),
        **(dict(has_otp="1") if has_otp_value else {}),
        **(dict(read=read_value) if read_value else {}),
        **(dict(q=q_value) if q_value else {}),
    })

    outbound_ui = outbound_ui_visible(request)
    domains = list(Domain.objects.filter(user=tenant(request)).order_by("name"))

    context = {
        "inboxes": _owned_inboxes(request).select_related("domain").order_by("label", "id"),
        "selected_inbox": selected_inbox,
        "domains": domains,
        "selected_domain": selected_domain,
        "domain_filter": domain_sig,
        "filter_query": filter_query,
        "filters": {
            "subject_contains": subject_value,
            "from": from_value,
            "has_otp": has_otp_value,
            "q": q_value,
            "read": read_value,
        },
        "group_by": group_by if grouped else "",
        "view_mode": "grouped" if grouped else "list",
        "outbound_ui": outbound_ui,
    }

    if grouped:
        
        # is ignored entirely here — a stale list cursor pasted into a
        # grouped URL is never decoded, and no cursor is ever minted for a
        # grouped page.
        rows = list(queryset.order_by("-received_at", "-id")[:GROUPED_LIMIT])
        key_fn = _group_key_sender if group_by == "sender" else _group_key_subject
        groups = []
        seen_keys = {}
        for message in rows:
            key = key_fn(message)
            if key not in seen_keys:
                seen_keys[key] = {"key": key, "messages": [], "count": 0}
                groups.append(seen_keys[key])
            seen_keys[key]["messages"].append(message)
            seen_keys[key]["count"] += 1
        context["groups"] = groups
        context["truncated"] = len(rows) >= GROUPED_LIMIT
        return render(request, "dashboard/messages.html", context)

    cursor = request.GET.get("cursor")
    if cursor:
        decoded = _decode_cursor(request, cursor, filter_signature)
        if decoded is None:
            return HttpResponse("Invalid cursor.", status=422, content_type="text/plain")
        received_at, row_id = decoded
        queryset = queryset.filter(Q(received_at__lt=received_at) | Q(received_at=received_at, id__lt=row_id))
    rows = list(queryset.order_by("-received_at", "-id")[: PAGE_SIZE + 1])
    has_more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    next_cursor = _encode_cursor(request, rows[-1], filter_signature) if has_more else None

    context["messages"] = rows
    context["next_cursor"] = next_cursor
    return render(request, "dashboard/messages.html", context)


def _message_json(message):
    return {
        "id": message.public_id,
        "inbox_id": message.inbox.public_id,
        "envelope_from": message.envelope_from,
        "envelope_recipient": message.envelope_recipient,
        "from": message.header_from,
        "to": message.header_to,
        "cc": message.header_cc,
        "subject": message.subject,
        "text": message.text_body,
        "html": message.html_body,
        "received_at": message.received_at.isoformat().replace("+00:00", "Z"),
        "status": message.ingest_status,
        "otp": message.detected_otp,
        "magic_link": message.magic_link,
    }


def _message_context(request, public_id):
    from apps.accounts.entitlements import outbound_ui_visible

    message = get_object_or_404(_owned_messages(request).select_related("inbox__domain"), public_id=public_id)
    outbound_ui = outbound_ui_visible(request)
    sendable = outbound_ui and _inbox_is_sendable(message.inbox)
    return {
        "message": message,
        "normalized_json": json.dumps(_message_json(message), indent=2, ensure_ascii=False),
        "outbound_ui": sendable,
    }


def _inbox_is_sendable(inbox):
    "Public API."
    from apps.domains.models import Domain

    return (
        inbox.domain_id is not None
        and inbox.domain.status == Domain.Status.VERIFIED
        and inbox.domain.dkim_status == Domain.DkimStatus.VERIFIED
    )


@dashboard_required
@require_GET
def message_detail(request, public_id):
    context = _message_context(request, public_id)
    message = context["message"]
    
    # on GET — public_ids are unguessable, worst case is marking your own
    # mail read.
    if message.read_at is None:
        message.read_at = timezone.now()
        message.save(update_fields=["read_at", "updated_at"])
    return render(request, "dashboard/message_detail.html", context)


@dashboard_required
@require_GET
def message_panel(request, public_id):
    return render_partial(request, "dashboard/partials/message_detail_panel.html", _message_context(request, public_id))


def _safe_next(request):
    "Public API."
    next_url = request.POST.get("next")
    if next_url and _is_relative_path(next_url) and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return next_url
    return reverse("dashboard:messages")


@dashboard_required
@post_only("dashboard:message-detail", url_kwargs=('public_id',))
def messages_mark_read(request, public_id):
    message = get_object_or_404(_owned_messages(request), public_id=public_id)
    if message.read_at is None:
        message.read_at = timezone.now()
        message.save(update_fields=["read_at", "updated_at"])
    return redirect(_safe_next(request))


@dashboard_required
@post_only("dashboard:message-detail", url_kwargs=('public_id',))
def messages_mark_unread(request, public_id):
    message = get_object_or_404(_owned_messages(request), public_id=public_id)
    if message.read_at is not None:
        message.read_at = None
        message.save(update_fields=["read_at", "updated_at"])
    return redirect(_safe_next(request))




# response WALL-FOR-WALL, at POST time, in order: (1) flag wall — not
# ENABLE_OUTBOUND -> Http404 before touching the DB (exact team_views.

# -> require_owner(request) True; (4) ownership anchors -> inbox owned,
# message anchored ON that inbox; (5) service call with user=tenant(request),
# created_by=request.user. Exceptions map to flash via the SAME taxonomy as
# api.views._map_enqueue_exception — never a 500.


def _outbound_enabled(view):
    def wrapped(request, *args, **kwargs):
        if not settings.ENABLE_OUTBOUND:
            raise Http404("Outbound sending is not enabled.")
        return view(request, *args, **kwargs)

    return wrapped


def _outbound_gate_flash(request):
    """Walls (2)+(3) of the dashboard POST gate stack (flag wall (1) is the
    _outbound_enabled decorator, checked before this ever runs). Returns an
    HttpResponse flash to short-circuit, or None."""
    from apps.accounts.team_services import is_demo_or_agent_user

    if is_demo_or_agent_user(request.user):
        return render_partial(request, "dashboard/partials/flash.html", {
            "flash": "Outbound sending is not available for this account.",
        }, status=403)
    if not require_owner(request):
        return render_partial(request, "dashboard/partials/flash.html", {
            "flash": "Only the account owner can send mail.",
        }, status=403)
    return None


def _map_enqueue_exception_to_flash(request, exc):
    "Public API."
    from apps.outbound.reply_forward import ReplyTargetInvalid
    from apps.outbound.services import (
        AddressFormatError,
        OutboundQuotaExceeded,
        OutboundVelocityExceeded,
        RecipientNotApproved,
        RecipientRejected,
        SendingDomainNotEligible,
    )

    if isinstance(exc, ReplyTargetInvalid):
        message = "This message has no usable reply address."
    elif isinstance(exc, AddressFormatError):
        message = "One or more addresses or fields are invalid."
    elif isinstance(exc, SendingDomainNotEligible):
        message = "This inbox's domain is not eligible to send mail."
    elif isinstance(exc, RecipientRejected):
        message = "One or more recipients are suppressed and cannot receive mail."
    elif isinstance(exc, RecipientNotApproved):
        message = "One or more recipients are not on the approved-recipients allowlist."
    elif isinstance(exc, OutboundQuotaExceeded):
        message = "Your outbound sending quota has been reached."
    elif isinstance(exc, OutboundVelocityExceeded):
        message = "Too much outbound mail sent recently. Please try again shortly."
    else:
        raise exc
    return render_partial(request, "dashboard/partials/flash.html", {"flash": message}, status=422)


def _owned_message_for_inbox_dashboard(inbox, public_id):
    return get_object_or_404(Message, inbox=inbox, public_id=public_id)


@dashboard_required
@_outbound_enabled
@require_http_methods(["GET", "POST"])
def compose(request):
    from apps.accounts.entitlements import outbound_ui_visible

    if request.method == "POST":
        gate = _outbound_gate_flash(request)
        if gate is not None:
            return gate

        inbox = _owned_inbox(request, request.POST.get("inbox"))

        if not _inbox_is_sendable(inbox):
            return render_partial(request, "dashboard/partials/flash.html", {
                "flash": "This inbox cannot send mail.",
            }, status=422)

        to = [addr.strip() for addr in (request.POST.get("to") or "").split(",") if addr.strip()]
        cc = [addr.strip() for addr in (request.POST.get("cc") or "").split(",") if addr.strip()]
        subject = request.POST.get("subject") or ""
        body_text = request.POST.get("body_text") or ""

        from apps.outbound.services import compose_from_inbox

        try:
            compose_from_inbox(
                tenant(request),
                request.user,
                inbox,
                to=to,
                subject=subject,
                body_text=body_text,
                cc=cc,
            )
        except Exception as exc:
            return _map_enqueue_exception_to_flash(request, exc)
        return redirect("dashboard:messages")

    sendable_inboxes = [
        inbox for inbox in _owned_inboxes(request).select_related("domain").order_by("label", "id")
        if _inbox_is_sendable(inbox)
    ]
    return render(request, "dashboard/compose.html", {
        "outbound_ui": outbound_ui_visible(request) and bool(sendable_inboxes),
        "sendable_inboxes": sendable_inboxes,
    })


@dashboard_required
@_outbound_enabled
@post_only("dashboard:message-detail", url_kwargs=('public_id',))
def message_reply(request, public_id):
    gate = _outbound_gate_flash(request)
    if gate is not None:
        return gate

    
    # be owned by tenant(request) via _owned_messages (inbox__user=tenant),
    # so its inbox is owned by construction. Re-fetch anchored ON that inbox
    # (Message.objects.get(inbox=inbox, public_id=...)) to mirror the exact
    # _owned_message_for_inbox uniform-404 shape the API views use.
    message_probe = get_object_or_404(_owned_messages(request).select_related("inbox"), public_id=public_id)
    inbox = message_probe.inbox
    message = _owned_message_for_inbox_dashboard(inbox, public_id)

    body_text = request.POST.get("body_text") or ""
    reply_all = request.POST.get("reply_all") == "1"
    cc = [addr.strip() for addr in (request.POST.get("cc") or "").split(",") if addr.strip()]

    from apps.outbound.services import reply_to_message

    try:
        reply_to_message(
            tenant(request),
            request.user,
            inbox,
            message,
            body_text=body_text,
            reply_all=reply_all,
            cc=cc,
        )
    except Exception as exc:
        return _map_enqueue_exception_to_flash(request, exc)
    return redirect("dashboard:message-detail", public_id=public_id)


@dashboard_required
@_outbound_enabled
@post_only("dashboard:message-detail", url_kwargs=('public_id',))
def message_forward(request, public_id):
    gate = _outbound_gate_flash(request)
    if gate is not None:
        return gate

    message_probe = get_object_or_404(_owned_messages(request).select_related("inbox"), public_id=public_id)
    inbox = message_probe.inbox
    message = _owned_message_for_inbox_dashboard(inbox, public_id)

    to = [addr.strip() for addr in (request.POST.get("to") or "").split(",") if addr.strip()]
    body_text = request.POST.get("body_text") or ""

    from apps.outbound.services import forward_message

    try:
        forward_message(
            tenant(request),
            request.user,
            inbox,
            message,
            to=to,
            body_text=body_text,
        )
    except Exception as exc:
        return _map_enqueue_exception_to_flash(request, exc)
    return redirect("dashboard:message-detail", public_id=public_id)


@dashboard_required
@require_GET
@xframe_options_sameorigin
def message_html(request, public_id):
    message = get_object_or_404(_owned_messages(request), public_id=public_id)
    body = message.html_body or "<p>No HTML body.</p>"
    response = HttpResponse(f"<!doctype html><html><head><meta charset=\"utf-8\"></head><body>{body}</body></html>", content_type="text/html; charset=utf-8")
    response.headers["Content-Security-Policy"] = PREVIEW_CSP
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Cache-Control"] = "private, no-store"
    return response


# System-wide operational metrics for the operator, distinct from the per-user
# `overview`. Aggregates across ALL users; never exposes message content, OTPs,
# API keys, or any secret — counts and derived stats only.


def _signup_sources(since):
    "Public API."
    from django.db.models import Value
    from django.db.models.functions import Coalesce, NullIf

    from apps.accounts.services import DEMO_USER_EMAIL

    paid_plans = [
        plan for plan, limits in settings.BILLING_PLAN_LIMITS.items()
        if limits.get("is_paid_tier")
    ]
    rows = (
        Account.objects.filter(created_at__gte=since)
        .exclude(email_normalized__endswith="@" + settings.WORKSPACE_ACCOUNT_DOMAIN.lower())
        .exclude(email_normalized=DEMO_USER_EMAIL.lower())
        .annotate(
            source=Coalesce(
                NullIf("src_ref", Value("")),
                NullIf("src_referrer_host", Value("")),
                Value("direct"),
            )
        )
        .values("source")
        .annotate(
            signups=Count("pk"),
            verified=Count("pk", filter=Q(email_verified_at__isnull=False)),
            paid=Count("pk", filter=Q(user__account_plan__plan__in=paid_plans)),
        )
        .order_by("-signups", "source")
    )
    return list(rows)


def _staff_only_404(view):
    """qafix2 #20: /app/fleet/ is invisible to customers — a non-staff (or
    anonymous) request gets a plain 404 instead of a redirect that would
    reveal the admin login path. Staff are unaffected."""
    from functools import wraps

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        user = getattr(request, "user", None)
        if not (user is not None and user.is_active and user.is_staff):
            raise Http404()
        return view(request, *args, **kwargs)

    return wrapped


@_staff_only_404
@require_GET
def fleet(request):
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - _timedelta(days=7)

    accounts = Account.objects.all()
    inboxes_all = Inbox.objects.filter(deleted_at__isnull=True)
    messages_all = Message.objects.all()

    # Plan breakdown across active (non-cancelled) plans.
    plan_rows = (
        AccountPlan.objects.values("plan")
        .annotate(n=Count("user"))
        .order_by("-n")
    )

    # Top users by stored message volume (label by email, no secrets).
    top_users = list(
        messages_all.values("inbox__user__account__email_normalized")
        .annotate(n=Count("id"))
        .order_by("-n")[:10]
    )

    context = {
        "generated_at": now,
        "signup_sources": _signup_sources(now - _timedelta(days=30)),
        "customers_total": accounts.count(),
        "customers_verified": accounts.filter(email_verified_at__isnull=False).count(),
        "customers_suspended": accounts.filter(is_suspended=True).count(),
        "customers_new_7d": accounts.filter(created_at__gte=week_ago).count(),
        "inboxes_total": inboxes_all.count(),
        "inboxes_active": inboxes_all.filter(is_enabled=True).count(),
        "messages_total": messages_all.count(),
        "messages_today": messages_all.filter(received_at__gte=today_start).count(),
        "messages_7d": messages_all.filter(received_at__gte=week_ago).count(),
        "api_keys_active": ApiKey.objects.filter(revoked_at__isnull=True).count(),
        "webhooks_active": Webhook.objects.filter(is_enabled=True).count(),
        "usage_month_messages": (
            MonthlyUsage.objects.filter(period_start__day=1, period_start=today_start.date().replace(day=1))
            .aggregate(s=Sum("messages_received"))["s"] or 0
        ),
        "usage_month_api": (
            MonthlyUsage.objects.filter(period_start=today_start.date().replace(day=1))
            .aggregate(s=Sum("api_requests"))["s"] or 0
        ),
        "plan_rows": plan_rows,
        "top_users": top_users,
        **_wait_capacity_panel(now),
        **_open_feedback_panel(),
    }
    return render(request, "dashboard/fleet.html", context)


def _open_feedback_panel():
    """Report a problem: open reports for the staff fleet page. The message
    preview is plain text (first 120 chars), autoescaped by the template."""
    from apps.feedback.models import FeedbackReport

    open_reports = FeedbackReport.objects.filter(status=FeedbackReport.Status.OPEN)
    rows = []
    for report in open_reports.order_by("-created_at", "-id")[:50]:
        text = " ".join(report.message.split())
        rows.append(
            {
                "pk": report.pk,
                "created_at": report.created_at,
                "plan": report.plan or "anonymous",
                "page_path": report.page_path,
                "preview": text[:120] + ("\u2026" if len(text) > 120 else ""),
                "status": report.get_status_display(),
                "admin_url": reverse("admin:feedback_feedbackreport_change", args=[report.pk]),
            }
        )
    return {"feedback_open": rows, "feedback_open_total": open_reports.count()}


def _wait_capacity_panel(now):
    """walert: live in-flight vs limit per wait pool + last 14 days of
    WaitCapacityDaily (peaks/refusals). Read-only, counts only."""
    from apps.accounts import wait_alerts
    from apps.accounts.services import WAIT_CAPACITY_FREE_KEY, WAIT_CAPACITY_KEY

    limits = wait_alerts.pool_limits()
    return {
        "wait_global_now": wait_alerts.current_in_flight(WAIT_CAPACITY_KEY),
        "wait_global_limit": limits[WAIT_CAPACITY_KEY],
        "wait_free_now": wait_alerts.current_in_flight(WAIT_CAPACITY_FREE_KEY),
        "wait_free_limit": limits[WAIT_CAPACITY_FREE_KEY],
        "wait_days": wait_alerts.recent_days(14, now=now),
    }
