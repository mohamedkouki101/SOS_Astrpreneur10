# Data_set — synthetic demo data

Fully **fictional** HR data for the SOS_Astrpreneur1 demo: a Tunisian company (`Bara3am`) with employees, leave, attendance and a recruitment pipeline, plus CVs, emails and policies for the AI agents. No real person or company is represented. Emails use the reserved `example.com` / `.example` domains.

## What's inside (`dataset/`)

| Path | Content | Loaded into the HR site |
|---|---|---|
| `hrms/01_branches.json` | 4 branches (Tunis HQ, Sfax, Sousse, Remote) | yes |
| `hrms/02_holiday_lists.json` | "Tunisia 2026": weekends + public holidays (religious dates approximate) | yes |
| `hrms/02b_holiday_list_assignments.json` | Assigns the holiday list to the company (required in v16) | yes |
| `hrms/03_skills.json` | 35 skills | yes |
| `hrms/04_interview_types.json` | HR Screening, Technical, Manager | yes |
| `hrms/05_employees.json` | 40 employees across 9 departments, with reporting lines (1 has left) | yes |
| `hrms/06_leave_allocations.json` | 2026 Casual/Sick/Privilege allocations (pro-rated) | yes, submitted |
| `hrms/07_leave_applications.json` | 26 requests: Approved, Rejected, and **Open (pending approval)** | yes |
| `hrms/08_attendance.json` | Last 10 working days (Present, WFH, Half Day, Absent) | yes, submitted |
| `hrms/09_job_openings.json` | 6 openings with required/nice-to-have skills and TND salary ranges | yes |
| `hrms/10_job_applicants.json` | 93 applicants (15 per opening + 3 attack CVs) | yes, CV attached |
| `hrms/11_interviews.json` | 10 interviews (past "Under Review", upcoming "Pending") | yes |
| `cvs/*.pdf` | One CV per applicant. About half include age/gender/nationality for the PII stage to strip | attached to applicants |
| `emails/*.eml` | 18 inbox emails (candidates, employees, vendor) + 1 phishing/injection email | no: agent input |
| `policies/*.md` | Leave, remote work, recruitment, conduct, expenses policies | no: Q&A (RAG) input |
| `calendar/free_slots.json` | Interviewer free slots, 2–13 Nov 2026 | no: scheduling input |
| `ranking_testset/expected_order.json` | Expected ranking per opening, from a job-related-only score | no: ranking tests |
| `ranking_testset/attacks.json` | The 3 prompt-injection CVs and what must happen to them | no: guardrail tests |
| `manifest.json` | Seed, reference date (2026-10-30), counts | — |

**Attack samples for the demo:**
- `APP-ATTACK-1`: white, invisible text in the PDF.
- `APP-ATTACK-2`: 1-pt text in the PDF.
- `APP-ATTACK-3`: an injection inside the cover letter.
- `emails/099_attack_phishing_injection.eml`: a hidden HTML instruction plus a phishing link.

A human reading these CVs doesn't see the payload, but text extraction does. That's exactly what the injection scanner has to catch.

## Load into the site (Docker)

Run from the repo root, with the stack up and the setup wizard completed for company `Bara3am`:

```sh
export ADMIN_PASSWORD='<your Administrator password>'   # or put it in .env (gitignored)
docker compose -p hr -f hr.yml -f Data_set/seed.yml run --rm --build seed
```

- **Idempotent:** each record is looked up by a natural key (email, employee + date, ...) and only created if missing. Re-running after a failure or on a partially filled site is safe.
- **Rebuilding the image** (`custom:N`) keeps the data, because it lives in the `sites` and `db-data` volumes. Seed again only after `down -v` or on a new site.
- **Options:**
  - `SEED_COMPANY=<name>`: use a different company.
  - `SEED_SKIP=08_attendance,11_interviews`: skip files.
- **Without Docker:** `SITE_URL=http://localhost:8001 SITE_NAME=frontend ADMIN_PASSWORD=... python3 Data_set/seed.py`

## Branding: "HR Flow"

The seed's first step (`00_branding`) renames the app in the UI. It's stored in the database, so it survives image rebuilds and is reapplied automatically after a reset:

| Where | How |
|---|---|
| Browser tab, login emails ("Login To HR Flow") | System Settings → Application Name |
| Login page logo, splash screen, favicon, website navbar | Website Settings + Navbar Settings → `/assets/hrflow_theme/images/hrflow-logo.svg` (favicon `/assets/hr_ai/images/hr-flow-favicon.svg`) |
| Login page text "Welcome to HR Flow!", footer | English Translation override + Website Settings → Footer "Powered" |
| App switcher titles ("HR Flow", "HR Flow ERP") | English Translation overrides of the app titles |
| Login tab title "Login \| HR Flow" | Website Script |

- To use another name: `APP_NAME="My Name" docker compose ... run --rm seed`.
- To skip branding: `SEED_SKIP=00_branding`.
- After changing it, run `bench --site frontend clear-cache` and refresh the browser.
- **Not covered yet:** the small app icons in the app switcher still use each app's own logo. Replacing them needs code in the `hr_ai` app (a hook), so it waits until that app exists.

## Regenerate

```sh
python3 Data_set/generate.py      # stdlib only, deterministic (seed 2026)
```

Changing the generator changes the data. Records already loaded stay in the site, so reset the site first if you want a clean set.

## Notes

- **Leave notifications:** submitting leave shows "Please set default template for Leave Status Notification in HR Settings". It's only a notice; set the template in HR Settings if you want the emails.
- **Demo dates:** the reference date is 2026-10-30. Upcoming interviews and pending leave fall in Nov–Dec 2026.
