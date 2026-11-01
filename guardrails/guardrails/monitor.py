"""Suspicious-Action Monitor: reads the audit log, raises alerts for HR.

Detections (config/monitor.json):
  bulk_send          many emails executed in a short window
  proposal_flood     one agent proposing far more than usual
  repeated_denials   one actor hitting guardrails again and again
  instant            single events that are alarming on their own:
                     an agent trying to approve, a forged token...
  suspected_injection a proposal blocked for an exfiltration/bias rule (the
                     AI was likely steered by injected content), or an
                     injection flag reported by the input pipeline
  off_hours          approvals / executions outside business hours
  execution_failure  outcome unknown or Frappe refused: a human must check
  audit_chain_broken the hash chain no longer verifies
  adverse_impact     four-fifths disparity test failed for a batch

The monitor only OBSERVES and ALERTS. It never blocks or executes
anything; enforcement stays in the Policy Engine and the gateway.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from .storage import AuditEntry, AuditLog, Database

SEVERITIES = ("info", "warning", "high", "critical")

MONITOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS monitor_alerts (
    id          INTEGER PRIMARY KEY,
    ts          REAL NOT NULL,
    rule        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    dedup_key   TEXT NOT NULL UNIQUE,
    subject     TEXT NOT NULL,
    message     TEXT NOT NULL,
    evidence    TEXT NOT NULL,
    acknowledged_by TEXT,
    acknowledged_at REAL
);
CREATE TABLE IF NOT EXISTS monitor_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class MonitorConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Alert:
    id: int
    ts: float
    rule: str
    severity: str
    subject: str
    message: str
    evidence: tuple[int, ...]  # audit seq numbers that triggered it
    acknowledged_by: str | None = None
    acknowledged_at: float | None = None


class Notifier(Protocol):
    def notify(self, alert: Alert) -> None: ...


class ConsoleNotifier:
    def notify(self, alert: Alert) -> None:
        print(f"[ALERT {alert.severity.upper()}] {alert.rule}: {alert.message}")


class Monitor:
    def __init__(self, config: Mapping[str, Any], audit: AuditLog,
                 notifiers: Iterable[Notifier] = (), now: Callable[[], float] = time.time,
                 fairness=None):
        self.cfg = _validate(config)
        self.audit = audit
        self.db: Database = audit.db
        self.notifiers = list(notifiers)
        self.now = now
        self.fairness = fairness
        with self.db.read() as c:
            c.executescript(MONITOR_SCHEMA)

    @classmethod
    def from_file(cls, path: str | Path, audit: AuditLog, **kw) -> "Monitor":
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh), audit, **kw)

    # --- main loop step ------------------------------------------------------

    def run_once(self) -> list[Alert]:
        """Process audit entries not seen yet. Safe to call repeatedly
        (e.g. every few seconds from a scheduler or after each request)."""
        new_alerts: list[Alert] = []

        chain = self.audit.verify_chain()
        if not chain.ok:
            new_alerts += self._raise("audit_chain_broken", "critical", "audit_log",
                                      f"audit chain broken at entry {chain.broken_at}: {chain.reason}",
                                      (chain.broken_at or 0,), key=f"chain:{chain.broken_at}")

        cursor = int(self._state("cursor", "0"))
        for e in self.audit.query(after_seq=cursor):
            if not e.event.startswith("monitor."):
                new_alerts += self._evaluate(e)
            cursor = e.seq
        self._set_state("cursor", str(cursor))
        return new_alerts

    # --- detections ---------------------------------------------------------

    def _evaluate(self, e: AuditEntry) -> list[Alert]:
        out: list[Alert] = []
        cfg = self.cfg
        d = e.details

        # instant: alarming single events
        if e.event in ("approval.denied", "execution.refused", "api.denied"):
            sev = cfg["instant_alert_codes"].get(d.get("code"))
            if sev:
                where = f" on {e.action_id}" if e.action_id else (
                    f" at {d['endpoint']}" if d.get("endpoint") else "")
                out += self._raise("instant", sev, e.actor,
                                   f"{e.actor} triggered '{d.get('code')}'{where}",
                                   (e.seq,), key=f"instant:{e.seq}")

        # suspected injection
        inj = cfg["injection"]
        if e.event == "action.submitted" and d.get("status") == "Blocked":
            hits = sorted(set(d.get("violations", [])) & set(inj["violation_rules"]))
            if hits:
                out += self._raise("suspected_injection", inj["severity"], e.actor,
                                   f"{e.actor} proposed a blocked action {e.action_id} "
                                   f"({', '.join(hits)}); its input may contain injected instructions. "
                                   f"Sources: {', '.join(d.get('source_hashes', [])) or 'none'}",
                                   (e.seq,), key=f"inj:{e.action_id}")
        if e.event in inj["events"]:
            out += self._raise("suspected_injection", inj["severity"], e.actor,
                               f"input pipeline flagged {d.get('source_hash')} ({', '.join(d.get('rules', []))})",
                               (e.seq,), key=f"inj-input:{e.seq}")

        # repeated denials by the same actor
        if self._is_denial(e):
            rd = cfg["repeated_denials"]
            seqs = self._window(e, rd["window_seconds"], lambda x: self._is_denial(x) and x.actor == e.actor)
            if len(seqs) >= rd["max"]:
                out += self._raise("repeated_denials", rd["severity"], e.actor,
                                   f"{e.actor} was denied {len(seqs)} times in {rd['window_seconds']}s",
                                   tuple(seqs), key=f"denials:{e.actor}:{int(e.ts // rd['window_seconds'])}")

        # proposal flood per agent
        if e.event == "action.submitted":
            pf = cfg["proposal_flood"]
            seqs = self._window(e, pf["window_seconds"],
                                lambda x: x.event == "action.submitted" and x.actor == e.actor)
            if len(seqs) >= pf["max"]:
                out += self._raise("proposal_flood", pf["severity"], e.actor,
                                   f"{e.actor} proposed {len(seqs)} actions in {pf['window_seconds']}s",
                                   tuple(seqs), key=f"flood:{e.actor}:{int(e.ts // pf['window_seconds'])}")

        # bulk send (all senders together)
        bs = cfg["bulk_send"]
        if e.event == "execution.started" and d.get("tool") in bs["tools"]:
            seqs = self._window(e, bs["window_seconds"],
                                lambda x: x.event == "execution.started" and x.details.get("tool") in bs["tools"])
            if len(seqs) >= bs["max"]:
                out += self._raise("bulk_send", bs["severity"], "email",
                                   f"{len(seqs)} emails sent in {bs['window_seconds']}s",
                                   tuple(seqs), key=f"bulk:{int(e.ts // bs['window_seconds'])}")

        # off hours
        oh = cfg["off_hours"]
        if e.event in oh["events"] and self._off_hours(e.ts):
            local = self._local(e.ts)
            out += self._raise("off_hours", oh["severity"], e.actor,
                               f"{e.event} by {e.actor} at {local:%a %H:%M} (outside business hours)",
                               (e.seq,), key=f"offhours:{e.actor}:{int(e.ts // 3600)}")

        # execution failure needing a human
        if e.event == "execution.failed":
            err = str(d.get("error") or "")
            out += self._raise("execution_failure", cfg["execution_failure"]["severity"], e.action_id or "-",
                               f"execution of {e.action_id} failed: {err[:200]}"
                               + (" (outcome unknown: check Frappe before retrying)"
                                  if err.startswith("outcome_unknown") else ""),
                               (e.seq,), key=f"fail:{e.action_id}")
        return out

    @staticmethod
    def _is_denial(e: AuditEntry) -> bool:
        return e.event in ("approval.denied", "execution.refused", "api.denied") or (
            e.event == "action.submitted" and e.details.get("status") == "Blocked")

    def _window(self, e: AuditEntry, seconds: int, pred) -> list[int]:
        """Seq numbers of entries in (e.ts - seconds, e.ts] up to e, matching pred."""
        return [x.seq for x in self.audit.query(since_ts=e.ts - seconds, upto_seq=e.seq) if pred(x)]

    def _local(self, ts: float) -> datetime:
        tz = timezone(timedelta(hours=self.cfg["off_hours"]["utc_offset_hours"]))
        return datetime.fromtimestamp(ts, tz)

    def _off_hours(self, ts: float) -> bool:
        oh = self.cfg["off_hours"]
        t = self._local(ts)
        return t.weekday() not in oh["workdays"] or not (oh["start_hour"] <= t.hour < oh["end_hour"])

    # --- fairness batch check ------------------------------------------------

    def check_disparity(self, job_opening: str, outcomes: Iterable[tuple[str, bool]],
                        actor: str = "monitor") -> tuple[Any, list[Alert]]:
        """Run the four-fifths test on one screening batch. Group labels come
        from trusted fictional data, never from the agent."""
        if self.fairness is None:
            raise RuntimeError("monitor was created without a FairnessChecker")
        report = self.fairness.disparity(list(outcomes))
        entry = self.audit.append("monitor.disparity_checked", actor, None, {
            "job_opening": job_opening,
            "status": report.status,
            "threshold": report.threshold,
            "groups": [{"group": g.group, "total": g.total, "selected": g.selected,
                        "impact_ratio": None if g.impact_ratio is None else round(g.impact_ratio, 4)}
                       for g in report.groups],
        })
        alerts = []
        if report.status == "adverse_impact":
            alerts = self._raise("adverse_impact", "high", job_opening,
                                 f"selection for {job_opening} fails the four-fifths rule for "
                                 f"group(s) {', '.join(report.flagged)}; review the shortlist",
                                 (entry.seq,), key=f"disparity:{job_opening}:{entry.seq}")
        return report, alerts

    # --- input pipeline hook --------------------------------------------------

    def report_input_flag(self, source_hash: str, detector: str, rules: list[str],
                          candidate: str | None = None, actor: str = "ingest") -> AuditEntry:
        """Called by the input pipeline (injection scanner) when it quarantines
        a document. Logged in the chain, picked up by run_once()."""
        return self.audit.append("input.injection_flagged", actor, None, {
            "source_hash": source_hash, "detector": detector,
            "rules": list(rules), "candidate": candidate,
        })

    # --- alerts --------------------------------------------------------------

    def _raise(self, rule, severity, subject, message, evidence, key) -> list[Alert]:
        ts = self.now()
        with self.db.transaction() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO monitor_alerts (ts, rule, severity, dedup_key, subject, message, evidence)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, rule, severity, key, str(subject), message, json.dumps(list(evidence))),
            )
            if cur.rowcount != 1:
                return []  # already alerted for this
            alert = Alert(cur.lastrowid, ts, rule, severity, str(subject), message, tuple(evidence))
            self.audit.append("monitor.alert", "monitor", None, {
                "alert_id": alert.id, "rule": rule, "severity": severity,
                "subject": str(subject), "evidence": list(evidence),
            })
        for n in self.notifiers:
            try:
                n.notify(alert)
            except Exception:
                pass  # a broken notifier must not stop monitoring; alert is stored anyway
        return [alert]

    def alerts(self, open_only: bool = False, min_severity: str = "info") -> list[Alert]:
        floor = SEVERITIES.index(min_severity)
        q = "SELECT * FROM monitor_alerts"
        if open_only:
            q += " WHERE acknowledged_by IS NULL"
        with self.db.read() as c:
            rows = c.execute(q + " ORDER BY id DESC").fetchall()
        return [Alert(r["id"], r["ts"], r["rule"], r["severity"], r["subject"], r["message"],
                      tuple(json.loads(r["evidence"])), r["acknowledged_by"], r["acknowledged_at"])
                for r in rows if SEVERITIES.index(r["severity"]) >= floor]

    def acknowledge(self, alert_id: int, user: str, note: str = "") -> None:
        with self.db.transaction() as c:
            cur = c.execute(
                "UPDATE monitor_alerts SET acknowledged_by = ?, acknowledged_at = ?"
                " WHERE id = ? AND acknowledged_by IS NULL",
                (user, self.now(), alert_id),
            )
            if cur.rowcount != 1:
                raise ValueError("unknown or already acknowledged alert")
            self.audit.append("monitor.acknowledged", user, None, {"alert_id": alert_id, "note": note})

    # --- state ---------------------------------------------------------------

    def _state(self, key: str, default: str) -> str:
        with self.db.read() as c:
            row = c.execute("SELECT value FROM monitor_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def _set_state(self, key: str, value: str) -> None:
        with self.db.transaction() as c:
            c.execute("INSERT INTO monitor_state (key, value) VALUES (?, ?)"
                      " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


# --- config validation ---------------------------------------------------------

_SECTIONS = {
    "bulk_send": {"tools", "max", "window_seconds", "severity"},
    "proposal_flood": {"max", "window_seconds", "severity"},
    "repeated_denials": {"max", "window_seconds", "severity"},
    "injection": {"violation_rules", "events", "severity"},
    "off_hours": {"utc_offset_hours", "start_hour", "end_hour", "workdays", "events", "severity"},
    "execution_failure": {"severity"},
}


def _validate(cfg: Mapping[str, Any]) -> dict:
    if not isinstance(cfg, Mapping):
        raise MonitorConfigError("monitor config must be an object")
    expected = set(_SECTIONS) | {"version", "instant_alert_codes"}
    if set(cfg) != expected:
        raise MonitorConfigError(f"monitor config keys must be exactly {sorted(expected)}")
    for name, keys in _SECTIONS.items():
        sec = cfg[name]
        if not isinstance(sec, Mapping) or set(sec) != keys:
            raise MonitorConfigError(f"{name} needs exactly {sorted(keys)}")
        if sec["severity"] not in SEVERITIES:
            raise MonitorConfigError(f"{name}.severity must be one of {SEVERITIES}")
        for k in ("max", "window_seconds"):
            if k in sec and (not isinstance(sec[k], int) or isinstance(sec[k], bool) or sec[k] < 1):
                raise MonitorConfigError(f"{name}.{k} must be a positive integer")
    for code, sev in cfg["instant_alert_codes"].items():
        if sev not in SEVERITIES:
            raise MonitorConfigError(f"instant_alert_codes.{code} has bad severity")
    oh = cfg["off_hours"]
    if not (0 <= oh["start_hour"] < oh["end_hour"] <= 24) or not set(oh["workdays"]) <= set(range(7)):
        raise MonitorConfigError("off_hours: bad hours or workdays")
    if not -12 <= oh["utc_offset_hours"] <= 14:
        raise MonitorConfigError("off_hours.utc_offset_hours out of range")
    return dict(cfg)
