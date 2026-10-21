"Public API."
import re
from datetime import timedelta
from io import StringIO
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan
from apps.feedback import alerts
from apps.feedback.models import FeedbackAlertBreaker, FeedbackReport
from apps.ops.testing import fake_telegram

pytestmark = pytest.mark.django_db

OPS = "ops@example.test"
PUBLIC = "/feedback/"
APP = "/app/feedback/"
UA = "Mozilla/5.0 SecretBrowser/9.9"
API_KEY = "ms_live_AbCdEf0123456789_-xyzXYZ0123456789abcdefghij"


def make_user(name="fbuser", *, plan=AccountPlan.Plan.PAID, staff=False):
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.test", password="pw-123456"
    )
    if staff:
        user.is_staff = True
        user.save()
    Account.objects.create(user=user, email_normalized=f"{name}@example.test", email_verified_at=timezone.now())
    AccountPlan.objects.update_or_create(user=user, defaults={"plan": plan})
    return user


def signed_in(user):
    client = Client()
    client.force_login(user)
    return client


def post(client, url=PUBLIC, ip="203.0.113.7", **data):
    payload = {"message": "The wait endpoint returned 500.", "email": "dev@example.org"}
    payload.update(data)
    return client.post(url, payload, REMOTE_ADDR=ip, HTTP_USER_AGENT=UA)


# --- form basics ----------------------------------------------------------------


def test_anonymous_requires_email():
    response = post(Client(), email="")
    assert response.status_code == 400
    assert "We need your email to reply." in response.content.decode()
    assert FeedbackReport.objects.count() == 0


def test_signed_in_does_not_require_email():
    user = make_user()
    with override_settings(OPS_ALERT_EMAIL=OPS):
        response = post(signed_in(user), url=APP, email="")
    assert "Reply to: fbuser@example.test (account email)" in mail.outbox[0].body
    assert response.status_code == 200
    report = FeedbackReport.objects.get()
    assert report.user == user and report.email == "" and report.plan == "paid"


def test_anonymous_success_page_shows_reference_and_stores_safe_fields():
    response = post(Client(), page_path="/docs/api/?token=abc#x", request_id="req_AbC-_0123456789abcdE")
    html = response.content.decode()
    report = FeedbackReport.objects.get()
    assert response.status_code == 200
    assert f"Received &mdash; reference #{report.pk}." in html or f"reference #{report.pk}" in html
    assert "We reply by email within 1 business day." in html
    assert report.page_path == "/docs/api/"  # query + fragment dropped
    assert report.request_id == "req_AbC-_0123456789abcdE"
    assert report.user is None and report.plan == ""
    assert report.ip_hash and "203.0.113.7" not in report.ip_hash and len(report.ip_hash) == 64
    assert report.user_agent == UA
    assert report.status == FeedbackReport.Status.OPEN


def test_user_agent_truncated_to_300():
    Client().post(PUBLIC, {"message": "x", "email": "a@b.example"}, HTTP_USER_AGENT="A" * 1000)
    assert len(FeedbackReport.objects.get().user_agent) == 300


def test_no_confirmation_email_to_customer():
    """: no outbound mail to a typed address (backscatter)."""
    with override_settings(OPS_ALERT_EMAIL=""):
        post(Client(), email="victim@example.org")
    assert FeedbackReport.objects.count() == 1
    assert all("victim@example.org" not in m.to for m in mail.outbox)
    assert mail.outbox == []


def test_2000_char_limit_enforced_server_side_and_stated_in_ui():
    page = Client().get(PUBLIC).content.decode()
    assert 'maxlength="2000"' in page and "Up to 2,000 characters" in page
    assert post(Client(), message="a" * 2000, ip="198.51.100.1").status_code == 200
    response = post(Client(), message="a" * 2001, ip="198.51.100.2")
    assert response.status_code == 400
    assert "Keep it under 2,000 characters." in response.content.decode()
    assert FeedbackReport.objects.count() == 1


def test_crlf_newlines_count_once_like_the_browser():
    body = ("a" * 9 + "\r\n") * 199 + "a" * 10  # 2000 chars as the textarea counts them
    assert post(Client(), message=body).status_code == 200
    assert len(FeedbackReport.objects.get().message) == 2000


def test_honeypot_drops_silently():
    with override_settings(OPS_ALERT_EMAIL=OPS), fake_telegram() as tg:
        response = post(Client(), website="http://spam.example")
    assert response.status_code == 200
    assert "We reply by email within 1 business day." in response.content.decode()
    assert FeedbackReport.objects.count() == 0
    assert tg.calls == [] and mail.outbox == []


# --- rate limits (before the DB write) ---------------------------------------------


def test_ip_rate_limit_returns_friendly_429_before_db_write():
    client = Client()
    for _ in range(5):
        assert post(client, message="hello").status_code == 200
    with mock.patch.object(FeedbackReport.objects, "create", wraps=FeedbackReport.objects.create) as create:
        response = post(client, message="sixth report text")
    assert response.status_code == 429
    html = response.content.decode()
    assert "several reports in the last hour" in html
    assert "sixth report text" in html  # their text is kept in the form
    create.assert_not_called()
    assert FeedbackReport.objects.count() == 5
    # A different IP is unaffected.
    assert post(client, ip="203.0.113.99").status_code == 200


def test_account_rate_limit_is_per_account_not_per_ip():
    user = make_user()
    client = signed_in(user)
    for i in range(10):
        assert post(client, url=APP, ip=f"192.0.2.{i}").status_code == 200
    response = post(client, url=APP, ip="192.0.2.200")
    assert response.status_code == 429
    assert FeedbackReport.objects.count() == 10


# --- ?from= / ?rid= prefill -------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example/x",
        "//evil.example/x",
        "/\\evil.example",
        "javascript:alert(1)",
        "evil.example/path",
        "/%2F%2Fevil.example",
    ],
)
def test_from_rejects_absolute_and_protocol_relative(value):
    html = Client().get(PUBLIC, {"from": value}).content.decode()
    field = re.search(r'<input id="fb-page"[^>]*>', html).group(0)
    assert 'value=""' in field
    response = post(Client(), page_path=value)
    assert response.status_code == 400
    assert FeedbackReport.objects.count() == 0


def test_from_prefills_path_only():
    html = Client().get(PUBLIC, {"from": "/app/inboxes/?q=secret&x=1"}).content.decode()
    field = re.search(r'<input id="fb-page"[^>]*>', html).group(0)
    assert 'value="/app/inboxes/"' in field and "secret" not in html


@pytest.mark.parametrize("value", ["abc", "req_<script>", "x" * 65, "req_" + "a" * 19 + "!", "' onmouseover=1"])
def test_rid_rejects_invalid_ids(value):
    html = Client().get(PUBLIC, {"rid": value}).content.decode()
    field = re.search(r'<input id="fb-rid"[^>]*>', html).group(0)
    assert 'value=""' in field
    assert post(Client(), request_id=value).status_code == 400


@pytest.mark.parametrize("value", ["req_AbC-_0123456789abcdE", "abcdef12-3456-7890"])
def test_rid_accepts_generated_and_client_supplied_ids(value):
    html = Client().get(PUBLIC, {"rid": value}).content.decode()
    assert f'value="{value}"' in re.search(r'<input id="fb-rid"[^>]*>', html).group(0)


# --- notifications --------------------------------------------------------------------


def test_telegram_payload_carries_no_customer_data():
    user = make_user("secretuser")
    message = f"My key {API_KEY} broke and code 482913 too"
    with override_settings(OPS_ALERT_EMAIL=OPS), fake_telegram() as tg:
        response = post(
            signed_in(user),
            url=APP,
            message=message,
            email="replyme@example.org",
            page_path="/app/inboxes/?token=supersecret",
            request_id="req_AbC-_0123456789abcdE",
        )
    assert response.status_code == 200
    report = FeedbackReport.objects.get()
    assert len(tg.calls) == 1
    text = tg.texts[0]
    assert text.startswith(f"\u2139\ufe0f New feedback #{report.pk} \u00b7 plan paid \u00b7 page /app/inboxes/")
    assert text.endswith(f"/admin/feedback/feedbackreport/{report.pk}/change/")
    # Email (only) names the reply-to address; signed-in with no typed email
    # falls back to the account email.
    assert "Reply to: replyme@example.org" in mail.outbox[0].body
    for forbidden in (
        "My key", API_KEY, "482913", "broke", "replyme", "example.org", "secretuser",
        f"pk={user.pk}", "token=", "supersecret", "?", "SecretBrowser", "Mozilla", "req_AbC",
    ):
        assert forbidden not in text, forbidden


def test_telegram_page_label_never_carries_path_params_or_free_text():
    assert alerts.alert_page("/verify-email/sometoken123/") == "/verify-email/<str:token>/"
    assert alerts.alert_page("/app/messages/msg_secret/") == "/app/messages/<str:public_id>/"
    assert alerts.alert_page("/my-key-is-ms_live_abcdef123456") == "(unmatched path)"
    assert alerts.alert_page("/docs/api/") == "/docs/api/"
    assert alerts.alert_page("") == "-"


def test_anonymous_telegram_says_anonymous():
    with override_settings(OPS_ALERT_EMAIL=""), fake_telegram() as tg:
        post(Client(), page_path="")
    assert " \u00b7 plan anonymous \u00b7 page -" in tg.texts[0]


def test_email_body_redacts_tokens_and_otp():
    message = (
        f"Key {API_KEY} fails. Header Authorization: Bearer {API_KEY}x. "
        "Also ms_test_abcdefghijkl. Your code: 482913. 774411 is your verification code. "
        "Order 2024 shipped."
    )
    with override_settings(OPS_ALERT_EMAIL=OPS), fake_telegram():
        post(Client(), message=message, email="replyme@example.org")
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    report = FeedbackReport.objects.get()
    assert msg.subject == f"[mailsocket] New feedback #{report.pk} (plan anonymous)"
    for leaked in (API_KEY, "ms_test_abcdefghijkl", "482913", "774411"):
        assert leaked not in msg.body, leaked
    assert msg.body.count("[redacted]") >= 4
    assert "Reply to: replyme@example.org" in msg.body
    assert "Order 2024 shipped." in msg.body  # plain numbers survive
    # Full text stays in the DB.
    assert report.message == message


def test_scrub_unit():
    assert alerts.scrub(f"k={API_KEY}") == "k=[redacted]"
    assert alerts.scrub("bearer abc.def-ghi") == "[redacted]"
    assert alerts.scrub("OTP is 1234") == "OTP is [redacted]"
    assert alerts.scrub("pin 87654321 ok") == "pin [redacted] ok"
    assert alerts.scrub("123456 is your code") == "[redacted] is your code"
    assert alerts.scrub("call 555 1234 at 10:30") == "call 555 1234 at 10:30"


def test_notify_failure_still_returns_success():
    with override_settings(OPS_ALERT_EMAIL=""), fake_telegram(fail=OSError("boom")):
        response = post(Client())
    assert response.status_code == 200
    assert "reference #" in response.content.decode()
    assert FeedbackReport.objects.count() == 1
    # The failed alert is counted for the digest.
    assert FeedbackAlertBreaker.objects.get().suppressed == 1


def test_notify_exception_inside_alerts_still_returns_success():
    with mock.patch("apps.feedback.alerts.ops_notify.notify", side_effect=RuntimeError("x")), \
            override_settings(OPS_ALERT_EMAIL=OPS):
        response = post(Client())
    assert response.status_code == 200
    assert FeedbackReport.objects.count() == 1
    
    # sent, so the report is counted for the digest.
    row = FeedbackAlertBreaker.objects.get()
    assert row.sent == 1 and row.suppressed == 1


def test_notify_exception_is_counted_and_reported_by_digest():
    """r2: an exception escaping ops_notify.notify must increment
    ``suppressed`` (exactly once) and the next digest must report it."""
    now = timezone.now()
    report = FeedbackReport.objects.create(message="x")
    with override_settings(OPS_ALERT_EMAIL=""), fake_telegram() as tg:
        with mock.patch("apps.feedback.alerts.ops_notify.notify", side_effect=RuntimeError("x")):
            assert alerts.notify_new_report(report, now=now) == "error"
            assert alerts.notify_new_report(report, now=now) == "error"
        assert FeedbackAlertBreaker.objects.get().suppressed == 2
        assert tg.calls == []
        assert alerts.send_due_feedback_digest(now=now) == 1
    assert len(tg.calls) == 1
    assert "2 report(s) were not alerted individually" in tg.texts[0]
    assert FeedbackAlertBreaker.objects.get().suppressed == 0


def test_breaker_caps_alerts_and_status_probe_sends_one_digest():
    with override_settings(OPS_ALERT_EMAIL=""), fake_telegram() as tg:
        for i in range(11):
            assert post(Client(), ip=f"198.18.0.{i}").status_code == 200
        assert FeedbackReport.objects.count() == 11
        assert len(tg.calls) == 10  # no 11th per-report notify
        assert FeedbackAlertBreaker.objects.get().suppressed == 1
        with mock.patch(
            "apps.status.management.commands.status_probe.run_probe", side_effect=lambda c: (True, 1)
        ):
            out = StringIO()
            call_command("status_probe", stdout=out)
            assert "feedback_digest=1" in out.getvalue()
            assert len(tg.calls) == 11
            assert tg.texts[-1].startswith("\u2139\ufe0f 11 new feedback reports in the last hour")
            assert "1 report(s) were not alerted individually" in tg.texts[-1]
            # More suppressed reports within the hour: still only one digest.
            FeedbackAlertBreaker.objects.update(suppressed=3)
            out = StringIO()
            call_command("status_probe", stdout=out)
            assert "feedback_digest=0" in out.getvalue()
            assert len(tg.calls) == 11


def test_breaker_window_resets_after_an_hour():
    now = timezone.now()
    report = FeedbackReport.objects.create(message="x")
    with override_settings(OPS_ALERT_EMAIL=""), fake_telegram() as tg:
        for _ in range(10):
            alerts.notify_new_report(report, now=now)
        assert alerts.notify_new_report(report, now=now) == "suppressed"
        assert alerts.notify_new_report(report, now=now + timedelta(minutes=61)) != "suppressed"
    assert len(tg.calls) == 11


def test_digest_failure_is_retried_next_run():
    FeedbackAlertBreaker.objects.create(key=alerts.BREAKER_KEY, suppressed=2)
    with override_settings(OPS_ALERT_EMAIL=""), fake_telegram(fail=OSError("x")):
        assert alerts.send_due_feedback_digest() == 0
    assert FeedbackAlertBreaker.objects.get().suppressed == 2
    with override_settings(OPS_ALERT_EMAIL=""), fake_telegram() as tg:
        assert alerts.send_due_feedback_digest() == 1
    assert len(tg.calls) == 1
    assert FeedbackAlertBreaker.objects.get().suppressed == 0


def test_no_digest_without_suppressed_reports():
    with override_settings(OPS_ALERT_EMAIL=""), fake_telegram() as tg:
        post(Client())
        assert alerts.send_due_feedback_digest() == 0
    assert len(tg.calls) == 1


# --- staff fleet table ----------------------------------------------------------------


def test_fleet_open_feedback_staff_only_and_escaped():
    FeedbackReport.objects.create(message="<script>alert(1)</script> broken " + "z" * 200, page_path="/app/")
    FeedbackReport.objects.create(message="closed one", status=FeedbackReport.Status.CLOSED)
    staff = make_user("fleetstaff", staff=True)
    html = signed_in(staff).get("/app/fleet/").content.decode()
    section = html.split('id="open-feedback"', 1)[1].split("</section>", 1)[0]
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; broken" in section
    assert "z" * 200 not in section  # preview is capped at 120 chars
    assert "closed one" not in section
    assert "/admin/feedback/feedbackreport/" in section
    assert "anonymous" in section
    # Non-staff and anonymous never see it.
    customer = make_user("customer")
    assert signed_in(customer).get("/app/fleet/").status_code == 404
    assert Client().get("/app/fleet/").status_code == 404


def test_admin_shows_message_escaped():
    report = FeedbackReport.objects.create(message="<b>bold</b><script>x()</script>")
    admin = get_user_model().objects.create_superuser("fbadmin", "fbadmin@example.test", "pw")
    client = Client()
    client.force_login(admin)
    html = client.get(f"/admin/feedback/feedbackreport/{report.pk}/change/").content.decode()
    assert "<script>x()</script>" not in html and "&lt;script&gt;x()&lt;/script&gt;" in html
    listing = client.get("/admin/feedback/feedbackreport/?status__exact=open").content.decode()
    assert "&lt;b&gt;bold&lt;/b&gt;" in listing
    # Staff can move a report to answered.
    response = client.post(f"/admin/feedback/feedbackreport/{report.pk}/change/", {"status": "answered"})
    assert response.status_code == 302
    report.refresh_from_db()
    assert report.status == "answered" and report.message == "<b>bold</b><script>x()</script>"


# --- entry points, error pages, CSP -------------------------------------------------------


def test_app_page_requires_login_and_sidebar_link_is_static():
    assert Client().get(APP).status_code == 302
    user = make_user()
    html = signed_in(user).get("/app/inboxes/").content.decode()
    # Static href: dashboard pages must stay byte-identical whatever their URL.
    assert f'href="{APP}">Report a problem</a>' in html


def test_app_page_prefills_page_from_same_host_referer_only():
    client = signed_in(make_user())

    def page_field(url=APP, **extra):
        html = client.get(url, **extra).content.decode()
        return re.search(r'<input id="fb-page"[^>]*>', html).group(0)

    assert 'value="/app/inboxes/"' in page_field(HTTP_REFERER="http://testserver/app/inboxes/?q=secret")
    assert 'value=""' in page_field(HTTP_REFERER="https://evil.example/app/inboxes/")
    assert 'value=""' in page_field(HTTP_REFERER="http://testserver//evil.example/")
    # ?from= wins over the Referer.
    assert 'value="/app/usage/"' in page_field(APP + "?from=/app/usage/", HTTP_REFERER="http://testserver/app/inboxes/")


def test_strip_request_noise_only_strips_request_echo():
    from apps.feedback.testing import strip_request_noise

    a = Client().get("/no-such-a/", HTTP_X_REQUEST_ID="abcdef12-aaaa-0001").content
    b = Client().get("/no-such-b/", HTTP_X_REQUEST_ID="abcdef12-bbbb-0002").content
    assert a != b and strip_request_noise(a) == strip_request_noise(b)
    assert strip_request_noise(a.replace(b"Page not found", b"Forbidden!!!!!")) != strip_request_noise(b)


def test_footer_and_faq_link_public_form():
    for url in (reverse("dashboard:root"), reverse("dashboard:docs"), reverse("dashboard:faq")):
        html = Client().get(url).content.decode()
        footer = html.split('<footer class="site-footer">', 1)[1].split("</footer>", 1)[0]
        assert f'href="{PUBLIC}">Report a problem</a>' in footer
    faq = Client().get(reverse("dashboard:faq")).content.decode()
    assert "How do I report a problem?" in faq


def test_404_shows_request_id_and_report_link():
    response = Client().get("/no-such-page/", HTTP_X_REQUEST_ID="abcdef12-feed-0001")
    html = response.content.decode()
    assert response.status_code == 404
    assert "Reference: <code>abcdef12-feed-0001</code>" in html
    assert 'href="/feedback/?rid=abcdef12-feed-0001&amp;from=/no-such-page/"' in html


def test_403_shows_request_id_and_report_link():
    from django.test import RequestFactory
    from django.views.defaults import permission_denied

    request = RequestFactory().get("/app/secret/")
    request.request_id = "abcdef12-feed-0403"
    html = permission_denied(request, PermissionError()).content.decode()
    assert "Reference: <code>abcdef12-feed-0403</code>" in html
    assert 'href="/feedback/?rid=abcdef12-feed-0403&amp;from=/app/secret/"' in html


def test_500_shows_request_id_and_report_link_and_stays_self_contained():
    client = Client(raise_request_exception=False)
    with mock.patch("apps.dashboard.views._pricing_context", side_effect=RuntimeError("boom")):
        response = client.get(reverse("dashboard:pricing"), HTTP_X_REQUEST_ID="abcdef12-feed-0500")
    html = response.content.decode()
    assert response.status_code == 500
    assert "Reference: <code>abcdef12-feed-0500</code>" in html
    assert 'href="/feedback/?rid=abcdef12-feed-0500&amp;from=/pricing/"' in html
    assert "app.css" not in html and "/static/" not in html


def _template_syntax_error(*args, **kwargs):
    from django.template import TemplateSyntaxError

    raise TemplateSyntaxError("broken 500.html")


@pytest.mark.parametrize("failure", [RuntimeError("loader exploded"), _template_syntax_error])
def test_500_returns_static_page_when_any_template_loading_raises(failure):
    "Public API."
    from django.test import RequestFactory

    from config.views import STATIC_500_HTML, server_error

    request = RequestFactory().get("/x")
    request.request_id = "abcdef12-feed-0500"
    with mock.patch("django.template.loader.get_template", side_effect=failure), \
            mock.patch("django.template.loader.select_template", side_effect=failure), \
            mock.patch("django.template.loader.render_to_string", side_effect=failure):
        response = server_error(request)
    assert response.status_code == 500
    assert response.content.decode() == STATIC_500_HTML
    body = response.content.decode()
    assert "abcdef12" not in body and "/x" not in body  # no request data


def test_500_static_page_end_to_end_when_500_template_is_broken():
    """Through the real handler500 path: the view raises, then 500.html
    rendering raises too; the client still gets the static page."""
    from django.template import TemplateSyntaxError

    from config.views import STATIC_500_HTML

    client = Client(raise_request_exception=False)
    with mock.patch("apps.dashboard.views._pricing_context", side_effect=RuntimeError("boom")), \
            mock.patch("django.template.loader.get_template", side_effect=TemplateSyntaxError("bad")):
        response = client.get(reverse("dashboard:pricing"))
    assert response.status_code == 500
    assert response.content.decode() == STATIC_500_HTML


@pytest.mark.parametrize("url", [PUBLIC, APP])
def test_feedback_pages_have_no_inline_script_or_style(url):
    client = signed_in(make_user()) if url == APP else Client()
    response = client.get(url)
    assert response.status_code == 200
    html = response.content.decode().lower()
    for tag in re.findall(r"<script\b[^>]*>", html):
        assert "src=" in tag, tag
    assert "<style" not in html
    assert not re.search(r"\sstyle=", html)
    assert not re.search(r"\son[a-z]+=", html)
    assert "'unsafe-inline'" not in response.headers["Content-Security-Policy"]
