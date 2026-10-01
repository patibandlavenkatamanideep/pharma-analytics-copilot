"""Same-origin, JSON-only, size-limited state changes.

The session is a cookie, and a browser attaches a cookie to a request another
site makes it send. SameSite=Lax blocks that in current browsers; this is the
second control, independent of the browser: a state-changing API request
whose Origin -- or, failing that, Sec-Fetch-Site -- names another site is
refused. A cross-site page cannot send JSON without a CORS preflight this API
never grants, so non-JSON bodies are refused too. And a body over the limit
is refused while it is read, not only when it declares its length.
"""

from __future__ import annotations

import pytest

from tests.security.helpers import sign_in

pytestmark = pytest.mark.security

Q = {"question": "What is our total volume this quarter?"}


def test_a_cross_site_post_is_refused_before_the_route_runs(client, make_identity):
    sign_in(client, make_identity("exec", can_view_wac=1))
    r = client.post("/api/ask", json=Q, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "cross_origin"


def test_a_same_origin_post_is_accepted(client, make_identity):
    sign_in(client, make_identity("exec", can_view_wac=1))
    r = client.post("/api/ask", json=Q, headers={"Origin": "http://testserver"})
    assert r.status_code == 200


def test_sec_fetch_site_is_used_when_there_is_no_origin(client, make_identity):
    sign_in(client, make_identity("exec", can_view_wac=1))
    r = client.post("/api/ask", json=Q, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_a_null_origin_is_refused(client, make_identity):
    """Sandboxed iframes and some redirects send Origin: null."""
    sign_in(client, make_identity("exec", can_view_wac=1))
    r = client.post("/api/ask", json=Q, headers={"Origin": "null"})
    assert r.status_code == 403


def test_login_csrf_is_refused(client, make_identity):
    """Signing a victim into the attacker's account is the attack here."""
    user = make_identity("exec", can_view_wac=1)
    r = client.post("/api/login", json={"email": user.email, "password": user.password},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_a_form_post_is_refused(client, make_identity):
    sign_in(client, make_identity("exec", can_view_wac=1))
    r = client.post("/api/ask", data="question=hi",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415


def test_an_oversized_body_is_refused(client, make_identity):
    sign_in(client, make_identity("exec", can_view_wac=1))
    r = client.post("/api/ask", content=b'{"question": "' + b"x" * 70_000 + b'"}',
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_an_oversized_body_without_a_declared_length_is_refused(client, make_identity):
    """Chunked: no Content-Length to check up front."""
    sign_in(client, make_identity("exec", can_view_wac=1))

    def chunks():
        yield b'{"question": "'
        for _ in range(20):
            yield b"x" * 8192
        yield b'"}'

    r = client.post("/api/ask", content=chunks(),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_reads_are_not_subject_to_the_origin_rule(client, make_identity):
    """GETs change nothing; a cross-site GET cannot read the response."""
    sign_in(client, make_identity("exec", can_view_wac=1))
    assert client.get("/api/me", headers={"Origin": "https://evil.example"}).status_code == 200


def test_logout_without_a_body_still_works(client, make_identity):
    sign_in(client, make_identity("exec", can_view_wac=1))
    assert client.post("/api/logout").status_code == 200
