"Public API."
from html.parser import HTMLParser

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan, RateLimitBucket, TeamMembership
from apps.domains.models import Domain
from apps.feedback.testing import strip_request_noise
from apps.inboxes.models import Inbox

pytestmark = pytest.mark.django_db(transaction=True)


def make_user(name, *, plan=AccountPlan.Plan.BUSINESS):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="password"
    )
    Account.objects.create(
        user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now()
    )
    AccountPlan.objects.create(user=user, plan=plan)
    return user


def login(user):
    client = Client()
    client.force_login(user)
    return client


def add_member(owner, member):
    return TeamMembership.objects.create(
        account_id=owner.pk, user=member, role=TeamMembership.Role.MEMBER
    )


def make_domain(user, name="byod.example.com", **overrides):
    return Domain.objects.create(user=user, name=name, **overrides)


# --- t1/t2: flag gating -------------------------------------------------------


@override_settings(ENABLE_BYOD=False)
def test_flag_off_all_four_routes_404():
    owner = make_user("flagoff_owner")
    client = login(owner)

    resp = client.get(reverse("dashboard:domains"))
    assert resp.status_code == 404

    resp = client.post(reverse("dashboard:domain-create"), {"name": "x.example.com"})
    assert resp.status_code == 404

    domain = make_domain(owner)
    resp = client.post(reverse("dashboard:domain-verify", kwargs={"public_id": domain.public_id}))
    assert resp.status_code == 404

    resp = client.post(reverse("dashboard:domain-delete", kwargs={"public_id": domain.public_id}))
    assert resp.status_code == 404


@override_settings(ENABLE_BYOD=False)
def test_flag_off_sidebar_has_no_domains_link():
    owner = make_user("flagoff_nav")
    client = login(owner)
    resp = client.get(reverse("dashboard:overview"))
    assert resp.status_code == 200
    assert b">Domains<" not in resp.content


@override_settings(ENABLE_BYOD=True)
def test_flag_on_sidebar_has_domains_link():
    owner = make_user("flagon_nav")
    client = login(owner)
    resp = client.get(reverse("dashboard:overview"))
    assert resp.status_code == 200
    assert b">Domains<" in resp.content


@override_settings(ENABLE_BYOD=True)
def test_business_owner_get_domains_page_renders_200_not_500():
    "Public API."
    owner = make_user("domainspage_business")
    client = login(owner)
    resp = client.get(reverse("dashboard:domains"))
    assert resp.status_code == 200


# --- t3/t4: list / tenancy -----------------------------------------------------


@override_settings(ENABLE_BYOD=True)
def test_list_renders_only_tenant_domains():
    owner = make_user("list_owner")
    other = make_user("list_other")
    mine = make_domain(owner, "mine.example.com")
    make_domain(other, "theirs.example.com")

    client = login(owner)
    resp = client.get(reverse("dashboard:domains"))
    assert resp.status_code == 200
    assert b"mine.example.com" in resp.content
    assert b"theirs.example.com" not in resp.content
    assert mine.public_id  # sanity


@override_settings(ENABLE_BYOD=True)
def test_foreign_public_id_verify_and_delete_404_no_oracle():
    owner = make_user("oracle_owner")
    other = make_user("oracle_other")
    foreign = make_domain(other, "foreign.example.com")

    client = login(owner)
    resp_verify_foreign = client.post(
        reverse("dashboard:domain-verify", kwargs={"public_id": foreign.public_id})
    )
    resp_verify_random = client.post(
        reverse("dashboard:domain-verify", kwargs={"public_id": "dom_doesnotexist"})
    )
    assert resp_verify_foreign.status_code == resp_verify_random.status_code == 404
    # byte-identical body — no existence oracle leaked via the 404 payload
    # Per-request noise only (own X-Request-Id + own path in the report link).
    assert strip_request_noise(resp_verify_foreign.content) == strip_request_noise(resp_verify_random.content)
    assert resp_verify_foreign["Content-Type"] == resp_verify_random["Content-Type"]

    resp_delete_foreign = client.post(
        reverse("dashboard:domain-delete", kwargs={"public_id": foreign.public_id})
    )
    resp_delete_random = client.post(
        reverse("dashboard:domain-delete", kwargs={"public_id": "dom_doesnotexist"})
    )
    assert resp_delete_foreign.status_code == resp_delete_random.status_code == 404
    assert strip_request_noise(resp_delete_foreign.content) == strip_request_noise(resp_delete_random.content)
    assert resp_delete_foreign["Content-Type"] == resp_delete_random["Content-Type"]
    # the foreign row must still exist — nothing leaked through
    assert Domain.objects.filter(pk=foreign.pk).exists()


@override_settings(ENABLE_BYOD=True, ENABLE_TEAMS=True)
def test_member_session_list_200_shows_account_domains():
    owner = make_user("member_list_owner")
    member = make_user("member_list_member")
    add_member(owner, member)
    make_domain(owner, "member-visible.example.com")

    client = login(member)
    resp = client.get(reverse("dashboard:domains"))
    assert resp.status_code == 200
    assert b"member-visible.example.com" in resp.content


# --- t6-t9: create --------------------------------------------------------------


@override_settings(ENABLE_BYOD=True)
def test_create_happy_path_302_row_pending_dns_txt_shown():
    owner = make_user("create_happy")
    client = login(owner)
    resp = client.post(reverse("dashboard:domain-create"), {"name": "happy.example.com"})
    assert resp.status_code == 302
    assert resp["Location"] == reverse("dashboard:domains")

    domain = Domain.objects.get(user=owner, name="happy.example.com")
    assert domain.status == Domain.Status.PENDING_DNS

    list_resp = client.get(reverse("dashboard:domains"))
    assert domain.verification_txt_host.encode() in list_resp.content
    assert domain.verification_token.encode() in list_resp.content


@override_settings(ENABLE_BYOD=True)
def test_create_duplicate_domain_taken_flash_no_row():
    owner = make_user("create_dup")
    make_domain(owner, "dup.example.com")
    client = login(owner)
    resp = client.post(reverse("dashboard:domain-create"), {"name": "dup.example.com"}, HTTP_HX_REQUEST="true")
    assert resp.status_code == 409
    assert b"already registered" in resp.content
    assert Domain.objects.filter(user=owner, name="dup.example.com").count() == 1


@override_settings(ENABLE_BYOD=True)
def test_create_malformed_fqdn_validation_error_flash():
    owner = make_user("create_malformed")
    client = login(owner)
    resp = client.post(reverse("dashboard:domain-create"), {"name": "not a domain"}, HTTP_HX_REQUEST="true")
    assert resp.status_code == 422
    assert Domain.objects.filter(user=owner).count() == 0


@override_settings(ENABLE_BYOD=True)
def test_create_reserved_apex_validation_error_flash():
    owner = make_user("create_reserved")
    client = login(owner)
    resp = client.post(reverse("dashboard:domain-create"), {"name": "sub.mailsocket.app"}, HTTP_HX_REQUEST="true")
    assert resp.status_code == 422
    assert Domain.objects.filter(user=owner).count() == 0


@override_settings(ENABLE_BYOD=True)
def test_create_non_business_plan_gets_entitlement_flash():
    owner = make_user("create_free", plan=AccountPlan.Plan.FREE)
    client = login(owner)
    resp = client.post(reverse("dashboard:domain-create"), {"name": "free.example.com"}, HTTP_HX_REQUEST="true")
    assert resp.status_code == 403
    assert b"Scale or Business plan" in resp.content
    assert Domain.objects.filter(user=owner).count() == 0


@override_settings(ENABLE_BYOD=True)
def test_create_business_at_cap_gets_limit_flash(settings):
    owner = make_user("create_cap")
    original = settings.BILLING_PLAN_LIMITS["business"]["custom_domains"]
    settings.BILLING_PLAN_LIMITS["business"]["custom_domains"] = 1
    try:
        make_domain(owner, "cap-existing.example.com")
        client = login(owner)
        resp = client.post(reverse("dashboard:domain-create"), {"name": "cap-new.example.com"}, HTTP_HX_REQUEST="true")
        assert resp.status_code == 403
        assert b"limit" in resp.content.lower()
        assert Domain.objects.filter(user=owner).count() == 1
    finally:
        settings.BILLING_PLAN_LIMITS["business"]["custom_domains"] = original


# --- t10/t11/t11b: verify -------------------------------------------------------


class FakeRecord:
    def __init__(self, *, strings=None, exchange=None):
        self.strings = strings or []
        self.exchange = exchange


class FakeResolver:
    def __init__(self, answers):
        self.answers = answers

    def resolve(self, qname, rdtype):
        import dns.exception

        result = self.answers.get((qname, rdtype))
        if result is None:
            raise dns.exception.DNSException()
        return result


@override_settings(ENABLE_BYOD=True)
def test_verify_mocked_resolver_ok_flips_verified(monkeypatch):
    owner = make_user("verify_ok")
    domain = make_domain(owner, "verify-ok.example.com")

    def fake_verify_domain(dom, *, resolver=None):
        dom.status = Domain.Status.VERIFIED
        dom.verified_at = timezone.now()
        dom.last_error = ""
        dom.save(update_fields=["status", "verified_at", "last_error"])
        return dom

    monkeypatch.setattr("apps.dashboard.domain_views.verify_domain", fake_verify_domain)

    client = login(owner)
    resp = client.post(reverse("dashboard:domain-verify", kwargs={"public_id": domain.public_id}))
    assert resp.status_code == 302
    domain.refresh_from_db()
    assert domain.status == Domain.Status.VERIFIED


@override_settings(ENABLE_BYOD=True)
def test_verify_txt_missing_stays_pending(monkeypatch):
    owner = make_user("verify_pending")
    domain = make_domain(owner, "verify-pending.example.com")

    def fake_verify_domain(dom, *, resolver=None):
        dom.last_error = "TXT record missing or does not match."
        dom.save(update_fields=["last_error"])
        return dom

    monkeypatch.setattr("apps.dashboard.domain_views.verify_domain", fake_verify_domain)

    client = login(owner)
    resp = client.post(reverse("dashboard:domain-verify", kwargs={"public_id": domain.public_id}))
    assert resp.status_code == 302
    domain.refresh_from_db()
    assert domain.status == Domain.Status.PENDING_DNS
    assert domain.last_error


@override_settings(ENABLE_BYOD=True)
def test_verify_never_demotes_already_verified_domain(monkeypatch):
    """Exercises the REAL verify_domain (apps.domains.services) through the
    dashboard verify route — only the DNS resolver is faked (mirrors the
    _StubResolver/FakeResolver discipline in
    apps.domains.tests.test_verification). The injected resolver always
    fails (no TXT/MX records), so a real failed re-check runs end-to-end;
    verify_domain's own never-demote doctrine (services.py:440-449) must
    keep status VERIFIED and record failure metadata."""
    owner = make_user("verify_never_demote")
    domain = make_domain(owner, "verify-never-demote.example.com", status=Domain.Status.VERIFIED)
    domain.verified_at = timezone.now()
    domain.last_error = ""
    domain.save(update_fields=["verified_at", "last_error"])

    failing_resolver = FakeResolver({})  # no TXT, no MX -> every lookup fails

    monkeypatch.setattr(
        "apps.domains.services._default_resolver", lambda: failing_resolver
    )

    client = login(owner)
    resp = client.post(reverse("dashboard:domain-verify", kwargs={"public_id": domain.public_id}))
    assert resp.status_code == 302

    domain.refresh_from_db()
    # never demoted, even though the injected resolver failed the real check
    assert domain.status == Domain.Status.VERIFIED
    # and failure metadata WAS recorded (last_error/last_checked_at) — this
    # is a real failed re-check, not a no-op skip.
    assert domain.last_error
    assert "TXT" in domain.last_error
    assert domain.last_checked_at is not None


@override_settings(ENABLE_BYOD=True)
def test_verify_rate_limit_second_call_429_shares_api_bucket(monkeypatch):
    from apps.accounts.services import _bucket_hash

    owner = make_user("verify_ratelimit")
    domain = make_domain(owner, "verify-ratelimit.example.com")

    monkeypatch.setattr(
        "apps.dashboard.domain_views.verify_domain", lambda dom, **kw: dom
    )

    client = login(owner)
    first = client.post(reverse("dashboard:domain-verify", kwargs={"public_id": domain.public_id}))
    assert first.status_code == 302

    second = client.post(reverse("dashboard:domain-verify", kwargs={"public_id": domain.public_id}), HTTP_HX_REQUEST="true")
    assert second.status_code == 429

    # the EXACT bucket_hash values the API view derives from the same keys
    # ("api:domain-verify:domain:30s"/domain.public_id and
    # "api:domain-verify:account:hour"/user.pk) must exist here — renaming
    # the dashboard bucket away from the API key fails this test.
    domain_bucket_hash = _bucket_hash("api:domain-verify:domain:30s", domain.public_id)
    account_bucket_hash = _bucket_hash("api:domain-verify:account:hour", str(owner.pk))

    assert RateLimitBucket.objects.count() == 2
    domain_bucket = RateLimitBucket.objects.get(bucket_hash=domain_bucket_hash)
    account_bucket = RateLimitBucket.objects.get(bucket_hash=account_bucket_hash)
    # increment_rate_limit always increments (even on the raising attempt,
    # accounts/services.py:156-157) — first call put both buckets at 1, the
    # second call's domain:30s increment pushed it to 2 (over limit=1) and
    # raised before the account:hour bucket was touched again.
    assert domain_bucket.count == 2
    assert account_bucket.count == 1


# --- t12/t13: mutation walls -----------------------------------------------------


@override_settings(ENABLE_BYOD=True, ENABLE_TEAMS=True)
def test_member_mutation_403_no_change():
    owner = make_user("member_mut_owner")
    member = make_user("member_mut_member")
    add_member(owner, member)
    domain = make_domain(owner, "member-mut.example.com")
    snapshot = (
        domain.status, domain.verified_at, domain.last_error,
        domain.last_checked_at, domain.consecutive_failures,
    )

    client = login(member)

    resp = client.post(reverse("dashboard:domain-create"), {"name": "member-new.example.com"}, HTTP_HX_REQUEST="true")
    assert resp.status_code == 403
    assert Domain.objects.filter(name="member-new.example.com").count() == 0

    resp = client.post(reverse("dashboard:domain-verify", kwargs={"public_id": domain.public_id}), HTTP_HX_REQUEST="true")
    assert resp.status_code == 403

    resp = client.post(reverse("dashboard:domain-delete", kwargs={"public_id": domain.public_id}), HTTP_HX_REQUEST="true")
    assert resp.status_code == 403
    assert Domain.objects.filter(pk=domain.pk).exists()

    domain.refresh_from_db()
    assert (
        domain.status, domain.verified_at, domain.last_error,
        domain.last_checked_at, domain.consecutive_failures,
    ) == snapshot


@override_settings(ENABLE_BYOD=True)
def test_demo_agent_mutation_403(monkeypatch):
    """The require_owner-returns-True-for-demo trap (design §1 wall 4):
    require_owner() is True for a demo/agent user, so the is_demo_or_agent_user
    wall MUST run first or writes would slip through."""
    owner = make_user("demo_agent_owner")
    domain = make_domain(owner, "demo-agent.example.com")
    snapshot = (
        domain.status, domain.verified_at, domain.last_error,
        domain.last_checked_at, domain.consecutive_failures,
    )

    monkeypatch.setattr(
        "apps.dashboard.domain_views.is_demo_or_agent_user", lambda user: True
    )

    client = login(owner)
    resp = client.post(reverse("dashboard:domain-create"), {"name": "demo-new.example.com"}, HTTP_HX_REQUEST="true")
    assert resp.status_code == 403
    assert Domain.objects.filter(name="demo-new.example.com").count() == 0

    resp = client.post(reverse("dashboard:domain-verify", kwargs={"public_id": domain.public_id}), HTTP_HX_REQUEST="true")
    assert resp.status_code == 403

    resp = client.post(reverse("dashboard:domain-delete", kwargs={"public_id": domain.public_id}), HTTP_HX_REQUEST="true")
    assert resp.status_code == 403
    assert Domain.objects.filter(pk=domain.pk).exists()

    domain.refresh_from_db()
    assert (
        domain.status, domain.verified_at, domain.last_error,
        domain.last_checked_at, domain.consecutive_failures,
    ) == snapshot


# --- t14: delete -------------------------------------------------------------------


@override_settings(ENABLE_BYOD=True)
def test_delete_happy_row_gone_name_reclaimable():
    owner = make_user("delete_happy")
    domain = make_domain(owner, "delete-happy.example.com")
    client = login(owner)

    resp = client.post(reverse("dashboard:domain-delete", kwargs={"public_id": domain.public_id}))
    assert resp.status_code == 302
    assert not Domain.objects.filter(pk=domain.pk).exists()

    # name is re-claimable
    resp = client.post(reverse("dashboard:domain-create"), {"name": "delete-happy.example.com"})
    assert resp.status_code == 302
    assert Domain.objects.filter(user=owner, name="delete-happy.example.com").exists()


@override_settings(ENABLE_BYOD=True)
def test_delete_with_bound_inbox_blocked():
    owner = make_user("delete_blocked")
    domain = make_domain(owner, "delete-blocked.example.com", status=Domain.Status.VERIFIED)
    Inbox.objects.create(user=owner, routing_key="bound", label="Bound", domain=domain)

    client = login(owner)
    resp = client.post(reverse("dashboard:domain-delete", kwargs={"public_id": domain.public_id}), HTTP_HX_REQUEST="true")
    assert resp.status_code == 409
    assert Domain.objects.filter(pk=domain.pk).exists()


# --- t15: template safety -----------------------------------------------------------


class _PageSafetyParser(HTMLParser):
    """Walks the DOM tree (via HTMLParser's tag stream) tracking a <pre>-depth
    stack so we can attribute text/data to whichever <pre> (if any) it's
    currently nested inside, and validate every <script> and every
    attribute on every element as we go."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pre_depth = 0
        self.pre_texts = []  # concatenated text of all <pre> blocks (any nesting)
        self.outside_pre_text_parts = []
        self._script_depth = 0
        self._current_script_has_src = False
        self._current_script_body = []
        self.bad_on_attrs = []  # list of (tag, attr_name) violations
        self.bad_scripts = []  # list of violation messages

    def handle_starttag(self, tag, attrs):
        self._check_attrs(tag, attrs)

        if tag == "script":
            self._script_depth += 1
            self._current_script_has_src = any(name.lower() == "src" for name, _ in attrs)
            self._current_script_body = []

        if tag == "pre":
            self.pre_depth += 1

    def handle_startendtag(self, tag, attrs):
        # self-closing / void tags, e.g. <br/> <input .../>
        self._check_attrs(tag, attrs)

        if tag == "script":
            # self-closing <script/> — a script element with no body by
            # construction, but still subject to the src assertion.
            has_src = any(name.lower() == "src" for name, _ in attrs)
            if not has_src:
                self.bad_scripts.append("<script/> (self-closing) missing src attribute")

    def _check_attrs(self, tag, attrs):
        for name, _value in attrs:
            if name.lower().startswith("on"):
                self.bad_on_attrs.append((tag, name))

    def handle_endtag(self, tag):
        if tag == "script" and self._script_depth > 0:
            body = "".join(self._current_script_body).strip()
            # The two assertions are independent: a <script> can fail one,
            # the other, both, or neither — each is reported separately.
            if not self._current_script_has_src:
                self.bad_scripts.append("<script> missing src attribute")
            if body != "":
                self.bad_scripts.append(f"inline <script> body found: {body!r}")
            self._script_depth -= 1
            self._current_script_body = []

        if tag == "pre" and self.pre_depth > 0:
            self.pre_depth -= 1

    def handle_data(self, data):
        if self._script_depth > 0:
            self._current_script_body.append(data)
        if self.pre_depth > 0:
            self.pre_texts.append(data)
        else:
            self.outside_pre_text_parts.append(data)


@override_settings(ENABLE_BYOD=True)
def test_page_has_no_inline_script_and_dns_in_pre():
    owner = make_user("template_safety")
    domain = make_domain(owner, "template-safety.example.com")  # pending_dns -> DNS block rendered

    client = login(owner)
    resp = client.get(reverse("dashboard:domains"))
    content = resp.content.decode()

    parser = _PageSafetyParser()
    parser.feed(content)
    parser.close()

    # no INLINE <script> element (a <script> tag with a JS body, with or
    # without extra attrs/nonce, INCLUDING an empty `<script></script>`) —
    # external `src=` includes (htmx/app.js etc, base.html) are fine and
    # not "inline".
    assert parser.bad_scripts == [], parser.bad_scripts

    # no inline on*= event-handler attribute anywhere in the page, on ANY
    # element, including unquoted values like onclick=alert(1).
    assert parser.bad_on_attrs == [], f"inline event-handler attribute(s) found: {parser.bad_on_attrs}"

    # every <pre>...</pre> block's text content, concatenated.
    pre_text = "".join(parser.pre_texts)
    assert parser.pre_texts, "expected at least one <pre> block"

    assert domain.verification_txt_host in pre_text
    assert domain.verification_token in pre_text
    assert domain.name in pre_text
    from apps.domains.services import MX_TARGET_HOST

    assert MX_TARGET_HOST in pre_text

    # and none of that DNS content leaks OUTSIDE every <pre> block (i.e. it's
    # only ever rendered inside <pre>, never duplicated elsewhere in the DOM
    # in a way a script could scrape without the <pre> wrapper).
    outside_text = "".join(parser.outside_pre_text_parts)
    assert domain.verification_token not in outside_text
