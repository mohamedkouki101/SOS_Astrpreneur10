"""Our own detection rules, for formats with a fixed shape.

Each rule has a named group "value": only that part is replaced, so the
label stays readable ("Âge : [REMOVED]") and the report shows what kind of
data was removed. Names written freely in the text are found by spaCy
(through Presidio) in mask.py, not here.

Labels and values must be on the same line ([ \\t] instead of \\s), so an
empty field never swallows the next heading.
"""

import re

FLAGS = re.IGNORECASE | re.MULTILINE
SEP = r"[ \t]*:[ \t]*"  # "Label : value" on one line
WORDS_1_3 = r"[^\W\d_]+(?:[ '’-][^\W\d_]+){0,2}"  # one to three words, letters only
APOS = r"['’]"  # straight or typographic apostrophe (NFKC keeps both)
AT = r"(?:@|\[@\]|\(at\)|\[at\]|\{at\})"
DOT = r"(?:\.|\[\.\]|\(dot\)|\[dot\])"

# --- Identifiers: replaced by reversible tokens such as <EMAIL_1> ---
IDENTIFIER_RULES: list[tuple[str, re.Pattern]] = [
    # Also obfuscated forms: amina[@]example[.]test, amina(at)example(dot)test
    ("EMAIL", re.compile(rf"(?P<value>[\w.%+-]+[ \t]*{AT}[ \t]*[\w-]+(?:[ \t]*{DOT}[ \t]*[\w-]+)+)", FLAGS)),
    # Tunisian numbers: +216 / 00216 prefix ...
    ("PHONE", re.compile(r"(?P<value>(?:\+|00)216[\s.-]?\d{2}[\s.-]?\d{3}[\s.-]?\d{3})\b", FLAGS)),
    # ... or the local 2-3-3 grouping, with a real mobile/landline prefix
    # (2x, 4x, 5x, 7x, 9x) and not followed by a currency (salaries, budgets).
    ("PHONE", re.compile(
        r"(?<![\d+])(?P<value>[24579]\d[ .]\d{3}[ .]\d{3})\b(?![ \t]*(?:dt|tnd|dinars?|€|eur|\$|usd|k\b))", FLAGS)),
    ("PHONE", re.compile(r"\b(?:t[ée]l(?:[ée]phone)?|phone|mobile|gsm|portable)[ \t]*[:.]?[ \t]*(?P<value>\d{8})\b", FLAGS)),
    # CIN: 8 digits (spaces allowed), only next to a label: 8 digits alone could be anything.
    ("CIN", re.compile(
        rf"\b(?:cin|c\.i\.n\.?|carte d{APOS}identit[ée](?: nationale)?|national id|id card)"
        r"[ \t]*(?:n°|no\.?|num[ée]ro)?[ \t]*[:#]?[ \t]*(?P<value>\d(?: ?\d){7})(?!\d)", FLAGS)),
    # LinkedIn / GitHub profiles, also in defanged form (hxxps://www[.]linkedin[.]com/in/...).
    ("PROFILE_URL", re.compile(
        r"(?P<value>(?:h[tx]{2}ps?://)?(?:www(?:\[\.\]|\.))?(?:linkedin|github)(?:\[\.\]|\.)com/(?:in/)?[\w-]+/?)",
        FLAGS)),
    # A postal address after a label: the rest of the line.
    ("ADDRESS", re.compile(
        r"^[ \t]*(?:adresse(?: postale| personnelle| du domicile)?|(?:home |postal )?address|domicile"
        rf"|lieu de r[ée]sidence){SEP}(?P<value>[^\n]{{3,150}}?)[ \t]*$", FLAGS)),
    # A name after a label, e.g. "Nom : Amina Ben Salem" (the whole line).
    ("PERSON", re.compile(
        rf"^[ \t]*(?:nom(?: et pr[ée]nom| complet)?|pr[ée]nom|name|full name){SEP}"
        rf"(?P<value>{WORDS_1_3})[ \t]*$", FLAGS)),
]

# --- Protected attributes: removed for good, replaced by [REMOVED] ---
# Contract names: AGE, BIRTH_DATE, GENDER, MARITAL_STATUS, NATIONALITY, RELIGION, PHOTO.
MONTH_WORD = r"[^\W\d_]{3,}\.?"
DATE = (
    r"\d{4}-\d{1,2}-\d{1,2}"                        # 1999-03-12
    r"|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}"             # 12/03/1999
    rf"|\d{{1,2}}(?:er)? {MONTH_WORD} \d{{4}}"      # 12 mars 1999
    rf"|{MONTH_WORD} \d{{1,2}},? \d{{4}}"           # March 4, 1995
    r"|\d{4}"                                       # 1999 (only after a birth label)
)
PROTECTED_RULES: list[tuple[str, re.Pattern]] = [
    # Only labeled ages: a bare "3 ans" is usually years of experience, which must stay.
    ("AGE", re.compile(r"\b[âa]ge[ \t]*:?[ \t]*(?P<value>\d{1,2}(?!\d)(?:[ \t]*(?:ans|years?(?: old)?))?)", FLAGS)),
    ("AGE", re.compile(r"\b[âa]g[ée]e? de (?P<value>\d{1,2} ans)\b", FLAGS)),
    ("AGE", re.compile(r"(?P<value>\b\d{1,2} years old)\b", FLAGS)),
    ("BIRTH_DATE", re.compile(
        r"\b(?:date de naissance|n[ée]e? (?:le|en)|date of birth|dob|born(?: on| in)?)[ \t]*:?[ \t]*"
        rf"(?P<value>{DATE})(?![\d/.-]?\d)", FLAGS)),
    ("GENDER", re.compile(rf"\b(?:sexe|gender|sex){SEP}(?P<value>[^\W\d_]+)", FLAGS)),
    # "Genre" also means "kind" in French: only with a gender value.
    ("GENDER", re.compile(rf"\bgenre{SEP}(?P<value>f|m|femme|homme|f[ée]minin|masculin)(?!\w)", FLAGS)),
    ("MARITAL_STATUS", re.compile(
        rf"\b(?:situation (?:familiale|matrimoniale)|[ée]tat civil|marital status){SEP}"
        rf"(?P<value>{WORDS_1_3}(?:\(e\))?)", FLAGS)),
    # French status words are specific enough alone. English ones ("single",
    # "married") are common in normal text ("single-page app"): labeled only.
    ("MARITAL_STATUS", re.compile(
        r"(?<![\w-])(?P<value>c[ée]libataire|mari[ée]e?(?:\(e\))?|divorc[ée]e?(?:\(e\))?|veuf|veuve)(?![\w-])", FLAGS)),
    ("NATIONALITY", re.compile(rf"\b(?:nationalit[ée]|nationality){SEP}(?P<value>{WORDS_1_3})", FLAGS)),
    ("RELIGION", re.compile(rf"\breligion{SEP}(?P<value>{WORDS_1_3})", FLAGS)),
]
