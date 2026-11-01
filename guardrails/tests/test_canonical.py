import pytest

from guardrails.canonical import CanonicalizationError, action_hash, canonical_json
from guardrails.models import ProposedAction, RecordRef


def act(payload, **kw):
    base = dict(action_id="a-1", agent="comms_agent", tool="send_email",
                subject=RecordRef("Job Applicant", "HR-APP-0001"), payload=payload,
                source_hashes=("sha256:x",))
    base.update(kw)
    return ProposedAction(**base)


def test_key_order_does_not_change_hash():
    assert action_hash(act({"a": 1, "b": 2})) == action_hash(act({"b": 2, "a": 1}))


def test_integral_float_equals_int():
    assert canonical_json({"s": 82.0}) == canonical_json({"s": 82})


def test_unicode_kept_as_utf8():
    assert canonical_json({"t": "é"}) == '{"t":"é"}'.encode("utf-8")


@pytest.mark.parametrize("change", [
    {"payload": {"body": "Hello!"}},              # one character
    {"action_id": "a-2"},
    {"agent": "recruitment_agent"},
    {"tool": "propose_interview_slots"},
    {"subject": RecordRef("Job Applicant", "HR-APP-0002")},
    {"source_hashes": ("sha256:y",)},
])
def test_any_change_changes_hash(change):
    assert action_hash(act({"body": "Hello"})) != action_hash(act(**{"payload": {"body": "Hello"}, **change}))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), {1: "x"}, {"s": {1, 2}}])
def test_unhashable_values_refused(bad):
    with pytest.raises(CanonicalizationError):
        canonical_json(bad if isinstance(bad, dict) else {"v": bad})
