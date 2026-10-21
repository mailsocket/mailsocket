import json
import re

from rest_framework import serializers

from apps.inboxes.services import RoutingKeyInvalid, normalize_routing_key
from apps.ingest.parser import extract_otp_candidates, html_to_text, mask_non_otp_spans

# NOTE: import cycle serializers → parser → webhooks.services → serializers is
# INTENTIONAL and broken on purpose: parser.py imports webhooks.services LAZILY
# (inside a function, parser.py:~562), so this top-level import is safe. Do not
# "fix" it by moving imports — the lazy import decouples webhook enqueue from the
# mail-store transaction. See parser.py enqueue_message_delivery call site.

OTP_CONTEXT_MAX_CHARS = 160


# {"run": "ci-123"}), not a document store — hard-capped so a pathological
# payload can never bloat storage or (via the webhook payload, which embeds
# it) delivery bodies.
METADATA_MAX_BYTES = 4096
METADATA_MAX_KEYS = 50

# Best-effort, static sender-domain -> provider label map. No network lookup;
# a convenience hint an agent can lean on but must never treat as authoritative.
PROVIDER_HINT_MAP = {
    "github.com": "github",
    "google.com": "google",
    "gmail.com": "google",
    "stripe.com": "stripe",
    "slack.com": "slack",
    "notion.so": "notion",
    "vercel.com": "vercel",
    "microsoft.com": "microsoft",
    "apple.com": "apple",
    "amazon.com": "amazon",
    "facebook.com": "facebook",
    "twitter.com": "twitter",
    "x.com": "x",
    "linkedin.com": "linkedin",
    "dropbox.com": "dropbox",
    "atlassian.net": "atlassian",
    "discord.com": "discord",
    "cloudflare.com": "cloudflare",
    "auth0.com": "auth0",
    "okta.com": "okta",
}

_HTML_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")

_SENDER_RE = re.compile(r"[\w.+-]+@([\w.-]+)", re.IGNORECASE)


def _strip_html(value: str) -> str:
    return _WS_RE.sub(" ", _HTML_TAG_RE.sub(" ", value)).strip()


def _provider_hint(header_from: str) -> str | None:
    if not header_from:
        return None
    match = _SENDER_RE.search(header_from)
    if match is None:
        return None
    domain = match.group(1).lower()
    return PROVIDER_HINT_MAP.get(domain)


def _otp_context(message) -> str | None:
    otp = message.otp or message.detected_otp
    if not otp:
        return None
    # Locate the code on a URL/e-mail/token-masked copy (same length, so the
    # offset maps 1:1) so the snippet never anchors on digits inside a link —
    # the same masking the extractor applies (qafix2 #1).
    source = _strip_html(message.text_body) if message.text_body else ""
    start = mask_non_otp_spans(source).find(otp)
    if start < 0 and message.subject:
        source = _strip_html(message.subject)
        start = mask_non_otp_spans(source).find(otp)
    if start < 0 and message.html_body:
        source = html_to_text(message.html_body)
        source = _WS_RE.sub(" ", source).strip()
        start = mask_non_otp_spans(source).find(otp)
    if start < 0:
        return None
    # A short bounded window centred on the OTP, expanding outward to the next
    # sentence boundary so the snippet reads naturally without leaking the body.
    begin = max(0, start - 40)
    end = min(len(source), start + len(otp) + 40)
    if begin > 0:
        space = source.find(" ", begin)
        if space != -1 and space < start:
            begin = space + 1
    if end < len(source):
        space = source.rfind(" ", begin, end + 1)
        if space != -1 and space > start + len(otp):
            end = space
    snippet = source[begin:end].strip()
    if len(snippet) > OTP_CONTEXT_MAX_CHARS:
        snippet = snippet[:OTP_CONTEXT_MAX_CHARS].rstrip()
    return snippet or None


class InboxWriteSerializer(serializers.Serializer):
    # The API field is named "label", which shadows Field.label (str) in the DRF stubs; the runtime
    # metaclass moves declared fields to _declared_fields, so nothing is shadowed.
    label = serializers.CharField(  # type: ignore[assignment]  # DRF field named like Field.label
        max_length=120, required=False, allow_blank=True, trim_whitespace=True
    )
    # Optional custom slug (the local-part of the inbox address). When omitted a
    # random key is minted. Must match the same pattern the model enforces:
    # lowercase alnum start, then [a-z0-9_-], length 3-64.
    #
    
    # CharField (not RegexField) is used deliberately: RegexField's validators
    # run inside Field.run_validation(), which happens BEFORE the
    # serializer-level validate_routing_key() hook ever runs — so a RegexField
    # with a lowercase-only pattern would 422 on any uppercase character
    # before .lower() got a chance to run, contradicting this exact comment.
    # Lowering first, then regex-checking inside validate_routing_key, makes
    # "callers may pass either case" actually true.
    routing_key = serializers.CharField(
        required=False,
        allow_blank=False,
        max_length=64,
    )
    #  (BYOD): optional public_id of a verified custom Domain
    # owned by the SAME caller to bind this inbox to. Omitted (or blank) ->
    # shared in.inboxpipe.net inbox, exactly as before Stage B. Ownership +
    # verified-status are enforced in the view (InboxListCreateView.post),
    # not here — that check needs a DB lookup scoped to request.user, which
    # a Serializer.validate_* hook doesn't have clean access to run under the
    # same transaction/lock as the create.
    domain = serializers.CharField(
        required=False, allow_blank=True, max_length=32, default=""
    )
    
    # — see Inbox.is_public model comment). Cross-checked against `domain` in
    # the view (InboxListCreateView.post), not here, because that check needs
    # to know whether a domain public_id was actually supplied vs blank.
    public = serializers.BooleanField(required=False, default=False)
    
    
    metadata = serializers.JSONField(required=False)

    def validate_routing_key(self, value):
        # Delegates to apps.inboxes.services.normalize_routing_key — the ONE
        # place the pattern + reserved blocklist live, shared with the
        # dashboard's InboxCreateForm so both surfaces enforce identical rules.
        # : is_shared is now derived from whether `domain` was
        
        
        # their whole namespace. Field order means `domain` is already in
        # initial_data by the time DRF validates routing_key.
        is_shared = not (self.initial_data.get("domain") or "").strip()
        try:
            return normalize_routing_key(value, is_shared=is_shared)
        except RoutingKeyInvalid as exc:
            raise serializers.ValidationError(exc.message, code=exc.code) from exc

    def validate_metadata(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError(
                "metadata must be a JSON object.", code="metadata_must_be_object"
            )
        compact = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        if len(compact.encode("utf-8")) > METADATA_MAX_BYTES:
            raise serializers.ValidationError(
                f"metadata must be at most {METADATA_MAX_BYTES} bytes (compact UTF-8 JSON).",
                code="metadata_too_large",
            )
        if len(value) > METADATA_MAX_KEYS:
            raise serializers.ValidationError(
                f"metadata must have at most {METADATA_MAX_KEYS} top-level keys.",
                code="metadata_too_large",
            )
        return value


class InboxSerializer(serializers.Serializer):
    id = serializers.CharField(source="public_id")
    label = serializers.CharField()  # type: ignore[assignment]  # DRF field named like Field.label (see above)
    address = serializers.CharField()
    is_enabled = serializers.BooleanField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    metadata = serializers.JSONField()


class InboxDetailSerializer(InboxSerializer):
    message_count_month = serializers.IntegerField()
    webhook_configured = serializers.BooleanField()


class MessageSummarySerializer(serializers.Serializer):
    id = serializers.CharField(source="public_id")
    inbox_id = serializers.CharField(source="inbox.public_id")
    from_ = serializers.CharField(source="header_from", label="from")
    to = serializers.ListField(source="header_to")
    cc = serializers.ListField(source="header_cc")
    subject = serializers.CharField()
    received_at = serializers.DateTimeField()
    status = serializers.CharField(source="ingest_status")
    otp = serializers.CharField(allow_null=True)
    otp_confidence = serializers.FloatField(allow_null=True)
    magic_link = serializers.CharField(allow_null=True)
    links = serializers.ListField(child=serializers.CharField(), allow_null=True)
    detected_otp = serializers.CharField(allow_null=True)
    has_html = serializers.SerializerMethodField()
    otp_context = serializers.SerializerMethodField()
    otp_candidates = serializers.SerializerMethodField()
    provider_hint = serializers.SerializerMethodField()
    read = serializers.SerializerMethodField()
    
    # select_related("inbox")s (views.py InboxMessagesView/LatestInboxMessageView/
    # MessageDetailView/_owned_message/_wait), so this is zero extra queries.
    # Because MessageSerializer subclasses this and the webhook payload IS
    # MessageSerializer output (webhooks/services.py build_event_payload), this
    # one addition propagates to poll/latest/wait/batch/webhook automatically.
    
    metadata = serializers.SerializerMethodField()

    def get_read(self, obj):
        return obj.read_at is not None

    def get_has_html(self, obj):
        return bool(obj.html_body)

    def get_otp_context(self, obj):
        return _otp_context(obj)

    def get_otp_candidates(self, obj):
        otp = obj.otp or obj.detected_otp
        if not otp:
            return None
        candidates = extract_otp_candidates(obj.subject or "", obj.text_body or "", obj.html_body or "")
        # The primary otp is the top pick; expose only the also-rans (drop the
        # leading candidate), so a single-code message yields null/empty.
        also_rans = [c for c in candidates if c["code"] != otp]
        return also_rans or None

    def get_provider_hint(self, obj):
        return _provider_hint(obj.header_from)

    def get_metadata(self, obj):
        return obj.inbox.metadata

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["from"] = data.pop("from_")
        return data


class PublicMessageSerializer(MessageSummarySerializer):
    "Public API."

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data.pop("otp_candidates", None)
        data.pop("metadata", None)
        data.pop("otp_context", None)
        data.pop("links", None)
        return data


class MessageSerializer(MessageSummarySerializer):
    envelope_from = serializers.CharField()
    envelope_recipient = serializers.CharField()
    text = serializers.CharField(source="text_body")
    html = serializers.CharField(source="html_body")

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data.pop("has_html")
        return data


class MessageReadWriteSerializer(serializers.Serializer):
    """PATCH /api/messages/<public_id> body: {"read": bool}."""

    read = serializers.BooleanField(required=True)

    def to_internal_value(self, data):
        # QA #4 + gate R2: `read` is the ONLY accepted key. Any other key is a
        # 422 that names it — whether or not `read` is also present — so a
        # typo like {"read": true, "is_read": false} can never be silently
        # half-applied. Each unknown key gets its own fields.<key> entry; a
        # missing `read` additionally keeps the fields.read hint naming them.
        if isinstance(data, dict):
            unknown = sorted(str(key) for key in data if key != "read")
            if unknown:
                errors = {
                    key: ['Unknown field. The only accepted field is "read".']
                    for key in unknown
                }
                if "read" not in data:
                    errors["read"] = [
                        "This field is required. Unknown field(s): "
                        + ", ".join(unknown)
                        + ' — send {"read": true} or {"read": false}.'
                    ]
                raise serializers.ValidationError(errors)
        return super().to_internal_value(data)


class MessageBatchSerializer(serializers.Serializer):
    """POST /api/messages/batch body: {action, ids: [...]}.
    Duplicate ids are de-duplicated (order-preserving) to avoid redundant
    per-id work/results for a repeated id.
    """

    action = serializers.ChoiceField(choices=["mark_read", "delete", "fetch"], required=True)
    ids = serializers.ListField(
        child=serializers.CharField(max_length=32),
        allow_empty=False,
        max_length=100,
        required=True,
    )

    def validate_ids(self, value):
        seen = set()
        deduped = []
        for item in value:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped


# ---  (BYOD): domain CRUD -------------------------------------


class DomainWriteSerializer(serializers.Serializer):
    """POST /api/v1/domains body: {name}."""

    name = serializers.CharField(max_length=253, allow_blank=False, trim_whitespace=True)


class DomainCatchAllWriteSerializer(serializers.Serializer):
    "Public API."

    catch_all = serializers.BooleanField()
    inbox_id = serializers.CharField(
        max_length=64, allow_blank=False, required=False, allow_null=True
    )


class DomainSerializer(serializers.Serializer):
    id = serializers.CharField(source="public_id")
    name = serializers.CharField()
    status = serializers.CharField()
    verification_txt_host = serializers.CharField()
    verification_token = serializers.CharField()
    mx_target = serializers.SerializerMethodField()
    last_checked_at = serializers.DateTimeField(allow_null=True)
    last_error = serializers.CharField()
    verified_at = serializers.DateTimeField(allow_null=True)
    
    # state. catch_all_inbox_id is the sentinel's public_id (or null) — never
    # exposes the numeric PK.
    catch_all = serializers.BooleanField()
    catch_all_inbox_id = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    
    dkim_status = serializers.CharField()
    dkim_selector = serializers.CharField()
    dkim_public_key_txt = serializers.CharField()

    def get_mx_target(self, obj):
        from apps.domains.services import MX_TARGET_HOST

        return MX_TARGET_HOST

    def get_catch_all_inbox_id(self, obj):
        return obj.catch_all_inbox.public_id if obj.catch_all_inbox_id else None





class OutboundEmailWriteSerializer(serializers.Serializer):
    """POST /api/v1/outbound_emails body.

    Shape/size/recipient-cap validation ONLY here — the sending-domain
    invariant, suppression, and quota gates are NOT serializer-level (they
    need DB lookups + a transaction, and the recipient cap here is checked
    against settings.OUTBOUND_MAX_RECIPIENTS, the V1 floor, deliberately
    BEFORE apps.outbound.services.enqueue_outbound_email re-validates
    everything from scratch server-side — this is defense-in-depth, not the
    sole guard).
    """

    from_address = serializers.CharField(max_length=320, allow_blank=False)
    to = serializers.ListField(
        child=serializers.CharField(max_length=320), allow_empty=False, max_length=10
    )
    cc = serializers.ListField(
        child=serializers.CharField(max_length=320), required=False, default=list, max_length=10
    )
    bcc = serializers.ListField(
        child=serializers.CharField(max_length=320), required=False, default=list, max_length=10
    )
    reply_to = serializers.CharField(max_length=320, required=False, allow_blank=True, default="")
    subject = serializers.CharField(max_length=512, required=False, allow_blank=True, default="")
    body_text = serializers.CharField(required=False, allow_blank=True, default="")
    body_html = serializers.CharField(required=False, allow_blank=True, default="")


class OutboundEmailSerializer(serializers.Serializer):
    id = serializers.CharField(source="public_id")
    status = serializers.CharField()
    from_address = serializers.CharField()
    to = serializers.ListField(child=serializers.CharField())
    cc = serializers.ListField(child=serializers.CharField())
    bcc = serializers.ListField(child=serializers.CharField())
    subject = serializers.CharField()
    recipient_count = serializers.IntegerField()
    error_code = serializers.CharField()
    created_at = serializers.DateTimeField()
    queued_at = serializers.DateTimeField(allow_null=True)
    sent_at = serializers.DateTimeField(allow_null=True)
    bounced_at = serializers.DateTimeField(allow_null=True)





class InboxReplyWriteSerializer(serializers.Serializer):
    """POST /api/v1/inboxes/{inbox_id}/messages/{message_id}/reply body.
    Shape-only — the reply target, threading, and every recipient are
    re-derived/re-validated server-side (design §3/§4)."""

    body_text = serializers.CharField(required=False, allow_blank=True, default="")
    body_html = serializers.CharField(required=False, allow_blank=True, default="")
    cc = serializers.ListField(
        child=serializers.CharField(max_length=320), required=False, default=list, max_length=10
    )
    reply_all = serializers.BooleanField(required=False, default=False)


class InboxForwardWriteSerializer(serializers.Serializer):
    """POST /api/v1/inboxes/{inbox_id}/messages/{message_id}/forward body."""

    to = serializers.ListField(
        child=serializers.CharField(max_length=320), allow_empty=False, max_length=10
    )
    body_text = serializers.CharField(required=False, allow_blank=True, default="")
    body_html = serializers.CharField(required=False, allow_blank=True, default="")


class InboxComposeWriteSerializer(serializers.Serializer):
    """POST /api/v1/inboxes/{inbox_id}/compose body — pure passthrough
    (design §4), no copied-through inbound data at all."""

    to = serializers.ListField(
        child=serializers.CharField(max_length=320), allow_empty=False, max_length=10
    )
    subject = serializers.CharField(max_length=512, required=False, allow_blank=True, default="")
    body_text = serializers.CharField(required=False, allow_blank=True, default="")
    body_html = serializers.CharField(required=False, allow_blank=True, default="")
    cc = serializers.ListField(
        child=serializers.CharField(max_length=320), required=False, default=list, max_length=10
    )





class ApprovedRecipientWriteSerializer(serializers.Serializer):
    """POST /api/v1/outbound_allowlist body: {email}."""

    email = serializers.CharField(max_length=320, allow_blank=False)


class ApprovedRecipientSerializer(serializers.Serializer):
    id = serializers.CharField(source="public_id")
    email = serializers.CharField(source="email_normalized")
    created_at = serializers.DateTimeField()


class OutboundAllowlistModeWriteSerializer(serializers.Serializer):
    """POST /api/v1/outbound_allowlist/mode body: {enabled}."""

    enabled = serializers.BooleanField()
