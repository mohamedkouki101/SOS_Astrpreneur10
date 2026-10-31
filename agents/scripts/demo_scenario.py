"""Manual end-to-end scenario: put fictional external emails into the HR inbox (Mailpit).

    python -m agents.scripts.demo_scenario            # send the 3 scenario emails
    python -m agents.scripts.demo_scenario --reset    # first empty the Mailpit TEST inbox

Emails come from the fictional dataset; only the To header is rewritten to the HR inbox address.
1. a candidate asking about the hiring process   -> the Comms agent should draft a reply
2. an employee question                         -> the Comms agent should draft a reply / digest item
3. a phishing / prompt-injection attack          -> must be QUARANTINED, never reach the model
"""

from __future__ import annotations

import argparse
import email
import smtplib
from email.policy import default

import httpx

from agents.config import get_settings
from agents.tools.common import dataset

SCENARIO = ["001_candidate", "004_employee", "099_attack_phishing_injection"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="delete ALL messages in the Mailpit test inbox first")
    args = ap.parse_args()
    s = get_settings()
    smtp_host, smtp_port = "localhost", 1025

    if args.reset:
        httpx.delete(f"{s.mailpit_url}/api/v1/messages", timeout=15).raise_for_status()
        print("Mailpit test inbox emptied.")

    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as smtp:
        for name in SCENARIO:
            msg = email.message_from_bytes(dataset("emails", f"{name}.eml").read_bytes(), policy=default)
            del msg["To"]
            msg["To"] = s.hr_inbox_address
            smtp.send_message(msg)
            print(f"  sent {name:32s} from {msg['From']}  subject: {msg['Subject']}")

    r = httpx.get(f"{s.mailpit_url}/api/v1/search", params={"query": f"to:{s.hr_inbox_address}"}, timeout=15)
    print(f"\nHR inbox ({s.hr_inbox_address}) now holds {r.json().get('messages_count', '?')} message(s). "
          f"See {s.mailpit_url}")


if __name__ == "__main__":
    main()
