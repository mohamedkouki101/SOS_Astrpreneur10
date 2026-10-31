"""French or English? A stop-word count: simple, fast, no extra package."""

import re
from typing import Literal

FRENCH = {
    "le", "la", "les", "des", "et", "est", "une", "pour", "avec", "dans", "sur", "du",
    "au", "aux", "je", "nous", "vous", "chez", "expérience", "compétences", "formation",
}
ENGLISH = {
    "the", "and", "is", "for", "with", "of", "to", "at", "my", "we", "you",
    "experience", "skills", "education", "worked",
}
WORD = re.compile(r"[a-zàâçéèêëîïôûùüÿœ']+")


def detect_language(text: str) -> Literal["fr", "en", "unknown"]:
    words = WORD.findall(text.lower())
    french = sum(word in FRENCH for word in words)
    english = sum(word in ENGLISH for word in words)
    if french == english:
        return "unknown"
    return "fr" if french > english else "en"
