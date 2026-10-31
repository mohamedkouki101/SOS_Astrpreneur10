"""Fake Frappe for local testing and demo rehearsal: no Docker needed.

Implements only what the guardrails service uses:
  GET  /api/method/frappe.auth.get_logged_user
  GET  /api/method/frappe.core.doctype.user.user.get_roles
  GET  /api/resource/<DocType>/<name>
  PUT  /api/resource/<DocType>/<name>               (gateway token only)
  POST /api/resource/<DocType>                      (gateway token only)
  POST /api/method/frappe.core.doctype.communication.email.make   (gateway only)
Debug:
  GET  /__state     sent emails, comments, current records (no auth)

All data is FICTIONAL. Emails are not sent: they are printed and stored.

Run:  python scripts/fake_frappe.py --port 8090
"""
from __future__ import annotations

import argparse
import copy
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

# token "KEY:SECRET" -> (user, roles). Same users as config/api.json.
TOKENS = {
    "demo-comms:secret": ("svc.comms@hr-ai.test", ["AI Agent"]),
    "demo-recruit:secret": ("svc.recruitment@hr-ai.test", ["AI Agent"]),
    "demo-ingest:secret": ("svc.ingest@hr-ai.test", ["Ingest"]),
    "demo-hr:secret": ("hr.manager@hr-ai.test", ["HR Manager", "Employee"]),
    "demo-employee:secret": ("sami.employee@hr-ai.test", ["Employee"]),
    "demo-gateway:secret": ("guardrails.gateway@hr-ai.test", ["HR Manager", "System Manager"]),
}
WRITERS = {"guardrails.gateway@hr-ai.test"}  # only the gateway may write

SEED = {
    ("Job Applicant", "HR-APP-0001"): {
        "name": "HR-APP-0001", "applicant_name": "Amira Ben Salah (fictional)",
        "email_id": "amira.bensalah@example.test", "job_title": "HR-OPN-0001",
        "status": "Open", "applicant_rating": 0,
    },
    ("Job Applicant", "HR-APP-0002"): {
        "name": "HR-APP-0002", "applicant_name": "Youssef Trabelsi (fictional)",
        "email_id": "youssef.trabelsi@example.test", "job_title": "HR-OPN-0001",
        "status": "Open", "applicant_rating": 0,
    },
    ("Job Applicant", "HR-APP-0666"): {
        "name": "HR-APP-0666", "applicant_name": "Malicious Applicant (fictional)",
        "email_id": "applicant666@example.test", "job_title": "HR-OPN-0001",
        "status": "Open", "applicant_rating": 0,
    },
    ("Employee", "HR-EMP-0001"): {
        "name": "HR-EMP-0001", "employee_name": "Sami Gharbi (fictional)",
        "prefered_email": None, "company_email": "sami.gharbi@acme.test",
        "personal_email": None,
    },
}


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.docs = copy.deepcopy(SEED)
        self.outbox: list[dict] = []
        self.comments: list[dict] = []
        self.counter = 0

    def next_name(self, prefix):
        self.counter += 1
        return f"{prefix}-{self.counter:05d}"


STATE = State()


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeFrappe/1.0"

    def log_message(self, fmt, *args):
        pass

    # -- helpers --

    def _send(self, code, body):
        raw = json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _user(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("token ") and auth[6:] in TOKENS:
            return TOKENS[auth[6:]]
        return None

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _resource(self, path):
        parts = [unquote(p) for p in path.split("/")[3:]]  # after /api/resource/
        return parts[0] if parts else None, parts[1] if len(parts) > 1 else None

    # -- routes --

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/__state":
            with STATE.lock:
                return self._send(200, {
                    "outbox": STATE.outbox, "comments": STATE.comments,
                    "docs": {f"{k[0]}/{k[1]}": v for k, v in STATE.docs.items()},
                })
        who = self._user()
        if who is None:
            return self._send(401, {"exc_type": "AuthenticationError"})
        user, roles = who
        if path == "/api/method/frappe.auth.get_logged_user":
            return self._send(200, {"message": user})
        if path == "/api/method/frappe.core.doctype.user.user.get_roles":
            return self._send(200, {"message": roles})
        if path.startswith("/api/resource/"):
            doctype, name = self._resource(path)
            with STATE.lock:
                doc = STATE.docs.get((doctype, name))
            if doc is None:
                return self._send(404, {"exc_type": "DoesNotExistError"})
            return self._send(200, {"data": doc})
        self._send(404, {"exc_type": "NotFound"})

    def do_PUT(self):
        who = self._user()
        if who is None:
            return self._send(401, {"exc_type": "AuthenticationError"})
        if who[0] not in WRITERS:
            return self._send(403, {"exc_type": "PermissionError"})
        path = urlparse(self.path).path
        doctype, name = self._resource(path)
        fields = self._body()
        with STATE.lock:
            doc = STATE.docs.get((doctype, name))
            if doc is None:
                return self._send(404, {"exc_type": "DoesNotExistError"})
            doc.update(fields)
        print(f"  [fake-frappe] UPDATE {doctype}/{name} {fields}")
        self._send(200, {"data": doc})

    def do_POST(self):
        who = self._user()
        if who is None:
            return self._send(401, {"exc_type": "AuthenticationError"})
        if who[0] not in WRITERS:
            return self._send(403, {"exc_type": "PermissionError"})
        path = urlparse(self.path).path
        body = self._body()
        if path == "/api/method/frappe.core.doctype.communication.email.make":
            with STATE.lock:
                name = STATE.next_name("COMM")
                mail = {"name": name, "to": body.get("recipients"), "subject": body.get("subject"),
                        "content": body.get("content"),
                        "reference": f"{body.get('doctype')}/{body.get('name')}",
                        "sent_at": datetime.now().isoformat(timespec="seconds")}
                STATE.outbox.append(mail)
            print(f"  [fake-frappe] EMAIL {name} to={mail['to']} subject={mail['subject']!r}")
            return self._send(200, {"message": {"name": name, "emails_not_sent_to": ""}})
        if path.startswith("/api/resource/"):
            doctype, _ = self._resource(path)
            with STATE.lock:
                name = STATE.next_name(doctype.upper().replace(" ", "-"))
                doc = {"name": name, "doctype": doctype, **body}
                if doctype == "Comment":
                    STATE.comments.append(doc)
                else:
                    STATE.docs[(doctype, name)] = doc
            print(f"  [fake-frappe] INSERT {doctype} {name}")
            return self._send(200, {"data": doc})
        self._send(404, {"exc_type": "NotFound"})


def make_server(port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args()
    srv = make_server(args.port)
    print(f"Fake Frappe on http://127.0.0.1:{args.port}  (state: /__state)")
    print("Demo tokens:")
    for tok, (user, roles) in TOKENS.items():
        print(f"  token {tok:24} -> {user} {roles}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
