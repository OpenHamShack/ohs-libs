"""The library's own sentences go through gettext, domain "shack-desktop"
(I18N.md §2: Python outside Qt UI uses stdlib gettext, one domain per
package, and libraries translate their own sentences). The beta ships
English only, so with no catalog installed `_` returns its argument;
placeholders are named and filled after translation, so a translator can
move them."""
from __future__ import annotations

import gettext
from pathlib import Path

DOMAIN = "shack-desktop"
_t = gettext.translation(DOMAIN, localedir=str(Path(__file__).resolve().parent / "locale"), fallback=True)


def _(message: str) -> str:
    return _t.gettext(message)
