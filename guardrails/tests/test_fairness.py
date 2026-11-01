import copy
import json
from pathlib import Path

import pytest

from guardrails.fairness import FairnessChecker, FairnessConfigError, disparity_report
from guardrails.models import ProposedAction, RecordRef

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "fairness.json"
APPLICANT = RecordRef("Job Applicant", "HR-APP-0001")
SRC = "sha256:" + "a" * 64
JOB_CRITERIA = {"python_experience", "sql", "communication"}


@pytest.fixture
def config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def checker(config):
    return FairnessChecker(config)


def criterion(name="python_experience", score=85,
              justification="5 years building Django REST APIs in production.", source=SRC):
    return {"criterion": name, "score": score, "justification": justification, "source_hash": source}


def rank(payload=None, tool="rank_candidate", sources=(SRC,)):
    if payload is None:
        payload = {"job_opening": "HR-OPN-0001", "score": 82, "criteria": [criterion()]}
    return ProposedAction("a-1", "recruitment_agent", tool, APPLICANT, payload, tuple(sources))


def rules(result):
    return {v.rule for v in result.violations}


# --- happy path -----------------------------------------------------------------

def test_well_justified_ranking_allowed(checker):
    r = checker.check(rank(), job_criteria=JOB_CRITERIA)
    assert r.allowed, r.reasons


def test_words_containing_protected_terms_do_not_false_positive(checker):
    # 'manage', 'message', 'language', 'percentage', 'human', 'women's' style
    # substrings must not trigger: matching is on whole words only.
    j = "Managed a team, strong language skills, human-centred design, high percentage of delivery."
    r = checker.check(rank({"job_opening": "HR-OPN-0001", "score": 70,
                            "criteria": [criterion(justification=j)],
                            "message_language": "en", "stage": "screening"}))
    assert r.allowed, r.reasons


# --- 1. protected attributes blind -------------------------------------------------

@pytest.mark.parametrize("key", ["age", "gender", "Nationality", "dateOfBirth",
                                 "date_of_birth", "Marital Status", "candidate_photo", "religion"])
def test_protected_key_anywhere_denied(checker, key):
    payload = {"job_opening": "HR-OPN-0001", "score": 80, "criteria": [criterion()],
               "candidate": {"profile": {key: "x"}}}
    r = checker.check(rank(payload))
    assert not r.allowed and "protected_attribute" in rules(r)


def test_protected_key_inside_list_denied(checker):
    payload = {"job_opening": "HR-OPN-0001", "score": 80,
               "criteria": [criterion()], "extra": [{"ok": 1}, {"gender": "F"}]}
    assert "protected_attribute" in rules(checker.check(rank(payload)))


def test_protected_key_checked_for_every_tool(checker):
    a = ProposedAction("a-2", "comms_agent", "send_email", APPLICANT,
                       {"recipients": [], "body": "hi", "age": 31}, ())
    assert "protected_attribute" in rules(checker.check(a))


def test_embedded_photo_denied(checker):
    payload = {"job_opening": "HR-OPN-0001", "score": 80, "criteria": [criterion()],
               "attachment": "data:image/png;base64,iVBORw0KGgo="}
    assert "embedded_image" in rules(checker.check(rank(payload)))


# --- 2. job-related justification ---------------------------------------------------

@pytest.mark.parametrize("text", [
    "Young and energetic, a good cultural fit.",
    "She is a mother of two, may be less available.",
    "Too old for a fast-paced startup.",
    "Candidate is a foreigner.",
    "Jeune femme dynamique.",
])
def test_protected_trait_in_justification_denied(checker, text):
    r = checker.check(rank({"job_opening": "HR-OPN-0001", "score": 40,
                            "criteria": [criterion(justification=text)]}))
    assert not r.allowed and "protected_term_in_text" in rules(r)


def test_missing_criteria_denied(checker):
    r = checker.check(rank({"job_opening": "HR-OPN-0001", "score": 90}))
    assert not r.allowed and "justification_missing" in rules(r)


def test_short_justification_denied(checker):
    r = checker.check(rank({"job_opening": "HR-OPN-0001", "score": 90,
                            "criteria": [criterion(justification="good")]}))
    assert "justification_too_short" in rules(r)


def test_unsourced_justification_denied(checker):
    r = checker.check(rank({"job_opening": "HR-OPN-0001", "score": 90,
                            "criteria": [criterion(source="sha256:" + "f" * 64)]}))
    assert "justification_unsourced" in rules(r)


def test_criterion_not_on_job_opening_denied(checker):
    r = checker.check(rank({"job_opening": "HR-OPN-0001", "score": 90,
                            "criteria": [criterion(name="cultural_fit")]}),
                      job_criteria=JOB_CRITERIA)
    assert "criterion_not_job_related" in rules(r)


@pytest.mark.parametrize("score", [-1, 101, "90", True, float("nan"), None])
def test_invalid_score_denied(checker, score):
    r = checker.check(rank({"job_opening": "HR-OPN-0001", "score": score,
                            "criteria": [criterion()]}))
    assert "score_invalid" in rules(r)


def test_extra_field_in_criterion_denied(checker):
    c = criterion()
    c["note"] = "smuggled"
    r = checker.check(rank({"job_opening": "HR-OPN-0001", "score": 80, "criteria": [c]}))
    assert "criterion_malformed" in rules(r)


def test_missing_job_opening_denied(checker):
    r = checker.check(rank({"score": 80, "criteria": [criterion()]}))
    assert "job_opening_missing" in rules(r)


# --- 3. score-disparity (four-fifths rule) -------------------------------------------

def outcomes(group, total, selected):
    return [(group, i < selected) for i in range(total)]


def test_equal_rates_ok():
    rep = disparity_report(outcomes("A", 10, 5) + outcomes("B", 10, 5))
    assert rep.status == "ok" and rep.flagged == ()


def test_adverse_impact_flagged():
    # A: 60% selected, B: 30% -> ratio 0.5 < 0.8
    rep = disparity_report(outcomes("A", 10, 6) + outcomes("B", 10, 3))
    assert rep.status == "adverse_impact" and rep.flagged == ("B",)
    b = next(g for g in rep.groups if g.group == "B")
    assert b.impact_ratio == pytest.approx(0.5)


def test_exactly_four_fifths_not_flagged():
    rep = disparity_report(outcomes("A", 10, 5) + outcomes("B", 10, 4))  # 0.4/0.5 = 0.8
    assert rep.status == "ok"


def test_small_groups_not_judged():
    rep = disparity_report(outcomes("A", 10, 6) + outcomes("B", 3, 0))
    assert rep.status == "insufficient_data" and rep.flagged == ()


def test_nobody_selected_is_not_adverse():
    rep = disparity_report(outcomes("A", 10, 0) + outcomes("B", 10, 0))
    assert rep.status == "ok"


def test_checker_uses_configured_threshold(checker):
    rep = checker.disparity(outcomes("A", 10, 6) + outcomes("B", 10, 3))
    assert rep.threshold == 0.8 and rep.status == "adverse_impact"


# --- config safety -------------------------------------------------------------

def test_shipped_config_loads():
    FairnessChecker.from_file(CONFIG_PATH)


def test_unknown_top_level_key_refuses_to_load(config):
    bad = copy.deepcopy(config)
    bad["protected_kyes"] = ["age"]
    with pytest.raises(FairnessConfigError):
        FairnessChecker(bad)


def test_typo_in_scored_tool_refuses_to_load(config):
    bad = copy.deepcopy(config)
    bad["scored_tools"]["rank_candidate"]["min_justifcation_chars"] = 5
    with pytest.raises(FairnessConfigError):
        FairnessChecker(bad)


@pytest.mark.parametrize("t", [0, 1.5, -0.2, "0.8"])
def test_bad_threshold_refuses_to_load(config, t):
    bad = copy.deepcopy(config)
    bad["disparity"]["threshold"] = t
    with pytest.raises(FairnessConfigError):
        FairnessChecker(bad)


def test_empty_protected_keys_refuses_to_load(config):
    bad = copy.deepcopy(config)
    bad["protected_keys"] = []
    with pytest.raises(FairnessConfigError):
        FairnessChecker(bad)
