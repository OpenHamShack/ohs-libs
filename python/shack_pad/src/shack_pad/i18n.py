"""The library's own words, through stdlib gettext (I18N.md §2: Python
outside Qt UI uses gettext, one domain per package; this one is
`shack-pad`).

Only what the library itself writes goes through here: its few key faces
(ON AIR, ARM, PAGE), the notes on a map that fell back, and the reasons a
remap is refused. Faces an app sends are already in the operator's language,
and action labels are catalog keys the app translates. The beta ships
English only, so with no catalog installed gettext hands back the English;
a translation is a `locale/<lang>/LC_MESSAGES/shack-pad.mo` beside this file,
made from the template `locale/shack-pad.pot` that tools/extract_pot.py writes.
"""

from __future__ import annotations

import gettext
from pathlib import Path

DOMAIN = "shack-pad"
LOCALE = Path(__file__).resolve().parent / "locale"

_t = gettext.translation(DOMAIN, LOCALE, fallback=True)


def use(languages: list[str] | None) -> None:
    """Switch the library's language (the app's own setting wins over the
    system's, I18N.md §2); None goes back to the system's."""
    global _t
    _t = gettext.translation(DOMAIN, LOCALE, languages=languages, fallback=True)


def _(message: str) -> str:
    return _t.gettext(message)
