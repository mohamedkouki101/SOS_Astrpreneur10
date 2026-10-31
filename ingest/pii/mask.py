"""PII masking: identifiers become tokens, protected attributes are removed.

    "Nom : Amina Ben Salem"  ->  "Nom : <PERSON_1>"     (reversible, mapping kept apart)
    "Âge : 27 ans"           ->  "Âge : [REMOVED]"      (irreversible)

Detection sources:
- our rules (recognizers.py): emails, Tunisian phones, CIN, profile links,
  labeled names, protected attributes;
- Presidio + spaCy: names written freely in the text, international phones.

Overlaps: protected attributes win, then the longer match. The same value
always gets the same token within one document.
"""

import re
from dataclasses import dataclass, field
from functools import lru_cache

from ingest.pii.language import detect_language
from ingest.pii.recognizers import IDENTIFIER_RULES, PROTECTED_RULES

REMOVED = "[REMOVED]"
MIN_PRESIDIO_SCORE = 0.4
# A number followed by a currency is an amount (salary, budget), never a phone.
CURRENCY_AFTER = re.compile(r"[ \t]*(?:dt|tnd|dinars?|€|eur|\$|usd|k\b)", re.IGNORECASE)


@dataclass(frozen=True)
class _Detection:
    start: int
    end: int
    entity: str  # PERSON, EMAIL, ... or a protected attribute name
    protected: bool


@dataclass(frozen=True)
class MaskedItem:
    """One token in the report: <PERSON_1> = "Amina Ben Salem", seen 3 times."""

    token: str
    entity: str
    original: str
    count: int


@dataclass(frozen=True)
class RemovedAttribute:
    attribute: str  # contract name: AGE, GENDER, ...
    original: str


@dataclass(frozen=True)
class MaskResult:
    masked_text: str
    language: str
    items: list[MaskedItem]
    removed: list[RemovedAttribute]
    first_email: str | None  # for candidate_ref
    mapping: dict[str, str] = field(repr=False)  # token -> original: restricted store only

    @property
    def tokens(self) -> list[str]:
        return [item.token for item in self.items]

    @property
    def protected_removed(self) -> list[str]:
        return sorted({item.attribute for item in self.removed})


# --- Presidio, loaded once (loading the spaCy models takes a second or two) ---

@lru_cache(maxsize=1)
def _analyzer():
    _block_tldextract_downloads()
    from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_analyzer.predefined_recognizers import PhoneRecognizer, SpacyRecognizer

    nlp_engine = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy",
        "models": [
            {"lang_code": "fr", "model_name": "fr_core_news_sm"},
            {"lang_code": "en", "model_name": "en_core_web_sm"},
        ],
        # spaCy's French model says "PER", the English one "PERSON".
        "ner_model_configuration": {
            "model_to_presidio_entity_mapping": {"PER": "PERSON", "PERSON": "PERSON"},
            "labels_to_ignore": ["LOC", "GPE", "ORG", "MISC", "DATE", "NORP", "CARDINAL"],
        },
    }).create_engine()

    registry = RecognizerRegistry(supported_languages=["fr", "en"])
    for language in ("fr", "en"):
        registry.add_recognizer(SpacyRecognizer(supported_language=language, supported_entities=["PERSON"]))
        registry.add_recognizer(PhoneRecognizer(
            supported_language=language, supported_regions=("TN", "FR", "GB", "US", "DE")))
    return AnalyzerEngine(registry=registry, nlp_engine=nlp_engine, supported_languages=["fr", "en"])


def _block_tldextract_downloads() -> None:
    """tldextract (a Presidio dependency) may download a domain list on first
    use. This service has no internet access and must not try: use the copy
    bundled with the package instead."""
    try:
        import tldextract.tldextract as module
    except ImportError:
        return
    module.TLD_EXTRACTOR = module.TLDExtract(suffix_list_urls=())


def _presidio_detections(text: str, language: str) -> list[_Detection]:
    detections = []
    for result in _analyzer().analyze(text=text, language=language, entities=["PERSON", "PHONE_NUMBER"]):
        if result.score < MIN_PRESIDIO_SCORE:
            continue
        value = text[result.start:result.end]
        if result.entity_type == "PERSON":
            # The small models also tag skills or headings as names. Keep only
            # names of 2+ capitalized words, with no digits or symbols.
            words = value.split()
            if len(words) < 2 or not all(word[:1].isupper() for word in words) or re.search(r"[\d@<>\[\]_/]", value):
                continue
            detections.append(_Detection(result.start, result.end, "PERSON", False))
        elif not CURRENCY_AFTER.match(text, result.end):
            detections.append(_Detection(result.start, result.end, "PHONE", False))
    return detections


# --- Our rules ---

def _rule_detections(text: str) -> list[_Detection]:
    detections = []
    for protected, rules in ((True, PROTECTED_RULES), (False, IDENTIFIER_RULES)):
        for entity, pattern in rules:
            for match in pattern.finditer(text):
                start, end = match.span("value")
                if end > start:
                    detections.append(_Detection(start, end, entity, protected))
    return detections


def _repeat_names(text: str, detections: list[_Detection]) -> list[_Detection]:
    """A name found once is masked everywhere it appears, even where the
    model missed it (e.g. in the experience section)."""
    names = {text[d.start:d.end] for d in detections if d.entity == "PERSON"}
    extra = []
    for name in names:
        for match in re.finditer(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE):
            extra.append(_Detection(match.start(), match.end(), "PERSON", False))
    return extra


def _resolve_overlaps(detections: list[_Detection]) -> list[_Detection]:
    """Protected attributes first, then longer matches; drop anything overlapping.

    Exception: a protected word *inside* an identifier ("single" in
    amina.single@example.test) is ignored, so the whole email stays masked."""
    identifiers = [d for d in detections if not d.protected]
    detections = [
        d for d in detections
        if not (d.protected and any(i.start <= d.start and d.end <= i.end for i in identifiers))
    ]
    ranked = sorted(detections, key=lambda d: (not d.protected, -(d.end - d.start), d.start))
    chosen: list[_Detection] = []
    for candidate in ranked:
        if all(candidate.end <= kept.start or candidate.start >= kept.end for kept in chosen):
            chosen.append(candidate)
    return sorted(chosen, key=lambda d: d.start)


def _normalize(entity: str, value: str) -> str:
    """The key that decides "same value, same token"."""
    if entity == "PHONE":
        digits = re.sub(r"\D", "", value)
        return digits[-8:] if digits.startswith(("216", "00216")) else digits
    if entity == "PROFILE_URL":
        value = re.sub(r"^h[tx]{2}ps?://|\[|\]|/$", "", value.lower())
        return value.removeprefix("www.")
    if entity == "EMAIL":
        return canonical_email(value)
    return " ".join(value.lower().split())


def canonical_email(value: str) -> str:
    """amina [at] example [.] test -> amina@example.test"""
    value = re.sub(r"\s+", "", value.lower())
    value = re.sub(r"\[@\]|\(at\)|\[at\]|\{at\}", "@", value)
    return re.sub(r"\[\.\]|\(dot\)|\[dot\]", ".", value)


# --- Main entry point ---

def mask(text: str) -> MaskResult:
    language = detect_language(text)
    # Language unclear: run both name models rather than guess.
    models = [language] if language != "unknown" else ["fr", "en"]
    detections = _rule_detections(text)
    for model_language in models:
        detections += _presidio_detections(text, model_language)
    detections += _repeat_names(text, detections)
    chosen = _resolve_overlaps(detections)

    tokens: dict[tuple[str, str], str] = {}  # (entity, normalized value) -> token
    counters: dict[str, int] = {}
    originals: dict[str, str] = {}
    counts: dict[str, int] = {}
    removed: list[RemovedAttribute] = []
    first_email = None
    pieces, position = [], 0

    for detection in chosen:  # left to right, so <PERSON_1> is the first name in the text
        value = text[detection.start:detection.end]
        pieces.append(text[position:detection.start])
        position = detection.end
        if detection.protected:
            pieces.append(REMOVED)
            removed.append(RemovedAttribute(detection.entity, value))
            continue
        key = (detection.entity, _normalize(detection.entity, value))
        if key not in tokens:
            counters[detection.entity] = counters.get(detection.entity, 0) + 1
            tokens[key] = f"<{detection.entity}_{counters[detection.entity]}>"
            originals[tokens[key]] = value
        token = tokens[key]
        counts[token] = counts.get(token, 0) + 1
        pieces.append(token)
        if detection.entity == "EMAIL" and first_email is None:
            first_email = canonical_email(value)
    pieces.append(text[position:])

    items = [MaskedItem(token, token[1:].rsplit("_", 1)[0], originals[token], counts[token]) for token in originals]
    return MaskResult(
        masked_text="".join(pieces),
        language=language,
        items=items,
        removed=removed,
        first_email=first_email,
        mapping=dict(originals),
    )
