"""Defang links: keep them readable, make them unclickable.

https://evil.example/path  ->  hxxps://evil[.]example/path
www.evil.example           ->  www[.]evil[.]example

Links in a CV or email are never fetched by this service. Defanging also
stops a dashboard, an email client or a chat tool from turning them back
into clickable links later. Email addresses are left alone: PII masking
replaces them next.
"""

import re

# A host ends with a letter or digit (any script), so a sentence's final "."
# or "," is not part of it. IPv6 literals like [::1] are accepted too.
HOST = r"(?:\[[0-9a-f:.]+\]|[^\s/?#<>\"'()\[\]]*[^\W_])"
URL_WITH_SCHEME = re.compile(rf"\b(https?|ftp)://({HOST})", re.IGNORECASE)
BARE_WWW = re.compile(rf"(?<![\w./\[])(www\.{HOST})", re.IGNORECASE)
SCHEMES = {"http": "hxxp", "https": "hxxps", "ftp": "fxp"}


def _defang_host(host: str) -> str:
    return host if host.startswith("[") else host.replace(".", "[.]")


def defang_links(text: str) -> tuple[str, int]:
    """Return the defanged text and how many links were changed."""
    text, with_scheme = URL_WITH_SCHEME.subn(
        lambda m: f"{SCHEMES[m.group(1).lower()]}://{_defang_host(m.group(2))}", text
    )
    text, bare = BARE_WWW.subn(lambda m: _defang_host(m.group(1)), text)
    return text, with_scheme + bare
