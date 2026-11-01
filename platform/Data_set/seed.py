#!/usr/bin/env python3
"""Load the synthetic dataset into the HR site over its REST API.

Idempotent: every record is looked up by a natural key first and only created if missing,
so it is safe to re-run after a rebuild or a partial failure. Stdlib only.

Environment:
  SITE_URL        default http://frontend:8080
  SITE_NAME       default frontend      (sent as Host header so the right site answers)
  ADMIN_USER      default Administrator
  ADMIN_PASSWORD  required
  COMPANY         default Bara3am       (must exist: finish the setup wizard first)
  DATASET_DIR     default <this folder>/dataset
  SKIP            comma list of file stems to skip, e.g. "08_attendance"
"""
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

SITE_URL = os.environ.get("SITE_URL", "http://frontend:8080").rstrip("/")
SITE_NAME = os.environ.get("SITE_NAME", "frontend")
USER = os.environ.get("ADMIN_USER", "Administrator")
PASSWORD = os.environ.get("ADMIN_PASSWORD")
COMPANY = os.environ.get("COMPANY", "Bara3am")
DATASET = Path(os.environ.get("DATASET_DIR", Path(__file__).resolve().parent / "dataset"))
SKIP = {s.strip() for s in os.environ.get("SKIP", "").split(",") if s.strip()}
APP_NAME = os.environ.get("APP_NAME", "HR Flow")

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
errors = []


class ApiError(Exception):
    pass


def _msg(body):
    try:
        data = json.loads(body)
    except ValueError:
        return body[:300]
    for k in ("_server_messages", "exception", "message", "exc_type"):
        if data.get(k):
            v = data[k]
            if k == "_server_messages":
                try:
                    v = " | ".join(json.loads(m).get("message", m) for m in json.loads(v))
                except (ValueError, AttributeError):
                    pass
            return str(v)[:400]
    return body[:300]


def call(method, path, payload=None, data=None, headers=None):
    hdrs = {"Host": SITE_NAME, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(SITE_URL + path, data=data, method=method, headers=hdrs)
    try:
        with opener.open(req, timeout=120) as r:
            body = r.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        raise ApiError(f"{method} {path.split('?')[0]} -> {e.code}: {_msg(e.read().decode(errors='replace'))}")


def q(v):
    return urllib.parse.quote(str(v), safe="")


def find(doctype, filters):
    path = f"/api/resource/{q(doctype)}?filters={q(json.dumps(filters))}&fields={q(json.dumps(['name']))}&limit_page_length=1"
    rows = call("GET", path).get("data", [])
    return rows[0]["name"] if rows else None


def exists(doctype, name):
    try:
        call("GET", f"/api/resource/{q(doctype)}/{q(name)}")
        return True
    except ApiError as e:
        if " 404" in str(e):
            return False
        raise


def create(doctype, doc):
    return call("POST", f"/api/resource/{q(doctype)}", doc)["data"]["name"]


def upload(doctype, docname, field, path, private=True, content_type="application/pdf"):
    boundary = uuid.uuid4().hex
    parts = []
    fields = {"is_private": "1" if private else "0"}
    if doctype:
        fields.update(doctype=doctype, docname=docname, fieldname=field)
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
                 f"Content-Type: {content_type}\r\n\r\n".encode() + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    res = call("POST", "/api/method/upload_file", data=b"".join(parts),
               headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return res["message"]["file_url"]


def wait_and_login():
    deadline = time.time() + int(os.environ.get("WAIT_SECONDS", "600"))
    while True:
        try:
            if call("GET", "/api/method/ping").get("message") == "pong":
                break
        except (ApiError, urllib.error.URLError, ConnectionError, OSError) as e:
            if time.time() > deadline:
                sys.exit(f"Site not reachable at {SITE_URL} ({e})")
        print("waiting for site ...", flush=True)
        time.sleep(5)
    call("POST", "/api/method/login", {"usr": USER, "pwd": PASSWORD})
    print(f"logged in to {SITE_URL} (site {SITE_NAME}) as {USER}")


# --------------------------------------------------------------------------- mapping
keys = {}   # dataset _key -> created docname


def clean(rec, abbr):
    doc = {k: v for k, v in rec.items() if not k.startswith("_")}
    if "_department" in rec:
        doc["department"] = f"{rec['_department']} - {abbr}"
    if "_branch" in rec:
        doc["branch"] = rec["_branch"]
    if rec.get("_reports_to"):
        doc["reports_to"] = keys[rec["_reports_to"]]
    if "_holiday_list" in rec:
        doc["holiday_list"] = keys[rec["_holiday_list"]]
    if "_employee" in rec:
        doc["employee"] = keys[rec["_employee"]]
    return doc


SPECS = {  # file stem -> (doctype, add company?, lookup(rec, doc) -> existing name or None)
    "01_branches": ("Branch", False, lambda r, d: d["branch"] if exists("Branch", d["branch"]) else None),
    "02_holiday_lists": ("Holiday List", False,
                         lambda r, d: d["holiday_list_name"] if exists("Holiday List", d["holiday_list_name"]) else None),
    "03_skills": ("Skill", False, lambda r, d: d["skill_name"] if exists("Skill", d["skill_name"]) else None),
    "02b_holiday_list_assignments": ("Holiday List Assignment", False, lambda r, d: find("Holiday List Assignment", {
        "applicable_for": "Company", "assigned_to": COMPANY, "holiday_list": d["holiday_list"], "docstatus": ["!=", 2]})),
    "04_interview_types": ("Interview Type", False, lambda r, d: d["interview_type_name"]
                           if exists("Interview Type", d["interview_type_name"]) else None),
    "05_employees": ("Employee", True, lambda r, d: find("Employee", {"personal_email": d["personal_email"]})),
    "06_leave_allocations": ("Leave Allocation", True, lambda r, d: find("Leave Allocation", {
        "employee": d["employee"], "leave_type": d["leave_type"], "from_date": d["from_date"], "docstatus": ["!=", 2]})),
    "07_leave_applications": ("Leave Application", True, lambda r, d: find("Leave Application", {
        "employee": d["employee"], "from_date": d["from_date"], "to_date": d["to_date"], "docstatus": ["!=", 2]})),
    "08_attendance": ("Attendance", True, lambda r, d: find("Attendance", {
        "employee": d["employee"], "attendance_date": d["attendance_date"], "docstatus": ["!=", 2]})),
    "09_job_openings": ("Job Opening", True, lambda r, d: find("Job Opening", {"job_title": d["job_title"], "company": COMPANY})),
    "10_job_applicants": ("Job Applicant", False, lambda r, d: find("Job Applicant", {"email_id": d["email_id"]})),
    "11_interviews": ("Interview", False, lambda r, d: find("Interview", {
        "job_applicant": d["job_applicant"], "scheduled_on": d["scheduled_on"]})),
}


def load_file(path, abbr):
    stem = path.stem
    doctype, add_company, lookup = SPECS[stem]
    records = json.loads(path.read_text(encoding="utf-8"))
    made = found = failed = 0
    for rec in records:
        try:
            doc = clean(rec, abbr)
            if add_company:
                doc["company"] = COMPANY
            if doctype == "Holiday List Assignment":
                doc["assigned_to"] = COMPANY
            if "_job_opening" in rec:
                doc["job_title" if doctype == "Job Applicant" else "job_opening"] = keys[rec["_job_opening"]]
            if "_job_applicant" in rec:
                doc["job_applicant"] = keys[rec["_job_applicant"]]
            name = lookup(rec, doc)
            if name:
                found += 1
            else:
                name = create(doctype, doc)
                made += 1
            keys[rec["_key"]] = name
            if doctype == "Job Applicant" and rec.get("_cv"):
                cur = call("GET", f"/api/resource/Job%20Applicant/{q(name)}")["data"]
                if not cur.get("resume_attachment"):
                    url = upload("Job Applicant", name, "resume_attachment", DATASET / "cvs" / rec["_cv"])
                    call("PUT", f"/api/resource/Job%20Applicant/{q(name)}", {"resume_attachment": url})
        except (ApiError, KeyError) as e:
            failed += 1
            errors.append(f"{stem} {rec.get('_key')}: {e}")
    print(f"{stem:24s} {doctype:18s} created {made:4d}  existing {found:4d}  failed {failed:3d}", flush=True)


def apply_branding():
    """Show APP_NAME (default "HR Flow") on the login page, browser tab, navbar, splash and login emails."""
    # The logo and favicon ship with the apps (hrflow_theme, hr_ai), so nothing is uploaded.
    url = "/assets/hrflow_theme/images/hrflow-logo.svg"
    favicon = "/assets/hr_ai/images/hr-flow-favicon.svg"
    call("PUT", "/api/resource/System%20Settings/System%20Settings", {"app_name": APP_NAME})
    call("PUT", "/api/resource/Website%20Settings/Website%20Settings",
         {"app_name": APP_NAME, "app_logo": url, "splash_image": url, "favicon": favicon,
          "brand_html": f'<img src="{url}" alt="{APP_NAME}" style="height:28px">'})
    call("PUT", "/api/resource/Navbar%20Settings/Navbar%20Settings", {"app_logo": url})
    call("PUT", "/api/resource/Website%20Settings/Website%20Settings", {"footer_powered": APP_NAME})
    call("PUT", "/api/resource/Website%20Script/Website%20Script", {"javascript":
         f'if (location.pathname === "/login") {{ document.title = "Login | {APP_NAME}"; }}'})
    # App titles and fixed UI strings go through translations, so English overrides rename them.
    renames = {"Frappe HR": APP_NAME, "Frappe": APP_NAME, "ERPNext": f"{APP_NAME} ERP", "Framework": f"{APP_NAME} Admin",
               "Welcome! Please sign in to continue.": f"Welcome to {APP_NAME}! Please sign in to continue."}
    for src, dst in renames.items():
        name = find("Translation", {"language": "en", "source_text": src})
        if name:
            call("PUT", f"/api/resource/Translation/{q(name)}", {"translated_text": dst})
        else:
            create("Translation", {"language": "en", "source_text": src, "translated_text": dst})
    print(f"{'00_branding':24s} app name '{APP_NAME}', logo {url}", flush=True)


def main():
    if not PASSWORD:
        sys.exit("ADMIN_PASSWORD is not set")
    wait_and_login()
    try:
        company = call("GET", f"/api/resource/Company/{q(COMPANY)}")["data"]
    except ApiError:
        sys.exit(f"Company '{COMPANY}' not found. Finish the setup wizard (or set COMPANY) and re-run.")
    abbr = company["abbr"]
    if "00_branding" in SKIP:
        print(f"{'00_branding':24s} skipped")
    else:
        try:
            apply_branding()
        except ApiError as e:
            errors.append(f"00_branding: {e}")
    for path in sorted((DATASET / "hrms").glob("*.json")):
        if path.stem in SKIP:
            print(f"{path.stem:24s} skipped")
            continue
        load_file(path, abbr)
        if path.stem == "02_holiday_lists" and "HL-TN-2026" in keys:
            call("PUT", f"/api/resource/Company/{q(COMPANY)}", {"default_holiday_list": keys["HL-TN-2026"]})
    if errors:
        print(f"\n{len(errors)} error(s):")
        for e in errors[:50]:
            print("  -", e)
        sys.exit(1)
    print("\nDone. All records loaded.")


if __name__ == "__main__":
    main()
