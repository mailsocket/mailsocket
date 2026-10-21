import logging
import math
import os
import re
import secrets
import time
from datetime import datetime
from datetime import timezone as datetime_timezone

from django.core.files.uploadhandler import MemoryFileUploadHandler
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.fields import DateTimeField
from rest_framework.parsers import JSONParser, MultiPartParser
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.client_ip import client_ip as _client_ip
from apps.accounts.entitlements import (
    can_add_domain,
    can_configure_webhook,
    can_create_inbox,
    record_api_request,
    record_verifications,
    record_webhook_attempt,
)
from apps.accounts.services import (
    RateLimitExceeded,
    WaitSlotError,
    increment_rate_limit,
    is_free_plan,
    wait_slot,
)
from apps.inboxes.filters import apply_message_filters
from apps.inboxes.models import Inbox, Message
from apps.inboxes.services import (
    InboxCreatePlanLimitReached,
    create_random_inbox,
    random_routing_key,
)
from apps.webhooks.models import Webhook, WebhookDelivery
from apps.webhooks.security import SSRFValidationError
from apps.webhooks.services import (
    SIGNATURE_VERSION,
    derive_signing_secret,
    event_body_bytes,
    signature_header,
)
from apps.webhooks.transport import DeliveryTransportError, post_json_pinned

from .authentication import BearerAuthentication
from .errors import error_response
from .idempotency import idempotent
from .pagination import paginate
from .request_guards import ClosingStream, EventStreamRenderer, agent_name_or_error, once
from .serializers import (
    ApprovedRecipientSerializer,
    ApprovedRecipientWriteSerializer,
    DomainCatchAllWriteSerializer,
    DomainSerializer,
    DomainWriteSerializer,
    InboxComposeWriteSerializer,
    InboxDetailSerializer,
    InboxForwardWriteSerializer,
    InboxReplyWriteSerializer,
    InboxSerializer,
    InboxWriteSerializer,
    MessageBatchSerializer,
    MessageReadWriteSerializer,
    MessageSerializer,
    MessageSummarySerializer,
    OutboundAllowlistModeWriteSerializer,
    OutboundEmailSerializer,
    OutboundEmailWriteSerializer,
    PublicMessageSerializer,
)


def _random_routing_key():
    return random_routing_key()


logger = logging.getLogger(__name__)

def _owned_inboxes(request):
    return Inbox.objects.filter(user=request.user, deleted_at__isnull=True)


def _owned_inbox(request, public_id):
    try:
        return _owned_inboxes(request).get(public_id=public_id)
    except Inbox.DoesNotExist:
        raise NotFound() from None


def _owned_messages(request):
    return Message.objects.filter(
        inbox__user=request.user,
        inbox__deleted_at__isnull=True,
    )


def _owned_message(request, public_id):
    try:
        return _owned_messages(request).select_related("inbox").get(public_id=public_id)
    except Message.DoesNotExist:
        raise NotFound() from None


def _owned_domains(request):
    from apps.domains.models import Domain

    return Domain.objects.filter(user=request.user)


def _owned_domain(request, public_id):
    from apps.domains.models import Domain

    try:
        return _owned_domains(request).get(public_id=public_id)
    except Domain.DoesNotExist:
        raise NotFound() from None


def _outbound_enabled():
    from django.conf import settings as django_settings

    return django_settings.ENABLE_OUTBOUND


def _batch_mutates(body):
    # POST /messages/batch: only mark_read/delete mutate; fetch is read-only,
    # so an Idempotency-Key on a fetch is ignored (always fresh data).
    return not (isinstance(body, dict) and body.get("action") == "fetch")


# Idempotency-Key pre-replay gates (r2): each is the decorated view's own
# leading caller/feature gates, in the view's order, so a replay is never
# handed to a caller the endpoint would refuse now (e.g. a plan downgrade).
# The views still run these gates themselves.


def _batch_idem_gate(request):
    # DOC-3: same order as the view (shape 422 first, answered by the view).
    if not MessageBatchSerializer(data=request.data).is_valid():
        return None
    if is_free_plan(request.user.pk):
        return error_response("plan_upgrade_required", 402)
    return None


def _domain_create_idem_gate(request):
    disabled = _byod_disabled_response()
    if disabled is not None:
        return disabled
    return _domain_owner_required_response(request)


def _outbound_idem_gate(request):
    return _w8_gate_response(request)


def _sending_setup_idem_gate(request):
    
    # check, so only the gate that precedes the lookup runs here (the flag
    # is already on: the decorator's enabled= checks it first); the
    
    return _outbound_forbidden_for_demo_or_agent(request)



# key" helper. Used by both the anonymous instant.md surface (apps.dashboard.
# instant) and PublicInboxMessagesView below so the exact same address-shape
# rules (single @, ROUTING_KEY_RE local part, domain == settings.MAIL_DOMAIN)
# gate both anonymous surfaces identically. Returns the lowercased routing_key
# on success, or None on ANY malformed input (never raises) — callers turn a
# None into their own uniform 404.
def parse_shared_address(email: str):
    from django.conf import settings as django_settings

    from apps.inboxes.models import ROUTING_KEY_RE

    if not isinstance(email, str) or email.count("@") != 1:
        return None
    local, _, domain = email.partition("@")
    local = local.strip().lower()
    domain = domain.strip().lower()
    if domain != django_settings.MAIL_DOMAIN.lower():
        return None
    if not ROUTING_KEY_RE.match(local):
        return None
    return local


class AgentRegisterView(APIView):
    """POST /api/v1/agents/register — zero-signup key minting for AI agents.

    UNauthenticated on purpose: an agent (or a dev) posts an optional name and
    gets back a working API key + a ready-to-use inbox in one call, no email and
    no human. IP-rate-limited. The plaintext key is returned ONCE and never
    retrievable again (only its hash is stored).
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        from apps.accounts.services import (
            AGENT_REGISTER_GLOBAL_RATE_LIMIT,
            AGENT_REGISTER_GLOBAL_RATE_NAME,
            AGENT_REGISTER_GLOBAL_RATE_WINDOW,
            AGENT_REGISTER_RATE_LIMIT,
            AGENT_REGISTER_RATE_NAME,
            AGENT_REGISTER_RATE_WINDOW,
            register_agent,
        )

        ip = _client_ip(request)
        try:
            increment_rate_limit(
                AGENT_REGISTER_RATE_NAME,
                ip,
                limit=AGENT_REGISTER_RATE_LIMIT,
                window_seconds=AGENT_REGISTER_RATE_WINDOW,
            )
            
            # alone is defeated by a routine /64 IPv6 allocation or a botnet;
            # this bounds the TOTAL number of agent accounts minted per hour
            # regardless of source IP.
            increment_rate_limit(
                AGENT_REGISTER_GLOBAL_RATE_NAME,
                "global",
                limit=AGENT_REGISTER_GLOBAL_RATE_LIMIT,
                window_seconds=AGENT_REGISTER_GLOBAL_RATE_WINDOW,
            )
        except RateLimitExceeded as exc:
            resp = error_response("rate_limited", 429)
            reset_seconds = max(1, math.ceil((exc.reset_at - timezone.now()).total_seconds()))
            resp["Retry-After"] = str(reset_seconds)
            return resp

        name, refused = agent_name_or_error(request.data)  
        if refused is not None:
            return refused

        user, api_key, token = register_agent(name)
        # Mint the agent's first inbox so the key is immediately useful.
        
        inbox = create_random_inbox(user, label="agent inbox")

        data = {
            "api_key": token,
            "key_prefix": api_key.prefix,
            "plan": "free",
            "message": "Store api_key securely; it will not be shown again.",
        }
        if inbox is not None:
            data["inbox"] = InboxSerializer(inbox).data
        return Response({"data": data}, status=201)


class BearerAPIView(APIView):
    authentication_classes = [BearerAuthentication]
    permission_classes = []

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        api_key = request.auth
        limits = []
        try:
            limits.append((120, *increment_rate_limit(
                "api:key:minute", api_key.public_id, limit=120, window_seconds=60
            )))
            limits.append((600, *increment_rate_limit(
                "api:key:hour", api_key.public_id, limit=600, window_seconds=3600
            )))
        except RateLimitExceeded as exc:
            self.rate_limit = (exc.limit, 0, exc.reset_at)
            raise
        self.rate_limit = min(limits, key=lambda row: row[1] / row[0])
        record_api_request(request.user)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        limit_data = getattr(response, "rate_limit", getattr(self, "rate_limit", None))
        if limit_data:
            limit, remaining, reset_at = limit_data
            reset_seconds = max(0, math.ceil((reset_at - timezone.now()).total_seconds()))
            response["X-RateLimit-Limit"] = str(limit)
            response["X-RateLimit-Remaining"] = str(remaining)
            response["X-RateLimit-Reset"] = str(math.ceil(reset_at.timestamp()))
            if response.status_code == 429:
                response["Retry-After"] = str(max(1, reset_seconds))
        return response


class VerifyEmailView(BearerAPIView):
    "Public API."

    def post(self, request):
        from apps.accounts.services import (
            VERIFY_ACCOUNT_RATE_LIMIT,
            VERIFY_ACCOUNT_RATE_WINDOW,
            VERIFY_KEY_RATE_LIMIT,
            VERIFY_KEY_RATE_WINDOW,
            acquire_verify_capacity,
            release_verify_capacity,
        )

        # design §2 step 1: existing safe per-key + per-account rate limits,
        # UNCHANGED, applied to BOTH modes (full is a superset of safe
        # traffic, so these limits still bound total /verify volume).
        try:
            increment_rate_limit(
                "api:verify:key:hour",
                str(getattr(request.auth, "id", "")),
                limit=VERIFY_KEY_RATE_LIMIT,
                window_seconds=VERIFY_KEY_RATE_WINDOW,
            )
            
            # up to MAX_ACTIVE_API_KEYS_PER_USER keys, so a per-key-only limit
            # is trivially multiplied by minting more keys.
            increment_rate_limit(
                "api:verify:account:hour",
                str(request.user.pk),
                limit=VERIFY_ACCOUNT_RATE_LIMIT,
                window_seconds=VERIFY_ACCOUNT_RATE_WINDOW,
            )
        except RateLimitExceeded as exc:
            resp = error_response("rate_limited", 429)
            reset_seconds = max(1, math.ceil((exc.reset_at - timezone.now()).total_seconds()))
            resp["Retry-After"] = str(reset_seconds)
            return resp

        # Strict mode parsing (design §2, no silent fallback): validated
        # BEFORE any socket/gate work below, so a typo'd mode never slips
        # through as "safe".
        mode = "safe"
        if isinstance(request.data, dict) and "mode" in request.data:
            raw_mode = request.data.get("mode")
            if raw_mode not in ("safe", "full"):
                return error_response(
                    "validation_error", 422, fields={"mode": ["Must be \"safe\" or \"full\"."]}
                )
            mode = raw_mode

        if mode == "full":
            return self._verify_full_gates(request)

        
        # tie up every worker with slow DNS lookups at once.
        if not acquire_verify_capacity():
            return error_response("rate_limited", 429)
        try:
            return self._verify(request)
        finally:
            release_verify_capacity()

    def _verify_full_gates(self, request):
        """design §2 steps 2a-2d: everything that must sit BEFORE any socket
        for mode="full", in order — flag -> tier -> full key/account buckets
        -> full capacity. Only once all of these pass does execution reach
        the safe checks + probe (design §2 steps 3-5, in _verify)."""
        from django.conf import settings as django_settings

        from apps.accounts.entitlements import can_use_full_verify
        from apps.accounts.services import (
            acquire_verify_full_capacity,
            release_verify_full_capacity,
        )

        if not django_settings.ENABLE_SMTP_VERIFY:
            return error_response("smtp_verify_disabled", 403)

        with transaction.atomic():
            if not can_use_full_verify(request.user):
                return error_response("verify_full_entitlement_required", 403)

        try:
            increment_rate_limit(
                "api:verify-full:key:hour",
                str(getattr(request.auth, "id", "")),
                limit=django_settings.VERIFY_FULL_KEY_RATE_LIMIT,
                window_seconds=django_settings.VERIFY_FULL_KEY_RATE_WINDOW,
            )
            increment_rate_limit(
                "api:verify-full:account:hour",
                str(request.user.pk),
                limit=django_settings.VERIFY_FULL_ACCOUNT_RATE_LIMIT,
                window_seconds=django_settings.VERIFY_FULL_ACCOUNT_RATE_WINDOW,
            )
        except RateLimitExceeded as exc:
            resp = error_response("rate_limited", 429)
            reset_seconds = max(1, math.ceil((exc.reset_at - timezone.now()).total_seconds()))
            resp["Retry-After"] = str(reset_seconds)
            return resp

        if not acquire_verify_full_capacity():
            return error_response("rate_limited", 429)
        try:
            return self._verify(request, full=True)
        finally:
            release_verify_full_capacity()

    def _verify(self, request, *, full=False):

        email = ""
        if isinstance(request.data, dict):
            raw = request.data.get("email", "")
            if isinstance(raw, str):
                email = raw[:320]
        if not email.strip():
            return error_response("validation_error", 422, fields={"email": ["This field is required."]})

        result = verify_one_email(email, full=full)
        record_verifications(request.user.pk, 1)
        return Response({"data": result})




# BOTH VerifyEmailView (single) and VerifyBatchView (bulk) call it — single-

# see the safe-mode-only early return below, which is byte-identical to the
# pre-factor code). NO new SMTP/verification code lives here or anywhere in

# sequence views.py:392-404 always ran, unchanged.
def verify_one_email(email: str, *, full: bool = False) -> dict:
    from apps.api.verification import verify_email

    result = verify_email(email)
    if not full:
        return result

    
    # not disposable; role addresses ARE probed. Per-domain cooldown is
    # checked immediately before the probe, never before. "checked" is left
    # at the safe value on every skipped/failed/degraded path, and is only
    # upgraded to "syntax+dns+smtp" on the completed-probe return below.
    domain = result.get("domain")
    if not result["has_mx"] or result["is_disposable"] or not domain:
        result["smtp"] = {"probe": "skipped", "rcpt_code": None, "catch_all": None, "reason": "not_probed"}
        return result

    from apps.accounts.services import check_verify_full_domain_cooldown, check_verify_full_global_fleet_cap

    if not check_verify_full_global_fleet_cap():
        result["smtp"] = {"probe": "skipped", "rcpt_code": None, "catch_all": None, "reason": "global_fleet_cap"}
        return result

    if not check_verify_full_domain_cooldown(domain):
        result["smtp"] = {"probe": "skipped", "rcpt_code": None, "catch_all": None, "reason": "domain_cooldown"}
        return result

    from apps.api.smtp_probe import run_full_probe

    try:
        probe = run_full_probe(email, domain)
    except Exception:
        logging.getLogger(__name__).exception("smtp_probe_uncaught_exception domain=%s", domain)
        result["smtp"] = {"probe": "failed", "rcpt_code": None, "catch_all": None, "reason": "unexpected_error"}
        return result

    result["smtp"] = {
        "probe": probe.probe,
        "rcpt_code": probe.rcpt_code,
        "catch_all": probe.catch_all,
        "reason": probe.reason,
    }
    if probe.probe == "completed":
        result["checked"] = "syntax+dns+smtp"
    if probe.verdict is not None:
        result["deliverable"] = probe.verdict
    return result



# Batch parse (design §2): ONE tokenizer for paste text AND uploaded files.
# Regex split, strip token + surrounding quotes, drop blanks, per-token cap
# 320 chars (mirror single verify's email[:320], views.py:374) BEFORE dedupe,
# then exact-match order-preserving dedupe (mirror MessageBatchSerializer.
# validate_ids, serializers.py:305-312).
_BATCH_TOKEN_RE = re.compile(r"[\s,;]+")


def _tokenize_batch_text(text: str) -> list[str]:
    tokens = []
    seen = set()
    for raw_token in _BATCH_TOKEN_RE.split(text):
        token = raw_token.strip().strip("'\"")
        if not token:
            continue
        token = token[:320]
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


class VerifyBatchView(BearerAPIView):
    "Public API."

    parser_classes = [JSONParser, MultiPartParser]

    def post(self, request):
        from django.conf import settings as django_settings

        # design §6b: swap upload handlers to IN-MEMORY BEFORE touching
        # request.data anywhere (DRF parses lazily; touching request.data
        # first makes this handler swap raise). The default chain includes
        # TemporaryFileUploadHandler, which streams any part >2.5MB to a DISK
        # temp file — MemoryFileUploadHandler alone buffers in RAM, so the
        # explicit size checks below (Content-Length pre-check + post-parse
        # re-check) are what actually bounds memory use.
        request.upload_handlers = [MemoryFileUploadHandler()]

        content_type = request.content_type or ""
        is_multipart = content_type.startswith("multipart/form-data")

        if is_multipart:
            content_length = request.META.get("CONTENT_LENGTH")
            try:
                content_length = int(content_length) if content_length is not None else None
            except (TypeError, ValueError):
                content_length = None
            if content_length is not None and content_length > django_settings.VERIFY_BATCH_FILE_MAX_BYTES:
                return error_response("file_too_large", 422)

            file_parts = request.FILES.getlist("file")
            if len(file_parts) != 1:
                return error_response("validation_error", 422, fields={"file": ["Exactly one file part named \"file\" is required."]})
            uploaded = file_parts[0]
            # Content-Length can lie — re-check the ACTUAL parsed size.
            if uploaded.size > django_settings.VERIFY_BATCH_FILE_MAX_BYTES:
                return error_response("file_too_large", 422)

            filename = os.path.basename(uploaded.name or "")
            if not filename.lower().endswith((".csv", ".txt")):
                return error_response("unsupported_file_type", 422)

            raw_bytes = uploaded.read()
            try:
                text = raw_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                return error_response("malformed_file", 422)
            text = text.lstrip("\ufeff")
            if "\x00" in text:
                return error_response("malformed_file", 422)

            mode_raw = request.data.get("mode") if isinstance(request.data, dict) else request.POST.get("mode")
            tokens = _tokenize_batch_text(text)
        else:
            body = request.data if isinstance(request.data, dict) else {}
            has_emails = "emails" in body
            has_text = "text" in body
            if has_emails and has_text:
                return error_response("validation_error", 422, fields={"emails": ["Provide either \"emails\" or \"text\", not both."]})
            if has_emails:
                raw_emails = body.get("emails")
                if not isinstance(raw_emails, list):
                    return error_response("validation_error", 422, fields={"emails": ["Must be a list of strings."]})
                tokens = _tokenize_batch_text("\n".join(str(item) for item in raw_emails))
            elif has_text:
                raw_text = body.get("text")
                if not isinstance(raw_text, str):
                    return error_response("validation_error", 422, fields={"text": ["Must be a string."]})
                tokens = _tokenize_batch_text(raw_text)
            else:
                return error_response("validation_error", 422, fields={"emails": ["At least one email is required."]})
            mode_raw = body.get("mode")

        mode = "safe"
        if mode_raw is not None:
            if mode_raw not in ("safe", "full"):
                return error_response("validation_error", 422, fields={"mode": ["Must be \"safe\" or \"full\"."]})
            mode = mode_raw

        if not tokens:
            return error_response("validation_error", 422, fields={"emails": ["At least one email is required."]})

        cap = django_settings.VERIFY_BATCH_MAX_FULL if mode == "full" else django_settings.VERIFY_BATCH_MAX_SAFE
        if len(tokens) > cap:
            return error_response("batch_too_large", 422)

        if mode == "full":
            return self._run_full(request, tokens)
        return self._run_safe(request, tokens)

    # -- shared charge helper -------------------------------------------------

    def _charge_buckets(self, request, n, *, key_bucket, account_bucket, key_limit, key_window,
                         account_limit, account_window):
        """design §3b: charge BOTH buckets atomically by N (conditional,
        via charge_rate_limit) — the SAME bucket names single verify uses,
        so singles and batches share ONE budget. An account-bucket refusal
        AFTER the key bucket already charged rolls the whole savepoint back
        (both statements ride the same connection/transaction), so the key
        bucket is un-charged too."""
        from apps.accounts.services import charge_rate_limit

        with transaction.atomic():
            charge_rate_limit(
                key_bucket, str(getattr(request.auth, "id", "")),
                amount=n, limit=key_limit, window_seconds=key_window,
            )
            charge_rate_limit(
                account_bucket, str(request.user.pk),
                amount=n, limit=account_limit, window_seconds=account_window,
            )

    def _run_safe(self, request, tokens):
        from apps.accounts.services import (
            VERIFY_ACCOUNT_RATE_LIMIT,
            VERIFY_ACCOUNT_RATE_WINDOW,
            VERIFY_KEY_RATE_LIMIT,
            VERIFY_KEY_RATE_WINDOW,
            acquire_verify_capacity,
            release_verify_capacity,
        )

        n = len(tokens)
        try:
            self._charge_buckets(
                request, n,
                key_bucket="api:verify:key:hour", account_bucket="api:verify:account:hour",
                key_limit=VERIFY_KEY_RATE_LIMIT, key_window=VERIFY_KEY_RATE_WINDOW,
                account_limit=VERIFY_ACCOUNT_RATE_LIMIT, account_window=VERIFY_ACCOUNT_RATE_WINDOW,
            )
        except RateLimitExceeded as exc:
            resp = error_response("rate_limited", 429)
            reset_seconds = max(1, math.ceil((exc.reset_at - timezone.now()).total_seconds()))
            resp["Retry-After"] = str(reset_seconds)
            return resp

        # design §3e: ONE verify-capacity slot for the WHOLE batch loop.
        if not acquire_verify_capacity():
            return error_response("rate_limited", 429)
        try:
            results = self._loop(tokens, full=False)
        finally:
            release_verify_capacity()

        record_verifications(request.user.pk, n)
        return Response({"data": self._envelope("safe", results)})

    def _run_full(self, request, tokens):
        from django.conf import settings as django_settings

        from apps.accounts.entitlements import can_use_full_verify
        from apps.accounts.services import (
            acquire_verify_full_capacity,
            release_verify_full_capacity,
        )

        # design §4a: gate order identical to _verify_full_gates, checked
        # ONCE per batch (principal is constant for the whole request).
        if not django_settings.ENABLE_SMTP_VERIFY:
            return error_response("smtp_verify_disabled", 403)
        with transaction.atomic():
            if not can_use_full_verify(request.user):
                return error_response("verify_full_entitlement_required", 403)

        from apps.accounts.services import (
            VERIFY_ACCOUNT_RATE_LIMIT,
            VERIFY_ACCOUNT_RATE_WINDOW,
            VERIFY_KEY_RATE_LIMIT,
            VERIFY_KEY_RATE_WINDOW,
        )

        n = len(tokens)
        try:
            # design §3f + §4a: full buckets IN ADDITION to safe buckets,
            # mirroring single's order (flag -> entitlement -> full buckets).
            self._charge_buckets(
                request, n,
                key_bucket="api:verify:key:hour", account_bucket="api:verify:account:hour",
                key_limit=VERIFY_KEY_RATE_LIMIT,
                key_window=VERIFY_KEY_RATE_WINDOW,
                account_limit=VERIFY_ACCOUNT_RATE_LIMIT,
                account_window=VERIFY_ACCOUNT_RATE_WINDOW,
            )
            self._charge_buckets(
                request, n,
                key_bucket="api:verify-full:key:hour", account_bucket="api:verify-full:account:hour",
                key_limit=django_settings.VERIFY_FULL_KEY_RATE_LIMIT,
                key_window=django_settings.VERIFY_FULL_KEY_RATE_WINDOW,
                account_limit=django_settings.VERIFY_FULL_ACCOUNT_RATE_LIMIT,
                account_window=django_settings.VERIFY_FULL_ACCOUNT_RATE_WINDOW,
            )
        except RateLimitExceeded as exc:
            resp = error_response("rate_limited", 429)
            reset_seconds = max(1, math.ceil((exc.reset_at - timezone.now()).total_seconds()))
            resp["Retry-After"] = str(reset_seconds)
            return resp

        # design §4b: ONE verify-full capacity slot for the WHOLE batch.
        if not acquire_verify_full_capacity():
            return error_response("rate_limited", 429)
        try:
            results = self._loop(tokens, full=True)
        finally:
            release_verify_full_capacity()

        record_verifications(request.user.pk, n)
        return Response({"data": self._envelope("full", results)})

    def _loop(self, tokens, *, full):
        from django.conf import settings as django_settings

        deadline = time.monotonic() + django_settings.VERIFY_BATCH_DEADLINE_SECONDS
        results = []
        for token in tokens:
            if time.monotonic() >= deadline:
                # design §3f: rows past the deadline degrade honestly — charge
                # STANDS (already charged above by N), meter counts them too
                # (charge == meter == N, no refund race).
                results.append({
                    "email": token,
                    "deliverable": "unknown",
                    "syntax_valid": False,
                    "domain": None,
                    "has_mx": False,
                    "is_disposable": False,
                    "is_role": False,
                    "reason": "batch_budget_exhausted",
                    "checked": "skipped (batch budget)",
                })
                continue
            results.append(verify_one_email(token, full=full))
        return results

    @staticmethod
    def _envelope(mode, results):
        summary = {"total": len(results), "deliverable": 0, "risky": 0, "undeliverable": 0, "unknown": 0}
        for row in results:
            verdict = row.get("deliverable")
            if verdict in summary:
                summary[verdict] += 1
        return {"mode": mode, "results": results, "summary": summary}


def _inbox_domain_or_error(request, domain_public_id):
    "Public API."
    if not domain_public_id:
        return None, None
    from django.conf import settings as django_settings

    if not django_settings.ENABLE_BYOD:
        return None, error_response("byod_disabled", 403)
    from apps.domains.models import Domain

    try:
        return Domain.objects.get(
            public_id=domain_public_id, user=request.user, status=Domain.Status.VERIFIED
        ), None
    except Domain.DoesNotExist:
        return None, error_response("domain_not_found_or_unverified", 422)


def _create_custom_slug_inbox(user, custom_key, domain_obj, data):
    """A caller-supplied slug is used verbatim (no retry loop — a collision is
    a definite "taken", 409 slug_taken). Scope the pre-check to the SAME
    domain dimension as the two partial unique constraints
    (inbox_shared_routing_key_uniq / inbox_custom_routing_key_uniq) — a plain
    routing_key= filter would wrongly 409 a custom-domain slug that only
    collides with an unrelated shared-domain (or different-domain) inbox.
    Returns (inbox, None) or (None, error response)."""
    if Inbox.objects.filter(routing_key=custom_key, domain=domain_obj).exists():
        return None, error_response("slug_taken", 409)
    try:
        with transaction.atomic():
            if not can_create_inbox(user):
                return None, error_response("plan_limit_reached", 409)
            return Inbox.objects.create(
                user=user, label=data.get("label", ""), routing_key=custom_key, domain=domain_obj,
                is_public=data.get("public", False), metadata=data.get("metadata", {}),
            ), None
    except IntegrityError:
        return None, error_response("slug_taken", 409)


class InboxListCreateView(BearerAPIView):
    def get(self, request):
        rows, pagination = paginate(
            request,
            _owned_inboxes(request),
            kind="inboxes",
            owner_id=request.user.pk,
            active_filter="not_deleted",
            timestamp_field="created_at",
        )
        return Response({"data": InboxSerializer(rows, many=True).data, "pagination": pagination})

    @idempotent()
    def post(self, request):
        serializer = InboxWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        custom_key = serializer.validated_data.get("routing_key")
        domain_public_id = (serializer.validated_data.get("domain") or "").strip()
        is_public = serializer.validated_data.get("public", False)
        metadata = serializer.validated_data.get("metadata", {})

        #  (BYOD) binding: see _inbox_domain_or_error.
        domain_obj, refused = _inbox_domain_or_error(request, domain_public_id)
        if refused is not None:
            return refused

        
        # inbox can never be made public — checked here BEFORE either create
        # branch runs, so both the custom-slug and random-slug paths are
        # covered by one check (design "sweep of every write path" note).
        if is_public and domain_public_id:
            return error_response("public_not_allowed_on_custom_domain", 422)

        try:
            remaining, reset_at = increment_rate_limit(
                "api:user:inbox-create:hour", str(request.user.pk), limit=10, window_seconds=3600
            )
            self.rate_limit = (10, remaining, reset_at)
        except RateLimitExceeded as exc:
            self.rate_limit = (exc.limit, 0, exc.reset_at)
            raise
        # A caller-supplied slug is used verbatim (no retry loop — a collision is
        # a definite "taken", returned as 409 slug_taken). A random slug retries
        # on the rare collision as before.
        if custom_key:
            inbox, refused = _create_custom_slug_inbox(
                request.user, custom_key, domain_obj, serializer.validated_data
            )
            if refused is not None:
                return refused
            response = Response({"data": InboxSerializer(inbox).data}, status=201)
            response["Location"] = f"/api/v1/inboxes/{inbox.public_id}"
            return response
        def _guard():
            if not can_create_inbox(request.user):
                raise InboxCreatePlanLimitReached()

        try:
            inbox = create_random_inbox(
                request.user,
                label=serializer.validated_data.get("label", ""),
                guard=_guard,
                domain=domain_obj,
                metadata=metadata,
                is_public=is_public,  
            )
        except InboxCreatePlanLimitReached:
            return error_response("plan_limit_reached", 409)
        if inbox is None:
            return error_response("conflict", 409)
        response = Response({"data": InboxSerializer(inbox).data}, status=201)
        response["Location"] = f"/api/v1/inboxes/{inbox.public_id}"
        return response


class InboxDetailView(BearerAPIView):
    def get(self, request, public_id):
        month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        try:
            inbox = _owned_inboxes(request).annotate(
                message_count_month=Count("messages", filter=Q(messages__received_at__gte=month_start))
            ).get(public_id=public_id)
        except Inbox.DoesNotExist:
            raise NotFound() from None
        inbox.webhook_configured = hasattr(inbox, "webhook")
        return Response({"data": InboxDetailSerializer(inbox).data})

    def delete(self, request, public_id):
        from apps.domains.services import CatchAllToggleRefused, assert_not_active_catch_all_target

        with transaction.atomic():
            try:
                inbox = _owned_inboxes(request).select_for_update().get(public_id=public_id)
            except Inbox.DoesNotExist:
                raise NotFound() from None
            try:
                assert_not_active_catch_all_target(inbox)
            except CatchAllToggleRefused as exc:
                return error_response(exc.code, 409)
            inbox.deleted_at = timezone.now()
            inbox.is_enabled = False
            inbox.save(update_fields=["deleted_at", "is_enabled", "updated_at"])
        return Response(status=204)


class InboxMessagesView(BearerAPIView):
    def get(self, request, public_id):
        inbox = _owned_inbox(request, public_id)
        queryset = Message.objects.filter(inbox_id=inbox.pk).select_related("inbox")
        queryset, filter_signature = apply_message_filters(queryset, request.query_params)
        active_filter = f"inbox:{inbox.public_id}:not_deleted"
        if filter_signature:
            active_filter = f"{active_filter}|{filter_signature}"
        rows, pagination = paginate(
            request,
            queryset,
            kind="inbox_messages",
            owner_id=request.user.pk,
            active_filter=active_filter,
            timestamp_field="received_at",
        )
        return Response({
            "data": MessageSummarySerializer(rows, many=True).data,
            "pagination": pagination,
        })


class LatestInboxMessageView(BearerAPIView):
    def get(self, request, public_id):
        inbox = _owned_inbox(request, public_id)
        message = Message.objects.filter(inbox_id=inbox.pk).select_related("inbox").order_by(
            "-received_at", "-id"
        ).first()
        if message is None:
            raise NotFound()
        return Response({"data": MessageSerializer(message).data})


class MessageDetailView(BearerAPIView):
    def get(self, request, public_id):
        return Response({"data": MessageSerializer(_owned_message(request, public_id)).data})

    def patch(self, request, public_id):
        serializer = MessageReadWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        message = _owned_message(request, public_id)
        if serializer.validated_data["read"]:
            if message.read_at is None:
                message.read_at = timezone.now()
                message.save(update_fields=["read_at", "updated_at"])
        else:
            if message.read_at is not None:
                message.read_at = None
                message.save(update_fields=["read_at", "updated_at"])
        return Response({"data": MessageSerializer(message).data})


class MessageBatchView(BearerAPIView):
    "Public API."

    @idempotent(applies=_batch_mutates, gate=_batch_idem_gate)
    def post(self, request):
        serializer = MessageBatchSerializer(data=request.data)  # DOC-3: 422 before 402
        serializer.is_valid(raise_exception=True)
        if is_free_plan(request.user.pk):
            return error_response("plan_upgrade_required", 402)

        try:
            remaining, reset_at = increment_rate_limit(
                "api:user:batch:hour", str(request.user.pk), limit=60, window_seconds=3600
            )
            self.rate_limit = (60, remaining, reset_at)
        except RateLimitExceeded as exc:
            self.rate_limit = (exc.limit, 0, exc.reset_at)
            raise

        action = serializer.validated_data["action"]
        ids = serializer.validated_data["ids"]

        # TENANT SCOPE — the single most important line in this view: load
        # ONLY the caller's own messages, keyed by public_id, BEFORE doing
        # anything else. Never touch Message.objects.filter(...) directly.
        owned = {
            m.public_id: m
            for m in _owned_messages(request).filter(public_id__in=ids).select_related("inbox")
        }

        results = []
        if action == "fetch":
            for public_id in ids:
                message = owned.get(public_id)
                if message is None:
                    results.append({"id": public_id, "status": "not_found"})
                else:
                    results.append({
                        "id": public_id,
                        "status": "ok",
                        "message": MessageSerializer(message).data,
                    })
        elif action == "mark_read":
            now = timezone.now()
            to_mark = [m.pk for m in owned.values() if m.read_at is None]
            if to_mark:
                Message.objects.filter(pk__in=to_mark).update(read_at=now, updated_at=now)
            for public_id in ids:
                if public_id in owned:
                    results.append({"id": public_id, "status": "ok"})
                else:
                    results.append({"id": public_id, "status": "not_found"})
        else:  # delete
            owned_pks = [m.pk for m in owned.values()]
            if owned_pks:
                with transaction.atomic():
                    # PROTECT FK (webhooks/models.py:76): WebhookDelivery MUST
                    # be removed before the Message row, exactly the pattern
                    # retention.py:120-123 already proved safe.
                    WebhookDelivery.objects.filter(message_id__in=owned_pks).delete()
                    Message.objects.filter(id__in=owned_pks).delete()
            for public_id in ids:
                if public_id in owned:
                    results.append({"id": public_id, "status": "deleted"})
                else:
                    results.append({"id": public_id, "status": "not_found"})

        return Response({"data": {"results": results}}, status=200)


class WebhookTestRefused(Exception):
    """A webhook test send that never reached (or was refused before) the
    transport. ``code``/``status``/``fields`` map 1:1 onto ``error_response``."""

    def __init__(self, code, status, fields=None, rate_limit=None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.fields = fields
        # Set once the rate-limit buckets were charged, so the API keeps
        # emitting X-RateLimit-* headers on post-charge failures.
        self.rate_limit = rate_limit


def send_webhook_test(inbox, account_user):
    "Public API."
    if not can_configure_webhook(account_user):
        raise WebhookTestRefused("webhook_entitlement_required", 403)

    try:
        webhook = Webhook.objects.get(inbox=inbox)
    except Webhook.DoesNotExist:
        raise WebhookTestRefused("webhook_not_configured", 404) from None

    # Decision Q3: test is allowed even when the webhook is disabled (a
    # dev verifying their receiver before flipping it on).
    inbox_remaining, inbox_reset_at = increment_rate_limit(
        "api:webhook-test:inbox:hour", inbox.public_id, limit=10, window_seconds=3600
    )
    account_remaining, account_reset_at = increment_rate_limit(
        "api:webhook-test:account:hour", str(account_user.pk), limit=30, window_seconds=3600
    )
    rate_limit = min(
        [(10, inbox_remaining, inbox_reset_at), (30, account_remaining, account_reset_at)],
        key=lambda row: row[1] / row[0],
    )

    event_id = f"evt_test_{secrets.token_urlsafe(18)}"
    now = timezone.now()
    payload = {
        "id": event_id,
        "event": "webhook.test",
        "created_at": now.astimezone(datetime_timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "data": {
            "message": {
                "id": "msg_test",
                "inbox_id": inbox.public_id,
                "from": "test@mailsocket.app",
                "to": [inbox.address],
                "cc": [],
                "subject": "Mailsocket webhook test",
                "received_at": now.astimezone(datetime_timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "status": "parsed",
                "otp": "123456",
                "otp_confidence": 0.95,
                "magic_link": None,
                "links": [],
                "detected_otp": "123456",
                "envelope_from": "test@mailsocket.app",
                "envelope_recipient": inbox.address,
                "text": "This is a synthetic test event from Mailsocket. Code: 123456",
                "html": "<p>This is a synthetic test event from Mailsocket. Code: 123456</p>",
                "otp_context": None,
                "otp_candidates": None,
                "provider_hint": None,
                "read": False,
            }
        },
    }
    body = event_body_bytes(payload)
    timestamp = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "mailsocket-webhooks/1",
        "X-Mailsocket-Event-Id": event_id,
        "X-Mailsocket-Event": "webhook.test",
        "X-Mailsocket-Signature": signature_header(
            derive_signing_secret(webhook), timestamp, body
        ),
        "X-Mailsocket-Signature-Version": SIGNATURE_VERSION,
    }

    
    # real-delivery semantics (engine.py:174) — every attempted test counts,
    # including timeouts/refused/TLS failures and SSRF-rejections (all of
    # which happen inside post_json_pinned). This is deliberate: "attempted"
    # is metered, not "succeeded".
    record_webhook_attempt(inbox.user_id)

    try:
        response = post_json_pinned(webhook.url, body, headers)
    except SSRFValidationError as exc:
        raise WebhookTestRefused(
            "webhook_ssrf_rejected", 400, fields={"url": [exc.code]}, rate_limit=rate_limit
        ) from None
    except DeliveryTransportError as exc:
        raise WebhookTestRefused(
            "webhook_unreachable", 502, fields={"error": [exc.code]}, rate_limit=rate_limit
        ) from exc

    return {
        "delivered": True,
        "response_status": response.status_code,
        "signature_version": SIGNATURE_VERSION,
        "event_id": event_id,
    }, rate_limit


class WebhookTestView(BearerAPIView):
    """POST /api/inboxes/<public_id>/webhook/test — one synchronous signed test send.

    Thin HTTP adapter over ``send_webhook_test`` (shared with the dashboard).
    """

    def post(self, request, public_id):
        inbox = _owned_inbox(request, public_id)
        try:
            data, self.rate_limit = send_webhook_test(inbox, request.user)
        except RateLimitExceeded as exc:
            self.rate_limit = (exc.limit, 0, exc.reset_at)
            raise
        except WebhookTestRefused as exc:
            if exc.rate_limit is not None:
                self.rate_limit = exc.rate_limit
            if exc.fields is not None:
                return error_response(exc.code, exc.status, fields=exc.fields)
            return error_response(exc.code, exc.status)
        return Response({"data": data})


class InboxTestCodeView(BearerAPIView):
    """POST /api/v1/inboxes/<public_id>/test-code — drop one sample OTP mail
    into one of the caller's own inboxes (ux3 N1).

    Thin HTTP adapter over ``apps.dashboard.views.send_test_code``, the same
    function behind the dashboard "Send a test code" button: same deliver
    path (store_message + parse_message, no SMTP), same per-inbox and
    per-account buckets keyed the same way, so the two paths share one
    quota. Tenant-scoped through ``_owned_inbox`` (foreign, deleted and
    unknown ids are the same 404 envelope, and nothing is charged).
    """

    @idempotent()
    def post(self, request, public_id):
        from django.urls import reverse

        from apps.dashboard.views import (
            InboxCannotTakeTestCode,
            rate_limited_test_code_message,
            send_test_code,
        )

        inbox = _owned_inbox(request, public_id)
        try:
            message, self.rate_limit = send_test_code(inbox, request.user)
        except InboxCannotTakeTestCode:
            return error_response("inbox_disabled", 409)
        except RateLimitExceeded as exc:
            self.rate_limit = (exc.limit, 0, exc.reset_at)
            response = error_response("rate_limited", 429)
            # ux3 N8: say when to retry; Retry-After carries the same moment.
            response.data["error"]["message"] = rate_limited_test_code_message(exc.reset_at)
            return response
        data = {
            "message_id": message.public_id,
            # Same format as every other API timestamp (DATETIME_FORMAT).
            "sent_at": DateTimeField().to_representation(message.received_at),
        }
        response = Response({"data": data}, status=201)
        response["Location"] = reverse("api:message-detail", args=[message.public_id])
        return response



#
# GET /api/v1/public/emails/<email>/messages — THE SAFE DEFAULT is opt-in:
# Inbox.is_public defaults False (design R1). This view re-guards at READ
# time (domain__isnull=True + is_public=True + MAIL_DOMAIN-parsed address),
# independently of the WRITE-time check in InboxListCreateView.post, so even
# a DB-forced is_public=True on a BYOD inbox can never be resolved here.
PUBLIC_MESSAGES_RATE_NAME = "public:messages:minute"
PUBLIC_MESSAGES_RATE_LIMIT = 30
PUBLIC_MESSAGES_RATE_WINDOW = 60
PUBLIC_MESSAGES_MAX_LIMIT = 20


class PublicInboxMessagesView(APIView):
    """GET /api/v1/public/emails/<email>/messages — no auth, opt-in only.

    Every miss (unknown address, private inbox, disabled, deleted, custom
    domain, malformed address) returns the IDENTICAL 404 envelope — no
    enumeration oracle distinguishing "exists but private" from "doesn't
    exist" (design P6).
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request, email):
        ip = _client_ip(request)
        try:
            increment_rate_limit(
                PUBLIC_MESSAGES_RATE_NAME,
                ip,
                limit=PUBLIC_MESSAGES_RATE_LIMIT,
                window_seconds=PUBLIC_MESSAGES_RATE_WINDOW,
            )
        except RateLimitExceeded as exc:
            resp = error_response("rate_limited", 429)
            resp["Cache-Control"] = "no-store"
            reset_seconds = max(1, math.ceil((exc.reset_at - timezone.now()).total_seconds()))
            resp["Retry-After"] = str(reset_seconds)
            return resp

        routing_key = parse_shared_address(email)
        if routing_key is None:
            resp = error_response("not_found", 404)
            resp["Cache-Control"] = "no-store"
            return resp

        # THE read-time re-guard (R1): domain__isnull=True (shared-domain
        # only — parse_shared_address already enforced the MAIL_DOMAIN shape,
        # this is the DB-side half) AND is_public=True AND is_enabled AND not
        # deleted. A BYOD inbox can never satisfy domain__isnull=True, so a
        # DB-forced is_public=True on one is structurally unreachable here.
        try:
            inbox = Inbox.objects.get(
                routing_key=routing_key,
                domain__isnull=True,
                is_public=True,
                is_enabled=True,
                deleted_at__isnull=True,
            )
        except Inbox.DoesNotExist:
            resp = error_response("not_found", 404)
            resp["Cache-Control"] = "no-store"
            return resp

        try:
            limit = min(
                PUBLIC_MESSAGES_MAX_LIMIT,
                max(1, int(request.query_params.get("limit", PUBLIC_MESSAGES_MAX_LIMIT))),
            )
        except (TypeError, ValueError):
            limit = PUBLIC_MESSAGES_MAX_LIMIT

        rows = list(
            Message.objects.filter(inbox_id=inbox.pk)
            .select_related("inbox")
            .order_by("-received_at", "-id")[:limit]
        )
        resp = Response({"data": PublicMessageSerializer(rows, many=True).data})
        resp["Cache-Control"] = "no-store"
        return resp


# --- wait-for-OTP/magic-link long-poll endpoint ---------------------------------
#
# Contract (GET /api/v1/inboxes/<public_id>/messages/wait):
#   timeout         seconds to block (default 20, clamped to [1, WAIT_TIMEOUT_MAX=25]).
#   since           ISO8601 datetime, a message public_id, or a unix timestamp.
#                   Only messages received strictly after this point match.
#                   Default: the request start time (only future arrivals match).
#   require         otp (default) | link | any  — what counts as a match.
#   min_confidence  float, default 0.0 — OTP must have otp_confidence >= this.
#
# Responses:
#   200 {"data": MessageSerializer(...)}  — a matching parsed message arrived.
#   204 (empty body)                       — waited, nothing yet, call again.
#   200/204 carry Mailsocket-Wait-Since (wait_cursor.py): send it back as since.
#   404                                    — inbox not owned / unknown.
#   429 too_many_wait_requests             — per-account concurrent-wait cap.
#   429 wait_capacity                      — global concurrent-wait cap.
#
# The web pool runs gthread workers (3 x 32 threads), so a blocked wait occupies
# one thread, not a whole process. A per-account slot cap (WaitLease) and a
# global cap (WaitCapacity) bound exhaustion across any number of accounts.
WAIT_TIMEOUT_MAX = 25
# Poll cadence: WAIT_POLL_SECONDS for the first WAIT_POLL_FAST_WINDOW_SECONDS
# (where most OTPs land), then WAIT_POLL_SLOW_SECONDS. Measured in the

# flat 0.5s, 64 held waits = 128 queries/s and pushed normal-request p95 past
# 1s on a 2-vCPU box; backing off after 5s keeps p95 < 300ms.
WAIT_POLL_SECONDS = 0.5
WAIT_POLL_FAST_WINDOW_SECONDS = 5.0
WAIT_POLL_SLOW_SECONDS = 1.0
WAIT_REQUIRE_CHOICES = ("otp", "link", "any")

# #36: both wait 429s (too_many_wait_requests,
# wait_capacity) and the SSE equivalent carry Retry-After so clients know
# the backoff policy — a small fixed integer, since both caps are freed by
# other requests finishing rather than a fixed-window reset we could report
# more precisely.
WAIT_SLOT_RETRY_AFTER_SECONDS = 2


def _parse_require(value, default):
    """QA #6: validate the `require` query param strictly.

    Absent or empty -> ``default`` (some clients serialize an unset param as
    ``require=``; that was always the default and stays so). Any other value
    outside otp|link|any raises a DRF
    ValidationError, which api_exception_handler renders as the standard
    422 validation_error envelope with ``fields.require`` listing the allowed
    values. Previously an unknown value (e.g. the typo ``require=links``) was
    silently coerced to the default, so the caller waited for the wrong thing.
    """
    if value is None or value == "":
        return default
    if value not in WAIT_REQUIRE_CHOICES:
        raise ValidationError({
            "require": [f"Must be one of: {', '.join(WAIT_REQUIRE_CHOICES)}."]
        })
    return value


def _parse_wait_timeout(value):
    if value is None:
        return 20
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 20
    if not math.isfinite(seconds):
        return 20
    return max(1.0, min(WAIT_TIMEOUT_MAX, seconds))


def _parse_min_confidence(value):
    if value is None or value == "":
        return 0.0
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(confidence):
        return 0.0
    return max(0.0, min(1.0, confidence))


def _parse_wait_since(value, inbox=None):
    "Public API."
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text == "0":
        return datetime(1970, 1, 1, tzinfo=datetime_timezone.utc)
    try:
        ts = float(text)
    except ValueError:
        ts = None
    if ts is not None and math.isfinite(ts):
        # Treat unix seconds; also accept millisecond precision when obviously so.
        if ts >= 1e12:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=datetime_timezone.utc)
        except (OverflowError, OSError, ValueError):
            return datetime(1970, 1, 1, tzinfo=datetime_timezone.utc)

    from .wait_cursor import parse_iso_since

    parsed = parse_iso_since(text)
    if parsed is not None:
        return parsed

    # A message public_id: resolve to that owned message's received_at. Scope the
    # lookup to the caller's inbox so a foreign id cannot shift the cutoff.
    if text.startswith("msg_") and inbox is not None:
        cutoff = Message.objects.filter(inbox_id=inbox.pk, public_id=text).values_list(
            "received_at", flat=True
        ).first()
        if cutoff is not None:
            return cutoff

    # Unparseable -> epoch (see docstring).
    return datetime(1970, 1, 1, tzinfo=datetime_timezone.utc)


def _matching_queryset(inbox, require, min_confidence):
    base = Message.objects.filter(
        inbox_id=inbox.pk,
        ingest_status=Message.IngestStatus.PARSED,
    )
    if require == "otp":
        base = base.filter(otp__isnull=False, otp_confidence__gte=min_confidence)
    elif require == "link":
        base = base.filter(magic_link__isnull=False)
    else:  # any
        otp_q = Q(otp__isnull=False, otp_confidence__gte=min_confidence)
        link_q = Q(magic_link__isnull=False)
        base = base.filter(otp_q | link_q)
    return base


def _wait_poll_interval(elapsed):
    "Public API."
    if elapsed < WAIT_POLL_FAST_WINDOW_SECONDS:
        return WAIT_POLL_SECONDS
    return max(WAIT_POLL_SECONDS, WAIT_POLL_SLOW_SECONDS)


def _release_db_connection_while_idle():
    "Public API."
    if getattr(connection, "pool", None) is not None and not connection.in_atomic_block:
        connection.close()


def _wait(inbox, require, min_confidence, timeout, since, *, serializer_class=MessageSerializer):
    started = time.monotonic()
    deadline = started + timeout
    while True:
        message = (
            _matching_queryset(inbox, require, min_confidence)
            .filter(received_at__gt=since)
            .order_by("received_at", "id")
            .select_related("inbox")
            .first()
        )
        if message is not None:
            return Response({"data": serializer_class(message).data})
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            return Response(status=204)
        _release_db_connection_while_idle()
        time.sleep(min(_wait_poll_interval(now - started), remaining))


def perform_wait(*, user, inbox, require, min_confidence, timeout, since,
                 serializer_class=MessageSerializer):
    "Public API."
    try:
        with wait_slot(user.pk):
            return _wait(
                inbox, require, min_confidence, timeout, since, serializer_class=serializer_class
            )
    except WaitSlotError as exc:
        resp = error_response(exc.reason, 429)
        resp["Retry-After"] = str(WAIT_SLOT_RETRY_AFTER_SECONDS)
        return resp


class WaitForMessageView(BearerAPIView):
    def get(self, request, public_id):
        from .wait_cursor import with_wait_since

        inbox = _owned_inbox(request, public_id)
        require = _parse_require(request.query_params.get("require"), "otp")

        min_confidence = _parse_min_confidence(request.query_params.get("min_confidence"))
        timeout = _parse_wait_timeout(request.query_params.get("timeout"))
        since = _parse_wait_since(request.query_params.get("since"), inbox=inbox)
        if since is None:
            since = timezone.now()

        return with_wait_since(perform_wait(
            user=request.user, inbox=inbox, require=require,
            min_confidence=min_confidence, timeout=timeout, since=since,
        ), since)


# Server-Sent Events stream duration. Bounded on purpose: the connection holds a
# gthread worker slot for its whole life (like long-poll), so we cap it and let
# EventSource auto-reconnect (default 3s, we advertise `retry:`). One stream ==
# one wait lease + one capacity slot, so SSE honours the same worker caps.
SSE_MAX_SECONDS = 25.0
SSE_HEARTBEAT_SECONDS = 5.0  
SSE_POLL_SECONDS = 1.0


class StreamMessagesView(BearerAPIView):
    """GET /api/v1/inboxes/<id>/stream — Server-Sent Events push of new messages.

    Emits a ``message`` event per newly-ingested message (matching require/
    min_confidence), a periodic ``: heartbeat`` comment, and a final ``bye``
    event before closing at SSE_MAX_SECONDS so clients reconnect with a fresh
    ``since`` cursor. Same lease/capacity gating as the long-poll wait.
    """

    # B1: accept text/event-stream (EventSource), */*, application/json, and
    # no-Accept-header requests. JSONRenderer stays so error_response() calls
    # elsewhere in the request lifecycle (e.g. 429 from wait_slot) still
    # negotiate correctly instead of 406-ing before this view's body runs.
    renderer_classes = [EventStreamRenderer, JSONRenderer]

    def get(self, request, public_id):
        import json as _json

        from django.http import StreamingHttpResponse

        from .wait_cursor import after_cursor, with_wait_since

        inbox = _owned_inbox(request, public_id)
        require = _parse_require(request.query_params.get("require"), "any")
        min_confidence = _parse_min_confidence(request.query_params.get("min_confidence"))
        since = _parse_wait_since(request.query_params.get("since"), inbox=inbox)
        if since is None:
            since = timezone.now()

        user_pk = request.user.pk
        try:
            slot_ctx = wait_slot(user_pk)
            slot_ctx.__enter__()
        except WaitSlotError as exc:
            resp = error_response(exc.reason, 429)
            resp["Retry-After"] = str(WAIT_SLOT_RETRY_AFTER_SECONDS)
            return resp

        
        release_slot = once(lambda: slot_ctx.__exit__(None, None, None))

        def event_stream():
            cursor_received_at, cursor_id = since, None
            deadline = time.monotonic() + SSE_MAX_SECONDS
            last_beat = time.monotonic()
            try:
                yield "retry: 3000\n\n"
                yield ": connected\n\n"
                while True:
                    
                    # since until the first emit (see after_cursor).
                    new_messages = list(
                        _matching_queryset(inbox, require, min_confidence)
                        .filter(after_cursor(cursor_received_at, cursor_id))
                        .order_by("received_at", "id")
                        .select_related("inbox")[:20]
                    )
                    for msg in new_messages:
                        cursor_received_at = msg.received_at
                        cursor_id = msg.id
                        payload = _json.dumps(MessageSerializer(msg).data)
                        yield f"event: message\ndata: {payload}\n\n"
                    now = time.monotonic()
                    if now >= deadline:
                        yield 'event: bye\ndata: {"reason":"reconnect"}\n\n'
                        return
                    if now - last_beat >= SSE_HEARTBEAT_SECONDS:
                        last_beat = now
                        yield ": heartbeat\n\n"
                    _release_db_connection_while_idle()
                    time.sleep(min(SSE_POLL_SECONDS, max(0.0, deadline - now)))
            finally:
                release_slot()

        response = StreamingHttpResponse(
            ClosingStream(event_stream(), release_slot), content_type="text/event-stream"
        )
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"  # disable nginx/proxy buffering
        return with_wait_since(response, since)


# ---  (BYOD): domain CRUD + DNS verify ------------------------
#

# (feature flag, default False). can_add_domain is a SEPARATE gate that
# applies ONLY to create (DomainListCreateView.post) — list/get/delete/verify
# are available to any BYOD-enabled account for domains it already owns (see
# "downgrade users keep read/list/delete" in the Stage A report). Neither
# gate touches the mail path: deploy/postfix/, resolve_inbox,
# parse_envelope_recipient, and store_message are untouched by Stage A.


def _byod_disabled_response():
    from django.conf import settings as django_settings

    if not django_settings.ENABLE_BYOD:
        return error_response("byod_disabled", 403)
    return None


def _api_key_is_owner_minted(request) -> bool:
    "Public API."
    api_key = request.auth
    created_by_id = getattr(api_key, "created_by_id", None)
    return created_by_id is None or created_by_id == api_key.user_id


def _domain_owner_required_response(request):
    if not _api_key_is_owner_minted(request):
        return error_response("account_forbidden", 403)
    return None


class DomainListCreateView(BearerAPIView):
    def get(self, request):
        disabled = _byod_disabled_response()
        if disabled is not None:
            return disabled
        rows, pagination = paginate(
            request,
            _owned_domains(request),
            kind="domains",
            owner_id=request.user.pk,
            active_filter="all",
            timestamp_field="created_at",
        )
        return Response({"data": DomainSerializer(rows, many=True).data, "pagination": pagination})

    @idempotent(gate=_domain_create_idem_gate)
    def post(self, request):
        from apps.domains.services import DomainCreateRefused, create_domain

        disabled = _byod_disabled_response()
        if disabled is not None:
            return disabled
        forbidden = _domain_owner_required_response(request)
        if forbidden is not None:
            return forbidden

        # QA #5: entitlement BEFORE body validation (same order as the
        # webhook-test and batch gates), so a plan that can never have custom
        # domains (custom_domains == 0: free/paid) gets the documented
        # 403 domain_entitlement_required instead of a 422 about the body.
        # Plain non-locking read (display-style pre-check); the authoritative
        # locked can_add_domain() re-check below still runs inside atomic().
        from apps.accounts.entitlements import plan_limits
        from apps.accounts.models import AccountPlan

        pre_plan = AccountPlan.objects.filter(user=request.user).first()
        if pre_plan is not None and plan_limits(pre_plan.plan)["custom_domains"] == 0:
            return error_response("domain_entitlement_required", 403)

        serializer = DomainWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            if not can_add_domain(request.user):
                
                # domain_entitlement_required for a tier that simply cannot
                # have custom domains at all (custom_domains == 0, e.g.
                # free/paid), vs domain_limit_reached for a Scale/Business
                # account that hit its own numeric cap. Both map to the same
                # can_add_domain() False, so re-derive the reason here rather
                # than duplicate can_add_domain's locking logic.
                plan = AccountPlan.objects.filter(user=request.user).first()
                if plan is not None and plan_limits(plan.plan)["custom_domains"] == 0:
                    return error_response("domain_entitlement_required", 403)
                return error_response("domain_limit_reached", 403)
            try:
                domain = create_domain(request.user, serializer.validated_data["name"])
            except DomainCreateRefused as exc:
                
                # reserved-apex full_clean() ValidationError into
                # DomainCreateRefused(code="validation_error") — that must
                # come back as 422 (the documented contract), never 409
                # (409 is reserved for the real "name already taken" case).
                status_code = 422 if exc.code == "validation_error" else 409
                return error_response(exc.code, status_code)
            except IntegrityError:
                return error_response("domain_taken", 409)

        response = Response({"data": DomainSerializer(domain).data}, status=201)
        response["Location"] = f"/api/v1/domains/{domain.public_id}"
        return response


class DomainDetailView(BearerAPIView):
    def get(self, request, public_id):
        disabled = _byod_disabled_response()
        if disabled is not None:
            return disabled
        domain = _owned_domain(request, public_id)
        return Response({"data": DomainSerializer(domain).data})

    def delete(self, request, public_id):
        from apps.domains.services import DomainDeleteRefused, delete_domain

        disabled = _byod_disabled_response()
        if disabled is not None:
            return disabled
        domain = _owned_domain(request, public_id)
        forbidden = _domain_owner_required_response(request)
        if forbidden is not None:
            return forbidden
        try:
            delete_domain(domain)
        except DomainDeleteRefused as exc:
            return error_response(exc.code, 409)
        return Response(status=204)


class DomainVerifyView(BearerAPIView):
    """POST /api/v1/domains/{id}/verify — on-demand DNS re-check.

    Rate-limited (): <=1/30s per domain, <=20/hour per account.
    """

    def post(self, request, public_id):
        from apps.domains.services import verify_domain

        disabled = _byod_disabled_response()
        if disabled is not None:
            return disabled
        domain = _owned_domain(request, public_id)
        forbidden = _domain_owner_required_response(request)
        if forbidden is not None:
            return forbidden

        try:
            increment_rate_limit(
                "api:domain-verify:domain:30s", domain.public_id, limit=1, window_seconds=30
            )
            increment_rate_limit(
                "api:domain-verify:account:hour", str(request.user.pk), limit=20, window_seconds=3600
            )
        except RateLimitExceeded as exc:
            resp = error_response("domain_verify_rate_limited", 429)
            reset_seconds = max(1, math.ceil((exc.reset_at - timezone.now()).total_seconds()))
            resp["Retry-After"] = str(reset_seconds)
            return resp

        domain = verify_domain(domain)
        return Response({"data": DomainSerializer(domain).data})


class DomainCatchAllView(BearerAPIView):
    "Public API."

    def patch(self, request, public_id):
        from apps.accounts.entitlements import can_toggle_catch_all
        from apps.domains.services import CatchAllToggleRefused, set_catch_all

        disabled = _byod_disabled_response()
        if disabled is not None:
            return disabled
        domain = _owned_domain(request, public_id)
        forbidden = _domain_owner_required_response(request)
        if forbidden is not None:
            return forbidden

        serializer = DomainCatchAllWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        enabled = serializer.validated_data["catch_all"]
        inbox_id = serializer.validated_data.get("inbox_id")

        try:
            increment_rate_limit(
                "api:domain-catch-all:domain:hour", domain.public_id, limit=6, window_seconds=3600
            )
        except RateLimitExceeded as exc:
            resp = error_response("catch_all_rate_limited", 429)
            reset_seconds = max(1, math.ceil((exc.reset_at - timezone.now()).total_seconds()))
            resp["Retry-After"] = str(reset_seconds)
            return resp

        try:
            with transaction.atomic():
                if enabled and not can_toggle_catch_all(request.user):
                    return error_response("catch_all_entitlement_required", 403)
                domain = set_catch_all(domain, request.user, enabled=enabled, inbox_id=inbox_id)
        except CatchAllToggleRefused as exc:
            status_code = 422 if exc.code == "catch_all_target_invalid" else (
                404 if exc.code == "domain_not_found_or_unverified" else (
                    403 if exc.code == "inbox_limit_reached" else 409
                )
            )
            return error_response(exc.code, status_code)
        return Response({"data": DomainSerializer(domain).data})



# Gated end-to-end behind settings.ENABLE_OUTBOUND (design build spec: "flag
# off = whole surface 404"). _outbound_disabled_response returns a uniform
# 404 (NOT byod_disabled's 403 shape) — with the flag off there is no
# resource here at all, mirroring how an unmounted URL 404s, not a 403.
# Checked FIRST in every view below, before touching the DB.


def _outbound_disabled_response():
    from django.conf import settings as django_settings

    if not django_settings.ENABLE_OUTBOUND:
        raise NotFound()
    return None


def _outbound_forbidden_for_demo_or_agent(request):
    """Independent wall #2 (design's "two independent walls"): demo/agent
    users are refused explicitly here, in ADDITION to their tier's
    outbound_per_day/outbound_per_hour being 0 (wall #1, enforced inside
    apps.accounts.entitlements.can_send_outbound via plan_limits). Either
    wall alone already blocks them; both exist so a future plan-limits
    change can never silently reopen this path for demo/agent accounts.
    """
    from apps.accounts.team_services import is_demo_or_agent_user

    if is_demo_or_agent_user(request.user):
        return error_response("outbound_entitlement_required", 403)
    return None


def _owned_outbound_emails(request):
    from apps.outbound.models import OutboundEmail

    return OutboundEmail.objects.filter(user=request.user)


def _owned_outbound_email(request, public_id):
    from apps.outbound.models import OutboundEmail

    try:
        return _owned_outbound_emails(request).get(public_id=public_id)
    except OutboundEmail.DoesNotExist:
        raise NotFound() from None


class OutboundEmailListCreateView(BearerAPIView):
    "Public API."

    def get(self, request):
        disabled = _outbound_disabled_response()
        if disabled is not None:
            return disabled
        forbidden = _outbound_forbidden_for_demo_or_agent(request)
        if forbidden is not None:
            return forbidden
        rows, pagination = paginate(
            request,
            _owned_outbound_emails(request),
            kind="outbound_emails",
            owner_id=request.user.pk,
            active_filter="none",
            timestamp_field="created_at",
        )
        return Response({"data": OutboundEmailSerializer(rows, many=True).data, "pagination": pagination})

    @idempotent(enabled=_outbound_enabled, gate=_outbound_idem_gate)
    def post(self, request):
        from apps.outbound.services import (
            AddressFormatError,
            OutboundQuotaExceeded,
            OutboundVelocityExceeded,
            RecipientNotApproved,
            RecipientRejected,
            SendingDomainNotEligible,
            enqueue_outbound_email,
        )

        disabled = _outbound_disabled_response()
        if disabled is not None:
            return disabled
        forbidden = _outbound_forbidden_for_demo_or_agent(request)
        if forbidden is not None:
            return forbidden
        forbidden = _domain_owner_required_response(request)
        if forbidden is not None:
            return forbidden

        serializer = OutboundEmailWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            outbound_email = enqueue_outbound_email(
                user=request.user,
                created_by=request.user,
                from_address=data["from_address"],
                to=data["to"],
                cc=data.get("cc") or [],
                bcc=data.get("bcc") or [],
                reply_to=data.get("reply_to") or "",
                subject=data.get("subject") or "",
                body_text=data.get("body_text") or "",
                body_html=data.get("body_html") or "",
            )
        except AddressFormatError:
            return error_response("validation_error", 422)
        except SendingDomainNotEligible:
            return error_response("sending_domain_not_eligible", 403)
        except RecipientRejected:
            return error_response("recipient_rejected", 422)
        except RecipientNotApproved:
            return error_response("recipient_not_approved", 422)
        except OutboundQuotaExceeded as exc:
            return error_response(exc.code, 403)
        except OutboundVelocityExceeded as exc:
            resp = error_response("outbound_rate_limited", 429)
            reset_seconds = max(1, math.ceil((exc.reset_at - timezone.now()).total_seconds()))
            resp["Retry-After"] = str(reset_seconds)
            return resp
        return Response({"data": OutboundEmailSerializer(outbound_email).data}, status=201)


class OutboundEmailDetailView(BearerAPIView):
    """GET /api/v1/outbound_emails/{id} (design §6)."""

    def get(self, request, public_id):
        disabled = _outbound_disabled_response()
        if disabled is not None:
            return disabled
        forbidden = _outbound_forbidden_for_demo_or_agent(request)
        if forbidden is not None:
            return forbidden
        outbound_email = _owned_outbound_email(request, public_id)
        return Response({"data": OutboundEmailSerializer(outbound_email).data})





# key 403), walls BEFORE any row lookup, then resolve inbox/message anchored
# on the already-owned inbox row (uniform 404, no oracle), then end in the



def _w8_gate_response(request):
    "Public API."
    disabled = _outbound_disabled_response()
    if disabled is not None:
        return disabled
    forbidden = _outbound_forbidden_for_demo_or_agent(request)
    if forbidden is not None:
        return forbidden
    forbidden = _domain_owner_required_response(request)
    if forbidden is not None:
        return forbidden
    return None


def _owned_message_for_inbox(inbox, message_id):
    """message = Message.objects.get(inbox=inbox, public_id=message_id);
    DoesNotExist -> the SAME NotFound as _owned_inbox (design §1) — anchored
    on the already-owned inbox row, so cross-inbox/cross-tenant/nonexistent
    message_id are indistinguishable (uniform 404, no oracle)."""
    try:
        return Message.objects.get(inbox=inbox, public_id=message_id)
    except Message.DoesNotExist:
        raise NotFound() from None


def _map_enqueue_exception(exc):
    "Public API."
    from apps.outbound.services import (
        AddressFormatError,
        OutboundQuotaExceeded,
        OutboundVelocityExceeded,
        RecipientNotApproved,
        RecipientRejected,
        SendingDomainNotEligible,
    )

    if isinstance(exc, AddressFormatError):
        return error_response("validation_error", 422)
    if isinstance(exc, SendingDomainNotEligible):
        return error_response("sending_domain_not_eligible", 403)
    if isinstance(exc, RecipientRejected):
        return error_response("recipient_rejected", 422)
    if isinstance(exc, RecipientNotApproved):
        return error_response("recipient_not_approved", 422)
    if isinstance(exc, OutboundQuotaExceeded):
        return error_response(exc.code, 403)
    if isinstance(exc, OutboundVelocityExceeded):
        resp = error_response("outbound_rate_limited", 429)
        reset_seconds = max(1, math.ceil((exc.reset_at - timezone.now()).total_seconds()))
        resp["Retry-After"] = str(reset_seconds)
        return resp
    raise exc


class InboxMessageReplyView(BearerAPIView):
    "Public API."

    @idempotent(enabled=_outbound_enabled, gate=_outbound_idem_gate)
    def post(self, request, public_id, message_public_id):
        from apps.outbound.reply_forward import ReplyTargetInvalid
        from apps.outbound.services import reply_to_message

        gate = _w8_gate_response(request)
        if gate is not None:
            return gate

        inbox = _owned_inbox(request, public_id)
        message = _owned_message_for_inbox(inbox, message_public_id)

        serializer = InboxReplyWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            outbound_email = reply_to_message(
                request.user,
                request.user,
                inbox,
                message,
                body_text=data.get("body_text") or "",
                body_html=data.get("body_html") or "",
                reply_all=bool(data.get("reply_all")),
                cc=list(data.get("cc") or []),
            )
        except ReplyTargetInvalid:
            return error_response("reply_target_invalid", 422)
        except Exception as exc:
            return _map_enqueue_exception(exc)
        return Response({"data": OutboundEmailSerializer(outbound_email).data}, status=201)


class InboxMessageForwardView(BearerAPIView):
    "Public API."

    @idempotent(enabled=_outbound_enabled, gate=_outbound_idem_gate)
    def post(self, request, public_id, message_public_id):
        from apps.outbound.services import forward_message

        gate = _w8_gate_response(request)
        if gate is not None:
            return gate

        inbox = _owned_inbox(request, public_id)
        message = _owned_message_for_inbox(inbox, message_public_id)

        serializer = InboxForwardWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            outbound_email = forward_message(
                request.user,
                request.user,
                inbox,
                message,
                to=list(data["to"]),
                body_text=data.get("body_text") or "",
                body_html=data.get("body_html") or "",
            )
        except Exception as exc:
            return _map_enqueue_exception(exc)
        return Response({"data": OutboundEmailSerializer(outbound_email).data}, status=201)


class InboxComposeView(BearerAPIView):
    "Public API."

    @idempotent(enabled=_outbound_enabled, gate=_outbound_idem_gate)
    def post(self, request, public_id):
        from apps.outbound.services import compose_from_inbox

        gate = _w8_gate_response(request)
        if gate is not None:
            return gate

        inbox = _owned_inbox(request, public_id)

        serializer = InboxComposeWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            outbound_email = compose_from_inbox(
                request.user,
                request.user,
                inbox,
                to=list(data["to"]),
                subject=data.get("subject") or "",
                body_text=data.get("body_text") or "",
                body_html=data.get("body_html") or "",
                cc=list(data.get("cc") or []),
            )
        except Exception as exc:
            return _map_enqueue_exception(exc)
        return Response({"data": OutboundEmailSerializer(outbound_email).data}, status=201)


class DomainSendingSetupView(BearerAPIView):
    "Public API."

    @idempotent(enabled=_outbound_enabled, gate=_sending_setup_idem_gate)
    def post(self, request, public_id):
        from apps.outbound.services import SendingSetupRefused, provision_sending_setup

        disabled = _outbound_disabled_response()
        if disabled is not None:
            return disabled
        forbidden = _outbound_forbidden_for_demo_or_agent(request)
        if forbidden is not None:
            return forbidden
        domain = _owned_domain(request, public_id)
        forbidden = _domain_owner_required_response(request)
        if forbidden is not None:
            return forbidden

        try:
            increment_rate_limit(
                "api:domain-sending-setup:domain:hour", domain.public_id, limit=6, window_seconds=3600
            )
        except RateLimitExceeded as exc:
            resp = error_response("domain_verify_rate_limited", 429)
            reset_seconds = max(1, math.ceil((exc.reset_at - timezone.now()).total_seconds()))
            resp["Retry-After"] = str(reset_seconds)
            return resp

        try:
            domain = provision_sending_setup(domain)
        except SendingSetupRefused as exc:
            return error_response(exc.code, 409)
        return Response({"data": DomainSerializer(domain).data})




# _outbound_disabled_response (flag off -> uniform 404) ->
# _outbound_forbidden_for_demo_or_agent -> _domain_owner_required_response

# (else 403 outbound_allowlist_entitlement_required). All four endpoints

# _domain_owner_required_response gate. Enforcement of the allowlist
# itself (which addresses a send may reach) is separate from this CRUD

# apps.outbound.services.enqueue_outbound_email gate #7.5 and
# apps.outbound.engine.deliver_claim.


def _owned_approved_recipients(request):
    from apps.outbound.models import ApprovedRecipient

    return ApprovedRecipient.objects.filter(user=request.user)


def _owned_approved_recipient(request, public_id):
    """Clones _owned_domain (views.py:95-101): wrong-tenant and nonexistent
    are byte-identical 404 — no cross-tenant oracle on DELETE."""
    from apps.outbound.models import ApprovedRecipient

    try:
        return _owned_approved_recipients(request).get(public_id=public_id)
    except ApprovedRecipient.DoesNotExist:
        raise NotFound() from None


def _outbound_allowlist_tier_forbidden_response(request):
    from apps.accounts.entitlements import can_configure_outbound_allowlist

    if not can_configure_outbound_allowlist(request.user):
        return error_response("outbound_allowlist_entitlement_required", 403)
    return None


class OutboundAllowlistListCreateView(BearerAPIView):
    """GET/POST /api/v1/outbound_allowlist (design §2)."""

    def get(self, request):
        disabled = _outbound_disabled_response()
        if disabled is not None:
            return disabled
        forbidden = _outbound_forbidden_for_demo_or_agent(request)
        if forbidden is not None:
            return forbidden
        forbidden = _domain_owner_required_response(request)
        if forbidden is not None:
            return forbidden
        with transaction.atomic():
            forbidden = _outbound_allowlist_tier_forbidden_response(request)
        if forbidden is not None:
            return forbidden
        rows, pagination = paginate(
            request,
            _owned_approved_recipients(request),
            kind="outbound_allowlist",
            owner_id=request.user.pk,
            active_filter="none",
            timestamp_field="created_at",
        )
        return Response({"data": ApprovedRecipientSerializer(rows, many=True).data, "pagination": pagination})

    @idempotent(enabled=_outbound_enabled, gate=_outbound_idem_gate)
    def post(self, request):
        from django.conf import settings as django_settings

        from apps.accounts.services import normalize_email
        from apps.outbound.models import ApprovedRecipient
        disabled = _outbound_disabled_response()
        if disabled is not None:
            return disabled
        forbidden = _outbound_forbidden_for_demo_or_agent(request)
        if forbidden is not None:
            return forbidden
        forbidden = _domain_owner_required_response(request)
        if forbidden is not None:
            return forbidden
        with transaction.atomic():
            forbidden = _outbound_allowlist_tier_forbidden_response(request)
        if forbidden is not None:
            return forbidden

        serializer = ApprovedRecipientWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            normalized = normalize_email(serializer.validated_data["email"])
        except Exception:
            return error_response("validation_error", 422, fields={"email": ["Enter a valid email address."]})

        try:
            increment_rate_limit(
                "api:outbound-allowlist-create:account:hour",
                str(request.user.pk),
                limit=60,
                window_seconds=3600,
            )
        except RateLimitExceeded as exc:
            resp = error_response("outbound_allowlist_rate_limited", 429)
            reset_seconds = max(1, math.ceil((exc.reset_at - timezone.now()).total_seconds()))
            resp["Retry-After"] = str(reset_seconds)
            return resp

        current_count = _owned_approved_recipients(request).count()
        if current_count >= django_settings.OUTBOUND_ALLOWLIST_MAX_ENTRIES:
            return error_response("outbound_allowlist_limit_reached", 422)

        try:
            with transaction.atomic():
                row = ApprovedRecipient.objects.create(
                    user=request.user,
                    email_normalized=normalized,
                    created_by=request.user,
                )
        except IntegrityError:
            # Per-(user,email) uniqueness — a conflict reveals only the
            # caller's OWN row, no cross-tenant oracle (design §2/§6).
            return error_response("approved_recipient_exists", 409)

        logger.info(
            "outbound_allowlist_mutation action=create user_id=%s apr_id=%s",
            request.user.pk, row.public_id,
        )
        response = Response({"data": ApprovedRecipientSerializer(row).data}, status=201)
        response["Location"] = f"/api/v1/outbound_allowlist/{row.public_id}"
        return response


class OutboundAllowlistDetailView(BearerAPIView):
    """DELETE /api/v1/outbound_allowlist/{public_id} (design §2)."""

    def delete(self, request, public_id):
        # design §2 prelude order matches the other 3 endpoints: flag-off ->
        
        
        # let a member key distinguish an existing owned id [404-vs-403]
        
        disabled = _outbound_disabled_response()
        if disabled is not None:
            return disabled
        forbidden = _outbound_forbidden_for_demo_or_agent(request)
        if forbidden is not None:
            return forbidden
        forbidden = _domain_owner_required_response(request)
        if forbidden is not None:
            return forbidden
        with transaction.atomic():
            forbidden = _outbound_allowlist_tier_forbidden_response(request)
        if forbidden is not None:
            return forbidden
        row = _owned_approved_recipient(request, public_id)

        apr_id = row.public_id
        row.delete()
        logger.info(
            "outbound_allowlist_mutation action=delete user_id=%s apr_id=%s",
            request.user.pk, apr_id,
        )
        return Response(status=204)


class OutboundAllowlistModeView(BearerAPIView):
    """POST /api/v1/outbound_allowlist/mode (design §2): flips
    Account.outbound_allowlist_enabled. Mirrors DomainCatchAllView shape."""

    def post(self, request):
        from apps.accounts.models import Account

        disabled = _outbound_disabled_response()
        if disabled is not None:
            return disabled
        forbidden = _outbound_forbidden_for_demo_or_agent(request)
        if forbidden is not None:
            return forbidden
        forbidden = _domain_owner_required_response(request)
        if forbidden is not None:
            return forbidden
        with transaction.atomic():
            forbidden = _outbound_allowlist_tier_forbidden_response(request)
        if forbidden is not None:
            return forbidden

        serializer = OutboundAllowlistModeWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        enabled = serializer.validated_data["enabled"]

        Account.objects.filter(pk=request.user.pk).update(outbound_allowlist_enabled=enabled)
        logger.info(
            "outbound_allowlist_mode_change user_id=%s enabled=%s",
            request.user.pk, enabled,
        )
        return Response({"data": {"enabled": enabled}})
