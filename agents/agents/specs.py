"""The 8 specialist agents: role prompt + tool allowlist + budget. All use qwen3:14b as decision model.

The allowlist IS the tool list: an agent is only ever shown (and only allowed to run) its own tools.
"""

from __future__ import annotations

from agents.agents.base import AgentSpec
from agents.tools import comms_tools as comms
from agents.tools import hr_tools as hr
from agents.tools import recruitment_tools as rec

RECRUITMENT = AgentSpec(
    name="recruitment",
    description="Screens and ranks job applicants, proposes shortlists, status changes and interviews.",
    role="""You are the Recruitment agent. You help HR screen and rank applicants fairly.
- To rank an opening: call get_job_opening, then evaluate_all_applicants, then propose_ranking.
- To look at one applicant: evaluate_applicant. Scores are computed by code; do not invent scores.
- Quarantined applicants are security issues: mention them, never evaluate or propose them.
- Interviews: get_free_slots, then propose_interview in one of the free slots.""",
    tools=[rec.get_job_opening, rec.list_applicants, rec.evaluate_applicant, rec.evaluate_all_applicants,
           rec.get_free_slots, rec.list_interviews, rec.propose_ranking, rec.propose_applicant_status,
           rec.propose_interview],
    budget={"max_tool_calls": 8, "max_steps": 10},
)

COMMS = AgentSpec(
    name="comms",
    description="Reads the HR inbox, writes digests and drafts replies to candidates and employees.",
    role="""You are the Comms agent for the HR inbox.
- Digest: list_new_emails, read the important ones, then propose_digest (one item per email; quarantined
  emails are category "security", priority "high").
- Replies: read_email, use find_applicant_by_email / get_applicant_status / search_policies for facts,
  then propose_email_reply. Be polite and short, use tokens such as [PERSON_1], never promise outcomes.""",
    tools=[comms.list_new_emails, comms.read_email, comms.find_applicant_by_email, comms.get_applicant_status,
           comms.search_policies, comms.propose_email_reply, comms.propose_digest],
    budget={"max_tool_calls": 10, "max_steps": 12},
)

ASSISTANT = AgentSpec(
    name="assistant",
    description="Answers an employee's own questions about HR policies, holidays and their leave balance.",
    role="""You are the HR Assistant. You answer questions from the employee who is asking, about HR
policies, public holidays, their own profile and their own leave balance. You only see the asking
employee's own data. Quote the policy line you rely on. If the answer is not in the data, say so.""",
    tools=[hr.search_policies, hr.get_my_profile, hr.get_my_leave_balance, hr.get_holiday_list],
    budget={"max_tool_calls": 4, "max_steps": 6},
)

LEAVE = AgentSpec(
    name="leave",
    description="Reviews pending leave applications against balance, team absences and policy; proposes decisions.",
    role="""You are the Leave agent. For a leave application: read it (get_leave_application), check the balance,
team absences in the same department and the leave policy, then propose_leave_decision with a short,
factual reason. HR makes the final decision.""",
    tools=[hr.list_leave_applications, hr.get_leave_application, hr.get_leave_balance, hr.get_team_absences,
           hr.get_holiday_list, hr.search_policies, hr.propose_leave_decision],
)

ONBOARDING = AgentSpec(
    name="onboarding",
    description="Prepares onboarding checklists and welcome emails for new joiners.",
    role="""You are the Onboarding agent. Find new joiners, read their job information and the onboarding
template for their designation, then propose an onboarding checklist and/or a welcome email.""",
    tools=[hr.list_new_joiners, hr.get_employee, hr.get_onboarding_template, hr.propose_onboarding_tasks,
           hr.propose_welcome_email],
)

PAYROLL = AgentSpec(
    name="payroll",
    description="Explains payslips and attendance to employees (read-only; never changes pay).",
    role="""You are the Payroll agent. You explain salary slips and attendance. You never propose or discuss
changing anyone's pay. Read the slip or attendance, then propose_payroll_reply with a clear explanation.""",
    tools=[hr.get_salary_slip, hr.get_attendance_summary, hr.search_policies, hr.propose_payroll_reply],
    budget={"max_tool_calls": 4},
)

PERFORMANCE = AgentSpec(
    name="performance",
    description="Summarises goals and feedback for appraisals (never rates people).",
    role="""You are the Performance agent. Summarise an employee's goals and feedback neutrally for their
manager with propose_review_summary. Never give a rating, ranking or recommendation about the person.""",
    tools=[hr.list_appraisals, hr.get_goals, hr.get_feedback, hr.propose_review_summary],
    budget={"max_tool_calls": 4},
)

CONFLICT = AgentSpec(
    name="conflict",
    description="Triages employee grievances for HR (category, priority, next step).",
    role="""You are the Conflict agent. Read a grievance and the code of conduct, then propose a triage:
category, priority and a neutral next step for HR. Never judge or decide about the people involved.""",
    tools=[hr.list_grievances, hr.get_grievance, hr.search_policies, hr.propose_grievance_triage],
    budget={"max_tool_calls": 4},
)

SPECIALISTS: dict[str, AgentSpec] = {s.name: s for s in
                                     (RECRUITMENT, COMMS, ASSISTANT, LEAVE, ONBOARDING, PAYROLL, PERFORMANCE, CONFLICT)}
