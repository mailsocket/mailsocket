"""Public comparison pages: /docs/compare/ plus one page per competitor.

Competitor facts were checked on the date in COMPARE_CHECKED and every one
links to its source in COMPARE_SOURCES (tests assert each link is rendered).
mailsocket numbers never appear as literals: they come from the same
views._pricing_context() tiers /pricing/ renders and from
settings.BILLING_PLAN_LIMITS, so a plan change updates these pages too.
"""

from django.conf import settings
from django.http import Http404
from django.shortcuts import render
from django.views.decorators.http import require_GET

COMPARE_CHECKED = "October 2026"

# Plan keys in ladder order (cheapest first), as in BILLING_PLAN_LIMITS.
PLAN_ORDER = ("free", "paid", "scale", "business")

# Every competitor fact on a page links to one of these. Keep them in sync
# with docs/research/competitors-2026-10.md.
COMPARE_SOURCES = {
    "mailpit": {
        "repo": "https://github.com/axllent/mailpit",
    },
    "mailosaur": {
        "pricing": "https://mailosaur.com/pricing",
        "codes": "https://mailosaur.com/docs/automation/codes.md",
        "wait": "https://mailosaur.com/docs/automation/python.md",
        "downloads": "https://api.npmjs.org/downloads/point/last-month/mailosaur",
    },
    "agentmail": {
        "pricing": "https://www.agentmail.to/pricing",
        "mcp": "https://docs.agentmail.to/integrations/mcp.md",
        "downloads": "https://pypistats.org/api/packages/agentmail/recent",
    },
    "mailtrap": {
        "pricing": "https://mailtrap.io/pricing.md",
        "pricing_html": "https://mailtrap.io/pricing/",
        "agent_inbox": "https://mailtrap.io/agent-inbox",
        "downloads": "https://api.npmjs.org/downloads/point/last-month/mailtrap",
    },
    "mailslurp": {
        "pricing": "https://www.mailslurp.com/pricing",
        "llms": "https://www.mailslurp.com/llms.txt",
        "codes": "https://www.mailslurp.com/blog/email-verification-code-api",
        "downloads": "https://api.npmjs.org/downloads/point/last-month/mailslurp-client",
    },
}

COMPARISONS = {
    "mailpit": {
        "name": "Mailpit",
        "url_name": "dashboard:compare-mailpit",
        "title": "Mailsocket or Mailpit?",
        "summary": "Mailpit catches mail your own app sends. mailsocket gives you real inboxes that receive mail from third parties.",
    },
    "mailosaur": {
        "name": "Mailosaur",
        "url_name": "dashboard:compare-mailosaur",
        "title": "mailsocket vs Mailosaur",
        "summary": "Email and SMS testing for QA teams, compared with an email-only OTP inbox API.",
    },
    "agentmail": {
        "name": "AgentMail",
        "url_name": "dashboard:compare-agentmail",
        "title": "mailsocket vs AgentMail",
        "summary": "Two-way email for AI agents, compared with a receive-and-extract OTP inbox API.",
    },
    "mailtrap": {
        "name": "Mailtrap",
        "url_name": "dashboard:compare-mailtrap",
        "title": "mailsocket vs Mailtrap",
        "summary": "Mailtrap's agent inbox gives AI agents two-way email with webhooks, compared with an OTP-and-magic-link extraction API.",
    },
    "mailslurp": {
        "name": "MailSlurp",
        "url_name": "dashboard:compare-mailslurp",
        "title": "mailsocket vs MailSlurp",
        "summary": "An established email and SMS testing platform with phone numbers, compared with a smaller, email-only OTP inbox API.",
    },
}


def _mailsocket_facts():
    """mailsocket's side of every table, read from settings (drift-proof)."""
    from apps.api.views import WAIT_TIMEOUT_MAX

    from .views import _pricing_context

    tiers = {t["key"]: t for t in _pricing_context()["tiers"]}
    limits = settings.BILLING_PLAN_LIMITS

    def first_plan(predicate):
        for key in PLAN_ORDER:
            if predicate(limits[key]):
                return tiers[key]
        return None

    return {
        "free": tiers["free"],
        "pro": tiers["paid"],
        "scale": tiers["scale"],
        "business": tiers["business"],
        "webhooks_plan": first_plan(lambda plan: plan["webhooks"]),
        "domains_plan": first_plan(lambda plan: plan.get("custom_domains", 0) > 0),
        "wait_max": WAIT_TIMEOUT_MAX,
        "mail_domain": settings.MAIL_DOMAIN,
    }


@require_GET
def compare_index(request):
    return render(request, "dashboard/compare_index.html", {
        "title": "Compare",
        "checked": COMPARE_CHECKED,
        "comparisons": [dict(slug=slug, **data) for slug, data in COMPARISONS.items()],
    })


@require_GET
def compare_page(request, slug):
    if slug not in COMPARISONS:
        raise Http404
    data = COMPARISONS[slug]
    return render(request, f"dashboard/compare_{slug}.html", {
        "title": data["title"],
        "checked": COMPARE_CHECKED,
        "competitor": data["name"],
        "src": COMPARE_SOURCES[slug],
        "ms": _mailsocket_facts(),
    })
