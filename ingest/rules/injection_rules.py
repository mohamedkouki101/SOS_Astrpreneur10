"""Prompt-injection rules, French + English. SHARED MODULE.

The ingest scanner and the AI layer's output validator both import this file,
so both sides flag exactly the same phrases. Standard library only: copy or
import it as is. Change it only together with the AI-layer teammate.

Rules run on a "folded" copy of the text (lowercase, accents removed), so
"IGNOREZ les instructions PRÉCÉDENTES" and "ignore prévious" still match.
Folding keeps one character per character, so match positions map straight
back onto the real text.
"""

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Rule:
    id: str  # appears in signals as "rule:<id>"
    label: str  # short English text for the dashboard
    weight_bp: int  # risk points, in basis points (10000 = certain)
    pattern: re.Pattern


@dataclass(frozen=True)
class RuleMatch:
    rule: Rule
    start: int
    end: int
    text: str  # the matched text, from the original (unfolded) string


# Typographic quotes from Word / Outlook: "Don’t" must match "don't".
QUOTES = {"’": "'", "‘": "'", "ʼ": "'", "“": '"', "”": '"'}


@lru_cache(maxsize=4096)
def _fold_char(char: str) -> str:
    """One character in, exactly one character out (so positions never shift)."""
    if char in QUOTES:
        return QUOTES[char]
    base = "".join(c for c in unicodedata.normalize("NFKD", char) if not unicodedata.combining(c))
    return (base or char).lower()[:1]


def fold(text: str) -> str:
    """Lowercase and strip accents, keeping the length: é -> e, É -> e."""
    return "".join(_fold_char(char) for char in text)


def _rule(rule_id: str, label: str, weight_bp: int, pattern: str) -> Rule:
    return Rule(rule_id, label, weight_bp, re.compile(pattern, re.MULTILINE))


# Patterns are written in folded form: lowercase, no accents, straight quotes.
# Two whitespace runs are never adjacent (each optional part starts with a
# word), and "^" is followed by [ \t]*, never \s*: that keeps every rule
# linear in time, even on text made of thousands of spaces or newlines.
RULES: list[Rule] = [
    # --- Override the instructions ---
    _rule("ignore_previous_en", "Tries to cancel the AI's instructions", 4000,
          r"\b(?:ignore|disregard|forget|override|skip)\s+"
          r"(?:(?:(?:all|any|the|your|these|those)\s+){0,2}"
          r"(?:(?:previous|prior|above|earlier|preceding|system|original|initial)\s+){1,2}"
          r"|(?:all|your)\s+(?:the\s+)?)"
          r"(?:instructions?|prompts?|rules?|directions?|messages?|context|guidelines?)\b"),
    _rule("ignore_previous_fr", "Tries to cancel the AI's instructions", 4000,
          r"\b(?:ignore[rz]?|oublie[rz]?|ne\s+(?:tiens|tenez)\s+pas\s+compte\s+des)\s+(?:toutes\s+)?(?:les\s+|tes\s+|vos\s+)?"
          r"(?:instructions?|consignes?|regles?|directives?)\b"
          r"(?:\s+(?:precedentes?|ci-dessus|anterieures?|du\s+systeme)\b)?"),

    # --- Change the AI's role ---
    _rule("role_override_en", "Tries to change the AI's role", 3000,
          r"\b(?:you\s+are\s+now|from\s+now\s+on,?\s+you|pretend\s+(?:to\s+be|you\s+are)|new\s+instructions?\s*:"
          r"|act\s+as\s+(?:an?\s+|the\s+)?(?:ai|assistant|recruiter|hr|system|admin|evaluator))\b"),
    _rule("role_override_fr", "Tries to change the AI's role", 3000,
          r"\b(?:tu\s+es\s+maintenant|vous\s+etes\s+maintenant|a\s+partir\s+de\s+maintenant,?\s+(?:tu|vous)"
          r"|agis\s+comme|agissez\s+comme|fais\s+comme\s+si|nouvelles?\s+instructions?\s*:)"),

    # --- Rig the ranking ---
    _rule("rank_manipulation_en", "Tries to rig the candidate ranking", 4000,
          r"\b(?:rank|rate|score|place|put|select|recommend|shortlist)\s+"
          r"(?:(?:this|that|my|the)\s+(?:candidate|application|profile|cv|resume)|me|him|her|this\s+one)\s+"
          r"(?:as\s+)?(?:first|top|highest|best|number\s+one)\b"
          r"|\b(?:give|assign)\s+(?:this\s+candidate\s+|me\s+|him\s+|her\s+)?(?:the\s+)?"
          r"(?:highest|maximum|max|perfect|top)\s+(?:score|rating|grade|mark)\b"
          r"|\bthis\s+is\s+the\s+(?:best|ideal|perfect)\s+candidate\b"),
    _rule("rank_manipulation_fr", "Tries to rig the candidate ranking", 4000,
          r"\b(?:classe[rz]?|place[rz]?|mets|mettez|selectionne[rz]?|recommande[rz]?)\s+(?:ce|cette|mon|ma|le|la)\s+"
          r"(?:candidat(?:e|ure)?|profil|cv)\s+(?:en\s+)?(?:premier|premiere|tete|1er|numero\s+un)\b"
          r"|\b(?:donne[rz]?|attribue[rz]?)\s+(?:a\s+ce(?:tte)?\s+candidat(?:e)?\s+)?(?:la\s+)?(?:note|score)\s+"
          r"(?:maximale|maximum|max|la\s+plus\s+haute)\b"),

    # --- Ask the AI to act ---
    # Targets are narrow on purpose: "I will send an email to confirm" is normal.
    _rule("action_request_en", "Asks the AI to take an action", 3000,
          r"\b(?:send|forward)\s+(?:an?\s+)?(?:email|e-mail|message|this|the\s+(?:data|file|cv))\s+to\s+"
          r"(?:[\w.+-]+@[\w.-]+|<email_\d+>|all|everyone|the\s+(?:hr|admin|recruiter|manager)s?)\b"
          r"|\b(?:approve|accept|hire)\s+(?:this|the)\s+(?:candidate|application)\b"
          r"|\bschedule\s+(?:an?\s+)?interview\s+(?:immediately|now)\b"
          r"|\b(?:call|use)\s+the\s+(?:tool|api|function)\b"),
    _rule("action_request_fr", "Asks the AI to take an action", 3000,
          r"\b(?:envoie[rsz]?|envoyer|envoyez|transmets|transmettez|transmettre|transfere[rz]?)\s+(?:un\s+)?"
          r"(?:e-?mail|mail|message|ce\s+(?:cv|fichier))\s+a\s+"
          r"(?:[\w.+-]+@[\w.-]+|<email_\d+>|tous|tout\s+le\s+monde|(?:la|les|l')\s*(?:rh|admin|recruteur|manager)s?)\b"
          r"|\b(?:approuve[rz]?|accepte[rz]?|embauche[rz]?|recrute[rz]?)\s+(?:ce|cette)\s+candidat(?:e|ure)?\b"
          r"|\bplanifie[rz]?\s+(?:un\s+)?entretien\s+(?:immediatement|tout\s+de\s+suite)\b"),

    # --- Extract the prompt ---
    _rule("prompt_extraction_en", "Tries to read the AI's hidden instructions", 3000,
          r"\b(?:system\s+prompt|reveal\s+(?:your|the)\s+(?:instructions?|prompt|rules)"
          r"|print\s+(?:your|the)\s+(?:instructions?|prompt)|what\s+are\s+your\s+instructions)"),
    _rule("prompt_extraction_fr", "Tries to read the AI's hidden instructions", 3000,
          r"\b(?:prompt\s+systeme|revele[rz]?\s+(?:tes|vos|les)\s+(?:instructions?|consignes?)"
          r"|affiche[rz]?\s+(?:tes|vos)\s+(?:instructions?|consignes?))"),

    # --- Fake chat markers (pretend the text is a system or assistant message) ---
    _rule("fake_chat_markers", "Contains fake AI chat markers", 3000,
          r"\[/?inst\]|<\|im_(?:start|end)\|>|</?[ \t]*(?:system|assistant)[ \t]*>"
          r"|^[ \t]*#{2,}[ \t]*(?:instructions?|system)\b"),

    # --- Ask to hide the attempt ---
    _rule("conceal_en", "Asks the AI to hide something", 2000,
          r"\b(?:do\s+not|don'?t|never)\s+(?:mention|tell|reveal|disclose|report)\s+(?:this|that|these|it)\b"),
    _rule("conceal_fr", "Asks the AI to hide something", 2000,
          r"\bne\s+(?:le\s+|la\s+|les\s+|l')?(?:mentionne[rz]?|dis|dites|revele[rz]?|signale[rz]?)\s+pas"
          r"|\bsans\s+le\s+(?:mentionner|signaler)"),

    # --- Encoded payload (instructions hidden in base64) ---
    _rule("encoded_payload", "Contains a long encoded block", 2000, r"[a-z0-9+/]{200,}={0,2}"),
]


def find_matches(text: str) -> list[RuleMatch]:
    """Every rule match in `text`, in order of position."""
    folded = fold(text)
    matches = [
        RuleMatch(rule, m.start(), m.end(), text[m.start():m.end()])
        for rule in RULES
        for m in rule.pattern.finditer(folded)
    ]
    return sorted(matches, key=lambda match: (match.start, match.end))
