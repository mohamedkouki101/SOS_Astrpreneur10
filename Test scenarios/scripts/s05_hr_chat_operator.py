"""S05: HR gives orders in RH Chat (HR mode, Administrator). The operator agent (read-only account)
only prepares the change; the HR user's Confirm is the approval token; the Execution Gateway applies it.
Cancel leaves the data untouched."""

import rhflow as r

r.require_stack("hr_core", "ingest", "guardrails", "agents", "ollama")
adm = r.admin()
adm.chat_clear()
IV = "HR-INT-2026-0008"


def order(text: str) -> dict:
    r.info(f"HR> {text}")
    rep = adm.chat(text)
    r.info("bot> " + str(rep.get("content") or rep).replace("\n", "\n        ")[:600])
    if rep.get("action_summary"):
        r.info("change card: " + rep["action_summary"].replace("\n", " | "))
    return rep


def confirm_and_wait(rep: dict, decision: str = "confirm") -> dict:
    res = adm.chat_decide(rep["name"], decision)
    r.check(res.status_code == 200, f"{decision} accepted", str(res.json().get("message")))
    return adm.wait_action(rep["ai_action"], statuses=("Executed", "Failed", "Rejected"), timeout=60)


iv = adm.doc("Interview", IV)
orig_date, from_t, to_t = str(iv["scheduled_on"]), iv["from_time"], iv["to_time"]
new_date = "2026-11-18" if orig_date != "2026-11-18" else "2026-11-19"

r.step(f"1. Reschedule interview {IV} ({orig_date}) -> {new_date}, then Confirm")
rep = order(f"Reschedule interview {IV} to {new_date}, same times.")
r.check(bool(rep.get("ai_action")) and rep.get("can_confirm"), "change card with a Confirm button", rep.get("ai_action") or "")
if rep.get("ai_action"):
    a = adm.action(rep["ai_action"])
    r.check(a["kind"] == "interview_update" and a["agent"] == "operator", "operator agent proposed interview_update")
    a = confirm_and_wait(rep)
    r.check(a["status"] == "Executed", "Executed by the Execution Gateway", (a.get("execution_result") or "")[:90])
    r.check(str(adm.doc("Interview", IV)["scheduled_on"]) == new_date, f"interview now on {new_date}")

r.step("2. Ask for an employee change, then Cancel: nothing is changed")
before = adm.doc("Employee", "HR-EMP-00006")["branch"]
target = "Sousse" if before != "Sousse" else "Sfax"
rep = order(f"Move employee Rim Sfaxi (HR-EMP-00006) to the {target} branch.")
r.check(bool(rep.get("ai_action")), "employee_update prepared", rep.get("ai_action") or "")
if rep.get("ai_action"):
    a = confirm_and_wait(rep, "cancel")
    r.check(a["status"] == "Rejected", "cancelled = Rejected")
    r.check(adm.doc("Employee", "HR-EMP-00006")["branch"] == before, f"branch still {before}")

r.step(f"3. Put the interview back on {orig_date} (keeps the demo data stable)")
rep = order(f"Reschedule interview {IV} to {orig_date}, same times.")
if rep.get("ai_action"):
    a = confirm_and_wait(rep)
    r.check(a["status"] == "Executed" and str(adm.doc("Interview", IV)["scheduled_on"]) == orig_date,
            f"interview back on {orig_date}")
else:
    r.check(False, "restore proposal created")
r.done()
