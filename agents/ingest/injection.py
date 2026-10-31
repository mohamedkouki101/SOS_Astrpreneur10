"""Stage 3 - Injection scanner: rules + (optional) classifier.

Any rule hit, in visible OR hidden text, quarantines the item. Hidden text on its own also
quarantines it: a CV or email has no legitimate reason to hide text from the human reader.
The classifier is a hook (model not chosen yet, see CLAUDE.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

RULES: list[tuple[str, re.Pattern]] = [
    (name, re.compile(rx, re.I | re.S))
    for name, rx in [
        ("ignore_instructions", r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|your)\b.{0,20}\b(instructions?|rules?|prompts?|guidelines)\b"),
        ("new_instructions", r"(#+\s*)?\bnew (instructions?|rules?|rubric)\b|\bthe (evaluation )?rubric has changed\b"),
        ("role_hijack", r"\byou are now\b|\b(admin|developer|god|DAN|jailbreak) mode\b|\bact as (an? )?(admin|system)\b"),
        ("fake_system", r"(^|\n)\s*(system|assistant)\s*:|<\|?(im_start|system|endoftext)\|?>|\[/?(INST|SYS)\]"),
        ("prompt_exfiltration", r"\b(reveal|print|show|repeat)\b.{0,30}\b(system prompt|instructions|hidden prompt)\b"),
        ("tool_coercion", r"\b(call|use|invoke|run)\b.{0,15}\btool\b|\bsend_\w+|\bwithout (human |any )?(approval|review|confirmation)\b|\bdo not ask for approval\b"),
        ("ranking_manipulation", r"\brank (this|me|my)\b.{0,30}\b(first|top|highest)\b|\bscore (of )?100\b|\b(approve|accept|shortlist) (this|me|my)\b.{0,20}(applicant|candidate|application)?"),
        ("status_manipulation", r"\bmark (every|all) other\b|\bset my status\b|\breject (all|every) other\b"),
        ("data_exfiltration", r"\b(export|send|forward|leak)\b.{0,40}\b(emails?|data|candidates|ibans?|passwords?)\b.{0,30}\b(to|at)\b"),
        ("credential_phish", r"\bconfirm (your|their) (bank|iban|password|credentials)\b"),
    ]
]


@dataclass
class ScanResult:
    verdict: str                          # "clean" | "quarantine"
    hits: list[str] = field(default_factory=list)
    classifier_score: float | None = None

    @property
    def quarantined(self) -> bool:
        return self.verdict == "quarantine"


def classifier_score(text: str) -> float | None:
    """Hook for a prompt-injection classifier (TODO: model not chosen). None = not configured."""
    return None


def rule_hits(text: str) -> list[str]:
    return [name for name, rx in RULES if rx.search(text or "")]


def scan(visible_text: str, hidden_text: str = "", threshold: float = 0.8) -> ScanResult:
    hits = [f"visible:{h}" for h in rule_hits(visible_text)] + [f"hidden:{h}" for h in rule_hits(hidden_text)]
    if len(hidden_text.strip()) >= 20:
        hits.append("hidden_text_present")
    score = classifier_score(visible_text + "\n" + hidden_text)
    if score is not None and score >= threshold:
        hits.append(f"classifier:{score:.2f}")
    return ScanResult(verdict="quarantine" if hits else "clean", hits=hits, classifier_score=score)
