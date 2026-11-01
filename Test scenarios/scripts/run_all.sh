#!/usr/bin/env bash
# Run the test scenarios against the running stack, in order. From the repo root (WSL):
#   bash "Test scenarios/scripts/run_all.sh"            # all 12 (about 25-35 min, mostly model time)
#   bash "Test scenarios/scripts/run_all.sh" fast       # only the ones without the model (about 3 min)
# S03 checks the LATEST ranking: create one first (see 03_cv_ranking_and_fairness.md), or it checks an older one.
set -u
cd "$(dirname "$0")/../.."
P=${P:-$HOME/.venvs/rhflow-agents/bin/python}
FAST="s02_attack_email s03_cv_ranking s07_guardrails_attacks s08_signed_webhooks s09_fail_closed s12_audit_monitor_tests"
ALL="s01_inbox_reply s02_attack_email s03_cv_ranking s04_employee_chat s05_hr_chat_operator s06_chat_injection
     s07_guardrails_attacks s08_signed_webhooks s09_fail_closed s10_orchestrator_agents s11_inbox_digest
     s12_audit_monitor_tests"
[ "${1:-}" = "fast" ] && LIST=$FAST || LIST=$ALL
declare -A RESULT
for s in $LIST; do
  echo; echo "################ $s"
  if "$P" "Test scenarios/scripts/$s.py"; then RESULT[$s]=PASS; else RESULT[$s]=FAIL; fi
done
echo; echo "################ summary"
for s in $LIST; do printf "  %-28s %s\n" "$s" "${RESULT[$s]}"; done
