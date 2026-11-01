"""S12: proof for the jury. (1) The three automated test suites (one test per guardrail: injection,
bypass, disparity, duplicate webhook, PII...). (2) Both audit trails are hash-chained and a tampered copy
is detected. (3) The Monitor's alerts."""

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

import rhflow as r

AGENTS_PY = Path.home() / ".venvs/rhflow-agents/bin/python"
LAYER_PY = r.REPO / "input-security-layer/.venv/bin/python"

r.step("1. Automated test suites")
for label, py, cwd, target in (("agents + hr_ai guardrail tests (tests/)", AGENTS_PY, r.REPO, "tests"),
                               ("guardrails service", AGENTS_PY, r.REPO / "guardrails", "tests"),
                               ("input security layer", LAYER_PY, r.REPO / "input-security-layer", "tests")):
    out = subprocess.run([str(py), "-m", "pytest", "-q", target], cwd=cwd, capture_output=True, text=True, timeout=600)
    summary = (re.findall(r"\d+ passed.*", out.stdout) or [out.stdout.strip().splitlines()[-1] if out.stdout.strip() else "no output"])[-1]
    r.check(out.returncode == 0, label, summary)

r.require_stack("hr_core", "guardrails")
adm = r.admin()

r.step("2. Agent-side audit trail (var/audit.jsonl): verify, then tamper with a copy")
out = subprocess.run([str(AGENTS_PY), "-m", "agents.cli", "audit-verify"], cwd=r.REPO, capture_output=True, text=True)
r.check("OK" in out.stdout, "live chain verified", out.stdout.strip())
with tempfile.TemporaryDirectory() as tmp:
    shutil.copy(r.REPO / "var/audit.jsonl", Path(tmp) / "audit.jsonl")
    lines = (Path(tmp) / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    i = next(n for n, ln in enumerate(lines) if '"proposal_submitted"' in ln or '"tool_call"' in ln)
    entry = json.loads(lines[i])
    entry["agent"] = "someone-else"            # rewrite history in the COPY only
    lines[i] = json.dumps(entry)
    (Path(tmp) / "audit.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = subprocess.run([str(AGENTS_PY), "-m", "agents.cli", "audit-verify"], cwd=r.REPO, capture_output=True,
                         text=True, env={**os.environ, "AGENTS_VAR_DIR": tmp})
    r.check("BROKEN" in out.stdout, f"edited entry #{i} in a copy -> chain BROKEN", out.stdout.strip())

r.step("3. Guardrails audit trail (hash-chained, append-only)")
v = r.guardrails_get("/audit/verify", adm).json()
r.check(v["ok"], "guardrails chain verified", f"{v['entries']} entries, head {v['head'][:24]}...")
entries = r.guardrails_get("/audit?limit=1000", adm).json()
r.info("events: " + ", ".join(f"{k}={n}" for k, n in Counter(e["event"] for e in entries).most_common(8)))
r.check(all(entries[k]["prev_hash"] == entries[k - 1]["entry_hash"] for k in range(1, len(entries))),
        "each entry points to the previous one")

r.step("4. Monitor alerts")
alerts = r.guardrails_get("/alerts?open_only=false", adm).json()
for a in alerts[-5:]:
    r.info(f"[{a['severity']}] {a['rule']}: {a['message'][:110]}")
r.check(isinstance(alerts, list), "guardrails monitor answers", f"{len(alerts)} alerts")
local = [json.loads(x) for x in (r.REPO / "var/alerts.jsonl").read_text().splitlines() if x.strip()]
r.info("agent-side alerts: " + ", ".join(f"{k}={n}" for k, n in Counter(a["event"] for a in local).most_common()))
r.check(any(a["event"] == "webhook_rejected" for a in local), "rejected webhooks raised alerts")
emp = r.employee()
res = r.guardrails_get("/audit/verify", emp)
r.check(res.status_code in (401, 403), "an employee cannot read the audit log", str(res.status_code))
r.done()
