"""What every scanner shares: literals, findings, catalog uses, the text rules."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Literal:
    """One string literal (or template) found at a sink.

    ``parts`` are the literal pieces with placeholders removed: the letters
    rule looks only at what a translator would have to translate. ``value``
    is the literal as the allow-list and the technical-terms list match it:
    the plain string, or a template with its placeholders written as in the
    source (``"{n} stations"``), so an allow-list entry can be copied from
    the finding.
    """

    value: str
    parts: tuple[str, ...]
    line: int
    col: int  # 1-based, in characters


@dataclass
class Finding:
    path: str  # relative to the repo root, forward slashes
    line: int
    col: int
    message: str
    warning: bool = False
    # Set for literal findings only: what the allow-list and terms list match.
    literal: str | None = None

    def render(self) -> str:
        prefix = "warning: " if self.warning else ""
        return f"{self.path}:{self.line}:{self.col}: {prefix}{self.message}"


@dataclass(frozen=True)
class CatalogUse:
    """A key the code asks a catalog for, so §3.2 can check it exists.

    ``system`` names the mechanism (``qt``, ``gettext``, ``web``,
    ``android``, ``apple``) because each has its own English catalog; a Qt
    source text is not looked for in ``en.json``.
    """

    system: str
    key: str
    path: str
    line: int
    col: int
    plural: bool = False
    context: str | None = None


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    uses: list[CatalogUse] = field(default_factory=list)


def quote(value: str) -> str:
    """A literal as findings print it: JSON, readable non-ASCII."""
    return json.dumps(value, ensure_ascii=False)


def _is_letter(ch: str) -> bool:
    return unicodedata.category(ch).startswith("L")


def has_letter_run(text: str) -> bool:
    """Two Unicode letters in a row (§3.1). ``str.isalpha`` would do, but the
    category test is the rule as written and is what the ports implement."""
    prev = False
    for ch in text:
        cur = _is_letter(ch)
        if cur and prev:
            return True
        prev = cur
    return False


# §3.1's two never-a-finding patterns, verbatim: identifiers, paths and MIME
# types (all lower case), and printf format specs.
_IDENTIFIER_OR_PATH = re.compile(r"^[a-z0-9_.:/-]+$")
_FORMAT_SPEC = re.compile(r"^%[-+ 0#]*\d*(\.\d+)?[a-z]$")
# §3.1 names URLs, but its path pattern stops at "?", "=", "&", "#" and upper
# case, so "https://example.org/a?b=1" would be a finding. A URL is a scheme,
# "://" and no whitespace; that is all the prose meant.
_URL = re.compile(r"^[a-z][a-z0-9+.-]*://\S+$", re.IGNORECASE)


def is_user_text(lit: Literal) -> bool:
    """Whether a literal at a sink is something a person reads."""
    if not any(has_letter_run(p) for p in lit.parts):
        return False
    joined = "".join(lit.parts).strip()
    if _IDENTIFIER_OR_PATH.match(joined) or _FORMAT_SPEC.match(joined) or _URL.match(joined):
        return False
    return True
