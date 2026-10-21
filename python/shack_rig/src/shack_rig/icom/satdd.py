"""An IC-9700 set up for a satellite pass or for 23 cm DD, and put back:
two more lists of `Setting`s for shack_rig.datasetup, so both get what the
packet data set-up already has - every value read before it is changed,
journalled on disk first, every write read back, restored last-first, and
put back by `recover()` after a crash (datasetup.py's rules).

    from shack_rig.datasetup import DataSetup, Journal
    setup = DataSetup(dd_settings(rig, call, frequency=1_299_525_000), Journal(path))
    setup.recover(); setup.apply(); ...; setup.restore()

`call(fn, *args)` runs `fn` on the rig's loop, as for icom/datasetup.py.

**Satellites** (Packet Radio 0.3.0, Doppler correction). Two ways, by what
the pass needs:

- `split`: uplink and downlink on one band - the ISS's APRS digipeater is
  145.825 MHz both ways. The 9700's satellite mode wants two bands, so this
  is split: receive on the selected VFO, transmit on the other (25 01),
  each corrected for Doppler separately.
- `satellite`: a cross-band bird. Satellite mode on (16 5A 01); MAIN is
  the uplink and SUB the downlink (Basic Manual), which with MAIN selected
  are 25 00 and 25 01.

Which audio a packet engine hears in satellite mode (MAIN's or SUB's over
the LAN) is not in the CI-V guide, so `satellite` is offered as untested;
`split` keeps receive on the VFO whose audio the link already plays.

**DD** (the 23 cm DD transport): the frequency, mode DD (06 22), RPS off
(0F 10) and, when asked, RF POWER. DD's transmitter is gated by the
front-panel TRANSMIT key's TX INHIBIT, which CI-V cannot set (1A 05 0087
is only its power-on default): the operator presses it, and the app says
so. The mode is read as [mode, data, filter] so the radio goes back
exactly to FM-D FIL1 or whatever it was in; DD itself has neither a data
flag nor a filter, so it reads as [0x22, 0, 0].

Nothing here keys: a DD radio transmits Ethernet frames that reach it,
and a satellite pass's transmissions are the packet link's own.
"""

from __future__ import annotations

from typing import Any, Callable

from ..datasetup import UNAVAILABLE, Setting
from . import civ
from .datasetup import ASK_S, NG_CONFIRM_S

SPLIT = "split"
SATELLITE = "satellite"
HOWS = (SPLIT, SATELLITE)

DD_MODE = civ.MODE_CODES["DD"]


def _mhz(hz) -> str:
    return f"{hz / 1e6:.6f} MHz"


def _asker(rig, call):
    def ask(command: bytes, key: bytes, match=None):
        got = call(lambda: rig.ask(command, key, ASK_S, match=match, refused=True), timeout=ASK_S + 2)
        if got != bytes([civ.NG]):
            return got
        got = call(lambda: rig.ask(command, key, NG_CONFIRM_S, match=match), timeout=NG_CONFIRM_S + 2)
        return UNAVAILABLE if got is None else got

    def acked(command: bytes) -> bool:
        """Sent and answered OK: a write the radio refused (NG) is False
        at once rather than after the read-back."""
        got = call(lambda: rig.ask(command, bytes([civ.OK]), ASK_S, refused=True), timeout=ASK_S + 2)
        return got == bytes([civ.OK])

    return ask, acked


def _vfo_setting(key: str, rig, call, unselected: bool, hz: int) -> Setting:
    ask, acked = _asker(rig, call)

    def read():
        got = ask(civ.read_vfo_freq(unselected), bytes([civ.VFO_FREQ, 0x01 if unselected else 0x00]))
        return got if got in (None, UNAVAILABLE) else civ.decode_vfo_freq(got)[1]

    # app_moves: retune() steps it for Doppler all through the pass, and the
    # end of the pass puts it back whatever it then reads (datasetup rule 6).
    return Setting(key, int(hz), read, lambda v: acked(civ.set_vfo_freq(int(v), unselected)), show=_mhz,
                   app_moves=True)


def satellite_settings(rig, call: Callable[..., Any], how: str, *, rx_hz: int, tx_hz: int) -> list[Setting]:
    """A pass's set-up, in order: split or satellite mode on, then the
    VFOs at the first corrected frequencies. In `split` the receive VFO is
    the packet link's channel (icom/datasetup.py set it); it is saved here
    too, so the end of the pass puts it back on the channel rather than
    where the last Doppler step left it."""
    if how not in HOWS:
        raise ValueError(f"no such satellite set-up: {how!r}")
    ask, acked = _asker(rig, call)
    out: list[Setting] = []
    if how == SPLIT:
        def read_split():
            got = ask(civ.read_split(), b"\x0f")
            return got if got in (None, UNAVAILABLE) else civ.decode_duplex(got)

        def want_split(before):
            # Split from simplex only: a repeater shift is the data set-up's
            # to clear (it runs first), and anything else is left alone.
            return 0x01 if before in (0x00, 0x01) else None

        def write_split(code) -> bool:
            return code in (0x00, 0x01) and acked(civ.set_split(code == 0x01))

        out.append(Setting("split", want_split, read_split, write_split,
                           show=lambda c: civ.DUPLEX_READS.get(c, f"0x{c:02x}")))
        out.append(_vfo_setting("rx_vfo", rig, call, False, rx_hz))
        out.append(_vfo_setting("tx_vfo", rig, call, True, tx_hz))
    else:
        def read_sat():
            got = ask(civ.read_satellite_mode(), bytes(civ.SATELLITE_MODE))
            return got if got in (None, UNAVAILABLE) else civ.decode_satellite_mode(got)

        out.append(Setting("satellite", True, read_sat, lambda on: acked(civ.set_satellite_mode(bool(on))),
                           show=lambda on: "on" if on else "off"))
        out.append(_vfo_setting("uplink_vfo", rig, call, False, tx_hz))
        out.append(_vfo_setting("downlink_vfo", rig, call, True, rx_hz))
    return out


def retune(rig, how: str, *, rx_hz: int | None = None, tx_hz: int | None = None) -> bool:
    """On the rig's loop: one Doppler step, without a read-back (a pass
    sends one every few seconds; the set-up's restore puts the VFOs back
    at the end). False if a frame did not go."""
    ok = True
    if how == SPLIT:
        if rx_hz is not None:
            ok = rig.send(civ.set_vfo_freq(int(rx_hz), False)) and ok
        if tx_hz is not None:
            ok = rig.send(civ.set_vfo_freq(int(tx_hz), True)) and ok
    else:
        if tx_hz is not None:
            ok = rig.send(civ.set_vfo_freq(int(tx_hz), False)) and ok
        if rx_hz is not None:
            ok = rig.send(civ.set_vfo_freq(int(rx_hz), True)) and ok
    return ok


# -- DD ----------------------------------------------------------------------

def dd_settings(rig, call: Callable[..., Any], *, frequency: int, power_level: int | None = None) -> list[Setting]:
    """DD on `frequency`, in order: the frequency, then DD (so the radio is
    never in DD off 23 cm, where it refuses it anyway), then RPS off, then
    RF POWER (per band on a 9700, so read and set on 23 cm). `power_level`
    0-255, or None to read and report it only."""
    ask, acked = _asker(rig, call)
    out: list[Setting] = []

    def read_freq():
        got = ask(civ.read_freq(), b"\x03")
        return got if got in (None, UNAVAILABLE) else civ.decode_freq(got[1:6])

    out.append(Setting("frequency", int(frequency), read_freq,
                       lambda hz: bool(call(rig.set_freq, int(hz))), show=_mhz))

    def read_mode():
        got = ask(civ.read_mode_data(), b"\x26")
        if got in (None, UNAVAILABLE):
            return got
        m = civ.parse_mode_data(got)
        if got[2] == DD_MODE:
            return [DD_MODE, 0, 0]
        return [got[2], 1 if m["data"] else 0, m["filter"]]

    def write_mode(raw) -> bool:
        if raw[0] == DD_MODE:
            return acked(civ.set_dd_mode())
        mode = civ.MODES.get(raw[0])
        return mode is not None and bool(call(lambda: rig.set_mode(mode, int(raw[2]), bool(raw[1]))))

    def show_mode(raw) -> str:
        if raw[0] == DD_MODE:
            return "DD"
        return f"{civ.mode_label(civ.MODES.get(raw[0], '?'), bool(raw[1]))} FIL{raw[2]}"

    out.append(Setting("mode", [DD_MODE, 0, 0], read_mode, write_mode, show=show_mode))

    def read_duplex():
        got = ask(civ.read_split(), b"\x0f")
        return got if got in (None, UNAVAILABLE) else civ.decode_duplex(got)

    def want_simplex(before):
        return 0x00 if before in (0x00, 0x13) + civ.DUPLEX_SHIFTS else None

    def write_duplex(code) -> bool:
        if code == 0x13:
            return acked(civ.set_rps(True))
        return acked(civ.set_duplex(int(code)))

    out.append(Setting("rps", want_simplex, read_duplex, write_duplex,
                       show=lambda c: civ.DUPLEX_READS.get(c, f"0x{c:02x}")))

    def read_power():
        got = ask(civ.read_rf_power(), b"\x14\x0a")
        return got if got in (None, UNAVAILABLE) else civ.decode_rf_power(got)

    out.append(Setting("rf_power", None if power_level is None else int(power_level), read_power,
                       lambda v: acked(civ.set_rf_power(int(v))), show=lambda v: f"{round(v * 100 / 255)}%"))

    def read_packet_output():
        item = civ.DD_PACKET_OUTPUT_ITEM
        got = ask(civ.read_menu(item), b"\x1a\x05", match=lambda p: civ.decode_menu(p)[0] == item)
        return got if got in (None, UNAVAILABLE) else civ.decode_menu(got)[1]

    out.append(Setting("dd_packet_output", None, read_packet_output, None,
                       show=lambda v: civ.DD_PACKET_OUTPUT.get(v, f"0x{v:02x}")))
    return out


def on_air(rig, call: Callable[..., Any]) -> tuple[bool | None, int | None]:
    """(transmitting, Po raw 0-255) as 1C 00 and 15 11 read now; None for
    an answer that did not come. What the DD chatter test polls."""
    ptt = call(lambda: rig.ask(civ.read_ptt(), b"\x1c\x00", ASK_S), timeout=ASK_S + 2)
    po = call(lambda: rig.ask(civ.read_power(), b"\x15\x11", ASK_S), timeout=ASK_S + 2)
    keyed = None if ptt is None or len(ptt) < 3 else ptt[2] != 0
    level = None
    if po is not None and len(po) >= 4:
        try:
            level = civ.decode_meter(po[2:4])
        except civ.CivError:
            level = None
    return keyed, level
