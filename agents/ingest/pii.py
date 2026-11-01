"""Stage 2 - PII masking (Presidio).

1. Lines stating protected attributes (age, date of birth, gender, nationality, marital status,
   religion, photo) are removed entirely: they must never influence scoring.
2. Known identifiers of the subject (from the HR Flow record: name, email, phone) are replaced
   with stable tokens such as [PERSON_1].
3. Presidio finds remaining names, emails and phone numbers (+ a Tunisian phone pattern).

The token -> original map is kept per run in the TokenStore. Where it is persisted long-term and
who may de-tokenize is an open question in the design notes; for now it is stored under var/ (gitignored).
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

PROTECTED_LINE = re.compile(
    r"^.*\b(date of birth|birth ?date|born on|d\.o\.b|age\s*[:\-]|gender|sex\s*[:\-]|nationality|"
    r"marital status|religion|photo)\b.*$",
    re.I | re.M,
)
TN_PHONE = r"(\+216[\s.-]?)?\b[2-9]\d[\s.-]?\d{3}[\s.-]?\d{3}\b"

# Words Presidio's small spaCy model may mistake for names; never masked.
ALLOW_WORDS = [
    "Python", "JavaScript", "SQL", "Docker", "React", "Git", "Linux", "Excel", "Power BI", "IFRS", "CRM", "SEO",
    "Agile", "Scrum", "Arabic", "French", "English", "Payroll", "Recruitment", "Onboarding", "Leadership",
    "Tunis", "Tunisia", "Sfax", "Sousse", "Monastir", "Nabeul", "Bizerte", "Gabes", "Kairouan", "Ariana",
    "Bachelor", "Master", "Engineering", "REST", "APIs", "Machine Learning", "Statistics", "Accounting",
]


@dataclass
class MaskResult:
    text: str
    tokens: dict[str, str] = field(default_factory=dict)      # token -> original
    removed_protected_lines: int = 0


class TokenStore:
    """token <-> original value, stable within a store. Persisted as JSON under var/ (gitignored)."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self._lock = threading.Lock()
        self.forward: dict[str, str] = {}      # original -> token
        self.reverse: dict[str, str] = {}      # token -> original
        if path and path.is_file():
            self.reverse = json.loads(path.read_text(encoding="utf-8"))
            self.forward = {v: k for k, v in self.reverse.items()}

    def token(self, kind: str, original: str) -> str:
        key = f"{kind}:{original.strip().lower()}"
        with self._lock:
            if key not in self.forward:
                n = sum(1 for k in self.forward if k.startswith(kind + ":")) + 1
                tok = f"[{kind}_{n}]"
                self.forward[key] = tok
                self.reverse[tok] = original.strip()
            return self.forward[key]

    def original(self, token: str) -> str | None:
        return self.reverse.get(token)

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.reverse, indent=1, ensure_ascii=False), encoding="utf-8")


@lru_cache(maxsize=1)
def _analyzer():
    from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    provider = NlpEngineProvider(
        nlp_configuration={"nlp_engine_name": "spacy", "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}]}
    )
    engine = AnalyzerEngine(nlp_engine=provider.create_engine(), supported_languages=["en"])
    engine.registry.add_recognizer(
        PatternRecognizer(supported_entity="PHONE_NUMBER", patterns=[Pattern("tn_phone", TN_PHONE, 0.7)])
    )
    return engine


KIND = {"PERSON": "PERSON", "EMAIL_ADDRESS": "EMAIL", "PHONE_NUMBER": "PHONE"}
# Deterministic email pass. Presidio validates the TLD, so addresses on internal or reserved
# domains (e.g. @bara3am.example, @rhflow.local) would otherwise reach the model unmasked.
EMAIL_RE = re.compile(r"(?<![\w.%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def mask(text: str, store: TokenStore, known: dict[str, str] | None = None) -> MaskResult:
    removed = len(PROTECTED_LINE.findall(text))
    text = PROTECTED_LINE.sub("[protected attribute removed]", text)

    # Known identifiers first (exact, case-insensitive), including each part of a full name.
    order = {"EMAIL": 0, "PHONE": 1, "PERSON": 2}   # emails/phones first: they contain name parts
    for kind, value in sorted((known or {}).items(), key=lambda kv: order.get(kv[0], 3)):
        if not value:
            continue
        values = [value] + ([p for p in value.split() if len(p) > 2] if kind == "PERSON" else [])
        for v in sorted(values, key=len, reverse=True):
            tok = store.token(kind, value)
            text = re.sub(re.escape(v), tok, text, flags=re.I)

    tokens: dict[str, str] = {}

    def _email(m: re.Match) -> str:
        tok = store.token("EMAIL", m.group(0))
        tokens[tok] = m.group(0)
        return tok

    text = EMAIL_RE.sub(_email, text)

    results = _analyzer().analyze(
        text=text, language="en", entities=list(KIND), allow_list=ALLOW_WORDS, score_threshold=0.5
    )
    placeholders = [(m.start(), m.end()) for m in re.finditer(r"\[[A-Z_]+_\d+\]", text)]
    for r in sorted(results, key=lambda r: r.start, reverse=True):
        original = text[r.start:r.end]
        if any(r.start < end and r.end > start for start, end in placeholders):
            continue   # never re-mask (part of) an existing placeholder
        tok = store.token(KIND[r.entity_type], original)
        tokens[tok] = original
        text = text[: r.start] + tok + text[r.end:]
    for kind, value in (known or {}).items():
        if value:
            tokens[store.token(kind, value)] = value
    return MaskResult(text=text, tokens=tokens, removed_protected_lines=removed)
