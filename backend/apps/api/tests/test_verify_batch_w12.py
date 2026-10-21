"Public API."

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test.client import BOUNDARY, encode_multipart
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import (
    AccountPlan,
    ApiKey,
    RateLimitBucket,
    WaitCapacity,
)
from apps.accounts.services import _bucket_hash, create_api_key

from .test_verify_full_w7 import (
    fake_connect_factory,
    make_user,
    never_called_connect,
    public_resolver,
)

pytestmark = pytest.mark.django_db(transaction=True)


def auth_client(user, *, label="w12-test"):
    _, token = create_api_key(user, label)
    client = APIClient(raise_request_exception=True)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def _wire_safe_dns(monkeypatch, resolver):
    import apps.api.verification as verification_mod

    def _delegate(self, qname, rdtype, *a, **kw):
        return resolver.resolve(str(qname).rstrip("."), rdtype)

    monkeypatch.setattr(verification_mod.dns.resolver.Resolver, "resolve", _delegate)


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    import dns.resolver

    import apps.api.verification as verification_mod

    def _default_deny(self, qname, rdtype, *a, **kw):
        raise dns.resolver.NXDOMAIN()

    monkeypatch.setattr(verification_mod.dns.resolver.Resolver, "resolve", _default_deny)


def _bucket_count(limit_name: str, subject: str) -> int:
    """Read the exact accounting count for a rate-limit bucket. Returns 0
    when no row exists yet (fresh window)."""
    h = _bucket_hash(limit_name, subject)
    row = RateLimitBucket.objects.filter(bucket_hash=h).first()
    return row.count if row else 0


def _capacity_count(key: str) -> int:
    row = WaitCapacity.objects.filter(key=key).first()
    return row.count if row else 0


# --- 1: mixed list, input order, invalid row is a row not a 422 -------------


def test_1_mixed_list_200_input_order_no_whole_batch_422():
    user = make_user("w12t1")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/verify/batch",
        {"emails": ["ok@example.test", "not-an-email", "hi@mailinator.com", "admin@example.test"]},
        format="json",
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["mode"] == "safe"
    assert [r["email"] for r in data["results"]] == [
        "ok@example.test", "not-an-email", "hi@mailinator.com", "admin@example.test",
    ]
    assert data["results"][1]["syntax_valid"] is False
    assert data["results"][1]["deliverable"] == "undeliverable"
    assert data["summary"]["total"] == 4

    # M3 positive control: at least one VALID row must actually be marked
    # valid with a proper row-level result — otherwise an impl that marks
    # EVERY row invalid (including the valid ones) would still pass the
    # negative assertion above undetected.
    valid_rows = [r for r in data["results"] if r["email"] in ("ok@example.test", "admin@example.test")]
    assert len(valid_rows) == 2
    for row in valid_rows:
        assert row["syntax_valid"] is True
        assert row["domain"] == "example.test"
        assert row["checked"] == "syntax+dns (no smtp probe)"


# --- 2: paste blob tokenization + dedupe ------------------------------------


def test_2_paste_blob_tokenized_deduped_order_preserving():
    user = make_user("w12t2")
    client = auth_client(user)
    blob = "a@example.test, b@example.test;c@example.test\na@example.test  b@example.test"
    resp = client.post("/api/v1/verify/batch", {"text": blob}, format="json")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [r["email"] for r in data["results"]] == ["a@example.test", "b@example.test", "c@example.test"]
    assert data["summary"]["total"] == 3


# --- 3: caps + empty + both-fields ------------------------------------------


def test_3_over_cap_422_at_cap_200_empty_422_both_fields_422(settings):
    settings.VERIFY_BATCH_MAX_SAFE = 100
    user = make_user("w12t3")
    client = auth_client(user)

    over = [f"n{i}@example.test" for i in range(101)]
    resp = client.post("/api/v1/verify/batch", {"emails": over}, format="json")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "batch_too_large"

    at_cap = [f"n{i}@example.test" for i in range(100)]
    resp = client.post("/api/v1/verify/batch", {"emails": at_cap}, format="json")
    assert resp.status_code == 200
    assert resp.json()["data"]["summary"]["total"] == 100

    resp = client.post("/api/v1/verify/batch", {"emails": []}, format="json")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"

    resp = client.post("/api/v1/verify/batch", {"emails": ["a@example.test"], "text": "b@example.test"}, format="json")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


# --- 4: mode typo 422, row shape byte-identical to single -------------------


def test_4_mode_typo_422_and_row_shape_matches_single():
    user = make_user("w12t4")
    client = auth_client(user)
    resp = client.post("/api/v1/verify/batch", {"emails": ["x@example.test"], "mode": "fulll"}, format="json")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"

    resp_batch = client.post("/api/v1/verify/batch", {"emails": ["x@example.test"]}, format="json")
    resp_single = client.post("/api/v1/verify", {"email": "x@example.test"}, format="json")
    assert resp_batch.json()["data"]["results"][0] == resp_single.json()["data"]


# --- 5: rate charge equivalence — H1: exact bucket-count accounting oracle --


def test_5_batch_charges_exactly_n_and_10x100_equals_1000_equivalence(monkeypatch):
    import apps.accounts.services as services_mod

    monkeypatch.setattr(services_mod, "VERIFY_KEY_RATE_LIMIT", 100000)
    monkeypatch.setattr(services_mod, "VERIFY_ACCOUNT_RATE_LIMIT", 1000)
    user = make_user("w12t5")
    client = auth_client(user)
    key_id = str(ApiKey.objects.get(user=user).id)

    for i in range(10):
        emails = [f"b{i}_{j}@example.test" for j in range(100)]
        resp = client.post("/api/v1/verify/batch", {"emails": emails}, format="json")
        assert resp.status_code == 200, resp.content

    # H1: the exact-count read is the required accounting oracle — not just
    # "the batches all returned 200". Ten batches of 100 MUST have charged
    # the account bucket to precisely 1000, and the key bucket by the same
    # amount (key limit here is high, but the bucket must still reflect the
    # real charge, not be a no-op).
    assert _bucket_count("api:verify:account:hour", str(user.pk)) == 1000
    assert _bucket_count("api:verify:key:hour", key_id) == 1000

    resp = client.post("/api/v1/verify", {"email": "overflow@example.test"}, format="json")
    assert resp.status_code == 429


# --- 6: over-budget  whole, unchanged, then equal-to-remaining OK --


def test_6_over_budget_429_unchanged_then_remaining_succeeds(monkeypatch):
    import apps.accounts.services as services_mod

    monkeypatch.setattr(services_mod, "VERIFY_ACCOUNT_RATE_LIMIT", 100)
    monkeypatch.setattr(services_mod, "VERIFY_KEY_RATE_LIMIT", 100000)
    user = make_user("w12t6")
    client = auth_client(user)
    key_id = str(ApiKey.objects.get(user=user).id)

    first = [f"a{i}@example.test" for i in range(99)]
    resp = client.post("/api/v1/verify/batch", {"emails": first}, format="json")
    assert resp.status_code == 200

    # H2: read the bucket count BEFORE the refused batch, then assert it is
    # UNCHANGED after — not merely that a later batch happens to succeed.
    before_account = _bucket_count("api:verify:account:hour", str(user.pk))
    before_key = _bucket_count("api:verify:key:hour", key_id)
    assert before_account == 99

    over = [f"c{i}@example.test" for i in range(2)]
    resp = client.post("/api/v1/verify/batch", {"emails": over}, format="json")
    assert resp.status_code == 429
    assert "Retry-After" in resp

    after_account = _bucket_count("api:verify:account:hour", str(user.pk))
    after_key = _bucket_count("api:verify:key:hour", key_id)
    assert after_account == before_account, "refused batch must consume NOTHING from the account bucket"
    assert after_key == before_key, "refused batch must consume NOTHING from the key bucket either"

    # Bonus: equal-to-remaining-budget batch still succeeds.
    exact = ["last@example.test"]
    resp = client.post("/api/v1/verify/batch", {"emails": exact}, format="json")
    assert resp.status_code == 200
    assert _bucket_count("api:verify:account:hour", str(user.pk)) == 100


# --- 7: key charged / account refused -> both rolled back -------------------


def test_7_key_charged_account_refused_both_rolled_back(monkeypatch):
    import apps.accounts.services as services_mod

    # H3: key limit is huge relative to account limit so a leaked key charge
    # is invisible via "does a later batch fit" alone — read BOTH bucket
    # counts explicitly to make the rollback assertion direct, not inferred.
    monkeypatch.setattr(services_mod, "VERIFY_KEY_RATE_LIMIT", 100000)
    monkeypatch.setattr(services_mod, "VERIFY_ACCOUNT_RATE_LIMIT", 5)
    user = make_user("w12t7")
    client = auth_client(user)
    key_id = str(ApiKey.objects.get(user=user).id)

    # Prime the account bucket to a LIVE (non-fresh) window via single
    # /verify — it shares the exact same bucket row (limit_name + subject)
    # as the batch's account charge, so this establishes count=1 in a live
    # window rather than letting the batch's own charge hit the "fresh
    # window always succeeds" first-call path (design §3a mirrors
    # increment_rate_limit's own idiom there, same as this file's charge_
    # rate_limit docstring — that path is unconditional on a truly fresh
    # bucket, by design, matching increment_rate_limit).
    resp = client.post("/api/v1/verify", {"email": "prime@example.test"}, format="json")
    assert resp.status_code == 200

    before_account = _bucket_count("api:verify:account:hour", str(user.pk))
    before_key = _bucket_count("api:verify:key:hour", key_id)
    assert before_account == 1  # primed by the single /verify above (limit=5)

    emails = [f"z{i}@example.test" for i in range(10)]
    resp = client.post("/api/v1/verify/batch", {"emails": emails}, format="json")
    assert resp.status_code == 429

    # Explicit rollback assertion: BOTH buckets are byte-identical to their
    # pre-call values — the key charge (which would easily fit under a
    # 100000 limit) was rolled back together with the account refusal, not
    # silently left charged and merely invisible against a huge limit.
    after_account = _bucket_count("api:verify:account:hour", str(user.pk))
    after_key = _bucket_count("api:verify:key:hour", key_id)
    assert after_account == before_account, "account bucket must be unchanged after the 429"
    assert after_key == before_key, "key bucket must be rolled back too — no leaked key charge"

    # Key bucket must have rolled back too — a batch of 4 must still fit
    # against the account bucket's remaining budget (limit 5, used 1).
    ok = [f"y{i}@example.test" for i in range(4)]
    resp = client.post("/api/v1/verify/batch", {"emails": ok}, format="json")
    assert resp.status_code == 200
    assert _bucket_count("api:verify:account:hour", str(user.pk)) == 5
    assert _bucket_count("api:verify:key:hour", key_id) == 5


# --- 8: one safe-capacity slot for the whole batch (H4 positive control) ----


def test_8_one_safe_capacity_slot_for_whole_batch(settings):
    from apps.accounts.services import VERIFY_CAPACITY_KEY

    settings.VERIFY_KEY_RATE_LIMIT = 100000
    settings.VERIFY_ACCOUNT_RATE_LIMIT = 100000
    WaitCapacity.objects.update_or_create(
        key=VERIFY_CAPACITY_KEY,
        defaults={"count": 8, "expires_at": timezone.now() + timezone.timedelta(seconds=30)},
    )
    user = make_user("w12t8")
    client = auth_client(user)
    resp = client.post("/api/v1/verify/batch", {"emails": ["a@example.test", "b@example.test"]}, format="json")
    assert resp.status_code == 429


def test_8b_one_safe_capacity_slot_positive_control_multi_row_takes_one_slot(settings):
    """H4: the negative case above (pool full at 8/8) is vacuous — a 1-slot
    impl and an N-slot impl both return 429 there. Positive control: leave
    exactly ONE slot free (7/8 occupied), send a MULTI-ROW batch, and assert
    it SUCCEEDS — proving the whole batch consumes ONE capacity slot, not N.
    Also assert the slot is released back to its pre-batch value afterwards
    (finally-release, no leak)."""
    from apps.accounts.services import VERIFY_CAPACITY_KEY

    settings.VERIFY_KEY_RATE_LIMIT = 100000
    settings.VERIFY_ACCOUNT_RATE_LIMIT = 100000
    WaitCapacity.objects.update_or_create(
        key=VERIFY_CAPACITY_KEY,
        defaults={"count": 7, "expires_at": timezone.now() + timezone.timedelta(seconds=30)},
    )
    user = make_user("w12t8b")
    client = auth_client(user)

    pre_count = _capacity_count(VERIFY_CAPACITY_KEY)
    assert pre_count == 7

    emails = [f"row{i}@example.test" for i in range(5)]
    resp = client.post("/api/v1/verify/batch", {"emails": emails}, format="json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["data"]["summary"]["total"] == 5

    # An N-slot-per-row implementation would have exhausted the 8-slot pool
    # partway through 5 rows on top of the 7 already occupied and 429'd; a
    # correct 1-slot-per-batch implementation succeeds AND releases back to
    # exactly the pre-batch value once the loop is done.
    post_count = _capacity_count(VERIFY_CAPACITY_KEY)
    assert post_count == pre_count, "capacity slot must be released back to pre-batch value in finally"


# --- 9: file happy path + csv header row ------------------------------------


def test_9_txt_and_csv_happy_path_header_becomes_invalid_row():
    user = make_user("w12t9")
    client = auth_client(user)

    txt = SimpleUploadedFile("list.txt", b"a@example.test\nb@example.test", content_type="text/plain")
    resp = client.post("/api/v1/verify/batch", {"file": txt}, format="multipart")
    assert resp.status_code == 200
    assert resp.json()["data"]["summary"]["total"] == 2

    csv_bytes = b'email\n"c@example.test"\n"d@example.test"\n'
    csv = SimpleUploadedFile("list.csv", csv_bytes, content_type="text/csv")
    resp = client.post("/api/v1/verify/batch", {"file": csv}, format="multipart")
    assert resp.status_code == 200
    rows = resp.json()["data"]["results"]
    assert rows[0]["email"] == "email"
    assert rows[0]["syntax_valid"] is False


# --- 10: bad extension / two parts / binary -> 422, never 500 --------------


def test_10_bad_extension_two_parts_binary_never_500():
    user = make_user("w12t10")
    client = auth_client(user)

    bad_ext = SimpleUploadedFile("list.exe", b"a@example.test", content_type="application/octet-stream")
    resp = client.post("/api/v1/verify/batch", {"file": bad_ext}, format="multipart")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "unsupported_file_type"

    from django.test import Client as DjangoTestClient

    _, token = create_api_key(make_user("w12t10b"), "x")
    raw_client = DjangoTestClient()
    part1 = SimpleUploadedFile("a.txt", b"a@example.test", content_type="text/plain")
    part2 = SimpleUploadedFile("b.txt", b"b@example.test", content_type="text/plain")
    resp = raw_client.post(
        "/api/v1/verify/batch",
        {"file": [part1, part2]},
        format="multipart",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"

    binary = SimpleUploadedFile("list.txt", b"\xff\xfe\x00\x01binary", content_type="text/plain")
    resp = client.post("/api/v1/verify/batch", {"file": binary}, format="multipart")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "malformed_file"

    nul = SimpleUploadedFile("list.txt", b"a@example.test\x00b@example.test", content_type="text/plain")
    resp = client.post("/api/v1/verify/batch", {"file": nul}, format="multipart")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "malformed_file"


# --- 11: file too large (real + lying Content-Length) + in-memory invariant -


def test_11_file_too_large_real_canary_dir_empty(settings, tmp_path):
    """Real oversize upload (truthful Content-Length) — rejected by the
    pre-check, and the FILE_UPLOAD_TEMP_DIR canary stays empty (nothing
    ever spilled to disk). The lying-Content-Length variant (small/wrong
    header, genuinely oversized body — the case that actually exercises
    the POST-PARSE re-check) is test_11b below."""
    settings.VERIFY_BATCH_FILE_MAX_BYTES = 100
    settings.FILE_UPLOAD_TEMP_DIR = str(tmp_path)
    user = make_user("w12t11")
    client = auth_client(user)

    big = SimpleUploadedFile("list.txt", b"a@example.test\n" * 20, content_type="text/plain")
    resp = client.post("/api/v1/verify/batch", {"file": big}, format="multipart")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "file_too_large"
    assert list(tmp_path.iterdir()) == []


def test_11b_lying_small_content_length_genuinely_oversized_body_rejected_by_postparse_recheck(settings, tmp_path):
    """H5: a SMALL/lying Content-Length header (passes the pre-check) paired
    with a GENUINELY oversized multipart body must still be rejected — by
    the post-parse size re-check (uploaded.size > VERIFY_BATCH_FILE_MAX_BYTES)
    — since the header lied small. Uses RequestFactory directly against the
    view so the WSGI request's true byte stream (sized off the real body) is
    preserved while only request.META["CONTENT_LENGTH"] is overridden after
    construction, mirroring a proxy/client that sends a wrong header but a
    full body on the wire."""
    from django.test import RequestFactory

    from apps.api.views import VerifyBatchView

    settings.VERIFY_BATCH_FILE_MAX_BYTES = 65536
    settings.FILE_UPLOAD_TEMP_DIR = str(tmp_path)
    user = make_user("w12t11c")
    _, token = create_api_key(user, "z")

    genuinely_big = (b"a" * 600 + b"@example.test\n") * 200  # ~120KB, >> 64KB cap
    up = SimpleUploadedFile("list.txt", genuinely_big, content_type="text/plain")
    boundary = BOUNDARY
    body = encode_multipart(boundary, {"file": up})
    assert len(body) > settings.VERIFY_BATCH_FILE_MAX_BYTES

    rf = RequestFactory()
    req = rf.generic(
        "POST", "/api/v1/verify/batch",
        data=body,
        content_type=f"multipart/form-data; boundary={boundary}",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    # Truthful at construction (so the WSGI LimitedStream is sized to the
    # REAL body length and every byte remains readable) — then lie SMALL,
    # under the cap, so the pre-check passes and the parser actually reads
    # the full oversized body, forcing the post-parse re-check to catch it.
    req.META["CONTENT_LENGTH"] = "100"

    resp = VerifyBatchView.as_view()(req)
    assert resp.status_code == 422
    assert resp.data["error"]["code"] == "file_too_large"
    assert list(tmp_path.iterdir()) == []


def test_11c_accepted_maximal_upload_buffered_in_memory_canary_dir_empty(settings, tmp_path):
    "Public API."
    settings.FILE_UPLOAD_TEMP_DIR = str(tmp_path)
    max_bytes = settings.VERIFY_BATCH_FILE_MAX_BYTES  # 65536 by default
    n_unique = 100
    settings.VERIFY_BATCH_MAX_SAFE = max(settings.VERIFY_BATCH_MAX_SAFE, n_unique)

    user = make_user("w12t11d")
    client = auth_client(user)

    # 100 distinct, valid, padded-local-part email lines (267 bytes each);
    # cycle through them, repeating (duplicate) lines, until the raw body
    # crosses 64000 bytes — genuinely near the 65536-byte cap — while the
    # tokenizer's exact-match dedupe still collapses the body back down to
    # exactly 100 unique rows (well within VERIFY_BATCH_MAX_SAFE).
    pad_len = 250
    unique_lines = [("u" * pad_len + f"{i:03d}@example.test\n").encode() for i in range(n_unique)]
    assert len(unique_lines[0]) == 267

    content = bytearray()
    i = 0
    while len(content) < 64000:
        content += unique_lines[i % n_unique]
        i += 1
    content = bytes(content)

    assert len(content) >= 64000
    assert len(content) < max_bytes

    up = SimpleUploadedFile("list.txt", content, content_type="text/plain")
    resp = client.post("/api/v1/verify/batch", {"file": up}, format="multipart")
    assert resp.status_code == 200, resp.content
    assert resp.json()["data"]["summary"]["total"] == n_unique
    assert list(tmp_path.iterdir()) == [], "accepted near-maximal upload must never touch the temp-dir canary"


# --- 12: full mode tier gate + flag gate ------------------------------------


@pytest.mark.parametrize("plan", [AccountPlan.Plan.FREE, AccountPlan.Plan.PAID, AccountPlan.Plan.SCALE])
def test_12_tier_gate_403_whole_batch(plan, settings):
    settings.ENABLE_SMTP_VERIFY = True
    user = make_user(f"w12t12_{plan}", plan=plan)
    client = auth_client(user)
    resp = client.post("/api/v1/verify/batch", {"emails": ["x@example.test"], "mode": "full"}, format="json")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "verify_full_entitlement_required"


def test_12_flag_off_403_whole_batch(settings):
    settings.ENABLE_SMTP_VERIFY = False
    user = make_user("w12t12flag")
    client = auth_client(user)
    resp = client.post("/api/v1/verify/batch", {"emails": ["x@example.test"], "mode": "full"}, format="json")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "smtp_verify_disabled"


# --- 13: full charges by N (H6), per-row fleet-cap + domain cooldown -------


def test_13_full_charges_by_n_and_domain_cooldown_per_row(settings, monkeypatch):
    settings.ENABLE_SMTP_VERIFY = True
    settings.VERIFY_FULL_KEY_RATE_LIMIT = 1000
    settings.VERIFY_FULL_ACCOUNT_RATE_LIMIT = 1000
    settings.VERIFY_KEY_RATE_LIMIT = 1000
    settings.VERIFY_ACCOUNT_RATE_LIMIT = 1000
    settings.VERIFY_FULL_PER_DOMAIN_LIMIT = 1
    settings.VERIFY_BATCH_MAX_FULL = 25

    domain = "w12fulldomain.example.test"
    mx_host = f"mx.{domain}"
    resolver = public_resolver(domain, mx_host)
    connect = fake_connect_factory(rcpt_codes=[250, 550] * 4)

    import apps.api.smtp_probe as smtp_probe_mod

    monkeypatch.setattr(smtp_probe_mod, "_default_mx_resolver", lambda lifetime: resolver)
    monkeypatch.setattr(smtp_probe_mod, "_default_connect", connect)
    _wire_safe_dns(monkeypatch, resolver)

    user = make_user("w12t13")
    client = auth_client(user)
    key_id = str(ApiKey.objects.get(user=user).id)

    # H6: read BOTH the full-mode key and account bucket counts before, so
    # the post-call delta can be asserted exactly — without this, full-rate
    # accounting could be removed entirely and only the SMTP
    # completion/cooldown assertions below would still catch it (they
    # don't).
    before_key_full = _bucket_count("api:verify-full:key:hour", key_id)
    before_account_full = _bucket_count("api:verify-full:account:hour", str(user.pk))
    assert before_key_full == 0
    assert before_account_full == 0

    emails = [f"one@{domain}", f"two@{domain}"]
    resp = client.post("/api/v1/verify/batch", {"emails": emails, "mode": "full"}, format="json")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["results"][0]["smtp"]["probe"] == "completed"
    assert data["results"][1]["smtp"]["probe"] == "skipped"
    assert data["results"][1]["smtp"]["reason"] == "domain_cooldown"

    after_key_full = _bucket_count("api:verify-full:key:hour", key_id)
    after_account_full = _bucket_count("api:verify-full:account:hour", str(user.pk))
    assert after_key_full - before_key_full == len(emails), "full-mode key bucket must charge exactly N"
    assert after_account_full - before_account_full == len(emails), "full-mode account bucket must charge exactly N"


# --- H7: full-mode batch cap boundary (25 -> 200, 26 -> 422) ---------------


def test_13b_full_cap_boundary_25_ok_26_batch_too_large(settings, monkeypatch):
    """H7: VERIFY_BATCH_MAX_FULL=25 is a real boundary — merely setting the
    cap and sending 2 rows (as test 13 does) never exercises it. Assert the
    boundary directly: exactly 25 full-mode rows succeed, 26 is refused
    whole-batch with batch_too_large, mirroring test 3's safe-mode cap
    boundary coverage."""
    settings.ENABLE_SMTP_VERIFY = True
    settings.VERIFY_BATCH_MAX_FULL = 25
    settings.VERIFY_FULL_KEY_RATE_LIMIT = 1000
    settings.VERIFY_FULL_ACCOUNT_RATE_LIMIT = 1000
    settings.VERIFY_KEY_RATE_LIMIT = 1000
    settings.VERIFY_ACCOUNT_RATE_LIMIT = 1000
    # Distinct domains per row avoid the per-domain cooldown (limit=10
    # default) confusing the cap-boundary assertion with cooldown skips.
    settings.VERIFY_FULL_PER_DOMAIN_LIMIT = 1000

    import apps.api.smtp_probe as smtp_probe_mod

    # No real MX/SMTP needed for the boundary itself: every row here has no
    # MX record (NXDOMAIN via the default-deny DNS fixture), so verify_one_
    # email degrades to "no mail-accepting DNS records" without ever
    # reaching FakeSmtp — the cap check (len(tokens) > cap) happens BEFORE
    # any per-row work, so this is a legitimate, network-free way to probe
    # the boundary.
    monkeypatch.setattr(smtp_probe_mod, "_default_connect", never_called_connect)

    user = make_user("w12t13b")
    client = auth_client(user)

    at_cap = [f"row{i}@w12fullcap.example.test" for i in range(25)]
    resp = client.post("/api/v1/verify/batch", {"emails": at_cap, "mode": "full"}, format="json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["data"]["summary"]["total"] == 25

    over_cap = [f"row{i}@w12fullcap.example.test" for i in range(26)]
    resp = client.post("/api/v1/verify/batch", {"emails": over_cap, "mode": "full"}, format="json")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "batch_too_large"


# --- 14: probe raising -> row probe=failed, batch still 200 -----------------


def test_14_probe_raising_row_failed_batch_still_200(settings, monkeypatch):
    settings.ENABLE_SMTP_VERIFY = True
    import apps.api.smtp_probe as smtp_probe_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(smtp_probe_mod, "run_full_probe", _boom)

    domain = "w12probeboom.example.test"
    mx_host = f"mx.{domain}"
    resolver = public_resolver(domain, mx_host)
    _wire_safe_dns(monkeypatch, resolver)

    user = make_user("w12t14")
    client = auth_client(user)
    resp = client.post("/api/v1/verify/batch", {"emails": [f"x@{domain}"], "mode": "full"}, format="json")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["results"][0]["smtp"]["probe"] == "failed"


# --- 15: meter += N (batch) / += 1 (single), isolation, usage_summary ------


def test_15_meter_increments_and_isolation_and_usage_summary():
    from apps.accounts.entitlements import usage_summary

    user_a = make_user("w12t15a")
    user_b = make_user("w12t15b")
    client_a = auth_client(user_a)

    # M1: read the counter AFTER the batch, and separately AFTER the single,
    # so a mutant like batch+=3/single+=0 (or batch+=0/single+=1 alone) is
    # caught by the per-step deltas — not just the combined final total.
    summary_before = usage_summary(user_a)
    assert summary_before["verifications_used"] == 0

    resp = client_a.post("/api/v1/verify/batch", {"emails": ["a@example.test", "b@example.test"]}, format="json")
    assert resp.status_code == 200
    summary_after_batch = usage_summary(user_a)
    assert summary_after_batch["verifications_used"] == 2, "batch of 2 must meter += 2 (N), not += 1"

    resp = client_a.post("/api/v1/verify", {"email": "single@example.test"}, format="json")
    assert resp.status_code == 200
    summary_after_single = usage_summary(user_a)
    assert summary_after_single["verifications_used"] == 3, "single verify must meter += 1 on top of the batch's 2"

    summary_b = usage_summary(user_b)
    assert summary_b["verifications_used"] == 0


def test_15_meter_db_error_logged_response_still_200(monkeypatch):
    """M2: prove the injected failure was actually CALLED (spy/invocation
    assertion) while the response still returns 200 — otherwise a response
    path that skips metering entirely (never calls _upsert_counter at all)
    would pass this test identically to one that calls it and swallows the
    error."""
    import apps.accounts.entitlements as entitlements_mod

    calls = []

    def _boom(*a, **kw):
        calls.append((a, kw))
        raise RuntimeError("db exploded")

    monkeypatch.setattr(entitlements_mod, "_upsert_counter", _boom)

    user = make_user("w12t15c")
    client = auth_client(user)
    resp = client.post("/api/v1/verify/batch", {"emails": ["a@example.test"]}, format="json")
    assert resp.status_code == 200
    # _upsert_counter is shared across counters (api_requests, verifications_used,
    # ...); isolate the verifications_used invocation specifically so this
    # doesn't get accidentally satisfied by the unrelated per-request api_requests
    # counter call.
    verification_calls = [c for c in calls if c[0][2] == "verifications_used"]
    assert len(verification_calls) == 1, "record_verifications must have actually invoked _upsert_counter (and swallowed its error)"
    assert verification_calls[0][1].get("amount", 1) == 1


# --- 16: deadline -> later rows unknown/batch_budget_exhausted, charge N ----


def test_16_deadline_exhausted_rows_unknown_charge_still_n(settings):
    from apps.accounts.entitlements import usage_summary

    # A deadline that has ALREADY elapsed by the time the loop starts is the
    # simplest deterministic way to exercise "rows past the deadline degrade
    # honestly" without a fragile monotonic()-call-count patch (verify_one_
    # email/DNS/etc. call time.monotonic() internally too, so patching the
    # global clock non-deterministically shifts which call is "the" deadline
    # check). Every row lands past-deadline — that still proves the
    # mechanism: unknown/batch_budget_exhausted/"skipped (batch budget)",
    # AND that the charge/meter still count all N (no refund race).
    settings.VERIFY_BATCH_DEADLINE_SECONDS = -1
    user = make_user("w12t16")
    client = auth_client(user)
    resp = client.post(
        "/api/v1/verify/batch",
        {"emails": ["a@example.test", "b@example.test", "c@example.test"]},
        format="json",
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    exhausted = [r for r in data["results"] if r["reason"] == "batch_budget_exhausted"]
    assert len(exhausted) == 3
    for row in exhausted:
        assert row["deliverable"] == "unknown"
        assert row["checked"] == "skipped (batch budget)"
    assert data["summary"]["total"] == 3
    assert data["summary"]["unknown"] == 3

    # Charge STANDS + meter counts N even though every row was skipped.
    assert usage_summary(user)["verifications_used"] == 3
    assert _bucket_count("api:verify:account:hour", str(user.pk)) == 3
