"""One-shot configuration of a fresh HR Flow stack. Standard library only.

    python3 scripts/setup_stack.py env     # 1. create the three .env files with fresh random keys
    python3 scripts/setup_stack.py site    # 2. (stack running) configure the Frappe site and wire the keys

`env` never overwrites a value that is already set, so it is safe to re-run.
`site` regenerates the two guardrails service-account keys on every run (it rewrites guardrails/.env).
Secrets only go to gitignored .env files and to the Frappe site config, never to stdout.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "-p", "hr", "-f", str(ROOT / "platform" / "hr.yml")]
SITE = "frontend"

ROOT_ENV = ROOT / ".env"
ISL_ENV = ROOT / "input-security-layer" / ".env"
GR_ENV = ROOT / "guardrails" / ".env"


# ---------------------------------------------------------------- .env helpers
def read_env(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def write_env(path: Path, template: Path, values: dict[str, str], force: frozenset[str] = frozenset()) -> None:
    """Fill `template` into `path`. Existing non-placeholder values win unless the key is in `force`."""
    current = read_env(path)
    src = path if path.exists() else template
    lines, seen = [], set()
    for line in src.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k = line.split("=", 1)[0].strip()
            seen.add(k)
            have = current.get(k, "")
            if k in values and (k in force or not have or have.startswith("replace-me")):
                line = f"{k}={values[k]}"
        lines.append(line)
    for k, v in values.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def key() -> str:
    return secrets.token_hex(32)


def cmd_env() -> None:
    isl_old = read_env(ISL_ENV)

    def fresh(k: str) -> str:
        old = isl_old.get(k, "")
        return old if old and not old.startswith("replace-me") else key()

    isl = {k: fresh(k) for k in ("INGEST_SIGNING_KEY", "INGEST_REF_KEY", "UPLOAD_API_KEY",
                                 "REPORT_API_KEY", "ENVELOPE_API_KEY", "RESOLVER_API_KEY")}
    write_env(ISL_ENV, ROOT / "input-security-layer" / ".env.example", isl)

    root_old = read_env(ROOT_ENV)
    write_env(ROOT_ENV, ROOT / ".env.example", {
        "RHFLOW_URL": "http://localhost:8001",
        "RHFLOW_PROPOSE_ENABLED": "1",
        "WEBHOOK_HMAC_SECRET": root_old.get("WEBHOOK_HMAC_SECRET") or key(),
        "WEBHOOK_REPLAY_WINDOW_SECONDS": "300",
        "AGENTS_HOST": "127.0.0.1",
        "AGENTS_PORT": "8200",
        # the agents must hold the SAME values as the input layer
        "INGEST_ENVELOPE_API_KEY": isl["ENVELOPE_API_KEY"],
        "INGEST_SIGNING_KEY": isl["INGEST_SIGNING_KEY"],
    }, force=frozenset({"INGEST_ENVELOPE_API_KEY", "INGEST_SIGNING_KEY"}))

    write_env(GR_ENV, ROOT / "guardrails" / "example.env", {
        "GUARDRAILS_APPROVAL_KEY": secrets.token_urlsafe(48),
        "GUARDRAILS_APPROVAL_KEY_ID": "k1",
    })
    print("OK  .env, input-security-layer/.env, guardrails/.env (secrets not shown)")
    print("Next: start the stack, then `python3 scripts/setup_stack.py site`")


# ---------------------------------------------------------------- site helpers
def bench(*args: str, capture: bool = False) -> str:
    run = subprocess.run([*COMPOSE, "exec", "-T", "backend", "bench", "--site", SITE, *args],
                         capture_output=True, text=True)
    if run.returncode != 0:
        sys.exit(f"FAILED: bench {' '.join(args[:2])}\n{run.stderr[-2000:]}")
    return run.stdout if capture else ""


def last_json(text: str):
    """Return value of `bench execute`: JSON for dicts/lists, raw text for plain strings, None if empty."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return lines[-1]


def cmd_site() -> None:
    root, isl = read_env(ROOT_ENV), read_env(ISL_ENV)
    if not isl.get("UPLOAD_API_KEY") or not root.get("WEBHOOK_HMAC_SECRET"):
        sys.exit("Run `python3 scripts/setup_stack.py env` first.")

    apps = bench("list-apps", capture=True)
    for app in ("hr_ai", "hrflow_theme"):
        if app not in apps:
            print(f"..  install-app {app}")
            bench("install-app", app)

    config = {
        "hr_ai_ingest_url": "http://ingest:8090",
        "hr_ai_ingest_upload_key": isl["UPLOAD_API_KEY"],
        "hr_ai_ingest_report_key": isl["REPORT_API_KEY"],
        "hr_ai_ingest_resolver_key": isl["RESOLVER_API_KEY"],
        "hr_ai_mailpit_url": "http://mailpit:8025",
        "hr_ai_hr_inbox": "hr@rhflow.local",
        "hr_ai_test_recipient": "hr.review@rhflow.local",
        "hr_ai_agents_url": "http://host.docker.internal:8200",
        "hr_ai_agents_webhook_secret": root["WEBHOOK_HMAC_SECRET"],
        "host_name": "http://localhost:8001",
    }
    for k, v in config.items():
        bench("set-config", k, v)
    print(f"OK  site config: {', '.join(config)}")

    # guardrails service accounts: check key -> site config, reader key -> guardrails/.env
    reader = last_json(bench("execute", "hr_ai.setup.setup_guardrails_accounts", capture=True))
    write_env(GR_ENV, ROOT / "guardrails" / "example.env", {
        "FRAPPE_URL": "http://frontend:8080",
        "FRAPPE_GATEWAY_API_KEY": reader["reader_api_key"],
        "FRAPPE_GATEWAY_API_SECRET": reader["reader_api_secret"],
    }, force=frozenset({"FRAPPE_GATEWAY_API_KEY", "FRAPPE_GATEWAY_API_SECRET"}))
    print("OK  guardrails accounts (check key in site config, reader key in guardrails/.env)")

    # outgoing mail through Mailpit (test mail server: nothing leaves the machine)
    exists = last_json(bench("execute", "frappe.db.exists", "--args", json.dumps(["Email Account", "HR Flow HR"]),
                             capture=True))
    if not exists:
        doc = {"doctype": "Email Account", "email_account_name": "HR Flow HR", "email_id": "hr@rhflow.local",
               "enable_outgoing": 1, "default_outgoing": 1, "smtp_server": "mailpit", "smtp_port": "1025",
               "no_smtp_authentication": 1, "enable_incoming": 0, "awaiting_password": 0,
               "always_use_account_email_id_as_sender": 1, "always_use_account_name_as_sender_name": 1}
        bench("execute", "frappe.client.insert", "--kwargs", json.dumps({"doc": doc}))
    print("OK  outgoing Email Account 'HR Flow HR' -> mailpit:1025")

    # chat demo user "Utilisateur" / user, linked to fictional employee HR-EMP-00007
    bench("execute", "hr_ai.setup.setup_demo_users", "--kwargs", json.dumps({"chat_password": "user"}))
    print("OK  chat demo user: Utilisateur / user")

    bench("enable-scheduler")
    bench("clear-cache")
    print("OK  scheduler enabled, cache cleared")
    print("Next: start the input layer and the guardrails containers, then create the agent users "
          "(see JURY_GUIDE.md, step 5).")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("env", "site"):
        sys.exit(__doc__)
    {"env": cmd_env, "site": cmd_site}[sys.argv[1]]()
