from django.urls import path

from apps.billing import views as billing_views

from . import account_views, compare, demo, domain_views, instant, settings_views, team_views, views
from .decorators import private_per_cookie
from .redirects import slash_redirect

app_name = "dashboard"

urlpatterns = [
    path("", views.root, name="root"),
    path("legal/aup", views.legal, {"document": "aup"}, name="legal-aup"),
    path("legal/privacy", views.legal, {"document": "privacy"}, name="legal-privacy"),
    path("legal/terms", views.legal, {"document": "terms"}, name="legal-terms"),
    # The canonical legal URLs have no trailing slash; the slashed forms used
    # to 404 (APPEND_SLASH only adds slashes). 301 them to the canonical URL.
    path("legal/aup/", slash_redirect("dashboard:legal-aup")),
    path("legal/privacy/", slash_redirect("dashboard:legal-privacy")),
    path("legal/terms/", slash_redirect("dashboard:legal-terms")),
    path("docs/", views.docs, name="docs"),
    path("docs/api/", views.api_reference, name="api-reference"),
    path("docs/errors/", views.error_reference, name="error-reference"),
    path("docs/openapi.yaml", views.openapi_yaml, name="openapi-yaml"),
    path("llms.txt", views.llms_txt, name="llms-txt"),
    path("docs/guides/", views.guides, name="guides"),
    path("docs/guides/<slug:slug>/", views.guide, name="guide"),
    path("docs/versioning", views.versioning, name="versioning"),
    path("security", views.security, name="security"),
    # ux3 N9: same slash -> no-slash 301 as legal/* above, for every other
    # public no-slash page.
    path("docs/versioning/", slash_redirect("dashboard:versioning")),
    path("security/", slash_redirect("dashboard:security")),
    path(".well-known/security.txt", views.security_txt, name="security-txt"),
    path("sitemap.xml", views.sitemap_xml, name="sitemap-xml"),
    path("robots.txt", views.robots_txt, name="robots-txt"),
    path("docs/faq", views.faq, name="faq"),
    path("docs/changelog", views.changelog, name="changelog"),
    path("docs/faq/", slash_redirect("dashboard:faq")),
    path("docs/changelog/", slash_redirect("dashboard:changelog")),
    path("docs/compare/", compare.compare_index, name="compare"),
    path("docs/compare/mailpit", compare.compare_page, {"slug": "mailpit"}, name="compare-mailpit"),
    path("docs/compare/mailosaur", compare.compare_page, {"slug": "mailosaur"}, name="compare-mailosaur"),
    path("docs/compare/agentmail", compare.compare_page, {"slug": "agentmail"}, name="compare-agentmail"),
    path("docs/compare/mailtrap", compare.compare_page, {"slug": "mailtrap"}, name="compare-mailtrap"),
    path("docs/compare/mailslurp", compare.compare_page, {"slug": "mailslurp"}, name="compare-mailslurp"),
    # ux3 N9: slashed compare URLs 301 to the canonical no-slash ones.
    path("docs/compare/mailpit/", slash_redirect("dashboard:compare-mailpit")),
    path("docs/compare/mailosaur/", slash_redirect("dashboard:compare-mailosaur")),
    path("docs/compare/agentmail/", slash_redirect("dashboard:compare-agentmail")),
    path("docs/compare/mailtrap/", slash_redirect("dashboard:compare-mailtrap")),
    path("docs/compare/mailslurp/", slash_redirect("dashboard:compare-mailslurp")),
    path("docs/try/", views.try_it, name="try-it"),
    path("docs/try/inbox", demo.try_create_inbox, name="try-inbox-create"),
    path("docs/try/inbox/<str:public_id>/wait", demo.try_wait, name="try-inbox-wait"),
    path("docs/try/inbox/<str:public_id>/sample", demo.try_send_sample, name="try-inbox-sample"),
    
    path("instant/email.md", instant.instant_email, name="instant-email"),
    path("instant/email/<str:email>/messages.md", instant.instant_messages, name="instant-messages"),
    # Billing kill switch: signed-in CTAs differ, so no shared cache (private, no-cache, Vary: Cookie).
    path("pricing/", private_per_cookie(views.pricing), name="pricing"),
    path("app/", views.overview, name="overview"),
    path("app/fleet/", views.fleet, name="fleet"),
    path("app/partials/recent-messages/", views.recent_messages, name="recent-messages"),
    path("app/inboxes/", views.inboxes, name="inboxes"),
    path("app/usage/", views.usage, name="usage"),
    path("app/inboxes/create/", views.inbox_create, name="inbox-create"),
    path("app/inboxes/<str:public_id>/rename/", views.inbox_rename, name="inbox-rename"),
    path("app/inboxes/<str:public_id>/enable/", views.inbox_enable, name="inbox-enable"),
    path("app/inboxes/<str:public_id>/disable/", views.inbox_disable, name="inbox-disable"),
    path("app/inboxes/<str:public_id>/delete/", views.inbox_delete, name="inbox-delete"),
    path("app/inboxes/<str:public_id>/test-code/", views.inbox_send_test_code, name="inbox-test-code"),
    path("app/messages/", views.messages, name="messages"),
    path("app/messages/<str:public_id>/", views.message_detail, name="message-detail"),
    path("app/messages/<str:public_id>/panel/", views.message_panel, name="message-panel"),
    path("app/messages/<str:public_id>/html/", views.message_html, name="message-html"),
    path("app/messages/<str:public_id>/read/", views.messages_mark_read, name="message-mark-read"),
    path("app/messages/<str:public_id>/unread/", views.messages_mark_unread, name="message-mark-unread"),
    path("app/messages/<str:public_id>/reply/", views.message_reply, name="message-reply"),
    path("app/messages/<str:public_id>/forward/", views.message_forward, name="message-forward"),
    path("app/compose/", views.compose, name="compose"),
    path("app/api-keys/", views.api_keys, name="api-keys"),
    path("app/api-keys/create/", views.api_key_create, name="api-key-create"),
    path("app/api-keys/onboarding/", views.onboarding_api_key_create, name="api-key-onboarding"),
    path("app/api-keys/<str:public_id>/revoke/", views.api_key_revoke, name="api-key-revoke"),
    path("app/webhooks/", views.webhooks, name="webhooks"),
    path("app/inboxes/<str:public_id>/webhook/save/", views.webhook_save, name="webhook-save"),
    path("app/inboxes/<str:public_id>/webhook/disable/", views.webhook_disable, name="webhook-disable"),
    path("app/inboxes/<str:public_id>/webhook/rotate-secret/", views.webhook_rotate, name="webhook-rotate"),
    path("app/inboxes/<str:public_id>/webhook/test/", views.webhook_test, name="webhook-test"),
    path("app/inboxes/<str:public_id>/webhook/delete/", views.webhook_delete, name="webhook-delete"),
    path("app/inboxes/<str:public_id>/webhook/deliveries/", views.webhook_deliveries, name="webhook-deliveries"),
    #  (Team Seats). URLs are always REGISTERED (mirrors the
    # ENABLE_BYOD API pattern: routes exist so reverse() never breaks), but
    # every view is wrapped in team_views._teams_enabled, which raises
    # Http404 when settings.ENABLE_TEAMS is False — so a request to any of
    # these paths with the flag off is indistinguishable from the route not
    # existing (404), which is what the Stage A "flag off = absent" guarantee
    # requires, while staying trivially testable with plain
    # @override_settings(ENABLE_TEAMS=...) (no urlconf reload needed).
    path("app/team/", team_views.team, name="team"),
    path("app/team/invite/", team_views.team_invite_create, name="team-invite-create"),
    path("app/team/invites/<int:invite_id>/revoke/", team_views.team_invite_revoke, name="team-invite-revoke"),
    path("app/team/members/<int:membership_id>/revoke/", team_views.team_member_revoke, name="team-member-revoke"),
    path("app/team/leave/", team_views.team_leave, name="team-leave"),
    path("team/accept/<str:token>", team_views.team_accept, name="team-accept"),
    path("team/accept/<str:token>/confirm", team_views.team_accept_confirm, name="team-accept-confirm"),
    
    # ENABLE_TEAMS pattern: routes exist so reverse() never breaks), but each
    # view is wrapped in account_views._multi_account_enabled, which raises
    # Http404 when settings.ENABLE_MULTI_ACCOUNT is False — a request to
    # either path with the flag off is indistinguishable from the route not
    # existing (404).
    path("app/account/switch/", account_views.account_switch, name="account-switch"),
    path("app/account/new/", account_views.account_create, name="account-new"),
    # Lemon Squeezy billing (apps.billing). URLs are always REGISTERED
    # (mirrors the ENABLE_TEAMS/ENABLE_MULTI_ACCOUNT pattern), but every view
    # is wrapped in apps.billing.views._billing_enabled, which raises Http404
    # when settings.ENABLE_BILLING is False.
    path("app/billing/", billing_views.billing_page, name="billing-page"),
    path("app/billing/checkout/", billing_views.checkout_create, name="billing-checkout"),
    path("app/billing/change/", billing_views.change_plan_confirm, name="billing-change"),
    path("app/billing/portal/", billing_views.portal, name="billing-portal"),
    # Settings (account info + change-password + account-erase). See
    
    
    # has a Settings page.
    path("app/settings/", settings_views.settings_page, name="settings"),
    path("app/settings/password/", settings_views.settings_password, name="settings-password"),
    path("app/settings/delete/", settings_views.settings_delete, name="settings-delete"),
    #  (BYOD custom domains). URLs are always REGISTERED
    # (mirrors the ENABLE_TEAMS/ENABLE_MULTI_ACCOUNT/ENABLE_BILLING
    # pattern), but every view is wrapped in domain_views._byod_enabled,
    # which raises Http404 when settings.ENABLE_BYOD is False — a request
    # to any of these paths with the flag off is indistinguishable from
    # the route not existing (404).
    path("app/domains/", domain_views.domains, name="domains"),
    path("app/domains/create/", domain_views.domain_create, name="domain-create"),
    path("app/domains/<str:public_id>/verify/", domain_views.domain_verify, name="domain-verify"),
    path("app/domains/<str:public_id>/delete/", domain_views.domain_delete, name="domain-delete"),
]
