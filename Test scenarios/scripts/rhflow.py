"""Small client used by the test-scenario scripts. Talks to the running HR Flow stack exactly like a
browser or an agent would: HTTP only, with the same logins and API keys. Nothing here bypasses a layer.

    HR Core                 http://localhost:8001   Administrator / admin, utilisateur@rhflow.local / user
    Input security layer    http://localhost:8090
    Guardrails              http://localhost:8100
    Agent service           http://127.0.0.1:8200
    Mailpit                 http://localhost:8025

Run every script from the repo root with the agents' venv:
    P=$HOME/.venvs/rhflow-agents/bin/python
    $P "Test scenarios/scripts/s01_inbox_reply.py"
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
HR = "http://localhost:8001"
GUARD = "http://localhost:8100"
INGEST = "http://localhost:8090"
AGENTS = "http://127.0.0.1:8200"
MAILPIT = "http://localhost:8025"

ADMIN = ("Administrator", "admin")
STACK_DIR = Path(os.environ.get("STACK_DIR") or Path(__file__).resolve().parents[2] / "platform")  # Frappe stack compose project (platform/)
EMPLOYEE = ("utilisateur@rhflow.local", "user")   # website user, chat only; linked to HR-EMP-00007


def dataset_dir() -> Path:
    """The fictional dataset (same setting the agents use: DATASET_DIR, see agents/config.py)."""
    return Path(env().get("DATASET_DIR") or STACK_DIR / "Data_set/dataset")


def env() -> dict:
    out = {}
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# ---------------------------------------------------------------- output

PASSED: list[str] = []
FAILED: list[str] = []


def step(title: str) -> None:
    print(f"\n\033[1m== {title}\033[0m", flush=True)


def info(msg: str) -> None:
    print(f"   {msg}", flush=True)


def check(cond: bool, label: str, detail: str = "") -> bool:
    (PASSED if cond else FAILED).append(label)
    mark = "\033[32mPASS\033[0m" if cond else "\033[31mFAIL\033[0m"
    print(f"   [{mark}] {label}" + (f"  ({detail})" if detail else ""), flush=True)
    return cond


def done() -> None:
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for f in FAILED:
        print(f"   failed: {f}")
    sys.exit(1 if FAILED else 0)


# ---------------------------------------------------------------- HR Core sessions


class Session:
    """A logged-in browser session (cookie) or an API-key client (agents)."""

    def __init__(self, user: str | None = None, password: str | None = None, token: str | None = None):
        headers = {"Authorization": f"token {token}"} if token else {}
        self.c = httpx.Client(base_url=HR, timeout=60, headers=headers)
        self.user = user
        if user:
            r = self.c.post("/api/method/login", data={"usr": user, "pwd": password})
            r.raise_for_status()

    @classmethod
    def agent(cls, name: str) -> Session:
        e = env()
        return cls(token=f"{e[f'RHFLOW_{name.upper()}_API_KEY']}:{e[f'RHFLOW_{name.upper()}_API_SECRET']}")

    def method(self, path: str, http: str = "POST", **data) -> httpx.Response:
        url = f"/api/method/{path}"
        if http == "GET":
            return self.c.get(url, params=data)
        return self.c.post(url, json=data)

    def doc(self, doctype: str, name: str) -> dict:
        r = self.c.get(f"/api/resource/{doctype}/{name}")
        r.raise_for_status()
        return r.json()["data"]

    def list(self, doctype: str, fields: list[str], filters: dict | list | None = None, limit: int = 50,
             order_by: str = "creation desc") -> list[dict]:
        r = self.c.get(f"/api/resource/{doctype}", params={
            "fields": json.dumps(fields), "filters": json.dumps(filters or {}),
            "limit_page_length": limit, "order_by": order_by})
        r.raise_for_status()
        return r.json()["data"]

    def run_doc_method(self, doctype: str, name: str, method: str, **args) -> httpx.Response:
        return self.c.post("/api/method/run_doc_method",
                           json={"dt": doctype, "dn": name, "method": method, "args": json.dumps(args)})

    # ------------------------------------------------ AI Action helpers

    def action(self, name: str) -> dict:
        return self.doc("AI Action", name)

    def approve(self, name: str, note: str = "Approved in test scenario") -> httpx.Response:
        return self.run_doc_method("AI Action", name, "approve", note=note)

    def reject(self, name: str, note: str = "Declined in test scenario") -> httpx.Response:
        return self.run_doc_method("AI Action", name, "reject", note=note)

    def wait_action(self, name: str, statuses=("Executed", "Failed"), timeout: int = 90) -> dict:
        end = time.time() + timeout
        while True:
            a = self.action(name)
            if a["status"] in statuses or time.time() > end:
                return a
            time.sleep(2)

    def latest_action(self, **filters) -> dict | None:
        rows = self.list("AI Action", ["name", "kind", "agent", "status", "subject", "creation"], filters, limit=1)
        return rows[0] if rows else None

    # ------------------------------------------------ HR Chat

    def chat_clear(self) -> None:
        self.method("hr_ai.chat.clear").raise_for_status()

    def chat(self, text: str, timeout: int = 420) -> dict:
        """Send one chat message and wait for the reply. Returns the reply row (+ the 'in' row)."""
        r = self.method("hr_ai.chat.send", message=text)
        if r.status_code != 200:
            return {"error": r.status_code, "body": r.text[:500]}
        name = r.json()["message"]["name"]
        end = time.time() + timeout
        while time.time() < end:
            rows = self.method("hr_ai.chat.history", http="GET", limit=20).json()["message"]
            ins = next((x for x in rows if x["name"] == name), {})
            out = next((x for x in rows if x.get("in_reply_to") == name), None)
            if out:
                return {**out, "in": ins}
            time.sleep(3)
        return {"error": "timeout", "in_status": ins.get("status")}

    def chat_decide(self, reply_name: str, decision: str = "confirm") -> httpx.Response:
        return self.method("hr_ai.chat.decide", message=reply_name, decision=decision)


def admin() -> Session:
    return Session(*ADMIN)


def employee() -> Session:
    return Session(*EMPLOYEE)


def server_messages(r: httpx.Response) -> list[str]:
    """HR Core error messages of a failed call, as plain text."""
    try:
        body = r.json()
    except ValueError:
        return [r.text[:300]]
    msgs = []
    for m in json.loads(body.get("_server_messages") or "[]"):
        try:
            msgs.append(json.loads(m).get("message", m))
        except (ValueError, AttributeError):
            msgs.append(str(m))
    return msgs or [str(body.get("exception") or body.get("exc_type") or body)[:300]]


def propose(agent: str, action: dict) -> httpx.Response:
    """POST hr_ai.api.propose_action as the given agent service user (the only way agents write)."""
    return Session.agent(agent).method("hr_ai.api.propose_action", action=action)


# ---------------------------------------------------------------- input layer, guardrails, mailpit


def guardrails_get(path: str, sess: Session) -> httpx.Response:
    sid = sess.c.cookies.get("sid")
    return httpx.get(GUARD + path, cookies={"sid": sid}, timeout=30)


def mailpit_messages(query: str | None = None) -> list[dict]:
    if query:
        r = httpx.get(f"{MAILPIT}/api/v1/search", params={"query": query}, timeout=15)
    else:
        r = httpx.get(f"{MAILPIT}/api/v1/messages", timeout=15)
    return r.json().get("messages") or []


def mailpit_body(msg_id: str) -> str:
    m = httpx.get(f"{MAILPIT}/api/v1/message/{msg_id}", timeout=15).json()
    return m.get("Text") or m.get("HTML") or ""


def health() -> dict:
    out = {}
    for name, url in (("hr_core", f"{HR}/api/method/ping"), ("ingest", f"{INGEST}/health"),
                      ("guardrails", f"{GUARD}/health"), ("agents", f"{AGENTS}/health"),
                      ("mailpit", f"{MAILPIT}/api/v1/info"), ("ollama", "http://localhost:11434/api/tags")):
        try:
            out[name] = httpx.get(url, timeout=5).status_code == 200
        except httpx.HTTPError:
            out[name] = False
    return out


def require_stack(*names: str) -> None:
    h = health()
    missing = [n for n in (names or h) if not h.get(n)]
    if missing:
        print(f"Stack not ready, down: {', '.join(missing)}. See 'Test scenarios/00_setup.md'.")
        sys.exit(2)


def agents_cli(*args: str, timeout: int = 900) -> tuple[str, list[dict]]:
    """Run `python -m agents.cli ...` (the agent service CLI) and return (stdout, new agent audit entries)."""
    import subprocess

    audit_path = REPO / "var/audit.jsonl"
    start = len(audit_path.read_text(encoding="utf-8").splitlines()) if audit_path.exists() else 0
    out = subprocess.run([sys.executable, "-m", "agents.cli", *args], cwd=REPO, capture_output=True, text=True,
                         timeout=timeout)
    new = [json.loads(x) for x in audit_path.read_text(encoding="utf-8").splitlines()[start:] if x.strip()]
    return out.stdout + out.stderr[-2000:], new


def proposals_in(stdout: str) -> list[str]:
    import re

    return re.findall(r"proposal (AIA-\d{4}-\d+)", stdout)


def bench_console(code: str) -> object:
    """Run Python inside the HR Core backend (bench console) and return what the code put in RESULT.
    Used ONLY to simulate an attacker with database access (tampering), never for the normal flow."""
    import subprocess

    script = (f"{code}\nimport json as _j\nprint('@@RESULT@@' + _j.dumps(RESULT, default=str))\n")
    out = subprocess.run(["docker", "compose", "-p", "hr", "-f", str(STACK_DIR / "hr.yml"), "exec", "-T",
                          "backend", "bench", "--site", "frontend", "console"],
                         input=script, capture_output=True, text=True, timeout=180).stdout
    for line in out.splitlines():
        if "@@RESULT@@" in line:
            return json.loads(line.split("@@RESULT@@", 1)[1])
    raise RuntimeError("bench console gave no result:\n" + out[-1500:])


def wait(cond, timeout: int = 120, every: float = 3.0):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(every)
    return None
