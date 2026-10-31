"""Guard against copy that presents mailsocket as third-party signup infrastructure."""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from apps.dashboard.tests.test_trust_docs import HTML_PAGES

BANNED = re.compile(
    r"throwaway|\bdisposable\b|\bephemeral\b|temp[ -]?mail|temporary email|any signup(?: form)?|"
    r"signup automation|sign up anywhere|skip verification|unlimited inboxes|"
    r"\bburner\b|hand the address to any|\bbypass\b",
    re.IGNORECASE,
)

# This sentence forbids the misuse; the identifier is a stable API field.
ALLOWED = (
    "You may not resell or expose mailsocket as a disposable-mail service for fraud, account farming, "
    "credential abuse, harassment, malware delivery, or circumvention of safety controls.",
    "is_disposable",
    # The email verification feature documents classification of disposable domains.
    "disposable/role heuristics",
    "bad syntax, disposable, a CSV header",
    # AUP explicitly forbids this behavior.
    "bypass or defeat another service's verification",
)


def _violations(text):
    for phrase in ALLOWED:
        text = text.replace(phrase, "")
    return BANNED.findall(text)


@pytest.mark.parametrize("name", HTML_PAGES)
def test_public_page_positioning(name):
    # Competitor comparison quotes are not our positioning.
    if name.startswith("dashboard:compare"):
        return
    body = Client().get(reverse(name)).content.decode()
    assert not _violations(body), name


def test_llms_positioning():
    assert not _violations(Client().get(reverse("dashboard:llms-txt")).content.decode())


def test_openapi_and_sdk_readmes_positioning():
    root = Path(settings.BASE_DIR).parent
    paths = [root / "docs/openapi.yaml", root / "backend/apps/dashboard/static/dashboard/openapi.yaml"]
    paths.extend((root / "sdks").glob("*/README.md"))
    for path in paths:
        assert not _violations(path.read_text()), str(path)


def test_guard_detects_misleading_claims():
    for text in ("Throwaway inbox", "any signup form", "signup automation", "bypass verification"):
        assert _violations(text)


def test_registration_notice_and_authorized_use_on_primary_pages():
    for name in ("dashboard:root", "dashboard:docs", "dashboard:pricing"):
        assert b"For testing apps you own or are authorized to test." in Client().get(reverse(name)).content
    response = Client().get(reverse("accounts:register"))
    assert b"By creating an account you agree to the" in response.content
    assert reverse("dashboard:legal-aup").encode() in response.content
