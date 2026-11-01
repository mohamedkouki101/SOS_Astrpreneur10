"""Tests the real HTTP client against a tiny local server (no Frappe needed)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from guardrails.frappe_client import FrappeClient, FrappeError, FrappeUnavailable


class Handler(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def _reply(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        Handler.seen.append((self.command, self.path, self.headers.get("Authorization"), body))
        if "forbidden" in self.path:
            return self._reply(403, {"exc_type": "PermissionError"})
        if "boom" in self.path:
            return self._reply(500, {"exc_type": "InternalError"})
        if self.path.startswith("/api/method/"):
            return self._reply(200, {"message": {"name": "COMM-1"}})
        return self._reply(200, {"data": {"name": "X", "echo": body}})

    do_GET = do_POST = do_PUT = _handle


@pytest.fixture(scope="module")
def server():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def client(server):
    Handler.seen.clear()
    return FrappeClient(server, "KEY", "SECRET", timeout=5)


def test_auth_header_and_url_encoding(client):
    client.get_doc("Job Applicant", "HR-APP/0001")
    method, path, auth, _ = Handler.seen[-1]
    assert method == "GET"
    assert path == "/api/resource/Job%20Applicant/HR-APP%2F0001"
    assert auth == "token KEY:SECRET"


def test_update_sends_json(client):
    out = client.update_doc("Job Applicant", "HR-APP-0001", {"status": "Replied"})
    assert Handler.seen[-1][0] == "PUT" and out["echo"] == {"status": "Replied"}


def test_method_call_returns_message(client):
    assert client.call("frappe.core.doctype.communication.email.make", {"a": 1}) == {"name": "COMM-1"}


def test_bad_method_name_refused(client):
    with pytest.raises(ValueError):
        client.call("../../etc/passwd", {})


def test_4xx_is_definite_rejection(client):
    with pytest.raises(FrappeError) as e:
        client.get_doc("forbidden", "x")
    assert e.value.status == 403


def test_5xx_is_unknown_outcome(client):
    with pytest.raises(FrappeUnavailable):
        client.get_doc("boom", "x")


def test_unreachable_is_unknown_outcome():
    c = FrappeClient("http://127.0.0.1:1", "K", "S", timeout=1)
    with pytest.raises(FrappeUnavailable):
        c.get_doc("Employee", "x")


def test_secret_not_in_repr():
    assert "SECRET" not in repr(FrappeClient("http://x", "KEY", "SECRET"))


@pytest.mark.parametrize("args", [("", "k", "s"), ("http://x", "", "s"), ("ftp://x", "k", "s")])
def test_missing_or_bad_config_refused(args):
    with pytest.raises(ValueError):
        FrappeClient(*args)
