app_name = "hr_ai"
app_title = "RH Flow AI"
app_publisher = "SOS_Astrpreneur1 team"
app_description = "AI Action approval queue and guardrails for RH Flow"
app_email = "team@rhflow.example"
app_license = "UNLICENSED"  # TODO: team to choose a licence

required_apps = ["frappe", "hrms"]

after_install = "hr_ai.approval.ensure_signing_key"

# Execution Gateway (build plan task 3): approved AI Actions are executed in a background job.
doc_events = {"AI Action": {"on_update": "hr_ai.executor.on_update"}}
# TODO (build plan task 4): signed webhooks to the agent service (webhooks.py)

# RH AI tile on the /desk apps screen -> decision page where HR allows or declines agent proposals.
add_to_apps_screen = [
    {
        "name": "hr_ai",
        "logo": "/assets/hr_ai/images/rh-ai-logo.svg",
        "title": "RH AI",
        "route": "/desk/rh-ai",
        "has_permission": "hr_ai.api.check_app_permission",
        "sequence_id": 3,
    }
]
