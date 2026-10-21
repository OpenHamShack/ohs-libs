"""Who this station is, and whether it may transmit at all.

Two separate things, kept separate because they fail differently:

**The callsign.** Every frame the engine sends carries it as the source
address, which is how an AX.25 station identifies itself (a UK licence
requires the callsign in every transmission; AX.25 does that for free, as
long as the callsign is real). So the engine will not build a frame from a
`Station` until it has a callsign that looks like one an administration
issues. `ax25.Address.from_text` accepts anything AX.25 can carry - APRS
destinations like APDW17, aliases like WIDE2-2 - which is right for reading
and wrong for transmitting: a station that identifies as WIDE1-1 is not
identifying.

The test is the ITU shape rather than a list of prefixes, because the
prefix list changes and the shape does not: one to three characters of
prefix, a digit, then up to three more ending in a letter, six at most.
That accepts G0ABC, GM4BRI, 2E0TST, M0A, 3DA0XX, K1A and VK2ABC, and refuses
APRS, WIDE1, NOCALL and anything ending in a digit. N0CALL has the shape and
is refused by name: it is what direwolf and half the TNCs in the world ship
in their configuration, and a station sending it has not been set up.

**The transmit-enable flag.** Separate from the callsign because it changes
often and the callsign does not: the app arms transmit when the operator
says so and disarms it when the arming expires or the radio link goes
(that layer sits on top of this one). The engine reads the flag at the
moment of keying, not when a frame is queued, so a frame queued while armed
and sent after disarming is refused - a TNC that transmits a minute after
the operator said stop is the failure this exists to prevent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .ax25 import Address
from .errors import PacketError

#: The ITU callsign shape (see the module docstring). Prefix, a digit, and a
#: suffix ending in a letter; the six-character limit is AX.25's.
_CALLSIGN = re.compile(r"^[A-Z0-9]{1,3}[0-9][A-Z0-9]{0,3}[A-Z]$")

#: Placeholders that have the shape and are never a real station.
_PLACEHOLDERS = {"N0CALL", "NOCALL", "MYCALL"}

#: Why a transmission was refused, as keys the app translates (as aprs.py's
#: PROBLEMS): the engine reports these in its events and counters.
REFUSALS = {
    "no-callsign": "the station has no valid callsign, so nothing may be sent",
    "transmit-disabled": "transmit is not enabled (the app has not armed it, or the arming expired)",
    "no-radio": "there is no radio link to send on",
}


class IdentityError(PacketError):
    """A callsign that cannot identify a station: for the developer (the app
    checks what the operator typed with `is_callsign` and says so in its own
    words first)."""


def is_callsign(text: str) -> bool:
    """True if `text` (no SSID) has the shape of an issued callsign."""
    call = text.strip().upper()
    return bool(_CALLSIGN.match(call)) and len(call) <= 6 and call not in _PLACEHOLDERS


def station_address(text: str) -> Address:
    """`GM4BRI` or `GM4BRI-7` as an Address, refusing anything that does not
    identify a station (see the module docstring)."""
    call, _, ssid = text.strip().upper().partition("-")
    if not is_callsign(call):
        raise IdentityError(f"{text!r} is not a callsign this station can identify with")
    if ssid and (not ssid.isdigit() or not 0 <= int(ssid) <= 15):
        raise IdentityError(f"{text!r}: the SSID is 0 to 15")
    return Address(call, int(ssid or 0))


@dataclass
class Station:
    """The station's identity and its transmit switch.

    `callsign` is None until the operator has set one; `tx_enabled` is False
    until the app arms it. Both are read by `refusal()` at the moment of
    keying.
    """
    callsign: Address | None = None
    tx_enabled: bool = False

    @classmethod
    def of(cls, callsign: str | None, tx_enabled: bool = False) -> Station:
        return cls(station_address(callsign) if callsign else None, tx_enabled)

    def set_callsign(self, text: str | None) -> None:
        self.callsign = station_address(text) if text else None

    def address(self, ssid: int | None = None) -> Address:
        """The station's address, with another SSID if asked (a mailbox on
        -1, a node on -7). Raises if there is no callsign."""
        if self.callsign is None:
            raise IdentityError("the station has no callsign")
        if ssid is None:
            return self.callsign
        if not 0 <= ssid <= 15:
            raise IdentityError(f"SSID {ssid} is not 0 to 15")
        return Address(self.callsign.call, ssid)

    def owns(self, address: Address) -> bool:
        """Whether a frame's address is this station's, at any SSID."""
        return self.callsign is not None and address.call == self.callsign.call

    def refusal(self) -> str | None:
        """None if a transmission may start now, else the REFUSALS key."""
        if self.callsign is None or not is_callsign(self.callsign.call):
            return "no-callsign"
        if not self.tx_enabled:
            return "transmit-disabled"
        return None
