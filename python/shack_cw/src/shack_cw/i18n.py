"""The library's own words, through stdlib gettext (I18N.md §2: Python
outside Qt UI uses gettext, one domain per package; this one is
`shack-cw`).

What goes through here is what an operator or developer embedding the
decoder reads back when they ask for something the library cannot give
them: an unknown engine name, weights or a language model built for a
different alphabet or front end, a corrupt recording, or a callsign list
that failed to download. Nothing here is heard over the air, and nothing
here is a log line (I18N.md §1 rule 8) - each is a ValueError raised
across the library's own API, so the caller's UI is expected to show it.
The beta ships English only, so with no catalog installed gettext hands
back the English; a translation is a `locale/<lang>/LC_MESSAGES/shack-cw.mo`
beside this file, and the template, `locale/shack-cw.pot`, is written by
tools/extract_pot.py.
"""

from __future__ import annotations

import gettext
from pathlib import Path

DOMAIN = "shack-cw"
LOCALE = Path(__file__).resolve().parent / "locale"

_t = gettext.translation(DOMAIN, LOCALE, fallback=True)


def use(languages: list[str] | None) -> None:
    """Switch the library's language (the app's own setting wins over the
    system's, I18N.md §2); None goes back to the system's."""
    global _t
    _t = gettext.translation(DOMAIN, LOCALE, languages=languages, fallback=True)


def _(message: str) -> str:
    return _t.gettext(message)
