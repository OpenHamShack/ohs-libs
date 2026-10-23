"""shack_sdr's own words, through stdlib gettext (I18N.md §2: Python
outside Qt UI uses gettext, one domain per package; this one is
`shack-sdr`).

Rust carries no text (RADIO-SDR-PLAN §4.1): every refusal and state is a
code the app words. What is here is data a listener reads that the
standard defines only as a number: RDS's programme types (wfm.py
`pty_name`). The beta ships English only, so with no catalog installed
gettext hands back the English; a translation is a
`locale/<lang>/LC_MESSAGES/shack-sdr.mo` beside this file, and the
template, `locale/shack-sdr.pot`, is written by tools/extract_pot.py.
"""

from __future__ import annotations

import gettext
from pathlib import Path

DOMAIN = "shack-sdr"
LOCALE = Path(__file__).resolve().parent / "locale"

_t = gettext.translation(DOMAIN, LOCALE, fallback=True)


def use(languages: list[str] | None) -> None:
    """Switch the library's language (the app's own setting wins over the
    system's, I18N.md §2); None goes back to the system's."""
    global _t
    _t = gettext.translation(DOMAIN, LOCALE, languages=languages, fallback=True)


def _(message: str) -> str:
    return _t.gettext(message)
