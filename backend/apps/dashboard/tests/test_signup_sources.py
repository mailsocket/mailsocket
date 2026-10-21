"""/app/fleet "Signups by source (last 30 days)" table + privacy disclosure."""

import re
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.accounts.models import Account, AccountPlan
from apps.accounts.services import create_product_user

pytestmark = pytest.mark.django_db


def _signup(email, *, ref=None, host=None, verified=False, plan=None, age_days=0):
    user = create_product_user(email, None)
    fields = {"src_ref": ref, "src_referrer_host": host}
    if ref or host:
        fields["src_first_seen"] = timezone.now()
    if verified:
        fields["email_verified_at"] = timezone.now()
    Account.objects.filter(user=user).update(**fields)
    if age_days:
        Account.objects.filter(user=user).update(
            created_at=timezone.now() - timedelta(days=age_days)
        )
    if plan:
        AccountPlan.objects.filter(user=user).update(plan=plan)
    return user


def _staff_client():
    staff = get_user_model().objects.create_user("src_staff", password="x", is_staff=True)
    client = Client()
    client.force_login(staff)
    return client


def _rows(body):
    """{source: (signups, verified, paid)} parsed from the sources table."""
    section = body.split("Signups by source (last 30 days)", 1)[1].split("</table>", 1)[0]
    rows = {}
    for tr in re.findall(r"<tr><td[^>]*>(.*?)</tr>", section, re.S):
        cells = re.findall(r">([^<>]*)</td>", "<td>" + tr)
        if len(cells) == 4:
            rows[cells[0]] = tuple(int(c) for c in cells[1:])
    return rows


def test_fleet_signups_by_source_counts():
    _signup("a@example.com", ref="hn", verified=True, plan="paid")
    _signup("b@example.com", ref="hn", host="news.ycombinator.com", verified=True)
    _signup("c@example.com", ref="hn")
    _signup("d@example.com", host="reddit.com", verified=True, plan="scale")
    _signup("e@example.com")  # direct
    _signup("f@example.com", ref="hn", age_days=45)  # outside 30-day window
    response = _staff_client().get("/app/fleet/")
    assert response.status_code == 200
    rows = _rows(response.content.decode())
    assert rows["hn"] == (3, 2, 1)
    assert rows["reddit.com"] == (1, 1, 1)
    # The staff user (plain create_user) has no Account row, so only e@.
    assert rows["direct"] == (1, 0, 0)
    assert "news.ycombinator.com" not in rows  # ref wins over referrer host


def test_fleet_sources_include_agent_register_and_no_emails():
    from rest_framework.test import APIClient

    assert APIClient().post("/api/v1/agents/register", {}, format="json").status_code == 201
    _signup("secret-person@example.com", ref="devto")
    body = _staff_client().get("/app/fleet/").content.decode()
    rows = _rows(body)
    assert rows["agent-register"] == (1, 1, 0)
    assert rows["devto"] == (1, 0, 0)
    section = body.split("Signups by source", 1)[1].split("</table>", 1)[0]
    assert "secret-person" not in section  # aggregate counts only


def test_fleet_sources_hidden_from_non_staff_and_anonymous():
    _signup("a@example.com", ref="hn")
    user = get_user_model().objects.create_user("src_normal", password="x")
    client = Client()
    client.force_login(user)
    response = client.get("/app/fleet/")
    assert response.status_code == 404  # qafix2 #20: 404, not an admin-login redirect
    assert b"Signups by source" not in response.content
    anon = Client().get("/app/fleet/")
    assert anon.status_code == 404
    assert b"Signups by source" not in anon.content


def test_privacy_page_discloses_signup_source_recording():
    body = Client().get("/legal/privacy").content.decode()
    assert "which site or campaign link brought you to the sign-up page" in body
    assert "host name and campaign tag only" in body
    assert "We set no tracking or advertising cookies." in body


def test_fleet_sources_from_signup_time_attribution():
    """End to end with the cookie-free capture: ref + Referer host recorded
    at signup feed the /app/fleet table."""
    Client().post("/register/?ref=hn", {"email": "p1@example.com"})
    Client().post("/register/?ref=hn", {"email": "p2@example.com"})
    Client().post("/register/", {"email": "p3@example.com"}, HTTP_REFERER="https://lobste.rs/s/x")
    Client().post("/register/", {"email": "p4@example.com"})
    rows = _rows(_staff_client().get("/app/fleet/").content.decode())
    assert rows["hn"] == (2, 0, 0)
    assert rows["lobste.rs"] == (1, 0, 0)
    assert rows["direct"] == (1, 0, 0)
