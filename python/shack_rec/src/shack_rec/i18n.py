"""The library's own words, through stdlib gettext (I18N.md §2: Python
outside Qt UI uses gettext, one domain per package; this one is
`shack-rec`).

What goes through here is what an operator reads: why a recording would
not start or why it stopped (Refused's text, an entry's `stopped`). Log
lines stay in English (I18N.md §1 rule 8). The beta ships English only, so
with no catalog installed gettext hands back the English; a translation is a
`locale/<lang>/LC_MESSAGES/shack-rec.mo` beside this file, and the
template, `locale/shack-rec.pot`, is written by tools/extract_pot.py.
"""

from __future__ import annotations

import gettext
from pathlib import Path

DOMAIN = "shack-rec"
LOCALE = Path(__file__).resolve().parent / "locale"

_t = gettext.translation(DOMAIN, LOCALE, fallback=True)


def use(languages: list[str] | None) -> None:
    """Switch the library's language (the app's own setting wins over the
    system's, I18N.md §2); None goes back to the system's."""
    global _t
    _t = gettext.translation(DOMAIN, LOCALE, languages=languages, fallback=True)


def _(message: str) -> str:
    return _t.gettext(message)
