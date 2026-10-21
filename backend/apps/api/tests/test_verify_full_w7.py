"Public API."
import ipaddress

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, AccountPlan, WaitCapacity
from apps.accounts.services import create_api_key

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


def auth_client(user, *, label="w7-test"):
    _, token = create_api_key(user, label)
    client = APIClient(raise_request_exception=True)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def client_for_token(token):
    client = APIClient(raise_request_exception=True)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


@pytest.fixture(autouse=True)
def _enable_smtp_verify(settings):
    settings.ENABLE_SMTP_VERIFY = True


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    """Every test in this module must be zero-network. apps.api.verification
    (the safe-mode DNS check) uses its own dns.resolver.Resolver instance
    independent of apps.api.smtp_probe's resolver — patch its .resolve to
    raise NXDOMAIN by default (fast, deterministic "no mail-accepting DNS
    records" outcome) so a test that forgets to wire a FakeResolver can never
    silently fall through to a real lookup. Tests that need the safe-mode
    has_mx check to see mail-accepting DNS call _wire_safe_dns(monkeypatch,
    resolver) to redirect it at the SAME FakeResolver script used by the
    full-mode probe.
    """
    import dns.resolver

    import apps.api.verification as verification_mod

    def _default_deny(self, qname, rdtype, *a, **kw):
        raise dns.resolver.NXDOMAIN()

    monkeypatch.setattr(verification_mod.dns.resolver.Resolver, "resolve", _default_deny)


def _wire_safe_dns(monkeypatch, resolver):
    """Redirect apps.api.verification's safe-mode DNS resolver to answer
    from the given FakeResolver script (zero network)."""
    import apps.api.verification as verification_mod

    def _delegate(self, qname, rdtype, *a, **kw):
        return resolver.resolve(str(qname).rstrip("."), rdtype)

    monkeypatch.setattr(verification_mod.dns.resolver.Resolver, "resolve", _delegate)


# --- fake DNS resolver (dnspython .resolve(name, rdtype) contract) ----------


class _FakeAnswer(list):
    pass


class _FakeMxRecord:
    def __init__(self, preference, exchange):
        self.preference = preference
        self.exchange = exchange + "."


class FakeResolver:
    """Scriptable resolver: mx_map[domain] -> list[(pref, exchange)] | "NXDOMAIN".
    a_map[host] -> list[ip strings] | "NXDOMAIN". AAAA is always empty unless
    provided in aaaa_map.
    """

    def __init__(self, mx_map=None, a_map=None, aaaa_map=None):
        self.mx_map = mx_map or {}
        self.a_map = a_map or {}
        self.aaaa_map = aaaa_map or {}
        self.calls = []

    def resolve(self, name, rdtype):
        import dns.resolver

        self.calls.append((name, rdtype))
        if rdtype == "MX":
            if name not in self.mx_map:
                raise dns.resolver.NXDOMAIN()
            entries = self.mx_map[name]
            if entries == "NXDOMAIN":
                raise dns.resolver.NXDOMAIN()
            return _FakeAnswer(_FakeMxRecord(p, e) for p, e in entries)
        if rdtype == "A":
            ips = self.a_map.get(name)
            if not ips:
                raise dns.resolver.NXDOMAIN()
            return _FakeAnswer(ips)
        if rdtype == "AAAA":
            ips = self.aaaa_map.get(name)
            if not ips:
                raise dns.resolver.NXDOMAIN()
            return _FakeAnswer(ips)
        raise dns.resolver.NXDOMAIN()


def public_resolver(domain, mx_host, ip="93.184.216.34"):
    return FakeResolver(mx_map={domain: [(10, mx_host)]}, a_map={mx_host: [ip]})


# --- FakeSmtp transport (smtplib.SMTP subset used by smtp_probe) ------------


class FakeSmtp:
    """Scriptable fake transport. ``script`` is a list of (code, msg) tuples
    consumed in order by ehlo/helo/mail/rcpt calls (skipping starttls, which
    is separately controlled). T18 asserts .data is never invoked — there is
    deliberately no data() method here, so any call raises AttributeError.

    MEDIUM T2 fixup: a missing/unscripted RCPT call used to silently default
    to 550, which could mask a bug where a second (random-recipient) RCPT was
    never actually attempted — a test asserting "550 -> undeliverable" would
    pass identically whether or not the random RCPT path ran at all. Any RCPT
    call beyond the scripted ``rcpt_codes`` now raises loudly instead.
    """

    instances: list["FakeSmtp"] = []

    def __init__(self, ehlo_code=250, mail_code=250, rcpt_codes=None, extns=None,
                 raise_on_connect=None):
        self.ehlo_code = ehlo_code
        self.mail_code = mail_code
        self.rcpt_codes = list(rcpt_codes or [])
        self.extns = extns or set()
        self.rcpt_calls = []
        self.quit_called = False
        if raise_on_connect:
            raise raise_on_connect

    def ehlo(self, name):
        return (self.ehlo_code, b"ok")

    def helo(self, name):
        return (self.ehlo_code, b"ok")

    def has_extn(self, name):
        return name in self.extns

    def starttls(self):
        return (220, b"ok")

    def mail(self, sender):
        return (self.mail_code, b"ok")

    def rcpt(self, address):
        self.rcpt_calls.append(address)
        idx = len(self.rcpt_calls) - 1
        if idx >= len(self.rcpt_codes):
            raise AssertionError(
                f"FakeSmtp.rcpt called for unscripted call #{idx + 1} "
                f"(address={address!r}); script exhausted at "
                f"{len(self.rcpt_codes)} code(s) — the test's rcpt_codes list "
                f"is missing an entry for a call it claims to exercise"
            )
        return (self.rcpt_codes[idx], b"ok")

    def quit(self):
        self.quit_called = True


def fake_connect_factory(**kwargs):
    """Returns a `connect(ip, port, timeout)` callable that always returns
    the same scripted FakeSmtp and records it for assertions."""
    created = []

    def _connect(ip, port, timeout):
        smtp = FakeSmtp(**kwargs)
        created.append(smtp)
        return smtp

    _connect.created = created
    return _connect


def never_called_connect(ip, port, timeout):
    raise AssertionError("FakeSmtp transport must not be called for this scenario")


# --- T1: safe regression, byte-identical, FakeSmtp never called -------------


def test_t1_safe_mode_regression_byte_identical_no_probe(monkeypatch):
    "Public API."
    import apps.api.smtp_probe as smtp_probe_mod

    connect = fake_connect_factory()
    monkeypatch.setattr(smtp_probe_mod, "_default_connect", connect)
    user = make_user("t1_safe")
    client = auth_client(user)

    resp_omitted = client.post("/api/v1/verify", {"email": "someone@example.test"}, format="json")
    resp_safe = client.post("/api/v1/verify", {"email": "someone@example.test", "mode": "safe"}, format="json")

    assert resp_omitted.status_code == 200
    assert resp_safe.status_code == 200

    expected = {
        "email": "someone@example.test",
        "deliverable": "undeliverable",
        "syntax_valid": True,
        "domain": "example.test",
        "has_mx": False,
        "is_disposable": False,
        "is_role": False,
        "reason": "domain has no mail-accepting DNS records",
        "checked": "syntax+dns (no smtp probe)",
    }
    assert resp_omitted.json()["data"] == expected
    assert resp_safe.json()["data"] == expected
    assert resp_omitted.content == resp_safe.content
    assert connect.created == []


# --- T2-T6: verdict mapping --------------------------------------------------


def _full_client_and_domain(name, domain="probeme.example.test", mx_host="mx.probeme.example.test"):
    user = make_user(name)
    client = auth_client(user)
    resolver = public_resolver(domain, mx_host)
    return client, domain, resolver


def _post_full(client, email, resolver, connect, monkeypatch):
    import apps.api.smtp_probe as smtp_probe_mod

    monkeypatch.setattr(smtp_probe_mod, "_default_mx_resolver", lambda lifetime: resolver)
    monkeypatch.setattr(smtp_probe_mod, "_default_connect", connect)
    _wire_safe_dns(monkeypatch, resolver)
    return client.post("/api/v1/verify", {"email": email, "mode": "full"}, format="json")


def test_t2_target_250_random_550_deliverable(monkeypatch):
    client, domain, resolver = _full_client_and_domain("t2")
    connect = fake_connect_factory(rcpt_codes=[250, 550])
    resp = _post_full(client, f"hello@{domain}", resolver, connect, monkeypatch)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["deliverable"] == "deliverable"
    assert data["smtp"]["probe"] == "completed"
    assert data["smtp"]["catch_all"] is False
    assert data["smtp"]["rcpt_code"] == 250
    assert data["checked"] == "syntax+dns+smtp"

    # MEDIUM T2 (load-bearing): prove the 250-target-then-random path was
    # ACTUALLY exercised — exactly two RCPT calls, the first to the target
    # address, the script fully consumed (FakeSmtp now raises rather than
    # silently defaulting an unscripted call, so this can't pass vacuously).
    smtp_instance = connect.created[0]
    assert len(smtp_instance.rcpt_calls) == 2
    assert smtp_instance.rcpt_calls[0] == f"hello@{domain}"


def test_t3_target_550_undeliverable_no_random_rcpt(monkeypatch):
    client, domain, resolver = _full_client_and_domain("t3")
    connect = fake_connect_factory(rcpt_codes=[550])
    resp = _post_full(client, f"hello@{domain}", resolver, connect, monkeypatch)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["deliverable"] == "undeliverable"
    assert data["smtp"]["probe"] == "completed"
    assert data["smtp"]["catch_all"] is None
    assert data["smtp"]["rcpt_code"] == 550
    assert data["checked"] == "syntax+dns+smtp"
    smtp_instance = connect.created[0]
    assert smtp_instance.rcpt_calls == [f"hello@{domain}"]  # random RCPT NOT attempted, and target was the ONE call


def test_t4_target_250_random_250_risky_catch_all(monkeypatch):
    client, domain, resolver = _full_client_and_domain("t4")
    connect = fake_connect_factory(rcpt_codes=[250, 250])
    resp = _post_full(client, f"hello@{domain}", resolver, connect, monkeypatch)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["deliverable"] == "risky"
    assert data["smtp"]["probe"] == "completed"
    assert data["smtp"]["catch_all"] is True
    assert data["smtp"]["rcpt_code"] == 250
    assert data["checked"] == "syntax+dns+smtp"


def test_t5_greylist_450_risky(monkeypatch):
    client, domain, resolver = _full_client_and_domain("t5")
    connect = fake_connect_factory(rcpt_codes=[450])
    resp = _post_full(client, f"hello@{domain}", resolver, connect, monkeypatch)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["deliverable"] == "risky"
    assert data["smtp"]["probe"] == "completed"
    assert data["smtp"]["reason"] == "greylisted_or_tempfail"
    assert data["smtp"]["catch_all"] is None
    assert data["smtp"]["rcpt_code"] == 450
    assert data["checked"] == "syntax+dns+smtp"


def test_t6_mailbox_full_552_risky(monkeypatch):
    client, domain, resolver = _full_client_and_domain("t6")
    connect = fake_connect_factory(rcpt_codes=[552])
    resp = _post_full(client, f"hello@{domain}", resolver, connect, monkeypatch)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["deliverable"] == "risky"
    assert data["smtp"]["probe"] == "completed"
    assert data["smtp"]["catch_all"] is None
    assert data["smtp"]["rcpt_code"] == 552
    assert data["smtp"]["reason"] == "mailbox full or over quota"
    assert data["checked"] == "syntax+dns+smtp"


# --- T7: timeout / connect error -> 200 degrade, never 500 ------------------


def test_t7_connect_error_degrades_to_200_never_500(monkeypatch):
    """MEDIUM T7: force a REAL timeout (TimeoutError, not a generic OSError —
    smtplib timeouts surface as socket.timeout/TimeoutError specifically),
    assert the degraded verdict EQUALS the preserved safe verdict exactly
    (not "any of the four enum values", which would also pass a broken
    mapping), and assert the response body leaks no traceback / exception
    text / internal MX host or IP."""
    domain = "t7.example.test"
    mx_host = "mx.t7.example.test"
    mx_ip = "93.184.216.44"
    resolver = public_resolver(domain, mx_host, ip=mx_ip)
    user = make_user("t7")
    client = auth_client(user)

    def _boom(ip, port, timeout):
        raise TimeoutError("simulated connect timeout")

    resp = _post_full(client, f"hello@{domain}", resolver, _boom, monkeypatch)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["smtp"]["probe"] == "failed"

    resp_safe = client.post("/api/v1/verify", {"email": f"hello@{domain}", "mode": "safe"}, format="json")
    assert resp_safe.status_code == 200
    assert data["deliverable"] == resp_safe.json()["data"]["deliverable"]
    # A failed probe must never dishonestly claim "syntax+dns+smtp": the
    # checked string must match the honest safe-mode value byte-for-byte.
    assert data["checked"] == resp_safe.json()["data"]["checked"]

    body = resp.content.decode()
    for leaked in ("Traceback", "traceback", "Exception", "TimeoutError", "simulated connect timeout", mx_host, mx_ip):
        assert leaked not in body


# --- T8: flag OFF -> 403 smtp_verify_disabled, no probe ---------------------


def test_t8_flag_off_403_no_probe(settings, monkeypatch):
    settings.ENABLE_SMTP_VERIFY = False
    import apps.api.smtp_probe as smtp_probe_mod

    monkeypatch.setattr(smtp_probe_mod, "_default_connect", never_called_connect)
    user = make_user("t8")
    client = auth_client(user)
    resp = client.post("/api/v1/verify", {"email": "x@example.test", "mode": "full"}, format="json")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "smtp_verify_disabled"


# --- T9: tier gate -----------------------------------------------------------


@pytest.mark.parametrize("plan", [AccountPlan.Plan.FREE, AccountPlan.Plan.PAID, AccountPlan.Plan.SCALE])
def test_t9_tier_gate_non_business_403(plan, monkeypatch):
    import apps.api.smtp_probe as smtp_probe_mod

    monkeypatch.setattr(smtp_probe_mod, "_default_connect", never_called_connect)
    user = make_user(f"t9_{plan}", plan=plan)
    client = auth_client(user)
    resp = client.post("/api/v1/verify", {"email": "x@example.test", "mode": "full"}, format="json")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "verify_full_entitlement_required"


def test_t9_demo_user_403(monkeypatch):
    """MEDIUM T9: the shared demo account (apps.accounts.services.get_demo_user,
    a REAL provisioned Free-plan account with an unusable password — not a
    synthetic FREE-plan test user) must also be denied full-mode verify."""
    import apps.api.smtp_probe as smtp_probe_mod

    monkeypatch.setattr(smtp_probe_mod, "_default_connect", never_called_connect)
    from apps.accounts.services import get_demo_user

    user = get_demo_user()
    _, token = create_api_key(user, "w7-demo-test")
    client = client_for_token(token)
    resp = client.post("/api/v1/verify", {"email": "x@example.test", "mode": "full"}, format="json")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "verify_full_entitlement_required"


def test_t9_agent_registered_key_403(monkeypatch):
    """MEDIUM T9: a key minted via the zero-signup POST /api/v1/agents/register
    path (apps.accounts.services.register_agent — a REAL throwaway Free-plan
    account, its own code path from make_user) must also be denied."""
    import apps.api.smtp_probe as smtp_probe_mod

    monkeypatch.setattr(smtp_probe_mod, "_default_connect", never_called_connect)
    from apps.accounts.services import register_agent

    _user, _api_key, token = register_agent("w7-test-agent")
    client = client_for_token(token)
    resp = client.post("/api/v1/verify", {"email": "x@example.test", "mode": "full"}, format="json")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "verify_full_entitlement_required"


def test_t9_business_probes(monkeypatch):
    client, domain, resolver = _full_client_and_domain("t9_business")
    connect = fake_connect_factory(rcpt_codes=[250, 550])
    resp = _post_full(client, f"hello@{domain}", resolver, connect, monkeypatch)
    assert resp.status_code == 200
    assert resp.json()["data"]["smtp"]["probe"] == "completed"


# --- T10: full-mode key bucket independent of safe bucket -------------------


def test_t10_full_key_bucket_independent_of_safe_bucket_51st_429(settings, monkeypatch):
    settings.VERIFY_FULL_KEY_RATE_LIMIT = 2
    import apps.api.smtp_probe as smtp_probe_mod

    monkeypatch.setattr(smtp_probe_mod, "_default_connect", never_called_connect)

    user = make_user("t10")
    client = auth_client(user)

    domain = "t10.example.test"
    mx_host = "mx.t10.example.test"
    resolver = public_resolver(domain, mx_host)
    connect = fake_connect_factory(rcpt_codes=[250, 550])

    r1 = _post_full(client, f"one@{domain}", resolver, connect, monkeypatch)
    r2 = _post_full(client, f"two@{domain}", resolver, connect, monkeypatch)
    assert r1.status_code == 200
    assert r2.status_code == 200

    r3 = client.post("/api/v1/verify", {"email": f"three@{domain}", "mode": "full"}, format="json")
    assert r3.status_code == 429

    # Safe requests still pass after full is exhausted.
    r_safe = client.post("/api/v1/verify", {"email": "safe-still-ok@example.test"}, format="json")
    assert r_safe.status_code == 200


# --- T11: per-account full bucket trips across two keys ---------------------


def test_t11_per_account_full_bucket_across_two_keys(settings, monkeypatch):
    settings.VERIFY_FULL_ACCOUNT_RATE_LIMIT = 1
    settings.VERIFY_FULL_KEY_RATE_LIMIT = 1000
    import apps.api.smtp_probe as smtp_probe_mod

    monkeypatch.setattr(smtp_probe_mod, "_default_connect", never_called_connect)

    user = make_user("t11")
    _, token_a = create_api_key(user, "key-a")
    _, token_b = create_api_key(user, "key-b")
    client_a = APIClient()
    client_a.credentials(HTTP_AUTHORIZATION=f"Bearer {token_a}")
    client_b = APIClient()
    client_b.credentials(HTTP_AUTHORIZATION=f"Bearer {token_b}")

    domain = "t11.example.test"
    mx_host = "mx.t11.example.test"
    resolver = public_resolver(domain, mx_host)
    connect = fake_connect_factory(rcpt_codes=[250, 550])

    r1 = _post_full(client_a, f"one@{domain}", resolver, connect, monkeypatch)
    assert r1.status_code == 200

    r2 = client_b.post("/api/v1/verify", {"email": f"two@{domain}", "mode": "full"}, format="json")
    assert r2.status_code == 429


# --- T12: SSRF (LOAD-BEARING P0) ---------------------------------------------


@pytest.mark.parametrize(
    "bad_ip",
    ["10.0.0.5", "169.254.169.254", "::ffff:127.0.0.1", "64:ff9b::a00:1"],
)
def test_t12_ssrf_non_public_mx_skipped_no_connect(bad_ip, monkeypatch):
    """HIGH T12 (critical, P0 SSRF): every non-public MX candidate must
    result in EXACTLY ZERO connect-factory invocations (an explicit
    call-count assertion, not merely "probe reported skipped" — a bug could
    dial out and then overwrite the reason afterward), AND the degraded
    response must equal the safe-mode verdict for the SAME email BYTE FOR
    BYTE (modulo the additive "smtp" key and the deliberate "checked"
    rewrite) — "deliverable != invalid" alone does not catch a probe that
    leaked a DIFFERENT (but still not "invalid") verdict.
    """
    import apps.api.smtp_probe as smtp_probe_mod

    connect = fake_connect_factory()
    monkeypatch.setattr(smtp_probe_mod, "_default_connect", connect)

    domain = f"ssrf-{abs(hash(bad_ip))}.example.test"
    mx_host = f"mx.{domain}"
    is_v6 = ipaddress.ip_address(bad_ip).version == 6
    resolver = FakeResolver(
        mx_map={domain: [(10, mx_host)]},
        a_map={} if is_v6 else {mx_host: [bad_ip]},
        aaaa_map={mx_host: [bad_ip]} if is_v6 else {},
    )
    user = make_user(f"t12_{abs(hash(bad_ip))}")
    client = auth_client(user)
    monkeypatch.setattr(smtp_probe_mod, "_default_mx_resolver", lambda lifetime: resolver)
    _wire_safe_dns(monkeypatch, resolver)

    email = f"hello@{domain}"
    resp_safe = client.post("/api/v1/verify", {"email": email, "mode": "safe"}, format="json")
    resp = client.post("/api/v1/verify", {"email": email, "mode": "full"}, format="json")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["smtp"]["probe"] == "skipped"
    assert data["smtp"]["reason"] == "mx_non_public"
    assert data["deliverable"] != "invalid"

    # --- HIGH T12(a): explicit zero-connect-calls assertion -----------------
    assert len(connect.created) == 0

    # --- HIGH T12(b): degraded response == safe-mode verdict, byte-for-byte,
    # modulo the additive "smtp" key only — a skipped probe must NOT upgrade
    # "checked", so no field rewriting here. ---
    assert resp_safe.status_code == 200
    full_minus_smtp = dict(data)
    full_minus_smtp.pop("smtp")
    assert full_minus_smtp == resp_safe.json()["data"]


def test_t12_positive_control_public_mx_causes_exactly_one_connect(monkeypatch):
    """HIGH T12 positive control: proves the zero-connect assertions above
    are not vacuously true because of an always-skip bug — the SAME wiring
    against a PUBLIC MX target must cause EXACTLY ONE connect call."""
    client, domain, resolver = _full_client_and_domain("t12_positive")
    connect = fake_connect_factory(rcpt_codes=[250, 550])
    resp = _post_full(client, f"hello@{domain}", resolver, connect, monkeypatch)
    assert resp.status_code == 200
    assert resp.json()["data"]["smtp"]["probe"] == "completed"
    assert len(connect.created) == 1


# --- T13: null MX -------------------------------------------------------------


def test_t13_null_mx_undeliverable_no_connect(monkeypatch):
    import apps.api.smtp_probe as smtp_probe_mod

    monkeypatch.setattr(smtp_probe_mod, "_default_connect", never_called_connect)

    domain = "nullmx.example.test"
    resolver = FakeResolver(mx_map={domain: [(0, "")]})
    user = make_user("t13")
    client = auth_client(user)
    monkeypatch.setattr(smtp_probe_mod, "_default_mx_resolver", lambda lifetime: resolver)
    _wire_safe_dns(monkeypatch, resolver)

    resp = client.post("/api/v1/verify", {"email": f"hello@{domain}", "mode": "full"}, format="json")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["deliverable"] == "undeliverable"
    assert data["smtp"]["probe"] == "skipped"
    assert data["smtp"]["reason"] == "null_mx"

    resp_safe = client.post("/api/v1/verify", {"email": f"hello@{domain}", "mode": "safe"}, format="json")
    assert resp_safe.status_code == 200
    # A skipped probe must never dishonestly claim "syntax+dns+smtp": the
    # checked string must match the honest safe-mode value byte-for-byte.
    assert data["checked"] == resp_safe.json()["data"]["checked"]


# --- T14: domain cooldown ------------------------------------------------


def test_t14_domain_cooldown_11th_skipped(settings, monkeypatch):
    settings.VERIFY_FULL_PER_DOMAIN_LIMIT = 10
    settings.VERIFY_FULL_KEY_RATE_LIMIT = 100
    settings.VERIFY_FULL_ACCOUNT_RATE_LIMIT = 100

    domain = "cooldown.example.test"
    mx_host = "mx.cooldown.example.test"
    resolver = public_resolver(domain, mx_host)

    user = make_user("t14")
    client = auth_client(user)

    import apps.api.smtp_probe as smtp_probe_mod

    for i in range(10):
        connect = fake_connect_factory(rcpt_codes=[250, 550])
        resp = _post_full(client, f"n{i}@{domain}", resolver, connect, monkeypatch)
        assert resp.status_code == 200
        assert resp.json()["data"]["smtp"]["probe"] == "completed"

    monkeypatch.setattr(smtp_probe_mod, "_default_connect", never_called_connect)
    monkeypatch.setattr(smtp_probe_mod, "_default_mx_resolver", lambda lifetime: resolver)
    _wire_safe_dns(monkeypatch, resolver)
    resp = client.post("/api/v1/verify", {"email": f"eleventh@{domain}", "mode": "full"}, format="json")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["smtp"]["probe"] == "skipped"
    assert data["smtp"]["reason"] == "domain_cooldown"

    resp_safe = client.post("/api/v1/verify", {"email": f"eleventh@{domain}", "mode": "safe"}, format="json")
    assert resp_safe.status_code == 200
    # A skipped probe must never dishonestly claim "syntax+dns+smtp": the
    # checked string must match the honest safe-mode value byte-for-byte.
    assert data["checked"] == resp_safe.json()["data"]["checked"]


def test_t14_domain_cooldown_is_global_across_accounts(settings, monkeypatch):
    """MEDIUM T14: the per-domain cooldown bucket is keyed on the TARGET
    DOMAIN, not the tenant (apps.accounts.services.check_verify_full_domain_
    cooldown, design §4d — the remote MX sees one probing IP regardless of
    which customer asked). Prove it's GLOBAL: account A exhausts the 10-slot
    window against a domain, then a DIFFERENT account B's 11th probe against
    the SAME domain must ALSO be skipped (never_called_connect makes this a
    hard failure if B is wrongly given its own independent bucket)."""
    settings.VERIFY_FULL_PER_DOMAIN_LIMIT = 10
    settings.VERIFY_FULL_KEY_RATE_LIMIT = 100
    settings.VERIFY_FULL_ACCOUNT_RATE_LIMIT = 100

    domain = "cooldown-cross-account.example.test"
    mx_host = "mx.cooldown-cross-account.example.test"
    resolver = public_resolver(domain, mx_host)

    user_a = make_user("t14_cross_a")
    client_a = auth_client(user_a)
    user_b = make_user("t14_cross_b")
    client_b = auth_client(user_b)

    import apps.api.smtp_probe as smtp_probe_mod

    for i in range(10):
        connect = fake_connect_factory(rcpt_codes=[250, 550])
        resp = _post_full(client_a, f"n{i}@{domain}", resolver, connect, monkeypatch)
        assert resp.status_code == 200
        assert resp.json()["data"]["smtp"]["probe"] == "completed"

    monkeypatch.setattr(smtp_probe_mod, "_default_connect", never_called_connect)
    monkeypatch.setattr(smtp_probe_mod, "_default_mx_resolver", lambda lifetime: resolver)
    _wire_safe_dns(monkeypatch, resolver)
    resp = client_b.post("/api/v1/verify", {"email": f"eleventh@{domain}", "mode": "full"}, format="json")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["smtp"]["probe"] == "skipped"
    assert data["smtp"]["reason"] == "domain_cooldown"

    resp_safe = client_b.post("/api/v1/verify", {"email": f"eleventh@{domain}", "mode": "safe"}, format="json")
    assert resp_safe.status_code == 200
    # A skipped probe must never dishonestly claim "syntax+dns+smtp": the
    # checked string must match the honest safe-mode value byte-for-byte.
    assert data["checked"] == resp_safe.json()["data"]["checked"]


# --- T15: strict mode parsing -------------------------------------------


@pytest.mark.parametrize("bad_mode", ["FULL", "fool", "Safe", ""])
def test_t15_invalid_mode_422(bad_mode):
    user = make_user(f"t15-{(bad_mode or 'empty').lower()}")
    client = auth_client(user)
    resp = client.post("/api/v1/verify", {"email": "x@example.test", "mode": bad_mode}, format="json")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


# --- T16: capacity exhausted -------------------------------------------


def test_t16_full_capacity_exhausted_429_safe_unaffected(settings, monkeypatch):
    settings.VERIFY_FULL_CAPACITY_LIMIT = 2
    from apps.accounts.services import VERIFY_FULL_CAPACITY_KEY

    WaitCapacity.objects.update_or_create(
        key=VERIFY_FULL_CAPACITY_KEY,
        defaults={"count": 2, "expires_at": timezone.now() + timezone.timedelta(seconds=30)},
    )

    import apps.api.smtp_probe as smtp_probe_mod

    monkeypatch.setattr(smtp_probe_mod, "_default_connect", never_called_connect)

    user = make_user("t16")
    client = auth_client(user)
    resp = client.post("/api/v1/verify", {"email": "x@example.test", "mode": "full"}, format="json")
    assert resp.status_code == 429

    resp_safe = client.post("/api/v1/verify", {"email": "y@example.test"}, format="json")
    assert resp_safe.status_code == 200


def test_t16_full_capacity_released_after_completed_probe_no_leak(settings, monkeypatch):
    """MEDIUM T16: acquire a full-mode capacity slot, let the probe COMPLETE
    (which must release it via VerifyEmailView._verify_full_gates' finally ->
    release_verify_full_capacity), then prove a LATER full-mode request still
    succeeds under a capacity limit of exactly 1 — i.e. the first request's
    slot was not leaked."""
    settings.VERIFY_FULL_CAPACITY_LIMIT = 1
    client, domain, resolver = _full_client_and_domain("t16_release")

    connect1 = fake_connect_factory(rcpt_codes=[250, 550])
    resp1 = _post_full(client, f"one@{domain}", resolver, connect1, monkeypatch)
    assert resp1.status_code == 200
    assert resp1.json()["data"]["smtp"]["probe"] == "completed"

    # If the slot from resp1 leaked, capacity is still "1/1 in use" and this
    # would 429 instead of succeeding.
    connect2 = fake_connect_factory(rcpt_codes=[250, 550])
    resp2 = _post_full(client, f"two@{domain}", resolver, connect2, monkeypatch)
    assert resp2.status_code == 200
    assert resp2.json()["data"]["smtp"]["probe"] == "completed"


# --- T17: probe raises unexpected exception -> caught, 200 degraded --------


def test_t17_probe_raises_caught_200_degraded(monkeypatch):
    """LOW T17: assert the PRESERVED safe verdict explicitly (equal to a
    fresh mode="safe" call for the same email, not merely "some string"),
    and assert the response body leaks no "boom" / traceback / exception
    text / internal MX host (same body-leak discipline as T7)."""
    import apps.api.smtp_probe as smtp_probe_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(smtp_probe_mod, "run_full_probe", _boom)

    domain = "t17.example.test"
    mx_host = "mx.t17.example.test"
    resolver = public_resolver(domain, mx_host)
    _wire_safe_dns(monkeypatch, resolver)

    user = make_user("t17")
    client = auth_client(user)
    resp = client.post("/api/v1/verify", {"email": f"x@{domain}", "mode": "full"}, format="json")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["smtp"]["probe"] == "failed"

    resp_safe = client.post("/api/v1/verify", {"email": f"x@{domain}", "mode": "safe"}, format="json")
    assert resp_safe.status_code == 200
    assert data["deliverable"] == resp_safe.json()["data"]["deliverable"]
    # A failed probe must never dishonestly claim "syntax+dns+smtp": the
    # checked string must match the honest safe-mode value byte-for-byte.
    assert data["checked"] == resp_safe.json()["data"]["checked"]

    body = resp.content.decode()
    for leaked in ("boom", "Boom", "Traceback", "traceback", "RuntimeError", mx_host):
        assert leaked not in body


# --- T18: DATA never sent -----------------------------------------------


def test_t18_data_never_sent_structural(monkeypatch):
    """FakeSmtp deliberately has no .data() method — any accidental call
    from smtp_probe would raise AttributeError, which is NOT caught inside
    _run_session, proving there is no code path that reaches it. As a second,
    static proof: walk the module's AST and assert no call site invokes a
    `.data(...)` method anywhere in the actual code (comments/docstrings
    don't count).

    LOW T18 fixup: also pin the expected COMPLETED verdict, not just "no
    AttributeError happened" — an indirect DATA attempt that got silently
    converted into probe="failed" somewhere upstream must not pass this test
    silently just because no exception propagated.
    """
    import ast
    import inspect

    import apps.api.smtp_probe as smtp_probe_mod

    tree = ast.parse(inspect.getsource(smtp_probe_mod))
    data_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "data"
    ]
    assert data_calls == []

    client, domain, resolver = _full_client_and_domain("t18")
    connect = fake_connect_factory(rcpt_codes=[250, 550])
    resp = _post_full(client, f"hello@{domain}", resolver, connect, monkeypatch)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["smtp"]["probe"] == "completed"
    assert data["deliverable"] == "deliverable"
    smtp_instance = connect.created[0]
    assert not hasattr(smtp_instance, "data")
