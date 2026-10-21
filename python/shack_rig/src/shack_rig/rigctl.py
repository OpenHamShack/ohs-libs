"""A Hamlib NET rigctl server, so third-party software can use the rig.

This is the same protocol `rigctld` speaks, which is what WSJT-X means by
"Hamlib NET rigctl". The point of serving it from the app rather than running
rigctld beside it is that the radio is reached over a link rigctld cannot
share (Icom's LAN protocol takes one client) - so there is one connection to
the radio and everything shares it, instead of two programs fighting over it.

It drives any `shack_rig.rig.Rig`: it reads `rig.state` and calls the `set_*`
methods, and has never formed a CI-V frame. Moved from OpenHamShack Icom's
`app/rigctl.py`; the one change in behaviour is transmit (below).

**Transmit goes through the app's lease, per client.** `T 1` asks the app's
`lease` callable for a TxLease naming this client ("rigctl 198.51.100.20:51234")
and keys the rig under it; no callable, or a None answer, or a lease that
refuses, is `RPRT -11` (RIG_ENAVAIL), which WSJT-X reports as "function not
available" rather than retrying. `T 0` unkeys and releases that lease. A client
that keyed and then vanished - WSJT-X crashing mid-over, a laptop lid shut -
is unkeyed when its socket closes: the Icom app left that to its lease timer,
and a socket close is a much earlier and much plainer signal.

The wire format is unforgiving in one specific way, taken from hamlib's own
rigctl_parse.c:

  * a *get* returns its values and nothing else - no "RPRT 0" afterwards
  * a *set* returns "RPRT 0" and nothing else
  * any failure returns "RPRT -n", with hamlib's negative error numbers
  * \\chk_vfo answers with the VFO-mode flag alone, and no RPRT either way

Get that wrong and a client does not report a protocol error; it hangs waiting
for a line that is never coming, or reads the RPRT as the frequency.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .lease import PttRefused, TxLease
from .model import RigModel

log = logging.getLogger("shack_rig.rigctl")

# Hamlib's error numbers, sent negated. Only the ones this server can mean.
RIG_OK = 0
RIG_EINVAL = 1       # bad argument
RIG_ENIMPL = 4       # not implemented here
RIG_ETIMEOUT = 5     # the radio did not answer
RIG_EIO = 6          # no link to the radio
RIG_ENAVAIL = 11     # function not available (transmit switched off)
RIG_ERJCTED = 9      # the radio was asked and would not
RIG_EDOM = 17        # the argument is outside what this radio can do

#: The modes this rig has, as hamlib names them. Hamlib has no name for the
#: IC-9700's DD mode - it is a bit in the mask with no string in hamlib's own
#: table - so it is passed through as "DD" and a hamlib client will not
#: recognise it. That is a gap in hamlib, and inventing a different name here
#: would only move the confusion.
CIV_TO_HAMLIB = {
    "LSB": "LSB", "USB": "USB", "AM": "AM", "CW": "CW", "RTTY": "RTTY",
    "FM": "FM", "CWR": "CWR", "RTTYR": "RTTYR", "DV": "D-STAR", "DD": "DD",
}
HAMLIB_TO_CIV = {
    "LSB": "LSB", "USB": "USB", "AM": "AM", "CW": "CW", "RTTY": "RTTY",
    "FM": "FM", "CWR": "CWR", "CW-R": "CWR", "RTTYR": "RTTYR",
    "RTTY-R": "RTTYR", "D-STAR": "DV", "DSTAR": "DV", "DV": "DV", "DD": "DD",
}

#: What hamlib calls a mode with the data flag set. It calls them "packet"
#: modes, from when the data socket carried 1200 baud AX.25, and they are the
#: only names it has for the distinction - netrigctl has no separate data flag.
#: So the flag folds into the mode name in both directions here: WSJT-X asks
#: for PKTUSB when it wants USB-D, and a module that answers plain "USB" to `m`
#: leaves WSJT-X's Mode box disagreeing with the radio for a whole session -
#: and then the operator "fixes" it from WSJT-X, which sets plain USB and takes
#: the radio back out of data.
CIV_TO_HAMLIB_DATA = {"LSB": "PKTLSB", "USB": "PKTUSB",
                      "FM": "PKTFM", "AM": "PKTAM"}
HAMLIB_DATA_TO_CIV = {v: k for k, v in CIV_TO_HAMLIB_DATA.items()}


def hamlib_mode(mode: str | None, data: bool = False) -> str:
    """A CI-V mode and its data flag as the one name hamlib has for the pair."""
    if data and mode in CIV_TO_HAMLIB_DATA:
        return CIV_TO_HAMLIB_DATA[mode]
    return CIV_TO_HAMLIB.get(mode, mode)


def civ_mode(name: str) -> tuple[str | None, bool]:
    """The inverse: a hamlib mode name as (CI-V mode, data flag).

    (None, False) for a name hamlib has and this module does not, so the caller
    answers RIG_EINVAL rather than sending something invented.
    """
    name = (name or "").strip()
    up = name.upper()
    if up in HAMLIB_DATA_TO_CIV:
        return HAMLIB_DATA_TO_CIV[up], True
    return HAMLIB_TO_CIV.get(up) or HAMLIB_TO_CIV.get(name), False


#: Which bands and powers are advertised comes from the radio in use. Telling a
#: client the rig does 23 cm when it is an IC-7610 is not a cosmetic error -
#: WSJT-X reads dump_state to decide what it may ask for.


def modes_mask(modes: list, data_modes: list | tuple = ()) -> int:
    """Hamlib's mode bitmask for a set of mode names.

    Built from the radio's own list rather than fixed, so an IC-7610 does not
    advertise D-STAR and an IC-9700 does not advertise the PSK modes. The bit
    positions are hamlib's, and a mode hamlib has no bit for contributes
    nothing - which is the honest answer, since a client could not ask for it
    by name either.

    `data_modes` adds the packet bits for the modes that take the data flag.
    They are separate bits, not a flag on the parent mode, so a rig that does
    USB-D has to set both - and WSJT-X reads this mask to decide whether it may
    ask for PKTUSB at all. Leave them out and WSJT-X's mode list simply has no
    data entry, which is what "there is no option for data" looks like from the
    other side of the link.
    """
    bits = {
        "AM": 0x01, "CW": 0x02, "USB": 0x04, "LSB": 0x08, "RTTY": 0x10,
        "FM": 0x20, "WFM": 0x40, "CWR": 0x80, "RTTYR": 0x100,
        "DV": 0x1000000,
    }
    # rig.h: PKTLSB is flag 10, PKTUSB 11, PKTFM 12 and PKTAM 22. They are not
    # adjacent to their parent modes and PKTAM is a long way off, so these are
    # copied from the header rather than worked out.
    data_bits = {"LSB": 0x400, "USB": 0x800, "FM": 0x1000, "AM": 0x400000}
    mask = 0
    for name in modes:
        mask |= bits.get(name, 0)
    for name in data_modes:
        mask |= data_bits.get(name, 0)
    return mask

#: Single-letter commands, as hamlib numbers them, mapped to the long names.
SHORT = {
    "f": "get_freq", "F": "set_freq",
    "m": "get_mode", "M": "set_mode",
    "t": "get_ptt", "T": "set_ptt",
    "v": "get_vfo", "V": "set_vfo",
    "s": "get_split_vfo", "S": "set_split_vfo",
    "q": "quit", "Q": "quit",
}

#: Which commands are gets. A get returns values with no RPRT; everything else
#: returns RPRT 0. This table is the whole difference, so it lives in one place.
GETS = {
    "get_freq", "get_mode", "get_ptt", "get_vfo", "get_split_vfo",
    "get_powerstat", "dump_state", "chk_vfo", "get_vfo_info",
}

VFO_NAMES = {"VFOA", "VFOB", "VFO", "currVFO", "VFO_A", "VFO_B", "Main", "Sub", "MEM"}


def _resolve_model(model: RigModel | str | None) -> RigModel:
    """A RigModel, or an Icom model key as the Icom app passed one."""
    if isinstance(model, RigModel):
        return model
    from .icom.models import DEFAULT_MODEL, model as icom_model
    return icom_model(model or DEFAULT_MODEL)


def _dump_state(model: RigModel | str | None = None) -> str:
    """The capability dump, in the order hamlib's netrigctl backend reads it.

    It is parsed strictly positionally until the "protocol 1" key=value tail,
    so a missing line does not produce a warning - it shifts every value after
    it and the client gives up with a protocol error.
    """
    rig = _resolve_model(model)
    rx_ranges = rig.rx_ranges
    tx_ranges = rig.tx_ranges
    tx_power = rig.tx_power
    mask = modes_mask(rig.modes, rig.data_modes())
    out = []
    out.append("1")           # protocol version; 1 means the key=value tail follows
    out.append("3081")        # rig model - read and ignored by the client
    out.append("0")           # deprecated ITU region
    for start, end in rx_ranges:
        out.append(f"{start} {end} 0x{mask:x} -1 -1 0x3 0x1")
    out.append("0 0 0 0 0 0 0")
    for start, end in tx_ranges:
        low, high = tx_power.get(start, (1000, 100000))
        out.append(f"{start} {end} 0x{mask:x} {low} {high} 0x3 0x1")
    out.append("0 0 0 0 0 0 0")
    for step in (1, 10, 100, 1000, 5000, 6250, 10000, 12500, 25000):
        out.append(f"0x{mask:x} {step}")
    out.append("0 0")
    for mask, width in (
        (0x10C, 2400),   # USB|LSB|RTTY-ish
        (0x10C, 1800),
        (0x10C, 3000),
        (0x82, 500),     # CW|CWR
        (0x82, 250),
        (0x82, 1200),
        (0x1, 9000),     # AM
        (0x20, 15000),   # FM
    ):
        out.append(f"0x{mask:x} {width}")
    out.append("0 0")
    out.append("9999")        # max_rit,  ±9.999 kHz
    out.append("9999")        # max_xit
    out.append("0")           # max_ifshift
    out.append("0")           # announces
    out.append("10 20")       # preamp steps, dB
    out.append("10 20 30")    # attenuator steps, dB
    out.append("0x0")         # has_get_func
    out.append("0x0")         # has_set_func
    out.append("0x40000020")  # has_get_level: RAWSTR | STRENGTH
    out.append("0x0")         # has_set_level
    out.append("0x0")         # has_get_parm
    out.append("0x0")         # has_set_parm
    # Protocol 1 tail. Order does not matter here and unknown keys are ignored,
    # but "done" must be the last line or the client keeps reading.
    out.append("vfo_ops=0x0")
    out.append("ptt_type=0x1")
    out.append("targetable_vfo=0x0")
    out.append("has_set_vfo=1")
    out.append("has_get_vfo=1")
    out.append("has_set_freq=1")
    out.append("has_get_freq=1")
    out.append("has_set_conf=0")
    out.append("has_get_conf=0")
    out.append("has_power2mW=0")
    out.append("has_mW2power=0")
    out.append("has_get_ant=0")
    out.append("has_set_ant=0")
    out.append("timeout=1000")
    out.append("rig_model=3081")
    out.append(f"rigctld_version=shack-rig-{rig.key}")
    out.append("done")
    return "\n".join(out) + "\n"


class RigctlServer:
    """rigctld's protocol on a TCP port, for one rig.

    `lease(holder)` is how this server asks the app for permission to key:
    it returns a TxLease for that holder, or None to refuse. Leave it out and
    the server is receive-only - every `T 1` answers RIG_ENAVAIL - which is the
    right default for an app that has not wired its lease in yet.

    `model` is the RigModel dump_state describes; by default the rig's own,
    and an Icom model key is still accepted for the app's existing call.
    """

    def __init__(self, radio, host: str, port: int,
                 model_key: RigModel | str | None = None,
                 lease: Callable[[str], TxLease | None] | None = None):
        self.model_key = model_key
        self.radio = radio
        self.host = host
        self.port = port
        self.lease = lease
        self._server: asyncio.AbstractServer | None = None
        self.clients = 0
        #: The lease each keyed client holds, by holder name. A client in here
        #: is one this server keyed the rig for, and the one it unkeys if the
        #: client goes away.
        self._held: dict[str, TxLease] = {}
        #: Every connected client's writer, so stop() can close them.
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def rig_model(self) -> RigModel:
        if self.model_key is None and getattr(self.radio, "model", None) is not None:
            return self.radio.model
        return _resolve_model(self.model_key)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, self.host, self.port)
        sock = self._server.sockets[0].getsockname() if self._server.sockets else None
        if sock and not self.port:
            # Port 0 asks the OS for one; tests and an app that lets the
            # platform allocate want to know which it got.
            self.port = sock[1]
        log.info("rigctl listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        """Unkey, hang up on every client, and stop listening.

        In that order, and the first is the one that matters. Since Python
        3.12, `Server.wait_closed()` waits for every connection the server
        accepted to finish, not just for the listening socket - so a WSJT-X
        left connected (which is how WSJT-X is left) made this wait for ever.
        The unkey used to come after that wait, so a client holding the key
        when the module was stopped stayed keyed until the process was
        killed; and the app's own shutdown, waiting here, ran out its stop
        timeout and was killed before it could log out of the radio.
        """
        for holder in list(self._held):
            self._unkey_for(holder, "the rigctl server stopped")
        if self._server is not None:
            server, self._server = self._server, None
            server.close()
            for writer in list(self._writers):
                writer.close()
            try:
                await asyncio.wait_for(server.wait_closed(), 2.0)
            except asyncio.TimeoutError:
                log.warning("rigctl: a client did not hang up; stopped without it")

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        holder = "rigctl " + (f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) and len(peer) >= 2
                              else str(peer))
        self.clients += 1
        self._writers.add(writer)
        log.info("rigctl client %s connected", peer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    text = line.decode("utf-8", "replace").strip()
                except Exception:
                    continue
                if not text:
                    continue
                reply, close = self._dispatch(text, holder)
                if reply:
                    writer.write(reply.encode())
                    await writer.drain()
                if close:
                    break
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            log.exception("rigctl client %s", peer)
        finally:
            self.clients -= 1
            self._writers.discard(writer)
            if holder in self._held:
                self._unkey_for(holder, "its rigctl connection closed while keyed")
            log.info("rigctl client %s gone", peer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _unkey_for(self, holder: str, why: str) -> None:
        lease = self._held.pop(holder, None)
        log.warning("unkeying for %s: %s", holder, why)
        try:
            self.radio.set_ptt(False)
        except Exception:
            log.exception("unkeying for %s", holder)
        _release(lease)

    # -- dispatch ----------------------------------------------------------

    def _dispatch(self, text: str, holder: str = "rigctl") -> tuple[str, bool]:
        """One command line in; the response, and whether to close afterwards."""
        parts = text.split()
        if not parts:
            return "", False
        head = parts[0]
        args = parts[1:]

        if head.startswith("\\"):
            name = head[1:]
        elif head in SHORT:
            name = SHORT[head]
        elif len(head) > 1 and head[0] in SHORT:
            # "F14074000" with no space - rigctl does this in some versions.
            name = SHORT[head[0]]
            args = [head[1:]] + args
        else:
            log.debug("unknown command %r", text)
            return f"RPRT -{RIG_EINVAL}\n", False

        if name == "quit":
            # rigctld answers the quit before hanging up, and hamlib's own
            # client treats a silent close as an I/O error on an otherwise
            # clean session. Cheap to be polite.
            return "RPRT 0\n", True

        # With VFO mode off a client should not send a VFO, but rigctl will if
        # the user turned it on, and an unexpected leading token would other-
        # wise be read as the frequency.
        args = [a for a in args if a not in VFO_NAMES]

        try:
            body, err = self._run(name, args, holder)
        except Exception:
            log.exception("rigctl %s", name)
            return f"RPRT -{RIG_EIO}\n", False

        if err:
            return f"RPRT -{err}\n", False
        if name in GETS:
            return body, False
        return "RPRT 0\n", False

    def _run(self, name: str, args: list[str], holder: str = "rigctl") -> tuple[str, int]:
        st = self.radio.state
        live = st.get("connected")

        if name == "chk_vfo":
            # 0 means "VFO mode off": the client will not prefix VFO names.
            return "0\n", 0
        if name == "dump_state":
            return _dump_state(self.rig_model), 0
        if name == "get_powerstat":
            return ("1\n" if live else "0\n"), 0
        if name == "set_powerstat":
            return "", RIG_ENIMPL

        # Everything below this line is about the radio rather than about the
        # server, so none of it can be answered when there is no link - and
        # the cache must not answer for it. It held the last frequency and
        # mode the radio reported, so a client polling a rig that had gone
        # away was told 144.174 MHz and RPRT 0 for as long as the module ran.
        # WSJT-X believes that: it sits decoding nothing while showing a band
        # it is not on, and would transmit on it. A disconnect makes this the
        # normal case rather than a rare one, which is why it is fixed here
        # and not left as it was.
        #
        # The listener stays up and the client's socket is never dropped. A
        # rigctl connection is made once and kept for hours, so closing it
        # would mean WSJT-X reporting a rig control failure the operator then
        # has to clear by hand - whereas an absent radio that says so is a
        # state hamlib clients already know how to sit through. get_powerstat
        # above is the verb for asking, and it answers 0.
        if not live:
            return "", RIG_EIO

        if name == "get_freq":
            if st.get("freq") is None:
                return "", RIG_ETIMEOUT
            return f"{st['freq']}\n", 0

        if name == "set_freq":
            if not args:
                return "", RIG_EINVAL
            try:
                # Clients send whole Hz, but some send "14074000.000000".
                hz = int(float(args[0]))
            except ValueError:
                return "", RIG_EINVAL
            # The answer was thrown away and RPRT 0 sent regardless, so asking
            # an IC-9700 for 14 MHz was reported as done. WSJT-X believes that,
            # changes band, and transmits nowhere. EDOM rather than EINVAL: the
            # number was well formed, it is this radio that cannot go there.
            if not self.radio.set_freq(hz):
                return "", RIG_EDOM
            return "", 0

        if name == "get_mode":
            mode = st.get("mode")
            if mode is None:
                return "", RIG_ETIMEOUT
            # Two lines: the mode, then the passband in Hz.
            return (f"{hamlib_mode(mode, st.get('data_mode'))}\n"
                    f"{self._passband(mode)}\n"), 0

        if name == "set_mode":
            if not args:
                return "", RIG_EINVAL
            mode, data = civ_mode(args[0])
            if mode is None:
                return "", RIG_EINVAL
            # The passband argument is accepted and not applied: the IC-9700
            # selects a filter number, not a width, and picking FIL1/2/3 from a
            # requested Hz would be a guess dressed as a setting.
            #
            # The data flag is applied, and a plain "USB" clears it. Hamlib
            # names the two separately, so "USB" here is a client saying not
            # PKTUSB - carrying the flag over would make it impossible for
            # WSJT-X to take the radio back out of data.
            # Same as set_freq and set_split: the answer was discarded, so a
            # mode this radio has not got came back as done. ERJCTED - the
            # request was well formed and the radio declined it.
            if not self.radio.set_mode(mode, data=data):
                return "", RIG_ERJCTED
            return "", 0

        if name == "get_ptt":
            return ("1\n" if st.get("ptt") else "0\n"), 0

        if name == "set_ptt":
            if not args:
                return "", RIG_EINVAL
            on = args[0] not in ("0", "RX", "rx")
            return "", self._set_ptt(on, holder)

        if name == "get_vfo":
            return f"{st.get('vfo') or 'VFOA'}\n", 0

        if name == "set_vfo":
            if not args:
                # The VFO token was stripped above as a positional; rigctl
                # sends "V VFOA", so an empty list here means VFOA was given.
                return "", 0
            return "", 0

        if name == "get_split_vfo":
            # Split flag, then the transmit VFO.
            return ("1\n" if st.get("split") else "0\n") + "VFOB\n", 0

        if name == "set_split_vfo":
            if not args:
                return "", RIG_EINVAL
            # As with set_freq: a refusal used to come back as success.
            # ERJCTED rather than EDOM - the request was reasonable and the
            # radio declined it.
            if not self.radio.set_split(args[0] not in ("0",)):
                return "", RIG_ERJCTED
            return "", 0

        if name == "get_vfo_info":
            freq = st.get("freq") or 0
            mode = st.get("mode") or "USB"
            return (
                f"{freq}\n{hamlib_mode(mode, st.get('data_mode'))}\n{self._passband(mode)}\n"
                f"{'1' if st.get('split') else '0'}\n0\n"
            ), 0

        return "", RIG_ENIMPL

    def _set_ptt(self, on: bool, holder: str) -> int:
        """Key under a lease the app grants this client, or unkey.

        Unkey is answered for any client, keyed by it or not: refusing a
        client's request to *stop* transmitting is never the safe answer.
        """
        if not on:
            lease = self._held.pop(holder, None)
            sent = self.radio.set_ptt(False)
            _release(lease)
            return RIG_OK if sent else RIG_EIO
        lease = self._held.get(holder)
        if lease is None:
            if self.lease is None:
                return RIG_ENAVAIL
            try:
                lease = self.lease(holder)
            except Exception:
                log.exception("asking the app for a lease for %s", holder)
                lease = None
            if lease is None:
                return RIG_ENAVAIL
        try:
            sent = self.radio.set_ptt(True, lease)
        except PttRefused as e:
            log.info("rigctl %s may not transmit: %s", holder, e.reason)
            self._held.pop(holder, None)
            _release(lease)
            return RIG_ENAVAIL
        if not sent:
            self._held.pop(holder, None)
            _release(lease)
            return RIG_EIO
        self._held[holder] = lease
        return RIG_OK

    @staticmethod
    def _passband(mode: str) -> int:
        """A representative passband, since the radio reports a filter number.

        The numbers are the IC-9700's default widths for FIL1 in each mode.
        """
        return {
            "LSB": 2400, "USB": 2400, "AM": 9000, "CW": 500, "CWR": 500,
            "RTTY": 500, "RTTYR": 500, "FM": 15000, "DV": 7000, "DD": 130000,
        }.get(mode, 2400)


def _release(lease) -> None:
    """End a lease this server was granted, if it can be ended."""
    if lease is None:
        return
    fn = getattr(lease, "release", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            log.exception("releasing %s", getattr(lease, "holder", "a lease"))
