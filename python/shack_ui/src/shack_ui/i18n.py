"""The library's own words, through stdlib gettext (I18N.md §2: Python
outside Qt UI uses gettext, one domain per package; this one is
`shack-ui`).

What goes through here is what a client may show: the error sentences of
the routes shack_ui answers itself (Holds). Everything a screen says is
the app's, from its own catalog (SCREENS.md §8.5). Log lines stay in
English (I18N.md §1 rule 8). The beta ships English only, so
with no catalog installed gettext hands back the English; a translation is a
`locale/<lang>/LC_MESSAGES/shack-ui.mo` beside this file, and the
template, `locale/shack-ui.pot`, is written by tools/extract_pot.py.
"""

from __future__ import annotations

import gettext
from pathlib import Path

DOMAIN = "shack-ui"
LOCALE = Path(__file__).resolve().parent / "locale"

_t = gettext.translation(DOMAIN, LOCALE, fallback=True)


def use(languages: list[str] | None) -> None:
    """Switch the library's language (the app's own setting wins over the
    system's, I18N.md §2); None goes back to the system's."""
    global _t
    _t = gettext.translation(DOMAIN, LOCALE, languages=languages, fallback=True)


def _(message: str) -> str:
    return _t.gettext(message)
