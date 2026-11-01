"""S10: the Orchestrator routes a plain-language request to the right specialist (per-agent tool
allowlists and budgets). The Leave agent proposes a decision that HR approves and the gateway applies.
A stubbed agent (payroll) has no allow rule in the guardrails: whatever it proposes is denied."""

import json

import rhflow as r

r.require_stack("hr_core", "guardrails", "ollama")
adm = r.admin()
# First open leave application (HR-LAP-2026-00009 is kept open for the employee chat scenario S04).
LAP = next(x["name"] for x in adm.list("Leave Application", ["name"], {"status": "Open", "docstatus": 0}, order_by="name desc")
           if x["name"] != "HR-LAP-2026-00009")

r.step(f"1. Orchestrator: 'Review leave application {LAP} and propose a decision'")
la = adm.doc("Leave Application", LAP)
r.info(f"{LAP}: {la['employee_name']}, {la['leave_type']} {la['from_date']} -> {la['to_date']}, status {la['status']}")
out, audit = r.agents_cli("ask", f"Review leave application {LAP} and propose a decision for HR.")
r.info(out.strip().replace("\n", "\n   ")[-1500:])
delegated = [e.get("agent") for e in audit if e["event"] == "run_start"]
tools = [f"{e['agent']}.{e['tool']}" for e in audit if e["event"] == "tool_call"]
r.check("leave" in delegated, "delegated to the Leave agent", f"agents run: {delegated}")
r.check(all(t.split(".")[0] in ("orchestrator", "leave") for t in tools), "only orchestrator / leave tools were used",
        ", ".join(sorted(set(tools))))
props = r.proposals_in(out)
r.check(bool(props), "leave_decision proposed", ", ".join(props) or "none")

if props and la["status"] == "Open":
    a = adm.action(props[-1])
    p = json.loads(a["payload"])
    r.info(f"proposal: {p['decision']} - reason: {p['reason']}")
    r.check(a["kind"] == "leave_decision" and a["agent"] == "leave", "kind leave_decision from agent leave")

    r.step("2. HR allows it -> the gateway submits the Leave Application")
    adm.approve(a["name"], "Team coverage checked").raise_for_status()
    a = adm.wait_action(a["name"])
    r.check(a["status"] == "Executed", "Executed", (a.get("execution_result") or "")[:100])
    la = adm.doc("Leave Application", LAP)
    r.check(la["docstatus"] == 1 and la["status"] == p["decision"], f"{LAP} submitted as {la['status']}")

r.step("3. A stubbed agent (payroll) is denied by default")
out, audit = r.agents_cli("run", "payroll", "Explain the October salary slip of employee HR-EMP-00007 to them.",
                          "--as", "HR-EMP-00007")
r.info(out.strip().replace("\n", "\n   ")[-700:])
failed = [e for e in audit if e["event"] == "proposal_submit_failed"]
r.check(not r.proposals_in(out), "nothing reached the HR queue")
if failed:
    r.info("refused: " + failed[0]["error"].split("Blocked by guardrails: ")[-1].split("\\")[0])
    r.check("tool_not_allowed" in failed[0]["error"], "guardrails: tool_not_allowed (no allow rule for payroll)")
else:
    r.info("the payroll agent found nothing to propose (no salary slips in the demo data)")
# The same HTTP call the agent's outbox makes, with the payroll agent's own key:
res = r.propose("payroll", {"kind": "payroll_reply", "agent": "payroll", "subject": "Employee/HR-EMP-00007",
                            "payload": {"employee": "HR-EMP-00007", "body": "Your October net pay is explained below."},
                            "source_hashes": ["c" * 64]})
r.check(res.status_code == 403 and "tool_not_allowed" in " ".join(r.server_messages(res)),
        "payroll_reply from the payroll agent: guardrails default deny", r.server_messages(res)[0][:80])

r.step("4. Budgets and the agent-side audit chain")
out, _ = r.agents_cli("audit-verify")
r.check("OK" in out, "agent audit chain verified", out.strip())
r.done()
