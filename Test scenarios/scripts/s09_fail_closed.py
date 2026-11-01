"""S09: fail closed. With the guardrails service down, no proposal is queued and no approved action is
executed, even one HR has just approved. The service is restarted at the end."""

import subprocess
import time

import httpx
import rhflow as r

r.require_stack("hr_core", "guardrails")
adm = r.admin()
CONTAINER = "guardrails-guardrails-1"
H = "a" * 64


def reschedule(date: str) -> dict:
    return {"kind": "interview_update", "agent": "operator", "subject": "Interview/HR-INT-2026-0002",
            "payload": {"interview": "HR-INT-2026-0002", "action": "reschedule", "date": date,
                        "from_time": "11:00:00", "to_time": "12:00:00"}, "source_hashes": [H]}


before = str(adm.doc("Interview", "HR-INT-2026-0002")["scheduled_on"])

r.step("1. Guardrails up: a valid change is proposed and waits for HR")
res = r.propose("operator", reschedule("2026-11-05"))
r.check(res.status_code == 200, "proposal accepted", str(res.json().get("message")))
pending = res.json()["message"]["name"]

r.step(f"2. The guardrails service goes down (docker stop {CONTAINER})")
subprocess.run(["docker", "stop", CONTAINER], check=True, capture_output=True)
try:
    down = r.wait(lambda: not r.health()["guardrails"], timeout=30, every=1)
    r.check(bool(down), "guardrails unreachable")

    r.step("3. New proposals are refused")
    res = r.propose("operator", reschedule("2026-11-06"))
    r.check(res.status_code == 403 and "fail closed" in " ".join(r.server_messages(res)),
            "proposal refused", r.server_messages(res)[0][:100])

    r.step("4. HR approves the pending change: the Execution Gateway refuses to act")
    adm.approve(pending, "Approved while guardrails are down").raise_for_status()
    a = adm.wait_action(pending, timeout=60)
    r.check(a["status"] == "Failed" and "fail closed" in (a.get("execution_result") or ""),
            "action Failed", a.get("execution_result", "")[:100])
    r.check(str(adm.doc("Interview", "HR-INT-2026-0002")["scheduled_on"]) == before, "interview unchanged", before)
finally:
    r.step(f"5. Restart (docker start {CONTAINER})")
    subprocess.run(["docker", "start", CONTAINER], check=True, capture_output=True)
    up = r.wait(lambda: r.health()["guardrails"], timeout=90, every=2)
    r.check(bool(up), "guardrails healthy again")
    time.sleep(2)
    r.check(httpx.get(f"{r.GUARD}/health", timeout=5).json().get("status") == "ok", "/health ok")
r.done()
