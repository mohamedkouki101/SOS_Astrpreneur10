"""Fairness & bias checks: deterministic, no LLM.

Three checks:
  1. Protected attributes blind: no protected attribute (age, gender,
     nationality, photo...) may appear as a key anywhere in a payload,
     and no image may be embedded. Applies to every action.
  2. Job-related justification: scoring actions must give a score per
     criterion, each with a written justification citing a source hash.
     The free text of scoring actions must not mention protected traits.
  3. Score-disparity test (four-fifths rule): over a BATCH of outcomes,
     each group's selection rate must be >= 80% of the best group's.
     Group labels come from a trusted source the agent never sees.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import ProposedAction, Violation

KNOWN_TOP_KEYS = {
    "version", "protected_keys", "protected_terms", "text_scan_tools",
    "scored_tools", "disparity",
}
KNOWN_SCORED_KEYS = {"score_min", "score_max", "min_criteria", "min_justification_chars"}
KNOWN_DISPARITY_KEYS = {"threshold", "min_group_size"}
CRITERION_KEYS = {"criterion", "score", "justification", "source_hash"}

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_WORD = re.compile(r"[^\w]+|_+")
_DATA_IMAGE = re.compile(r"data:image/", re.IGNORECASE)


class FairnessConfigError(ValueError):
    """The fairness file is malformed. The service must refuse to start."""


@dataclass(frozen=True)
class FairnessResult:
    allowed: bool
    fairness_version: str
    violations: tuple[Violation, ...] = field(default_factory=tuple)

    @property
    def reasons(self) -> list[str]:
        return [f"{v.rule}: {v.reason}" for v in self.violations]


@dataclass(frozen=True)
class ScoredToolRules:
    score_min: float
    score_max: float
    min_criteria: int
    min_justification_chars: int


def _tokens(key: str) -> tuple[str, ...]:
    """'dateOfBirth' / 'Date of Birth' / 'date_of_birth' -> ('date','of','birth')."""
    spaced = _CAMEL.sub(" ", key)
    return tuple(t for t in _NON_WORD.split(spaced.lower()) if t)


def _contains_seq(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    n = len(needle)
    return any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))


def _walk(value: Any, path: str = "payload"):
    """Yield (path, key_or_None, value) for every node of a JSON-like tree."""
    if isinstance(value, Mapping):
        for k, v in value.items():
            child = f"{path}.{k}"
            yield child, str(k), v
            yield from _walk(v, child)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")


class FairnessChecker:
    def __init__(self, config: Mapping[str, Any]):
        self._parse(config)

    @classmethod
    def from_file(cls, path: str | Path) -> "FairnessChecker":
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))

    # --- per-action check ------------------------------------------------

    def check(
        self, action: ProposedAction, job_criteria: Iterable[str] | None = None
    ) -> FairnessResult:
        """`job_criteria`: criteria defined on the Job Opening (from Frappe).
        When given, every scored criterion must be one of them."""
        violations: list[Violation] = []
        violations += self._protected_keys_and_images(action.payload)

        if action.tool in self.text_scan_tools:
            violations += self._protected_terms_in_text(action.payload)

        rules = self.scored_tools.get(action.tool)
        if rules is not None:
            allowed = set(job_criteria) if job_criteria is not None else None
            violations += self._scoring(action, rules, allowed)

        return FairnessResult(not violations, self.version, tuple(violations))

    def _protected_keys_and_images(self, payload) -> list[Violation]:
        out = []
        for path, key, value in _walk(payload):
            toks = _tokens(key)
            hit = next((" ".join(p) for p in self.protected_keys if _contains_seq(toks, p)), None)
            if hit:
                out.append(Violation(
                    "protected_attribute",
                    f"{path} looks like a protected attribute ('{hit}')",
                ))
            if isinstance(value, str) and _DATA_IMAGE.search(value):
                out.append(Violation("embedded_image", f"{path} contains an embedded image"))
        return out

    def _protected_terms_in_text(self, payload) -> list[Violation]:
        out = []
        for path, _key, value in _walk(payload):
            if isinstance(value, str):
                found = sorted({m.group(0).lower() for m in self.term_regex.finditer(value)})
                if found:
                    out.append(Violation(
                        "protected_term_in_text",
                        f"{path} mentions protected trait(s): {', '.join(found)}",
                    ))
        return out

    def _scoring(self, action, rules: ScoredToolRules, allowed_criteria) -> list[Violation]:
        p = action.payload
        out = []

        job = p.get("job_opening")
        if not isinstance(job, str) or not job.strip():
            out.append(Violation("job_opening_missing", "payload.job_opening is required"))

        if not _is_number(p.get("score")) or not rules.score_min <= p["score"] <= rules.score_max:
            out.append(Violation(
                "score_invalid",
                f"payload.score must be a number in [{rules.score_min}, {rules.score_max}]",
            ))

        criteria = p.get("criteria")
        if not isinstance(criteria, list) or len(criteria) < rules.min_criteria:
            out.append(Violation(
                "justification_missing",
                f"payload.criteria must list at least {rules.min_criteria} justified criterion",
            ))
            return out

        sources = set(action.source_hashes)
        for i, c in enumerate(criteria):
            where = f"payload.criteria[{i}]"
            if not isinstance(c, Mapping) or set(c) != CRITERION_KEYS:
                out.append(Violation(
                    "criterion_malformed",
                    f"{where} must have exactly {sorted(CRITERION_KEYS)}",
                ))
                continue
            name = c["criterion"]
            if not isinstance(name, str) or not name.strip():
                out.append(Violation("criterion_malformed", f"{where}.criterion must be a name"))
            elif allowed_criteria is not None and name not in allowed_criteria:
                out.append(Violation(
                    "criterion_not_job_related",
                    f"{where}: '{name}' is not a criterion of this job opening",
                ))
            if not _is_number(c["score"]) or not rules.score_min <= c["score"] <= rules.score_max:
                out.append(Violation("score_invalid", f"{where}.score out of range"))
            j = c["justification"]
            if not isinstance(j, str) or len(j.strip()) < rules.min_justification_chars:
                out.append(Violation(
                    "justification_too_short",
                    f"{where}.justification needs >= {rules.min_justification_chars} characters",
                ))
            if c["source_hash"] not in sources:
                out.append(Violation(
                    "justification_unsourced",
                    f"{where}.source_hash does not match any source of this action",
                ))
        return out

    # --- config ----------------------------------------------------------

    def _parse(self, config):
        if not isinstance(config, Mapping):
            raise FairnessConfigError("fairness config must be a JSON object")
        unknown = set(config) - KNOWN_TOP_KEYS
        if unknown:
            raise FairnessConfigError(f"unknown keys: {sorted(unknown)}")

        self.version = config.get("version")
        if not isinstance(self.version, str) or not self.version:
            raise FairnessConfigError("version is required")

        keys = _str_list(config, "protected_keys")
        self.protected_keys = [t for t in (_tokens(k) for k in keys) if t]

        terms = _str_list(config, "protected_terms")
        alts = sorted({re.escape(t.lower()).replace(r"\ ", r"\s+") for t in terms}, key=len, reverse=True)
        self.term_regex = re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)", re.IGNORECASE)

        self.text_scan_tools = frozenset(_str_list(config, "text_scan_tools", allow_empty=True))

        scored = config.get("scored_tools", {})
        if not isinstance(scored, Mapping):
            raise FairnessConfigError("scored_tools must be an object")
        self.scored_tools: dict[str, ScoredToolRules] = {}
        for tool, spec in scored.items():
            if not isinstance(spec, Mapping) or set(spec) != KNOWN_SCORED_KEYS:
                raise FairnessConfigError(
                    f"scored_tools.{tool} needs exactly {sorted(KNOWN_SCORED_KEYS)}")
            lo, hi = spec["score_min"], spec["score_max"]
            if not (_is_number(lo) and _is_number(hi) and lo < hi):
                raise FairnessConfigError(f"scored_tools.{tool}: need score_min < score_max")
            self.scored_tools[tool] = ScoredToolRules(
                lo, hi,
                _pos_int(spec["min_criteria"], f"{tool}.min_criteria"),
                _pos_int(spec["min_justification_chars"], f"{tool}.min_justification_chars"),
            )

        d = config.get("disparity")
        if not isinstance(d, Mapping) or set(d) != KNOWN_DISPARITY_KEYS:
            raise FairnessConfigError(f"disparity needs exactly {sorted(KNOWN_DISPARITY_KEYS)}")
        if not _is_number(d["threshold"]) or not 0 < d["threshold"] <= 1:
            raise FairnessConfigError("disparity.threshold must be in (0, 1]")
        self.disparity_threshold = float(d["threshold"])
        self.disparity_min_group = _pos_int(d["min_group_size"], "disparity.min_group_size")

    def disparity(self, outcomes: Iterable[tuple[str, bool]]) -> "DisparityReport":
        return disparity_report(outcomes, self.disparity_threshold, self.disparity_min_group)


# --- batch check: four-fifths rule -----------------------------------------------

@dataclass(frozen=True)
class GroupStat:
    group: str
    total: int
    selected: int
    rate: float
    impact_ratio: float | None  # None when the group is too small to judge


@dataclass(frozen=True)
class DisparityReport:
    status: str  # "ok" | "adverse_impact" | "insufficient_data"
    threshold: float
    groups: tuple[GroupStat, ...]
    flagged: tuple[str, ...]


def disparity_report(
    outcomes: Iterable[tuple[str, bool]], threshold: float = 0.8, min_group_size: int = 5
) -> DisparityReport:
    """outcomes: (group_label, was_selected) per candidate in one screening batch.

    Impact ratio = group selection rate / highest group selection rate.
    A group under `threshold` (default 0.8, the EEOC four-fifths rule) is
    flagged. Groups smaller than `min_group_size` are reported but not
    judged: ratios on tiny samples are noise. This is an ALERT for the
    human reviewer, not an automatic decision about any candidate.
    """
    totals, chosen = Counter(), Counter()
    for group, selected in outcomes:
        totals[group] += 1
        chosen[group] += bool(selected)

    rates = {g: chosen[g] / totals[g] for g in totals}
    judged = [g for g in totals if totals[g] >= min_group_size]
    best = max((rates[g] for g in judged), default=0.0)

    stats, flagged = [], []
    for g in sorted(totals):
        ratio = None
        if g in judged and best > 0:
            ratio = rates[g] / best
            if ratio < threshold and not math.isclose(ratio, threshold):
                flagged.append(g)
        stats.append(GroupStat(g, totals[g], chosen[g], rates[g], ratio))

    if len(judged) < 2:
        status = "insufficient_data"
    elif flagged:
        status = "adverse_impact"
    else:
        status = "ok"
    return DisparityReport(status, threshold, tuple(stats), tuple(flagged))


# --- helpers ---------------------------------------------------------------------

def _is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _pos_int(x, name) -> int:
    if not isinstance(x, int) or isinstance(x, bool) or x < 1:
        raise FairnessConfigError(f"{name} must be a positive integer")
    return x


def _str_list(config, key, allow_empty=False) -> list[str]:
    val = config.get(key)
    if not isinstance(val, list) or not all(isinstance(x, str) and x.strip() for x in val):
        raise FairnessConfigError(f"{key} must be a list of non-empty strings")
    if not val and not allow_empty:
        raise FairnessConfigError(f"{key} must not be empty")
    return val
