"""Adapter: AI agent layer output (hr_ai "AI Action" format) -> guardrails.

The agents produce proposals like:
    {"kind": "email_reply", "agent": "comms", "subject": "email/010_candidate",
     "reference_doctype": null, "reference_name": null,
     "payload": {"email_id": "010_candidate", "to": "[EMAIL_1]", "subject": "...", "body": "Dear [PERSON_1], ..."},
     "token_map": {"[PERSON_1]": "Cyrine Jlassi", "[EMAIL_1]": "cyrine.jlassi.sr2@example.com"},
     "source_hashes": ["bcee57..."], "action_hash": "84c22d...", "external_id": "AIA-6ec528d223", "run_id": "c4ba..."}

The adapter turns that into a ProposedAction, plus any problems found while
translating. Security rules applied here:
  - The token_map is supplied by the agent layer, so it is NOT trusted:
      * an email address must belong to a known Job Applicant / Employee in
        Frappe, which then becomes the recipient RECORD (the gateway re-reads
        the address from Frappe at send time);
      * every token value must match that person's Frappe record, so an
        injected value cannot hide behind "[EMAIL_1]" or "[PERSON_1]".
  - Unknown agents / kinds are passed through unchanged: the default-deny
    Policy Engine then refuses them.
  - Replies to an inbox email ("to": "<SENDER>"): the address is NOT taken from
    the proposal; it is read from the Inbound Email record in Frappe, then must
    belong to a known applicant / employee like any other recipient.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from .executors import EMAIL_RE, PERSON_FIELDS, person_values
from .frappe_client import FrappeAPI, FrappeError, FrappeUnavailable
from .models import ProposedAction, RecordRef, Violation

TOKEN_KEY = re.compile(r"^\[[A-Z]+_\d+\]$")
HEX64 = re.compile(r"^(sha256:)?([0-9a-f]{64})$")
ACTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}$")
KNOWN_FIELDS = {"kind", "agent", "subject", "reference_doctype", "reference_name", "summary",
                "payload", "source_hashes", "token_map", "action_hash", "external_id", "run_id"}
SENDER = "<SENDER>"  # input-layer placeholder: "the person who wrote this inbox email"
DOC_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
MIN_JUSTIFICATION = 20  # same minimum as fairness.json for scored tools


class AgentLayerError(ValueError):
    """The proposal is not even well-formed: refuse before storing anything."""


class DirectoryUnavailable(Exception):
    pass


class Directory(Protocol):
    def find_person(self, email: str) -> RecordRef | None: ...
    def person_values(self, ref: RecordRef) -> set[str]: ...
    def inbound_sender(self, email_id: str) -> tuple[str, str] | None: ...


class FrappeDirectory:
    """Finds people in Frappe by email (read-only lookups)."""

    def __init__(self, frappe: FrappeAPI):
        self.frappe = frappe

    def find_person(self, email: str) -> RecordRef | None:
        email = email.strip().lower()
        try:
            for doctype, spec in PERSON_FIELDS.items():
                for f in spec["emails"]:
                    rows = self.frappe.list_docs(doctype, [[f, "=", email]], ["name"], limit=2)
                    if len(rows) == 1:
                        return RecordRef(doctype, rows[0]["name"])
                    if len(rows) > 1:
                        return None  # ambiguous: refuse rather than guess
        except (FrappeError, FrappeUnavailable) as e:
            raise DirectoryUnavailable(str(e)) from None
        return None

    def person_values(self, ref: RecordRef) -> set[str]:
        try:
            return person_values(ref.doctype, self.frappe.get_doc(ref.doctype, ref.name))
        except (FrappeError, FrappeUnavailable) as e:
            raise DirectoryUnavailable(str(e)) from None

    def inbound_sender(self, email_id: str) -> tuple[str, str] | None:
        """(sender address, doc_id) of an Inbound Email, or None if it does not exist.
        sender_email is an HR-only field: the gateway user needs the HR Manager role."""
        try:
            rows = self.frappe.list_docs("Inbound Email", [["name", "=", email_id]],
                                         ["sender_email", "doc_id"], limit=1)
        except (FrappeError, FrappeUnavailable) as e:
            raise DirectoryUnavailable(str(e)) from None
        if not rows or not rows[0].get("sender_email"):
            return None
        return str(rows[0]["sender_email"]), str(rows[0].get("doc_id") or "")


@dataclass(frozen=True)
class AgentOutput:
    kind: str
    agent: str
    external_id: str
    payload: Mapping[str, Any]
    token_map: Mapping[str, str] = field(default_factory=dict)
    source_hashes: tuple[str, ...] = ()
    subject: str | None = None
    reference_doctype: str | None = None
    reference_name: str | None = None
    summary: str | None = None
    action_hash: str | None = None
    run_id: str | None = None

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> "AgentOutput":
        """Accepts the AI Action fields as the agent layer sends them;
        payload / token_map / source_hashes may be JSON strings (Frappe Code fields)."""
        if not isinstance(raw, Mapping):
            raise AgentLayerError("agent output must be a JSON object")
        unknown = set(raw) - KNOWN_FIELDS
        if unknown:
            raise AgentLayerError(f"unknown fields: {sorted(unknown)}")
        for k in ("kind", "agent", "external_id"):
            if not isinstance(raw.get(k), str) or not raw[k].strip():
                raise AgentLayerError(f"{k} is required")
        if not ACTION_ID.match(raw["external_id"]):
            raise AgentLayerError("external_id has invalid characters")
        payload = _json_field(raw.get("payload"), dict, "payload")
        token_map = _json_field(raw.get("token_map") or {}, dict, "token_map")
        if not all(isinstance(k, str) and TOKEN_KEY.match(k) and isinstance(v, str) for k, v in token_map.items()):
            raise AgentLayerError("token_map must map tokens like [PERSON_1] to strings")
        hashes = _json_field(raw.get("source_hashes") or [], list, "source_hashes")
        if not all(isinstance(h, str) for h in hashes):
            raise AgentLayerError("source_hashes must be strings")
        opt = {k: raw.get(k) for k in ("subject", "reference_doctype", "reference_name",
                                         "summary", "action_hash", "run_id")}
        for k, v in opt.items():
            if v is not None and not isinstance(v, str):
                raise AgentLayerError(f"{k} must be a string")
        return cls(raw["kind"], raw["agent"], raw["external_id"], payload, token_map,
                   tuple(hashes), **opt)


@dataclass(frozen=True)
class Translation:
    action: ProposedAction
    violations: tuple[Violation, ...]


class AgentLayerAdapter:
    def __init__(self, config: Mapping[str, Any], directory: Directory):
        expected = {"version", "agents", "kinds"}
        if set(config) - {"_comment"} != expected:
            raise ValueError(f"agent layer config keys must be {sorted(expected)}")
        self.version = config["version"]
        self.agents = dict(config["agents"])
        self.kinds = dict(config["kinds"])
        self.directory = directory

    def translate(self, out: AgentOutput) -> Translation:
        problems: list[Violation] = []
        hashes = []
        for h in out.source_hashes:
            m = HEX64.match(h.strip().lower())
            if m:
                hashes.append("sha256:" + m.group(2))
            else:
                problems.append(Violation("bad_source_hash", f"'{h[:20]}' is not a SHA-256 hash"))

        reference = None
        if out.reference_doctype and out.reference_name:
            reference = RecordRef(out.reference_doctype, out.reference_name)

        tool = self.kinds.get(out.kind, out.kind)
        try:
            if out.kind == "email_reply":
                subject, payload = self._email_reply(out, reference, problems)
            elif out.kind == "interview":
                subject, payload = self._interview(out, reference, problems)
            elif out.kind == "applicant_status":
                subject, payload = self._applicant_status(out, reference, problems)
            elif out.kind == "ranking":
                subject, payload = self._ranking(out, reference, problems)
            elif out.kind == "digest":
                # Read by HR only: nothing is sent or written, so there is no recipient to verify.
                subject = RecordRef("(none)", out.subject or out.external_id)
                payload = dict(out.payload)
            else:
                subject = reference or RecordRef("(none)", out.subject or out.external_id)
                payload = dict(out.payload)
                if out.token_map:
                    payload["token_map"] = dict(out.token_map)
                    self._check_tokens(out.token_map, reference, problems)
        except DirectoryUnavailable:
            problems.append(Violation("directory_unavailable",
                                      "could not verify recipients in Frappe; refusing (fail closed)"))
            subject = reference or RecordRef("(none)", out.subject or out.external_id)
            payload = dict(out.payload)

        action = ProposedAction(
            action_id=out.external_id,
            agent=self.agents.get(out.agent, out.agent),
            tool=tool,
            subject=subject,
            payload=payload,
            source_hashes=tuple(hashes),
            model=None,
            prompt_version=f"run:{out.run_id}" if out.run_id else None,
        )
        return Translation(action, tuple(problems))

    # -- per kind --

    def _email_reply(self, out, reference, problems):
        p = out.payload
        missing = {"to", "subject", "body"} - set(p)
        if missing:
            raise AgentLayerError(f"email_reply payload misses {sorted(missing)}")
        extra = set(p) - {"email_id", "to", "subject", "body", "doc_id"}
        if extra:
            problems.append(Violation("agent_output_malformed", f"unexpected payload keys {sorted(extra)}"))
        # hr_ai references the Inbound Email being answered. That is the conversation,
        # not the person: the action's subject is the recipient's own record.
        if reference is not None and reference.doctype == "Inbound Email":
            reference = None
        to = str(p["to"]).strip()
        if to == SENDER:
            address = self._sender_address(p, problems)
        else:
            if TOKEN_KEY.match(to) and to not in out.token_map:
                problems.append(Violation("unresolved_token", f"{to} is not in the token_map"))
            address = out.token_map.get(to, to)

        recipient: Any = address or "(unknown)"  # stays a raw string unless Frappe knows the person
        if address is None:
            pass  # already reported by _sender_address
        elif EMAIL_RE.match(address):
            ref = self.directory.find_person(address)
            if ref is None:
                problems.append(Violation("recipient_unknown",
                                          "recipient address is not a known applicant/employee in Frappe"))
            else:
                recipient = {"doctype": ref.doctype, "name": ref.name}
                self._check_tokens(out.token_map, ref, problems)
        else:
            problems.append(Violation("recipient_unknown", "recipient is not an email address"))

        subject = reference or (RecordRef(recipient["doctype"], recipient["name"])
                                if isinstance(recipient, dict) else
                                RecordRef("Inbound Email", str(p.get("email_id") or out.external_id)))
        payload = {"recipients": [recipient], "subject": p["subject"], "body": p["body"]}
        if out.token_map:
            payload["token_map"] = dict(out.token_map)
        if p.get("email_id"):
            payload["inbound_email"] = str(p["email_id"])
        return subject, payload

    def _sender_address(self, p, problems) -> str | None:
        """Address behind "<SENDER>", read from Frappe, never from the proposal."""
        email_id, doc_id = str(p.get("email_id") or ""), str(p.get("doc_id") or "")
        if not email_id or not DOC_ID.match(doc_id):
            problems.append(Violation("agent_output_malformed",
                                      "a <SENDER> reply needs the email_id and doc_id of the inbox email"))
            return None
        found = self.directory.inbound_sender(email_id)
        if found is None:
            problems.append(Violation("recipient_unknown", f"inbox email '{email_id}' not found in Frappe"))
            return None
        address, stored_doc_id = found
        if stored_doc_id != doc_id:
            problems.append(Violation("subject_mismatch",
                                      "the proposal's doc_id does not match the inbox email it answers"))
        return address

    def _applicant_status(self, out, reference, problems):
        p = out.payload
        if not {"status", "reason"} <= set(p):
            raise AgentLayerError("applicant_status payload needs status and reason")
        if "applicant" in p and not isinstance(p["applicant"], str):
            raise AgentLayerError("applicant_status payload applicant must be a string")
        extra = set(p) - {"applicant", "status", "reason"}
        if extra:
            problems.append(Violation("agent_output_malformed", f"unexpected payload keys {sorted(extra)}"))
        if reference is None or reference.doctype != "Job Applicant":
            problems.append(Violation("subject_mismatch", "a status change must reference a Job Applicant"))
            reference = reference or RecordRef("(none)", out.subject or out.external_id)
        # hr_ai repeats the applicant in the payload: it must be the referenced record.
        if "applicant" in p and out.token_map.get(p["applicant"], p["applicant"]) != reference.name:
            problems.append(Violation("subject_mismatch",
                                      "payload applicant does not match the referenced Job Applicant"))
        payload = {"status": p["status"], "reason": p["reason"]}
        if out.token_map:
            self._check_tokens(out.token_map, reference, problems, extra_allowed={reference.name.lower()})
        return reference, payload

    def _ranking(self, out, reference, problems):
        """hr_ai ranks a whole opening at once. Nothing is written to Frappe for a
        ranking (HR's decision is recorded), so the checks are on what HR reads:
        one entry per applicant, scores 0-100, a job-related justification each."""
        p = out.payload
        entries = p.get("entries")
        if not isinstance(p.get("opening"), str) or not isinstance(entries, list) or not entries:
            raise AgentLayerError("ranking payload needs an opening and a non-empty entries list")
        if reference is None or reference.doctype != "Job Opening" or reference.name != p["opening"]:
            problems.append(Violation("subject_mismatch", "a ranking must reference its Job Opening"))
            reference = reference or RecordRef("(none)", out.subject or out.external_id)
        checked, seen = [], set()
        for i, e in enumerate(entries, 1):
            if not isinstance(e, Mapping):
                raise AgentLayerError("ranking entries must be objects")
            applicant, score, why = e.get("applicant"), e.get("score"), e.get("justification")
            if not isinstance(applicant, str) or not applicant:
                raise AgentLayerError(f"ranking entry {i}: applicant must be a non-empty string")
            if applicant in seen:
                problems.append(Violation("agent_output_malformed", f"entry {i}: applicant ranked twice"))
            seen.add(applicant)
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 100:
                problems.append(Violation("score_out_of_range", f"entry {i}: score must be 0-100"))
            if not isinstance(why, str) or not why.strip():
                problems.append(Violation("justification_missing", f"entry {i} has no justification"))
            elif len(why.strip()) < MIN_JUSTIFICATION:
                problems.append(Violation("justification_too_short", f"entry {i}: justification too short"))
            # Applicant ids are left out on purpose: the fairness text scan reads every
            # string, and an address like "x.man@..." would look like a protected term.
            checked.append({"rank": e.get("rank"), "score": score, "justification": why})
        payload = {"job_opening": p["opening"], "entries": checked}
        if p.get("note"):
            payload["note"] = p["note"]
        return reference, payload

    def _interview(self, out, reference, problems):
        p = out.payload
        required = {"applicant", "interview_type", "date", "from_time", "to_time"}
        if not required <= set(p):
            raise AgentLayerError(f"interview payload misses {sorted(required - set(p))}")
        if reference is None or reference.doctype != "Job Applicant":
            problems.append(Violation("subject_mismatch", "an interview must reference a Job Applicant"))
            reference = reference or RecordRef("(none)", out.subject or out.external_id)
        applicant = out.token_map.get(p["applicant"], p["applicant"])
        if applicant != reference.name:
            problems.append(Violation("subject_mismatch",
                                      "payload applicant does not match the referenced Job Applicant"))
        payload = {k: p[k] for k in required}
        if out.token_map:
            payload["token_map"] = dict(out.token_map)
            self._check_tokens(out.token_map, reference, problems,
                               extra_allowed={reference.name.lower()})
        return reference, payload

    def _check_tokens(self, token_map, ref, problems, extra_allowed=frozenset()):
        if not token_map:
            return
        if ref is None or ref.doctype not in PERSON_FIELDS:
            problems.append(Violation("token_value_not_from_record",
                                      "tokens cannot be verified without a person record"))
            return
        allowed = self.directory.person_values(ref) | set(extra_allowed)
        bad = sorted(t for t, v in token_map.items() if v.strip().lower() not in allowed)
        if bad:
            problems.append(Violation("token_value_not_from_record",
                                      f"{', '.join(bad)} do not match {ref.doctype} {ref.name} in Frappe"))


def _json_field(value, typ, name):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise AgentLayerError(f"{name} is not valid JSON") from None
    if not isinstance(value, typ):
        raise AgentLayerError(f"{name} must be a JSON {typ.__name__}")
    return value
