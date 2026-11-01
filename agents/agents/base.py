"""The LangGraph loop shared by all agents. qwen3:14b decides; code enforces the limits.

    START -> model --(tool calls?)--> guarded_tools --> model ... --> END
                   \\--(no tool calls / budget hit)--> END

`guarded_tools` is our own node (not LangGraph's prebuilt ToolNode) so that every call is checked:
allowlist, argument schema, tool-call budget. Tool results are wrapped as untrusted <data>.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from agents.context.context_builder import system_prompt, wrap_data
from agents.context.run_context import RunContext, current, run_context
from agents.orchestrator import audit
from agents.orchestrator.budgets import Budget
from agents.orchestrator.llm_client import make_llm
from agents.validation.validator import ValidationError, validate_tool_call


@dataclass
class AgentSpec:
    name: str
    description: str               # one line, shown to the Orchestrator
    role: str                      # role-specific part of the trusted system prompt
    tools: list[BaseTool]
    budget: dict = field(default_factory=dict)   # overrides for Budget.default()

    @property
    def tool_names(self) -> set[str]:
        return {t.name for t in self.tools}


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    steps: Annotated[int, operator.add]
    tool_calls: Annotated[int, operator.add]
    tokens: Annotated[int, operator.add]
    stopped: str | None


def _tokens(msg: AIMessage) -> int:
    usage = getattr(msg, "usage_metadata", None) or {}
    return int(usage.get("total_tokens", 0))


def build_agent(spec: AgentSpec, llm: Any = None):
    """Compile the LangGraph for one agent. `llm` can be injected (tests); default is qwen3:14b."""
    budget = Budget.default(**spec.budget)
    model = (llm or make_llm()).bind_tools(spec.tools)
    tools = {t.name: t for t in spec.tools}
    prompt = SystemMessage(system_prompt(spec.role))

    def call_model(state: AgentState) -> dict:
        if reason := budget.exceeded(steps=state["steps"], tool_calls=state["tool_calls"], tokens=state["tokens"]):
            audit.log("budget_stop", agent=spec.name, run_id=current().run_id, reason=reason)
            return {"stopped": reason}
        reply = model.invoke([prompt, *state["messages"]])
        return {"messages": [reply], "steps": 1, "tokens": _tokens(reply)}

    def guarded_tools(state: AgentState) -> dict:
        ctx = current()
        last = state["messages"][-1]
        out, used = [], 0
        for call in last.tool_calls:
            name, args, call_id = call["name"], call.get("args") or {}, call["id"]
            try:
                validate_tool_call(spec.name, name, spec.tool_names)
            except ValidationError as exc:
                audit.log("tool_rejected", agent=spec.name, run_id=ctx.run_id, tool=name, reason=str(exc))
                out.append(ToolMessage(content=f"ERROR: {exc}. Use only your listed tools.", tool_call_id=call_id,
                                       name=name, status="error"))
                continue
            if state["tool_calls"] + used >= budget.max_tool_calls:
                audit.log("budget_stop", agent=spec.name, run_id=ctx.run_id, reason="tool-call budget", tool=name)
                out.append(ToolMessage(content="ERROR: tool-call budget reached. Answer now without tools.",
                                       tool_call_id=call_id, name=name, status="error"))
                continue
            used += 1
            try:
                result = tools[name].invoke(args)
                status = "error" if isinstance(result, dict) and "error" in result else "success"
            except Exception as exc:  # schema errors and unexpected failures go back to the model as data
                result, status = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}, "error"
            audit.log("tool_call", agent=spec.name, run_id=ctx.run_id, tool=name, args=args, status=status)
            out.append(ToolMessage(content=wrap_data(f"tool_result:{name}", result), tool_call_id=call_id,
                                   name=name, status=status))
        return {"messages": out, "tool_calls": used}

    def after_model(state: AgentState) -> str:
        if state.get("stopped"):
            return END
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else END

    graph = StateGraph(AgentState)
    graph.add_node("model", call_model)
    graph.add_node("tools", guarded_tools)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", after_model, {"tools": "tools", END: END})
    graph.add_edge("tools", "model")
    return graph.compile(name=spec.name)


def final_text(state: dict) -> str:
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage) and msg.content and not msg.tool_calls:
            return str(msg.content).strip()
    return state.get("stopped") or ""


def run_agent(spec: AgentSpec, task: str, *, ctx: RunContext | None = None, llm: Any = None,
              on_event: Callable[[str, dict], None] | None = None) -> dict:
    """Run one specialist agent on a task. Returns answer, proposals and a trace of tool calls."""
    ctx = ctx or RunContext(agent=spec.name)
    graph = build_agent(spec, llm)
    budget = Budget.default(**spec.budget)
    with run_context(ctx):
        audit.log("run_start", agent=spec.name, run_id=ctx.run_id, caller=ctx.caller_employee)
        state = {"messages": [HumanMessage(task)], "steps": 0, "tool_calls": 0, "tokens": 0, "stopped": None}
        final: dict = state
        for update in graph.stream(state, {"recursion_limit": 2 * budget.max_steps + 6}, stream_mode="values"):
            final = update
            if on_event:
                last = update["messages"][-1]
                for call in getattr(last, "tool_calls", None) or []:
                    on_event("tool_call", {"agent": spec.name, "tool": call["name"], "args": call.get("args")})
        audit.log("run_end", agent=spec.name, run_id=ctx.run_id, steps=final["steps"], tool_calls=final["tool_calls"],
                  proposals=[p["id"] for p in ctx.proposals], stopped=final.get("stopped"))
        return {"agent": spec.name, "run_id": ctx.run_id, "answer": final_text(final),
                "proposals": [{"id": p["id"], "rhflow_name": p.get("rhflow_name"), "kind": p["kind"], "subject": p["subject"]} for p in ctx.proposals],
                "steps": final["steps"], "tool_calls": final["tool_calls"], "stopped": final.get("stopped")}
