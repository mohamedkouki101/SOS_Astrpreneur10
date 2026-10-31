"""HTML email body to plain text, without hidden content.

Uses the standard library parser: it never runs scripts, loads images or
follows links. Hidden elements and HTML comments are removed and recorded,
like hidden text in files. An element is hidden by:
- inline style (display:none, white/transparent color, tiny font, ...),
- a CSS class or id hidden in a <style> block,
- the legacy <font color="white">, or the `hidden` attribute.

Parsing is linear in the input size: each tag is pushed and popped once,
and the "is this hidden?" answer is stored with each open tag.
"""

import re
from html.parser import HTMLParser
from typing import NamedTuple

from ingest.sanitizer.hidden import RemovedItem

SKIPPED_TAGS = {"script", "style", "head", "title", "noscript", "template", "iframe", "object", "svg"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "dl", "dt", "dd", "tr", "td", "th", "table", "thead", "tbody",
    "section", "article", "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "hr",
}
MAX_LINK_PARTS = 200  # only compare short link texts with their href
CSS_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
SIMPLE_SELECTOR = re.compile(r"^[a-z0-9]*([.#])([\w-]+)$", re.IGNORECASE)
NUMBER = re.compile(r"^(-?\d*\.?\d+)([a-z%]*)$")


# --- CSS checks (floats are fine here: this never goes into the envelope) ---

def _declarations(style: str) -> dict[str, str]:
    """'Color: #FFF !important; font-size:0' -> {'color': '#fff', 'font-size': '0'}"""
    result = {}
    for declaration in style.split(";"):
        name, colon, value = declaration.partition(":")
        if colon:
            result[name.strip().lower()] = value.replace("!important", "").strip().lower()
    return result


def _number(value: str) -> tuple[float, str] | None:
    match = NUMBER.match(value.replace(" ", ""))
    return (float(match.group(1)), match.group(2)) if match else None


def _color_reason(value: str) -> str | None:
    value = value.replace(" ", "").lower()
    if value == "transparent":
        return "transparent_text"
    if value == "white":
        return "white_text"
    channels, alpha = None, 1.0
    if re.fullmatch(r"#[0-9a-f]{3,4}", value):
        channels = [int(c * 2, 16) for c in value[1:4]]
        alpha = int(value[4] * 2, 16) / 255 if len(value) == 5 else 1.0
    elif re.fullmatch(r"#[0-9a-f]{6}([0-9a-f]{2})?", value):
        channels = [int(value[i:i + 2], 16) for i in (1, 3, 5)]
        alpha = int(value[7:9], 16) / 255 if len(value) == 9 else 1.0
    elif match := re.fullmatch(r"rgba?\(([\d.]+),([\d.]+),([\d.]+)(?:,([\d.]+))?\)", value):
        channels = [float(match.group(i)) for i in (1, 2, 3)]
        alpha = float(match.group(4)) if match.group(4) else 1.0
    if channels is None:
        return None
    if alpha < 0.1:
        return "transparent_text"
    return "white_text" if min(channels) >= 0xF0 else None


def _style_reason(declarations: dict[str, str]) -> str | None:
    if declarations.get("display") == "none" or declarations.get("visibility") in ("hidden", "collapse"):
        return "html_hidden"
    if (size := _number(declarations.get("font-size", ""))) is not None:
        value, unit = size
        limit = {"em": 0.3, "rem": 0.3, "%": 30}.get(unit, 4)  # px, pt or no unit: 4
        if value < limit:
            return "tiny_text"
    if (opacity := _number(declarations.get("opacity", ""))) is not None and opacity[0] < 0.1:
        return "transparent_text"
    if "color" in declarations and (reason := _color_reason(declarations["color"])):
        return reason
    for name in ("text-indent", "left", "top", "margin-left", "margin-top"):
        if (offset := _number(declarations.get(name, ""))) is not None and offset[0] <= -999:
            return "off_page"
    for name in ("max-height", "height"):
        if declarations.get(name) in ("0", "0px") and declarations.get("overflow") == "hidden":
            return "html_hidden"
    return None


# --- The parser ---

class _Open(NamedTuple):
    tag: str
    hidden_by: str | None  # own reason, or inherited from a parent
    href: str | None
    start: int  # index in `visible` where this element's text starts


class _TextCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)  # &eacute; -> é
        self.visible: list[str] = []
        self.original: list[str] = []
        self.removed: list[RemovedItem] = []
        self.stack: list[_Open] = []
        self.open_counts: dict[str, int] = {}
        self.hidden_selectors: dict[str, str] = {}  # ".h" / "#x" -> reason, from <style>

    def _current_hidden(self) -> str | None:
        return self.stack[-1].hidden_by if self.stack else None

    def _own_reason(self, tag: str, attrs: dict) -> str | None:
        if tag in SKIPPED_TAGS:
            return "html_non_text"
        if "hidden" in attrs:
            return "html_hidden"
        if tag == "font" and attrs.get("color") and (reason := _color_reason(attrs["color"])):
            return reason
        if reason := _style_reason(_declarations(attrs.get("style") or "")):
            return reason
        for name in (attrs.get("class") or "").split():
            if reason := self.hidden_selectors.get("." + name):
                return reason
        return self.hidden_selectors.get("#" + (attrs.get("id") or ""))

    def _newline(self):
        self.visible.append("\n")
        self.original.append("\n")

    def handle_starttag(self, tag, attrs):
        attrs = {name: value or "" for name, value in attrs}
        if tag in BLOCK_TAGS:
            self._newline()
        if tag in VOID_TAGS:
            return
        hidden_by = self._own_reason(tag, attrs) or self._current_hidden()
        href = attrs.get("href") if tag == "a" else None
        self.stack.append(_Open(tag, hidden_by, href, len(self.visible)))
        self.open_counts[tag] = self.open_counts.get(tag, 0) + 1

    def handle_startendtag(self, tag, attrs):
        # Browsers ignore the "/" in <span/>: the element stays open, so treat it as a start tag.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.open_counts.get(tag, 0):
            # Close up to the matching open tag (tolerates broken HTML).
            while True:
                closed = self.stack.pop()
                self.open_counts[closed.tag] -= 1
                if closed.tag == tag:
                    break
            if closed.href and not closed.hidden_by and len(self.visible) - closed.start <= MAX_LINK_PARTS:
                href = closed.href.strip()
                if href not in "".join(self.visible[closed.start:]):
                    # Show where a link really points when its text claims otherwise.
                    self.visible.append(f" ({href})")
        if tag in BLOCK_TAGS:
            self._newline()

    def handle_data(self, data):
        if self.stack and self.stack[-1].tag == "style":
            self._learn_css(data)
        self.original.append(data)
        reason = self._current_hidden()
        if reason is None:
            self.visible.append(data)
        elif data.strip() and reason != "html_non_text":
            # Script/style code is dropped silently; hidden *text* is reported.
            self.removed.append(RemovedItem(reason, data.strip(), "html"))

    def handle_comment(self, data):
        if data.strip():
            self.removed.append(RemovedItem("html_comment", data.strip(), "html"))

    def _learn_css(self, css: str):
        """Remember simple selectors (.class, #id, tag.class) whose rules hide text."""
        for selectors, body in CSS_RULE.findall(css):
            reason = _style_reason(_declarations(body))
            if not reason:
                continue
            for selector in selectors.split(","):
                if match := SIMPLE_SELECTOR.match(selector.strip()):
                    self.hidden_selectors[match.group(1) + match.group(2)] = reason


def html_to_text(html: str) -> tuple[str, str, list[RemovedItem]]:
    """Return (original text, visible text, removed items)."""
    collector = _TextCollector()
    collector.feed(html)
    collector.close()

    def tidy(parts: list[str]) -> str:
        lines = (line.strip() for line in "".join(parts).splitlines())
        return "\n".join(line for line in lines if line)

    return tidy(collector.original), tidy(collector.visible), collector.removed
