"""The library's own words, through stdlib gettext (I18N.md §2: Python
outside Qt UI uses gettext, one domain per package; this one is
`shack-packet`).

What goes through here is what a person at the other end of a connection
reads: the mailbox's greeting, prompts and replies, sent over the air to
whoever connected (mailbox.py). Everything else the engine says to the app
is a key the app translates (datalink.REASONS, identity.REFUSALS,
aprs_station.EVENTS, ip.REFUSALS), as aprs.py's PROBLEMS are. Log lines
stay in English (I18N.md §1 rule 8). The beta ships English only, so
with no catalog installed gettext hands back the English; a translation is a
`locale/<lang>/LC_MESSAGES/shack-packet.mo` beside this file, and the
template, `locale/shack-packet.pot`, is written by tools/extract_pot.py.
"""

from __future__ import annotations

import gettext
from pathlib import Path

DOMAIN = "shack-packet"
LOCALE = Path(__file__).resolve().parent / "locale"

_t = gettext.translation(DOMAIN, LOCALE, fallback=True)


def use(languages: list[str] | None) -> None:
    """Switch the library's language (the app's own setting wins over the
    system's, I18N.md §2); None goes back to the system's."""
    global _t
    _t = gettext.translation(DOMAIN, LOCALE, languages=languages, fallback=True)


def _(message: str) -> str:
    return _t.gettext(message)
