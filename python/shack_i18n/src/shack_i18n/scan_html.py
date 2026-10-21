"""HTML via ``html.parser`` (I18N.md §3.1, HTML row).

A text node with letters is a finding unless an element around it carries
``data-i18n`` (the loader replaces the whole content, so the English inside
is only a fallback the catalog also holds). The listed attributes are
findings unless that element's ``data-i18n-attr`` names them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from ._core import CatalogUse, Finding, Literal, ScanResult, is_user_text, quote

TEXT_ATTRIBUTES = ("title", "placeholder", "alt", "aria-label")
SKIP_ELEMENTS = frozenset({"script", "style", "code", "pre"})
VOID_ELEMENTS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
# value= is visible only on buttons (§3.1 "value (buttons)"): an <option>'s
# or a text input's value is data.
_BUTTON_INPUT_TYPES = frozenset({"button", "submit", "reset"})

# Server-side template syntax (Jinja, which the Dock360 apps render pages
# with) is not text a person reads: "{% if tx %}" would otherwise be a finding
# for its letters. Expressions and statements are blanked before the letter
# test, and count as placeholders.
_TEMPLATE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.DOTALL)


def _covered_attrs(spec: str | None) -> set[str]:
    """Attribute names a ``data-i18n-attr="title:key;placeholder:key2"`` covers."""
    if not spec:
        return set()
    return {item.split(":", 1)[0].strip() for item in spec.split(";") if ":" in item}


def _attr_keys(spec: str | None) -> list[str]:
    if not spec:
        return []
    return [item.split(":", 1)[1].strip() for item in spec.split(";") if ":" in item and item.split(":", 1)[1].strip()]


@dataclass
class _Frame:
    tag: str
    translated: bool  # data-i18n here or above
    skip: bool  # script/style/code/pre/translate="no" here or above


@dataclass
class _Run:
    """A piece of text a person would read, before the letter test."""

    text: str
    line: int
    col: int  # 0-based, as html.parser reports
    where: str  # for the message
    fix: str


class _Parser(HTMLParser):
    def __init__(self, extra_attributes: tuple[str, ...] = ()):
        super().__init__(convert_charrefs=True)
        self.stack: list[_Frame] = []
        self.runs: list[_Run] = []
        self.keys: list[tuple[str, int, int]] = []
        self.attributes = TEXT_ATTRIBUTES + tuple(a for a in extra_attributes if a not in TEXT_ATTRIBUTES)

    def _top(self) -> _Frame:
        return self.stack[-1] if self.stack else _Frame("", False, False)

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, push=tag not in VOID_ELEMENTS)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, push=False)

    def _start(self, tag, attrs, push):
        a = {k: (v if v is not None else "") for k, v in attrs}
        top = self._top()
        line, col = self.getpos()
        skip = top.skip or tag in SKIP_ELEMENTS or a.get("translate", "").lower() == "no"
        if "data-i18n" in a and a["data-i18n"]:
            self.keys.append((a["data-i18n"], line, col))
        for key in _attr_keys(a.get("data-i18n-attr")):
            self.keys.append((key, line, col))
        if not skip:
            covered = _covered_attrs(a.get("data-i18n-attr"))
            names = list(self.attributes)
            if tag == "button" or (tag == "input" and a.get("type", "").lower() in _BUTTON_INPUT_TYPES):
                names.append("value")
            for name in names:
                if name in a and name not in covered:
                    self.runs.append(
                        _Run(a[name], line, col, f'in the {name} attribute of <{tag}>', f'data-i18n-attr="{name}:key"')
                    )
        if push:
            self.stack.append(_Frame(tag, top.translated or "data-i18n" in a, skip))

    def handle_endtag(self, tag):
        # Unclosed <p>/<li> are common; pop back to the matching element and
        # ignore an end tag that matches nothing open.
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        top = self._top()
        if top.skip or top.translated or not data.strip():
            return
        line, col = self.getpos()
        lead = data[: len(data) - len(data.lstrip())]
        line += lead.count("\n")
        col = len(lead) - lead.rfind("\n") - 1 if "\n" in lead else col + len(lead)
        where = f"in <{top.tag}>" if top.tag else "in the page"
        self.runs.append(_Run(data.strip(), line, col, where, 'data-i18n="key"'))


def _literal(text: str, line: int, col: int) -> Literal:
    parts = tuple(_TEMPLATE.sub("\x00", text).split("\x00"))
    return Literal(text, parts, line, col)


def text_runs(markup: str) -> list[tuple[str, str, str]]:
    """(text, where, fix) for every readable run in a fragment of HTML.

    Used by the JavaScript scanner for ``.innerHTML``: tags in the literal
    are markup, and only what lies between them (and the text attributes)
    is shown.
    """
    p = _Parser()
    p.feed(markup)
    p.close()
    return [(r.text, r.where, r.fix) for r in p.runs]


def scan(path: str, source: str, extra_attributes: tuple[str, ...] = ()) -> ScanResult:
    p = _Parser(extra_attributes)
    p.feed(source)
    p.close()
    res = ScanResult()
    for r in p.runs:
        lit = _literal(r.text, r.line, r.col + 1)
        if is_user_text(lit):
            res.findings.append(
                Finding(path, r.line, r.col + 1, f"user-visible text {quote(r.text)} {r.where}; add {r.fix}", literal=r.text)
            )
    for key, line, col in p.keys:
        res.uses.append(CatalogUse("web", key, path, line, col + 1))
    return res
