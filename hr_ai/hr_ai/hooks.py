app_name = "hr_ai"
app_title = "HR Flow AI"
app_publisher = "SOS_Astrpreneur1 team"
app_description = "AI Action approval queue and guardrails for HR Flow"
app_email = "team@rhflow.example"
app_license = "UNLICENSED"  # TODO: team to choose a licence

required_apps = ["frappe", "hrms"]

after_install = "hr_ai.setup.after_install"
after_migrate = "hr_ai.setup.after_migrate"

# Execution Gateway (build plan task 3): approved AI Actions are executed in a background job.
doc_events = {
    "AI Action": {"on_update": "hr_ai.executor.on_update"},
    # Input security layer: a CV attached to an applicant is inspected before anything reads it.
    "Job Applicant": {"on_update": "hr_ai.ingest.on_job_applicant_update"},
}

# Input security layer: pull new mail for the HR inbox (Mailpit) every minute.
scheduler_events = {"cron": {"* * * * *": ["hr_ai.ingest.sync_inbox_job"]}}
# Released mail is sent to the agent service as a signed event (webhooks.py); failed deliveries are
# retried by the same job.

# Apps screen: the ERPNext tile is replaced by the RH Chat tile (boot data only; erpnext is untouched).
extend_bootinfo = "hr_ai.desk.extend_bootinfo"
# Chat users (website users, no Desk) land on the chat page after login; everyone else is unchanged.
get_website_user_home_page = "hr_ai.desk.home_page"

# RH AI tile on the /desk apps screen -> decision page where HR allows or declines agent proposals.
add_to_apps_screen = [
    {
        "name": "hr_ai",
        "logo": "/assets/hr_ai/images/hr-ai-logo.svg",
        "title": "RH AI",
        "route": "/desk/rh-ai",
        "has_permission": "hr_ai.api.check_app_permission",
        "sequence_id": 3,
    }
]
