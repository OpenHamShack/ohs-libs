"""Decimal, group and minus symbols per locale, for the pure-Python helpers.

Why a table rather than the stdlib ``locale`` module: ``locale.setlocale`` is
process-global (one request in German would switch every thread of a server
to German) and only knows the locales the host has generated, which on a
container image is usually just C and C.UTF-8 (I18N.md §4).

Where the values came from: CLDR's ``common/main/<locale>.xml``,
``numbers/symbols[numberSystem=latn]`` (``decimal``, ``group``,
``minusSign``). They were written from CLDR and then checked on 09/10/2026
against Node 20's ICU (CLDR 46) with
``new Intl.NumberFormat(tag, {useGrouping: "always"}).formatToParts(-12345.6)``
for every row below; all agreed. To add a locale, run that line for it and
copy the three symbols in, escaping anything that is not printable ASCII so
a reviewer can see which space is which.

Grouping size is always three digits here. Locales that group differently
(en-IN's lakh, 12,34,567) are not in the table and fall back to their
language, which is acceptable because §4 only groups counts over 9999 and
nobody counts a lakh of contacts.
"""

from __future__ import annotations

from typing import NamedTuple


class Symbols(NamedTuple):
    decimal: str
    group: str
    minus: str


HYPHEN = "-"
MINUS = "−"  # what CLDR uses for the Nordic languages
NBSP = " "
NNBSP = " "  # narrow no-break space: French grouping since CLDR 34

TABLE: dict[str, Symbols] = {
    "en": Symbols(".", ",", HYPHEN),
    "en-GB": Symbols(".", ",", HYPHEN),
    "en-US": Symbols(".", ",", HYPHEN),
    "de": Symbols(",", ".", HYPHEN),
    "de-AT": Symbols(",", NBSP, HYPHEN),
    "de-CH": Symbols(".", "’", HYPHEN),
    "fr": Symbols(",", NNBSP, HYPHEN),
    "fr-CA": Symbols(",", NBSP, HYPHEN),
    "es": Symbols(",", ".", HYPHEN),
    "es-MX": Symbols(".", ",", HYPHEN),
    "it": Symbols(",", ".", HYPHEN),
    "nl": Symbols(",", ".", HYPHEN),
    "nb": Symbols(",", NBSP, MINUS),
    "nn": Symbols(",", NBSP, MINUS),
    "no": Symbols(",", NBSP, MINUS),
    "sv": Symbols(",", NBSP, MINUS),
    "da": Symbols(",", ".", HYPHEN),
    "fi": Symbols(",", NBSP, MINUS),
    "pl": Symbols(",", NBSP, HYPHEN),
    # CLDR's plain "pt" is Brazilian Portuguese; Portugal is the variant.
    "pt": Symbols(",", ".", HYPHEN),
    "pt-BR": Symbols(",", ".", HYPHEN),
    "pt-PT": Symbols(",", NBSP, HYPHEN),
    "ja": Symbols(".", ",", HYPHEN),
    "zh": Symbols(".", ",", HYPHEN),
    "ru": Symbols(",", NBSP, HYPHEN),
    "uk": Symbols(",", NBSP, HYPHEN),
    "cs": Symbols(",", NBSP, HYPHEN),
}


def symbols(locale: str | None) -> Symbols:
    """Symbols for a BCP 47 or POSIX tag: exact tag, then language, then en.

    POSIX spellings arrive from environment variables (``de_DE.UTF-8``), so
    the encoding and ``@modifier`` are dropped and ``_`` read as ``-``. A
    region we do not list (``de-LU``) takes its language's symbols, which is
    right far more often than taking English's.
    """
    if not locale:
        return TABLE["en"]
    tag = locale.split(".", 1)[0].split("@", 1)[0].replace("_", "-")
    parts = [p for p in tag.split("-") if p]
    if not parts:
        return TABLE["en"]
    lang = parts[0].lower()
    region = next((p.upper() for p in parts[1:] if len(p) == 2 and p.isalpha()), None)
    if region and f"{lang}-{region}" in TABLE:
        return TABLE[f"{lang}-{region}"]
    return TABLE.get(lang, TABLE["en"])
