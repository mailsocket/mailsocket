from django.urls import path

from apps.api.views import (
    AgentRegisterView,
    DomainCatchAllView,
    DomainDetailView,
    DomainListCreateView,
    DomainSendingSetupView,
    DomainVerifyView,
    InboxComposeView,
    InboxDetailView,
    InboxListCreateView,
    InboxMessageForwardView,
    InboxMessageReplyView,
    InboxMessagesView,
    InboxTestCodeView,
    LatestInboxMessageView,
    MessageBatchView,
    MessageDetailView,
    OutboundAllowlistDetailView,
    OutboundAllowlistListCreateView,
    OutboundAllowlistModeView,
    OutboundEmailDetailView,
    OutboundEmailListCreateView,
    PublicInboxMessagesView,
    StreamMessagesView,
    VerifyBatchView,
    VerifyEmailView,
    WaitForMessageView,
    WebhookTestView,
)

app_name = "api"
urlpatterns = [
    path("agents/register", AgentRegisterView.as_view(), name="agent-register"),
    # "verify/batch" is registered right under "verify" (both exact-match
    # path() routes with no <capture> sibling, so no greedy-order hazard) —
    # kept for ordering discipline, mirroring the "messages/batch" precedent
    # at api_urls.py:46.
    path("verify", VerifyEmailView.as_view(), name="verify-email"),
    path("verify/batch", VerifyBatchView.as_view(), name="verify-batch"),
    path("inboxes", InboxListCreateView.as_view(), name="inboxes"),
    path("inboxes/<str:public_id>", InboxDetailView.as_view(), name="inbox-detail"),
    path("inboxes/<str:public_id>/messages", InboxMessagesView.as_view(), name="inbox-messages"),
    path("inboxes/<str:public_id>/messages/latest", LatestInboxMessageView.as_view(), name="inbox-message-latest"),
    path("inboxes/<str:public_id>/messages/wait", WaitForMessageView.as_view(), name="inbox-message-wait"),
    path("inboxes/<str:public_id>/stream", StreamMessagesView.as_view(), name="inbox-stream"),
    path("inboxes/<str:public_id>/webhook/test", WebhookTestView.as_view(), name="inbox-webhook-test"),
    path("inboxes/<str:public_id>/test-code", InboxTestCodeView.as_view(), name="inbox-test-code"),
    
    # "messages/batch"/"messages/<public_id>" is irrelevant here (disjoint
    # "public/" prefix), but kept near the other inbox-scoped routes.
    path("public/emails/<str:email>/messages", PublicInboxMessagesView.as_view(), name="public-inbox-messages"),
    # "messages/batch" MUST be registered BEFORE "messages/<public_id>" — Django
    # resolves urlpatterns in order, so a swapped order would make "batch" match
    # the <str:public_id> capture group and the batch endpoint would never run.
    path("messages/batch", MessageBatchView.as_view(), name="message-batch"),
    path("messages/<str:public_id>", MessageDetailView.as_view(), name="message-detail"),
    #  (BYOD) — gated by settings.ENABLE_BYOD + can_add_domain.
    path("domains", DomainListCreateView.as_view(), name="domains"),
    path("domains/<str:public_id>", DomainDetailView.as_view(), name="domain-detail"),
    path("domains/<str:public_id>/verify", DomainVerifyView.as_view(), name="domain-verify"),
    path("domains/<str:public_id>/catch_all", DomainCatchAllView.as_view(), name="domain-catch-all"),
    
    # (flag off -> uniform 404, see apps.api.views._outbound_disabled_response).
    path("domains/<str:public_id>/sending_setup", DomainSendingSetupView.as_view(), name="domain-sending-setup"),
    path("outbound_emails", OutboundEmailListCreateView.as_view(), name="outbound-emails"),
    path("outbound_emails/<str:public_id>", OutboundEmailDetailView.as_view(), name="outbound-email-detail"),
    
    # registered BEFORE "outbound_allowlist/<public_id>" (same ordering
    # discipline as "messages/batch" above): Django resolves urlpatterns in
    # order, so a swapped order would make "mode" match the <public_id>
    # capture group and OutboundAllowlistModeView would never run.
    path("outbound_allowlist", OutboundAllowlistListCreateView.as_view(), name="outbound-allowlist"),
    path("outbound_allowlist/mode", OutboundAllowlistModeView.as_view(), name="outbound-allowlist-mode"),
    path(
        "outbound_allowlist/<str:public_id>",
        OutboundAllowlistDetailView.as_view(),
        name="outbound-allowlist-detail",
    ),
    
    # same settings.ENABLE_OUTBOUND envelope (see apps.api.views._w8_gate_
    # response). "compose" is inbox-scoped like "messages"/"stream" above;
    # "reply"/"forward" are message-scoped, mirroring the message/inbox
    # anchoring apps.api.views._owned_message_for_inbox performs.
    path(
        "inboxes/<str:public_id>/messages/<str:message_public_id>/reply",
        InboxMessageReplyView.as_view(),
        name="inbox-message-reply",
    ),
    path(
        "inboxes/<str:public_id>/messages/<str:message_public_id>/forward",
        InboxMessageForwardView.as_view(),
        name="inbox-message-forward",
    ),
    path("inboxes/<str:public_id>/compose", InboxComposeView.as_view(), name="inbox-compose"),
]
