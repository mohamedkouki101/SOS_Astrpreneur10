"""FrappeAuthenticator against a tiny local server pretending to be Frappe."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote

import pytest

from guardrails.auth import AuthError, FrappeAuthenticator

USERS = {
    "token hrkey:hrsecret": ("hr.manager@acme.test", ["HR Manager", "Employee"]),
    "sid=hr-session": ("hr.manager@acme.test", ["HR Manager"]),
    "token guest:x": ("Guest", ["Guest"]),
}


class FakeFrappeAuth(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        FakeFrappeAuth.calls += 1
        cred = self.headers.get("Authorization") or self.headers.get("Cookie")
        if cred == "token bad:bad":
            return self._send(401, {"exc_type": "AuthenticationError"})
        if cred not in USERS:
            return self._send(403, {"exc_type": "PermissionError"})
        user, roles = USERS[cred]
        if self.path.endswith("frappe.auth.get_logged_user"):
            return self._send(200, {"message": user})
        if self.path.startswith("/api/resource/User/"):
            if unquote(self.path.split("/")[-1]) != user:   # own record only
                return self._send(403, {"exc_type": "PermissionError"})
            return self._send(200, {"data": {"name": user, "roles": [{"role": r} for r in roles]}})
        self._send(404, {})

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture(scope="module")
def frappe_url():
    srv = HTTPServer(("127.0.0.1", 0), FakeFrappeAuth)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_token_resolves_user_and_roles(frappe_url):
    ident = FrappeAuthenticator(frappe_url).authenticate(
        "GET", {"authorization": "token hrkey:hrsecret"}, {})
    assert ident.user == "hr.manager@acme.test" and "HR Manager" in ident.roles


def test_session_cookie_resolves_user(frappe_url):
    ident = FrappeAuthenticator(frappe_url).authenticate("GET", {}, {"sid": "hr-session"})
    assert ident.roles == frozenset({"HR Manager"})


def test_guest_refused(frappe_url):
    with pytest.raises(AuthError) as e:
        FrappeAuthenticator(frappe_url).authenticate("GET", {"authorization": "token guest:x"}, {})
    assert e.value.status == 401


def test_bad_credentials_refused(frappe_url):
    with pytest.raises(AuthError) as e:
        FrappeAuthenticator(frappe_url).authenticate("GET", {"authorization": "token bad:bad"}, {})
    assert e.value.status == 401


def test_frappe_down_is_503():
    with pytest.raises(AuthError) as e:
        FrappeAuthenticator("http://127.0.0.1:1", timeout=1).authenticate(
            "GET", {"authorization": "token a:b"}, {})
    assert e.value.status == 503


def test_identity_cached(frappe_url):
    auth = FrappeAuthenticator(frappe_url)
    FakeFrappeAuth.calls = 0
    for _ in range(5):
        auth.authenticate("GET", {"authorization": "token hrkey:hrsecret"}, {})
    assert FakeFrappeAuth.calls == 2   # get_logged_user + own User record, once
