"""③ Prompt-Injection Scanner: risk score and verdict.

Inputs: the masked text (what would go to the AI) and the hidden passages
removed by the sanitizer. Output: a verdict with a score breakdown, so the
dashboard can show *why* a document got its verdict.

    score < 3000          clean        -> sent as is
    3000 <= score < 7000  suspicious   -> sent, matched phrases neutralized, visibly flagged
    score >= 7000         quarantined  -> nothing sent to the AI, HR alerted

Security does not rely on this detection alone: downstream the LLM has no
tools or keys, its output is validated, and a human approves every action.
"""

from dataclasses import dataclass
from typing import Literal

from ingest.rules.injection_rules import RuleMatch, find_matches
from ingest.sanitizer.hidden import RemovedItem

SUSPICIOUS_FROM = 3_000
QUARANTINE_FROM = 7_000
MAX_SCORE = 10_000
HIDDEN_TEXT_POINTS = 2_000
INVISIBLE_CHARS_POINTS = 1_000
INVISIBLE_CHARS_MIN = 5  # one stray soft hyphen in an Outlook email is normal
# HTML comments are routine in real emails (e.g. Outlook's <!--[if mso]>):
# they count only if they contain an instruction.
HARMLESS_ALONE = {"html_comment"}
NEUTRALIZED = "[REMOVED: suspected instruction]"
MAX_SIGNALS = 100  # contract limit

Verdict = Literal["clean", "suspicious", "quarantined"]


@dataclass(frozen=True)
class ScanReason:
    """One line of the score breakdown, e.g. ("rule:ignore_previous_fr", "...", 4000)."""

    code: str
    label: str
    points: int


@dataclass(frozen=True)
class ScanMatch:
    rule_id: str
    label: str
    text: str
    where: Literal["visible", "hidden"]


@dataclass(frozen=True)
class ScanResult:
    verdict: Verdict
    risk_score_bp: int
    signals: list[str]
    reasons: list[ScanReason]
    matches: list[ScanMatch]
    neutralized_text: str  # empty when quarantined: nothing goes to the AI


def _verdict(score: int) -> Verdict:
    if score >= QUARANTINE_FROM:
        return "quarantined"
    return "suspicious" if score >= SUSPICIOUS_FROM else "clean"


def _neutralize(text: str, matches: list[RuleMatch]) -> str:
    """Replace each matched phrase with a visible marker."""
    # 1. Merge overlapping matches into spans (matches are sorted by start).
    spans: list[list[int]] = []
    for match in matches:
        if spans and match.start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], match.end)
        else:
            spans.append([match.start, match.end])
    # 2. Rebuild the text with each span replaced.
    pieces, position = [], 0
    for start, end in spans:
        pieces += [text[position:start], NEUTRALIZED]
        position = end
    pieces.append(text[position:])
    return "".join(pieces)


def scan(
    masked_text: str,
    removed: list[RemovedItem],
    invisible_chars_removed: int,
    sanitizer_signals: list[str],
) -> ScanResult:
    visible = find_matches(masked_text)
    hidden = [(item, match) for item in removed for match in find_matches(item.text)]

    reasons: list[ScanReason] = []
    # Each rule counts once, however often it appears.
    for rule in {m.rule.id: m.rule for m in visible}.values():
        reasons.append(ScanReason(f"rule:{rule.id}", rule.label, rule.weight_bp))

    hiding = [item for item in removed if item.why not in HARMLESS_ALONE]
    if hiding:
        reasons.append(ScanReason("hidden_text", f"{len(hiding)} hidden passage(s) removed", HIDDEN_TEXT_POINTS))
    # A hidden instruction scores its full rule weight, on top of the hiding:
    # hiding it shows intent, so it must never weigh less than a visible one.
    for rule in {m.rule.id: m.rule for _, m in hidden}.values():
        reasons.append(ScanReason(f"hidden_rule:{rule.id}", f"Hidden text: {rule.label.lower()}", rule.weight_bp))
    if invisible_chars_removed >= INVISIBLE_CHARS_MIN:
        reasons.append(ScanReason(
            "invisible_chars", f"{invisible_chars_removed} invisible character(s) removed", INVISIBLE_CHARS_POINTS))

    score = min(MAX_SCORE, sum(reason.points for reason in reasons))
    verdict = _verdict(score)

    signals = list(sanitizer_signals)
    signals += [f"rule:{rule_id}" for rule_id in dict.fromkeys(m.rule.id for m in visible)]
    signals += [f"hidden_rule:{rule_id}" for rule_id in dict.fromkeys(m.rule.id for _, m in hidden)]

    return ScanResult(
        verdict=verdict,
        risk_score_bp=score,
        signals=list(dict.fromkeys(signals))[:MAX_SIGNALS],
        reasons=reasons,
        matches=[ScanMatch(m.rule.id, m.rule.label, m.text, "visible") for m in visible]
        + [ScanMatch(m.rule.id, m.rule.label, m.text, "hidden") for _, m in hidden],
        neutralized_text=_text_for_ai(verdict, masked_text, visible),
    )


def _text_for_ai(verdict: Verdict, masked_text: str, visible: list[RuleMatch]) -> str:
    """clean: as is. suspicious: matched phrases neutralized. quarantined: nothing."""
    if verdict == "quarantined":
        return ""
    if verdict == "suspicious":
        return _neutralize(masked_text, visible)
    return masked_text
