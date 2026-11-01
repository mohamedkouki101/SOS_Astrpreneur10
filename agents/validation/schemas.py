"""Strict schemas (Pydantic) for model outputs and AI Action proposals. Unknown fields are rejected."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------- extraction (Recruitment, per CV)
class SkillFinding(Strict):
    skill: str
    found: bool
    quote: str | None = Field(default=None, max_length=300, description="Exact line from the CV proving it")


class CandidateEvaluation(Strict):
    skills: list[SkillFinding]
    years_experience: int = Field(ge=0, le=50)
    degree_level: Literal["none", "bachelor", "master", "engineering", "phd", "other"]
    degree_relevant: bool
    summary: str = Field(max_length=400)


# ---------------------------------------------------------------- proposal payloads
class Evidence(Strict):
    skill: str
    quote: str = Field(max_length=300)
    source_hash: str = Field(min_length=64, max_length=64)


class RankingEntry(Strict):
    applicant: str
    rank: int = Field(ge=1)
    score: float
    justification: str = Field(max_length=400)
    evidence: list[Evidence]


class RankingPayload(Strict):
    opening: str
    entries: list[RankingEntry] = Field(min_length=1, max_length=50)
    quarantined: list[str] = []
    note: str = Field(default="", max_length=400)


class ApplicantStatusPayload(Strict):
    applicant: str
    status: Literal["Shortlisted", "Rejected", "Hold", "Replied"]
    reason: str = Field(max_length=400)


class InterviewPayload(Strict):
    applicant: str
    interview_type: Literal["HR Screening", "Technical Interview", "Manager Interview"]
    date: dt.date
    from_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    to_time: str = Field(pattern=r"^\d{2}:\d{2}$")


class EmailReplyPayload(Strict):
    email_id: str
    # [EMAIL_n]: local pipeline token. <SENDER>: input-layer email, the gateway replies to the original sender.
    to: str = Field(pattern=r"^(\[EMAIL_\d+\]|<SENDER>)$", description="Masked recipient")
    subject: str = Field(max_length=200)
    body: str = Field(max_length=3000)
    doc_id: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$",
                               description="Input-layer document the <TOKENS> belong to")

    @model_validator(mode="after")
    def sender_needs_doc(self) -> EmailReplyPayload:
        if (self.to == "<SENDER>") != (self.doc_id is not None):
            raise ValueError("<SENDER> replies need the doc_id of the email, and only they have one")
        return self


class DigestItem(Strict):
    email_id: str
    priority: Literal["high", "medium", "low"]
    category: Literal["candidate", "employee", "vendor", "internal", "security", "other"]
    summary: str = Field(max_length=300)


class DigestPayload(Strict):
    items: list[DigestItem] = Field(min_length=1, max_length=100)


class LeaveDecisionPayload(Strict):
    application: str
    decision: Literal["Approved", "Rejected"]
    reason: str = Field(max_length=400)


class OnboardingTasksPayload(Strict):
    employee: str
    tasks: list[str] = Field(min_length=1, max_length=20)


class WelcomeEmailPayload(Strict):
    employee: str
    subject: str = Field(max_length=200)
    body: str = Field(max_length=3000)


class PayrollReplyPayload(Strict):
    employee: str
    body: str = Field(max_length=3000)


class ReviewSummaryPayload(Strict):
    employee: str
    summary: str = Field(max_length=2000)


class GrievanceTriagePayload(Strict):
    grievance: str
    category: Literal["harassment", "discrimination", "workload", "pay", "management", "safety", "other"]
    priority: Literal["urgent", "high", "normal", "low"]
    next_step: str = Field(max_length=400)


PROPOSAL_SCHEMAS: dict[str, type[Strict]] = {
    "ranking": RankingPayload,
    "applicant_status": ApplicantStatusPayload,
    "interview": InterviewPayload,
    "email_reply": EmailReplyPayload,
    "digest": DigestPayload,
    "leave_decision": LeaveDecisionPayload,
    "onboarding_tasks": OnboardingTasksPayload,
    "welcome_email": WelcomeEmailPayload,
    "payroll_reply": PayrollReplyPayload,
    "review_summary": ReviewSummaryPayload,
    "grievance_triage": GrievanceTriagePayload,
}

# Which agent may propose which kind (default-deny).
PROPOSAL_KINDS_BY_AGENT: dict[str, set[str]] = {
    "recruitment": {"ranking", "applicant_status", "interview"},
    "comms": {"email_reply", "digest"},
    "leave": {"leave_decision"},
    "onboarding": {"onboarding_tasks", "welcome_email"},
    "payroll": {"payroll_reply"},
    "performance": {"review_summary"},
    "conflict": {"grievance_triage"},
}

FREE_TEXT_FIELDS = {"justification", "reason", "note", "body", "subject", "summary", "next_step", "tasks", "quote"}
