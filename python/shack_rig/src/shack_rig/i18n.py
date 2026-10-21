"""The library's own words, through stdlib gettext (I18N.md §2: Python
outside Qt UI uses gettext, one domain per package; this one is
`shack-rig`).

What goes through here is what an operator reads: why keying was refused
(PttRefused's reason), why a serial port or sound card could not be used,
and the state payload's error text. CI-V decoding errors do not: a
malformed frame is a bug report for a developer, in English like a log
line (I18N.md §1 rule 8). The beta ships English only, so with no catalog
installed gettext hands back the English; a translation is a
`locale/<lang>/LC_MESSAGES/shack-rig.mo` beside this file, and the
template, `locale/shack-rig.pot`, is written by tools/extract_pot.py.
"""

from __future__ import annotations

import gettext
from pathlib import Path

DOMAIN = "shack-rig"
LOCALE = Path(__file__).resolve().parent / "locale"

_t = gettext.translation(DOMAIN, LOCALE, fallback=True)


def use(languages: list[str] | None) -> None:
    """Switch the library's language (the app's own setting wins over the
    system's, I18N.md §2); None goes back to the system's."""
    global _t
    _t = gettext.translation(DOMAIN, LOCALE, languages=languages, fallback=True)


def _(message: str) -> str:
    return _t.gettext(message)
