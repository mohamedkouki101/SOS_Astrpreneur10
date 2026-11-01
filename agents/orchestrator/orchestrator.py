"""The Orchestrator: a LangGraph supervisor. qwen3:14b decides which specialist handles a request.

    START -> orchestrator --delegate(agent=X)--> X (specialist subgraph) --> orchestrator ... -> END
                         \\--other tool--> tools -----------------------------^
Each specialist node runs that agent's own compiled LangGraph (agents/agents/base.py) in its own
run context, with its own read-only credentials and tool allowlist.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Callable, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from agents.agents.base import AgentSpec, final_text, run_agent
from agents.agents.specs import SPECIALISTS
from agents.context.context_builder import system_prompt, wrap_data
from agents.context.run_context import RunContext, current, run_context
from agents.orchestrator import audit
from agents.orchestrator.budgets import Budget
from agents.orchestrator.llm_client import make_llm
from agents.tools import orchestrator_tools as ot
from agents.validation.validator import ValidationError, validate_tool_call

MAX_DELEGATIONS = 3

ORCHESTRATOR = AgentSpec(
    name="orchestrator",
    description="Routes requests to the right specialist agent.",
    role="You are the Orchestrator of HR Flow. You route each request to the right specialist agent:\n"
    + "\n".join(f"- {s.name}: {s.description}" for s in SPECIALISTS.values())
    + "\nDelegate to ONE specialist at a time with a clear task that includes any IDs from the request. "
      "After the specialist answers, summarise the result for the user in 2-4 sentences, including any "
      "proposal IDs (they wait for human approval). Use list_pending_events when asked what needs attention.",
    tools=[ot.delegate, ot.list_pending_events, ot.get_ai_action_status],
    budget={"max_tool_calls": 5, "max_steps": 8},
)


class OrchestratorState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    steps: Annotated[int, operator.add]
    tool_calls: Annotated[int, operator.add]
    delegations: Annotated[int, operator.add]
    stopped: str | None
    results: Annotated[list[dict], operator.add]


def build_orchestrator(llm: Any = None, specialist_llm: Any = None,
                       on_event: Callable[[str, dict], None] | None = None):
    budget = Budget.default(**ORCHESTRATOR.budget)
    model = (llm or make_llm()).bind_tools(ORCHESTRATOR.tools)
    prompt = SystemMessage(system_prompt(ORCHESTRATOR.role))
    read_tools = {t.name: t for t in ORCHESTRATOR.tools if t.name != "delegate"}

    def orchestrate(state: OrchestratorState) -> dict:
        if reason := budget.exceeded(steps=state["steps"], tool_calls=state["tool_calls"], tokens=0):
            audit.log("budget_stop", agent="orchestrator", run_id=current().run_id, reason=reason)
            return {"stopped": reason}
        reply = model.invoke([prompt, *state["messages"]])
        return {"messages": [reply], "steps": 1}

    def route(state: OrchestratorState) -> str:
        if state.get("stopped"):
            return END
        last = state["messages"][-1]
        if not (isinstance(last, AIMessage) and last.tool_calls):
            return END
        first = last.tool_calls[0]
        agent = (first.get("args") or {}).get("agent")
        if first["name"] == "delegate" and agent in SPECIALISTS:
            return agent
        return "tools"

    def tools_node(state: OrchestratorState) -> dict:
        ctx, out, used = current(), [], 0
        for call in state["messages"][-1].tool_calls:
            name, args = call["name"], call.get("args") or {}
            try:
                validate_tool_call("orchestrator", name, ORCHESTRATOR.tool_names)
                if name == "delegate":
                    raise ValidationError(f"unknown specialist '{args.get('agent')}'" if args.get("agent") not in
                                          SPECIALISTS else "one delegation per step; call delegate again")
            except ValidationError as exc:
                audit.log("tool_rejected", agent="orchestrator", run_id=ctx.run_id, tool=name, reason=str(exc))
                out.append(ToolMessage(content=f"ERROR: {exc}", tool_call_id=call["id"], name=name, status="error"))
                continue
            used += 1
            result = read_tools[name].invoke(args)
            audit.log("tool_call", agent="orchestrator", run_id=ctx.run_id, tool=name, args=args)
            out.append(ToolMessage(content=wrap_data(f"tool_result:{name}", result), tool_call_id=call["id"], name=name))
        return {"messages": out, "tool_calls": used}

    def specialist_node(spec: AgentSpec):
        def node(state: OrchestratorState) -> dict:
            parent = current()
            out, results, handled = [], [], False
            for call in state["messages"][-1].tool_calls:
                args = call.get("args") or {}
                if not handled and call["name"] == "delegate" and args.get("agent") == spec.name:
                    handled = True
                    if state["delegations"] >= MAX_DELEGATIONS:
                        content = f"ERROR: delegation budget reached ({MAX_DELEGATIONS}). Answer the user now."
                    else:
                        if on_event:
                            on_event("delegate", {"agent": spec.name, "task": args.get("task", "")})
                        audit.log("delegate", agent="orchestrator", run_id=parent.run_id, to=spec.name)
                        child = RunContext(agent=spec.name, caller_employee=parent.caller_employee)
                        res = run_agent(spec, str(args.get("task", "")), ctx=child, llm=specialist_llm,
                                        on_event=on_event)
                        results.append(res)
                        content = wrap_data(f"specialist_result:{spec.name}",
                                            {k: res[k] for k in ("answer", "proposals", "stopped")})
                    out.append(ToolMessage(content=content, tool_call_id=call["id"], name="delegate"))
                else:
                    out.append(ToolMessage(content="ERROR: one action per step; call again.", tool_call_id=call["id"],
                                           name=call["name"], status="error"))
            return {"messages": out, "tool_calls": 1, "delegations": 1, "results": results}
        return node

    graph = StateGraph(OrchestratorState)
    graph.add_node("orchestrator", orchestrate)
    graph.add_node("tools", tools_node)
    for spec in SPECIALISTS.values():
        graph.add_node(spec.name, specialist_node(spec))
        graph.add_edge(spec.name, "orchestrator")
    graph.add_edge(START, "orchestrator")
    graph.add_conditional_edges("orchestrator", route, {**{n: n for n in SPECIALISTS}, "tools": "tools", END: END})
    graph.add_edge("tools", "orchestrator")
    return graph.compile(name="orchestrator")


def ask(request: str, *, caller_employee: str | None = None, llm: Any = None, specialist_llm: Any = None,
        on_event: Callable[[str, dict], None] | None = None) -> dict:
    """Entry point for chat requests, webhooks and scheduled triggers."""
    ctx = RunContext(agent="orchestrator", caller_employee=caller_employee)
    graph = build_orchestrator(llm, specialist_llm, on_event)
    with run_context(ctx):
        audit.log("run_start", agent="orchestrator", run_id=ctx.run_id, caller=caller_employee)
        state = {"messages": [HumanMessage(request)], "steps": 0, "tool_calls": 0, "delegations": 0,
                 "stopped": None, "results": []}
        final = graph.invoke(state, {"recursion_limit": 40})
        proposals = [p for r in final["results"] for p in r["proposals"]]
        audit.log("run_end", agent="orchestrator", run_id=ctx.run_id, delegations=final["delegations"],
                  proposals=[p["id"] for p in proposals], stopped=final.get("stopped"))
        return {"answer": final_text(final), "delegated_to": [r["agent"] for r in final["results"]],
                "proposals": proposals, "run_id": ctx.run_id, "stopped": final.get("stopped")}
