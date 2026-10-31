"""The Orchestrator routes to specialist subgraphs; unknown agents and audit tampering are caught."""

import json

from langchain_core.messages import AIMessage

from agents.orchestrator import audit
from agents.orchestrator.orchestrator import ask, build_orchestrator
from tests.conftest import FakeLLM


def test_delegates_to_specialist_and_summarises():
    orch = FakeLLM([
        AIMessage("", tool_calls=[{"name": "delegate", "args": {"agent": "assistant", "task": "holiday question"},
                                   "id": "d1"}]),
        AIMessage("The assistant answered."),
    ])
    spec_llm = FakeLLM([AIMessage("Next holiday is Revolution Day.")])
    res = ask("When is the next holiday?", llm=orch, specialist_llm=spec_llm)
    assert res["delegated_to"] == ["assistant"]
    assert res["answer"] == "The assistant answered."
    assert "specialist_result:assistant" in orch.seen[1][-1].content
    assert set(spec_llm.tool_names) == {"search_policies", "get_my_profile", "get_my_leave_balance", "get_holiday_list"}


def test_unknown_specialist_is_rejected():
    orch = FakeLLM([
        AIMessage("", tool_calls=[{"name": "delegate", "args": {"agent": "superuser", "task": "x"}, "id": "d1"}]),
        AIMessage("cannot"),
    ])
    res = ask("do something", llm=orch, specialist_llm=FakeLLM([]))
    assert res["delegated_to"] == []
    assert "unknown specialist" in orch.seen[1][-1].content


def test_graph_has_all_nine_agents():
    class NoLLM:
        def bind_tools(self, tools):
            return self

    nodes = set(build_orchestrator(llm=NoLLM()).get_graph().nodes)
    assert {"orchestrator", "recruitment", "comms", "assistant", "leave", "onboarding", "payroll", "performance",
            "conflict"} <= nodes


def test_audit_chain_detects_tampering():
    from agents.config import get_settings

    for i in range(3):
        audit.log("test", i=i)
    assert audit.verify() == (True, 3)
    path = get_settings().var_dir / "audit.jsonl"
    lines = path.read_text().splitlines()
    entry = json.loads(lines[1])
    entry["i"] = 99
    lines[1] = json.dumps(entry)
    path.write_text("\n".join(lines) + "\n")
    assert audit.verify()[0] is False
