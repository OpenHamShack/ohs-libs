"""Locale formatting for frequencies and numbers (I18N.md §4).

Every port (Kotlin ``ShackFormat``, Swift ``ShackFormat``, JS ``fmt``) must give
the same text for the same input; ``spec/vectors/i18n/format.json`` is the
contract between them, and this module is only one implementation of it.

Decisions that the vectors pin, so a port cannot quietly differ:

- Arithmetic is decimal, not binary floating point, and ties round half away
  from zero (14074.05 kHz at one decimal is 14074.1). A float argument is
  read through its shortest ``repr`` first, so ``number(2.675, decimals=2)``
  is ``2.68`` as a person expects, not the ``2.67`` that ``2.675``'s binary
  value would give. JS ``toFixed`` does the latter; the JS port must not use
  it.
- Frequencies are never grouped: "10,489.750" and "10.489,750" each read as
  the other in half the world (§4.1).
- The unit follows a no-break space so a line break can never separate a
  number from its unit.
- Unit symbols are the SI ones in every locale for now. Some scripts write
  them differently (Russian ``МГц`` is a guess in §4.1); that comes from the
  shared catalog when such a language is added, not from this table.
"""

from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal

from ._cldr import NBSP, symbols

__all__ = [
    "FormatError",
    "frequency",
    "parse_frequency",
    "offset",
    "number",
    "swr",
    "UNITS",
]

#: Hz per unit. The maximum decimals for a unit is its Hz resolution (kHz 3,
#: MHz 6, GHz 9): anything finer would show digits the integer input cannot
#: have.
UNITS: dict[str, int] = {"Hz": 1, "kHz": 1_000, "MHz": 1_000_000, "GHz": 1_000_000_000}
_MAX_DECIMALS = {"Hz": 0, "kHz": 3, "MHz": 6, "GHz": 9}
# §4.1: "MHz 3 by default (kHz resolution) ... kHz 1 by default". GHz is only
# used for a 10 GHz display and keeps kHz resolution like MHz does.
_DEFAULT_DECIMALS = {"Hz": 0, "kHz": 1, "MHz": 3, "GHz": 6}
_AUTO_MHZ_FROM = 30_000_000  # §4.1: kHz below 30 MHz, MHz from 30 MHz up

# §4.1 fixes U+2212 for signed shifts in every locale, unlike number(), which
# takes the locale's own minus sign from CLDR.
_SHIFT_MINUS = "−"


class FormatError(ValueError):
    """A value the helpers refuse, with a machine code for callers and ports.

    ``str(err)`` is one whole English sentence that is safe to show. A UI
    that has a catalog shows its own translation of ``code`` (with
    ``params``) instead, the same arrangement as a server's ``error`` and
    ``code`` (CONTROLLER-CONTRACT.md §9). The vectors compare ``code``, never
    the sentence, so a port may word its English differently.
    """

    def __init__(self, code: str, message: str, **params: object) -> None:
        super().__init__(message)
        self.code = code
        self.params = params


def _decimal(x: object) -> Decimal:
    if isinstance(x, bool) or not isinstance(x, (int, float, Decimal)):
        raise FormatError("not-a-number", f"{x!r} is not a number.", value=repr(x))
    if isinstance(x, float):
        if not math.isfinite(x):
            raise FormatError("not-finite", f"{x!r} is not a finite number.", value=repr(x))
        return Decimal(repr(x))
    return Decimal(x)


def _check_unit(unit: str, allowed: dict[str, int]) -> None:
    if unit not in allowed:
        names = ", ".join(allowed)
        raise FormatError("bad-unit", f'"{unit}" is not a unit these helpers know; use one of {names}.', unit=unit)


def _check_decimals(decimals: int, unit: str) -> None:
    top = _MAX_DECIMALS[unit]
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= top:
        raise FormatError(
            "bad-decimals",
            f"A value in {unit} can show between 0 and {top} decimal places, not {decimals!r}.",
            unit=unit,
            decimals=decimals,
        )


def _digits(value: Decimal, decimals: int, decimal_sep: str, group_sep: str | None = None) -> str:
    """Absolute value to fixed decimals, half away from zero, optional grouping."""
    q = abs(value).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    whole, _, frac = f"{q:f}".partition(".")
    if group_sep is not None and len(whole) > 4 and int(whole) > 9999:
        head = len(whole) % 3 or 3
        whole = whole[:head] + "".join(group_sep + whole[i : i + 3] for i in range(head, len(whole), 3))
    return whole + (decimal_sep + frac if decimals else "")


def frequency(hz: int | float | Decimal, locale: str = "en", unit: str = "auto", decimals: int | None = None) -> str:
    """A frequency for display: ``frequency(14074000)`` is ``'14074.0\\u00a0kHz'``.

    ``unit="auto"`` chooses kHz below 30 MHz and MHz from 30 MHz up; an app
    that wants one unit for a field (QO-100 downlinks in MHz) passes it.
    ``decimals=None`` takes the unit's default, and trailing zeros are kept
    so a dial does not change width as it tunes.
    """
    value = _decimal(hz)
    if value < 0:
        raise FormatError(
            "negative-frequency",
            "A frequency cannot be negative; use offset() for a shift.",
            hz=str(value),
        )
    if unit == "auto":
        unit = "MHz" if value >= _AUTO_MHZ_FROM else "kHz"
    _check_unit(unit, UNITS)
    if decimals is None:
        decimals = _DEFAULT_DECIMALS[unit]
    _check_decimals(decimals, unit)
    sym = symbols(locale)
    return _digits(value / UNITS[unit], decimals, sym.decimal) + NBSP + unit


def offset(hz: int | float | Decimal, unit: str = "Hz", locale: str = "en", decimals: int | None = None) -> str:
    """A signed shift or tone: ``'+1500\\u00a0Hz'``, ``'\\u2212600\\u00a0kHz'``.

    Zero carries no sign: "+0 Hz" suggests a direction that does not exist.
    ``decimals=None`` shows as many decimals as the value needs and no more,
    because shifts are round numbers (600 kHz, 7.6 MHz) and "−600.000 kHz"
    hides that.
    """
    value = _decimal(hz)
    _check_unit(unit, UNITS)
    scaled = value / UNITS[unit]
    if decimals is None:
        exponent = scaled.normalize().as_tuple().exponent
        decimals = min(max(0, -exponent) if isinstance(exponent, int) else 0, _MAX_DECIMALS[unit])
    _check_decimals(decimals, unit)
    text = _digits(scaled, decimals, symbols(locale).decimal)
    rounded_zero = text.strip("0.,") == ""
    sign = "" if rounded_zero else ("+" if value > 0 else _SHIFT_MINUS)
    return sign + text + NBSP + unit


def number(x: int | float | Decimal, locale: str = "en", decimals: int = 0) -> str:
    """A plain number (dB, W, MER, a count) in the locale's symbols.

    Grouping only for magnitudes over 9999 (§4.2: "12 345 contacts"), so
    four-digit values such as a year or 1500 W are never split. The minus
    sign is the locale's own from CLDR (U+2212 in the Nordic languages).
    """
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 12:
        raise FormatError("bad-decimals", f"A number can show between 0 and 12 decimal places, not {decimals!r}.", decimals=decimals)
    value = _decimal(x)
    sym = symbols(locale)
    text = _digits(value, decimals, sym.decimal, sym.group)
    negative = value < 0 and text.strip("0.,  ’") != ""
    return (sym.minus if negative else "") + text


def swr(x: int | float | Decimal, locale: str = "en") -> str:
    """A standing-wave ratio, one decimal: ``swr(1.5)`` is ``'1.5:1'``."""
    value = _decimal(x)
    if value < 1:
        raise FormatError("bad-swr", f"An SWR cannot be below 1:1, and {x!r} is.", value=str(value))
    return _digits(value, 1, symbols(locale).decimal) + ":1"


_UNIT_BY_LOWER = {u.lower(): u for u in UNITS}
_TYPED = re.compile(r"^(?P<num>[0-9.,]+)(?P<unit>[^\W\d_]*)$")
_UNITS_SENTENCE = "Hz, kHz, MHz or GHz"


def parse_frequency(text: str, locale: str = "en", unit: str | None = None) -> int:
    """What a person typed, as integer Hz: ``parse_frequency("10489,750", "de", "MHz")``.

    Either ``.`` or ``,`` is the decimal separator, whatever the locale: a
    German operator pasting "14.074" from a spot list means 14.074 MHz, and
    frequencies are never grouped, so there is no grouping symbol to confuse
    it with. ``locale`` is accepted for symmetry with the formatters and for
    the day localised unit symbols (``МГц``) are accepted here. Spaces and
    no-break spaces anywhere are ignored. A typed unit (any case) wins over
    the field's ``unit``; with neither, the text is refused rather than
    guessed. A value finer than 1 Hz is refused, not rounded, because the
    person evidently meant something precise and rounding would hide that.
    """
    if unit is not None and unit != "auto":
        _check_unit(unit, UNITS)
    field_unit = None if unit == "auto" else unit
    compact = "".join(ch for ch in text if not ch.isspace())
    if not compact:
        raise FormatError("empty", "The frequency is empty; type a number such as 14074 kHz.")
    m = _TYPED.match(compact)
    if not m or not any(ch.isdigit() for ch in m["num"]):
        raise FormatError(
            "not-a-number",
            f'"{text}" is not a frequency; type digits with at most one decimal separator and an optional unit ({_UNITS_SENTENCE}).',
            text=text,
        )
    num, typed = m["num"], m["unit"]
    if num.count(".") + num.count(",") > 1:
        raise FormatError(
            "two-separators",
            f'"{text}" has more than one decimal separator; type the frequency with a single "." or "," and no digit grouping.',
            text=text,
        )
    if typed:
        if typed.lower() not in _UNIT_BY_LOWER:
            raise FormatError("unknown-unit", f'"{typed}" is not a frequency unit; use {_UNITS_SENTENCE}.', unit=typed)
        chosen = _UNIT_BY_LOWER[typed.lower()]
    elif field_unit is not None:
        chosen = field_unit
    else:
        raise FormatError("no-unit", f'"{text}" has no unit; add {_UNITS_SENTENCE}.', text=text)
    whole, _, frac = num.replace(",", ".").partition(".")
    hz = Decimal(f"{whole or '0'}.{frac or '0'}") * UNITS[chosen]
    if hz != hz.to_integral_value():
        raise FormatError("too-precise", f'"{text}" is finer than 1 Hz; use fewer decimal places.', text=text)
    return int(hz)
