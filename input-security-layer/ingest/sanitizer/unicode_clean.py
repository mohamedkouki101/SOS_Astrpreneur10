"""Unicode cleanup: remove invisible characters, then normalize with NFKC.

Invisible characters let an attacker show one text to a human and another to
the AI (hidden instructions, reversed text, split keywords). Removing them is
a cleanup step; the count is also reported as a risk signal.
"""

import unicodedata
from typing import NamedTuple

KEEP_CONTROLS = {"\n", "\t"}
# \v and \f are Word line/page breaks: turning them into \n keeps words apart.
LINE_BREAKS = {
    "\r\n": "\n", "\r": "\n", "\x0b": "\n", "\x0c": "\n",
    " ": "\n", " ": "\n", "\x85": "\n",
}
# Characters with no visible width that are not in a "format" category, so
# they are listed by hand: blank-looking letters, the combining grapheme
# joiner, and Khmer / Mongolian invisible marks.
INVISIBLE_EXTRAS = {
    "ᅟ", "ᅠ", "ㅤ", "ﾠ", "⠀",
    "͏", "឴", "឵", "᠋", "᠌", "᠍", "᠎", "᠏",
}


class CleanText(NamedTuple):
    text: str
    removed_count: int  # becomes the signal "invisible_chars_removed:N"


def _is_invisible(char: str) -> bool:
    category = unicodedata.category(char)
    if category == "Cf":
        # Format characters: zero-width spaces/joiners, bidi controls
        # ("Trojan Source"), BOM, soft hyphen, and tag characters
        # U+E0000-E007F that can hide a whole instruction ("ASCII smuggling").
        return True
    if category == "Cc" and char not in KEEP_CONTROLS:
        return True  # control characters (NUL, BEL, ESC, ...)
    if category == "Cs":
        return True  # lone surrogates: cannot be encoded as UTF-8, would crash hashing
    if category == "Cn":
        return True  # unassigned code points: no legitimate text uses them
    if "︀" <= char <= "️" or "\U000e0100" <= char <= "\U000e01ef":
        return True  # variation selectors, can also carry hidden data
    return char in INVISIBLE_EXTRAS


def clean_text(text: str) -> CleanText:
    # 1. One kind of line break, so sections and rules see the same text.
    for old, new in LINE_BREAKS.items():
        text = text.replace(old, new)

    # 2. Remove invisible characters and count them.
    kept = [char for char in text if not _is_invisible(char)]
    removed = len(text) - len(kept)

    # 3. NFKC last, on the cleaned text: folds lookalike forms such as
    #    fullwidth "ｉｇｎｏｒｅ" -> "ignore" and ligatures "ﬁ" -> "fi",
    #    so the injection rules match them. It does NOT fix letters from
    #    other scripts (a Cyrillic "о" inside a Latin word).
    return CleanText(unicodedata.normalize("NFKC", "".join(kept)), removed)
