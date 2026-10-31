"""Guardrail: scoring uses job-related criteria only (protected attributes cannot change a score)."""

from agents.tools.recruitment_tools import parse_requirements, score
from agents.validation.schemas import CandidateEvaluation

REQ = {"required": ["Python", "SQL", "Docker"], "nice_to_have": ["React"], "min_years": 3, "degree_field": "CS"}


def ev(years=5, level="master", relevant=True):
    return CandidateEvaluation(skills=[], years_experience=years, degree_level=level, degree_relevant=relevant,
                               summary="")


def test_score_depends_only_on_job_criteria():
    a = score(REQ, ev(), {"Python", "SQL"})
    b = score(REQ, ev(), {"Python", "SQL"})
    assert a == b
    assert score(REQ, ev(), {"Python", "SQL", "Docker"})["score"] > a["score"]
    assert score(REQ, ev(years=1), {"Python", "SQL"})["score"] < a["score"]       # under minimum years


def test_score_function_has_no_protected_inputs():
    import inspect

    from agents.validation import schemas

    fields = set(schemas.CandidateEvaluation.model_fields)
    assert not fields & {"age", "gender", "nationality", "marital_status", "religion", "photo", "name"}
    assert set(inspect.signature(score).parameters) == {"req", "ev", "found"}


def test_parse_requirements_from_opening():
    op = {"description": "<p><b>Required skills:</b> Python, SQL, REST APIs.</p><p><b>Nice to have:</b> React.</p>"
                         "<p><b>Experience:</b> at least 4 years. <b>Education:</b> degree in Computer Science.</p>"}
    req = parse_requirements(op)
    assert req["required"] == ["Python", "SQL", "REST APIs"] and req["min_years"] == 4
