"""Create one least-privilege, READ-ONLY API user per agent in RH Flow and write its key to .env.

Run once (and again after a site reset), as an administrator:
    ADMIN_PASSWORD=... python agents/scripts/setup_agent_users.py

For each agent it creates:
  - a Role "RH Flow Agent <Name>" with no desk access,
  - read-only permission rules (via the official permission manager, which keeps standard rules),
    ONLY on the doctypes that agent's tools need,
  - a User agent.<name>@rhflow.local with only that role, and an API key/secret.
The administrator password is used only here, never by the agents.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
URL = os.environ.get("RHFLOW_URL", "http://localhost:8080").rstrip("/")

READS: dict[str, list[str]] = {
    "orchestrator": ["Leave Application", "Job Applicant"],
    "recruitment": ["Job Opening", "Job Applicant", "Interview"],
    "comms": ["Job Applicant", "Job Opening", "Inbound Email"],
    "assistant": ["Employee", "Leave Ledger Entry", "Holiday List"],
    "leave": ["Leave Application", "Leave Ledger Entry", "Holiday List", "Employee"],
    "onboarding": ["Employee", "Employee Onboarding Template"],
    "payroll": ["Salary Slip", "Attendance"],
    "performance": ["Appraisal", "Goal", "Employee Performance Feedback"],
    "conflict": ["Employee Grievance"],
}


def main() -> int:
    pwd = os.environ.get("ADMIN_PASSWORD")
    if not pwd:
        print("Set ADMIN_PASSWORD", file=sys.stderr)
        return 2
    http = httpx.Client(base_url=URL, timeout=60)
    http.post("/api/method/login", json={"usr": os.environ.get("ADMIN_USER", "Administrator"), "pwd": pwd}).raise_for_status()

    def exists(doctype: str, name: str) -> bool:
        return http.get(f"/api/resource/{doctype}/{name}").status_code == 200

    env_lines: dict[str, str] = {}
    for agent, doctypes in READS.items():
        title = agent.capitalize()
        role, user = f"RH Flow Agent {title}", f"agent.{agent}@rhflow.local"
        if not exists("Role", role):
            http.post("/api/resource/Role", json={"role_name": role, "desk_access": 0}).raise_for_status()
        for dt in doctypes:
            r = http.post("/api/method/frappe.core.page.permission_manager.permission_manager.add",
                          data={"parent": dt, "role": role, "permlevel": 0})
            if r.status_code >= 400 and "already exists" not in r.text:
                print(f"  ! could not add read rule {dt} for {role}: {r.status_code}")
        if not exists("User", user):
            http.post("/api/resource/User", json={
                "email": user, "first_name": f"Agent {title}", "user_type": "System User", "send_welcome_email": 0,
                "roles": [{"role": role}],
            }).raise_for_status()
        secret = http.post("/api/method/frappe.core.doctype.user.user.generate_keys", data={"user": user})
        secret.raise_for_status()
        api_secret = secret.json()["message"]["api_secret"]
        api_key = http.get(f"/api/resource/User/{user}").json()["data"]["api_key"]
        env_lines[f"RHFLOW_{agent.upper()}_API_KEY"] = api_key
        env_lines[f"RHFLOW_{agent.upper()}_API_SECRET"] = api_secret
        print(f"  ✓ {agent:12s} {user}  read-only on: {', '.join(doctypes)}")

    env = REPO / ".env"
    text = env.read_text(encoding="utf-8") if env.is_file() else ""
    for k in env_lines:
        text = re.sub(rf"^{k}=.*\n?", "", text, flags=re.M)
    text = text.rstrip("\n") + ("\n" if text else "") + "".join(f"{k}={v}\n" for k, v in env_lines.items())
    env.write_text(text, encoding="utf-8")
    print(f"\nKeys written to {env} (gitignored). Secrets were not printed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
