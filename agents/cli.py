"""Command line for the HR Flow agents.

    python -m agents.cli ask "Rank the applicants for Senior Python Developer"
    python -m agents.cli ask "How many casual leave days do I have left?" --as HR-EMP-00007
    python -m agents.cli run leave "Review leave application HR-LAP-2026-00003"
    python -m agents.cli ingest path/to/cv.pdf
    python -m agents.cli proposals
    python -m agents.cli graph            # Mermaid diagram of the orchestrator graph
    python -m agents.cli audit-verify
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _print_event(kind: str, data: dict) -> None:
    if kind == "delegate":
        print(f"  → delegate to [{data['agent']}]: {data['task'][:100]}", flush=True)
    else:
        args = json.dumps(data.get("args") or {}, ensure_ascii=False, default=str)
        print(f"    · {data['agent']}.{data['tool']}({args[:100]})", flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agents.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask", help="send a request to the Orchestrator")
    a.add_argument("request")
    a.add_argument("--as", dest="caller", help="employee ID of the person asking (own-data tools)")
    r = sub.add_parser("run", help="run one specialist agent directly")
    r.add_argument("agent")
    r.add_argument("task")
    r.add_argument("--as", dest="caller")
    i = sub.add_parser("ingest", help="run the 4-stage security pipeline on a file")
    i.add_argument("path")
    sub.add_parser("proposals", help="list proposals waiting for approval")
    sub.add_parser("graph", help="print the orchestrator graph (Mermaid)")
    sub.add_parser("audit-verify", help="verify the hash chain of the agent audit trail")
    args = p.parse_args(argv)

    if args.cmd == "ask":
        from agents.orchestrator.orchestrator import ask

        t = time.time()
        res = ask(args.request, caller_employee=args.caller, on_event=_print_event)
        print(f"\n{res['answer']}\n")
        for prop in res["proposals"]:
            print(f"  proposal {prop.get('rhflow_name') or prop['id']} [{prop['kind']}] {prop['subject']}  (status: Proposed)")
        print(f"  ({time.time() - t:.0f}s, delegated to: {', '.join(res['delegated_to']) or '-'}"
              f"{', stopped: ' + res['stopped'] if res['stopped'] else ''})")
    elif args.cmd == "run":
        from agents.agents.base import run_agent
        from agents.agents.specs import SPECIALISTS
        from agents.context.run_context import RunContext

        spec = SPECIALISTS.get(args.agent)
        if not spec:
            print(f"unknown agent; choose from: {', '.join(SPECIALISTS)}", file=sys.stderr)
            return 2
        t = time.time()
        res = run_agent(spec, args.task, ctx=RunContext(agent=spec.name, caller_employee=args.caller),
                        on_event=_print_event)
        print(f"\n{res['answer']}\n")
        for prop in res["proposals"]:
            print(f"  proposal {prop.get('rhflow_name') or prop['id']} [{prop['kind']}] {prop['subject']}  (status: Proposed)")
        print(f"  ({time.time() - t:.0f}s, {res['tool_calls']} tool calls"
              f"{', stopped: ' + res['stopped'] if res['stopped'] else ''})")
    elif args.cmd == "ingest":
        from agents.ingest.pipeline import ingest_bytes

        path = Path(args.path)
        res = ingest_bytes(f"file/{path.name}", path.name, path.read_bytes())
        print(json.dumps(res.summary(), indent=1))
        if res.status == "ok":
            print("\n--- masked text sent to the model ---\n" + res.text[:1500])
    elif args.cmd == "proposals":
        from agents.tools.outbox import list_actions

        for a in list_actions("Proposed"):
            print(f"{a['id']}  {a['created']}  {a['agent']:12s} {a['kind']:16s} {a['subject']}")
    elif args.cmd == "graph":
        from agents.orchestrator.orchestrator import build_orchestrator

        class _NoLLM:
            def bind_tools(self, tools):
                return self

        print(build_orchestrator(llm=_NoLLM()).get_graph().draw_mermaid())
    elif args.cmd == "audit-verify":
        from agents.orchestrator.audit import verify

        ok, n = verify()
        print(f"audit chain {'OK' if ok else 'BROKEN'} ({n} entries)")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
