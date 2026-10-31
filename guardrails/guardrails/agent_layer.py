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


class AgentLayerError(ValueError):
    """The proposal is not even well-formed: refuse before storing anything."""


class DirectoryUnavailable(Exception):
    pass


class Directory(Protocol):
    def find_person(self, email: str) -> RecordRef | None: ...
    def person_values(self, ref: RecordRef) -> set[str]: ...


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
        extra = set(p) - {"email_id", "to", "subject", "body"}
        if extra:
            problems.append(Violation("agent_output_malformed", f"unexpected payload keys {sorted(extra)}"))
        to = str(p["to"]).strip()
        if TOKEN_KEY.match(to) and to not in out.token_map:
            problems.append(Violation("unresolved_token", f"{to} is not in the token_map"))
        address = out.token_map.get(to, to)

        recipient: Any = address  # stays a raw string unless Frappe knows the person
        if EMAIL_RE.match(address):
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
