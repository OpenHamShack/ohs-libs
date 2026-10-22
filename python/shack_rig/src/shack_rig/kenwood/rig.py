"""A Kenwood TM-V71 on its PC port, with packet through its DATA jack: the
third `Rig` backend.

The same surface as YaesuRig (shack_rig/rig.py's contract: a state cache
one poll loop keeps current, `listen`, `set_*` returning False when nothing
was sent, keying only under a lease) and built the same way - threads, not
asyncio, because the desktop app has no loop to lend and a Windows COM port
is a pyserial object no loop can watch. What differs is where things are:

- **Control** is the PC port (cat.py): the band's frequency, mode and
  tones through FO, power through PC, the squelch through BY and SQ, the
  DATA jack's band and speed through MU. Strictly one question, one answer:
  the TM-V71 has no auto-information mode in any source, so nothing arrives
  unasked, and an answer is simply the next line (`ask`). That is also the
  only way to read a refusal, which carries no command name ("?" or "N").
- **Audio** is a sound-card interface on the DATA jack (a Digirig, a
  SignaLink, a CM108 board): receive from PR9 (or PR1), transmit into PKD,
  through shack_rig.sound at the card's own rate both ways, as YaesuRig
  runs the FTX-1's codec.
- **PTT** is the interface pulling the DATA jack's PKS low: RTS or DTR on
  its serial port (`ptt_device`, opened by serialio with both lines low),
  a CM108 GPIO pin (cm108.py, low on open), or the interface's own VOX
  (a SignaLink keys itself on the audio). Never `TX` on the PC port -
  cat.py says why - so `set_ptt` has no CAT route at all, and with
  `ptt="none"` the rig cannot be keyed.

**The band.** The radio has two, A and B; the DATA jack works one of them,
the one EXT. DATA BAND (menu 517) names. This rig drives one band
(`self.band`) and reports it; which band, and making the DATA jack follow
it, is the data set-up's business (datasetup.py), because changing it is a
change to the radio that has to be put back.

**Keying** is YaesuRig's sequence: the lease, `allow_ptt`, a known
frequency inside the model's transmit bands, then the line. While keyed
the poll asks the lease again every cycle and unkeys on the first refusal;
an unkey that could not be sent is owed and retried every cycle.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from typing import Any, Callable

from .. import lease as txlease
from ..i18n import _
from ..lease import PttRefused, TxLease
from ..model import mode_label
from ..serialio import SerialError, SerialPort
from . import cat
from .models import DEFAULT_MODEL, KenwoodModel, model as find_model, segment

log = logging.getLogger("shack_rig.kenwood")

PTT_ROUTES = ("rts", "dtr", "cm108", "vox", "none")

#: How long to wait for an answer. At 9600 baud FO's ~50-character answer
#: is 50 ms on the wire; this is paid only by a question it ignores.
ANSWER_TIMEOUT_S = 0.6

#: With nothing heard for this long the link is called down and rebuilt by
#: the supervisor - a radio switched off, a cable pulled.
SILENT_S = 3.0


class KenwoodRig:
    """One TM-V71. `device` is the PC port; `ptt_device` the interface's
    serial port for rts/dtr, or its CM108 HID device ("" to use the only
    one plugged in) for cm108. `band` 0 (A) or 1 (B) to drive that band;
    None to take the DATA jack's receive band as the radio has it.

    `open_port(device, baud, stop_bits)` and `open_cm108(device)` are for
    tests: a stand-in port (a pty has no modem lines) and a stand-in HID
    device (cm108.Cm108Ptt's `writer`)."""

    def __init__(self, device: str, model: KenwoodModel | str | None = None, *, baud: int = 0,
                 band: int | None = None, ptt: str = "rts", ptt_device: str = "", cm108_gpio: int = 3,
                 allow_ptt: bool = False, poll_interval: float = 0.25, audio_in: str = "", audio_out: str = "",
                 sound_backend=None, open_port: Callable[..., Any] | None = None, open_cm108=None):
        if isinstance(model, str) or model is None:
            model = find_model(model or DEFAULT_MODEL)
        if ptt not in PTT_ROUTES:
            raise ValueError(_("PTT is one of {routes}, not {ptt}").format(routes=", ".join(PTT_ROUTES),
                                                                              ptt=repr(ptt)))
        if band not in (None, *cat.BANDS):
            raise ValueError(_("band is 0 (A) or 1 (B), not {band}").format(band=repr(band)))
        self.device = device
        self.model = model
        self.baud = baud or model.pc_baud
        self.band = band
        self._band_given = band is not None
        self.ptt_route = ptt
        self.ptt_device = ptt_device
        self.cm108_gpio = cm108_gpio
        self.allow_ptt = allow_ptt
        self.poll_interval = poll_interval
        self.audio_in = audio_in
        self.audio_out = audio_out
        self._sound_backend = sound_backend
        self._open_port = open_port or (lambda dev, baud, bits: SerialPort(dev, baud, stop_bits=bits))
        self._open_cm108 = open_cm108

        self.state: dict[str, Any] = {
            "connected": False, "wanted": True, "link_state": "connecting", "error": None,
            "radio": None, "freq": None, "mode": None, "data_mode": False, "mode_label": None,
            "filter": None, "ptt": False, "vfo": "VFOA", "split": False, "smeter": None,
            "smeter_db": None, "preamp": None, "preamp_mode": None, "att": None, "power": None,
            "swr": None, "audio": False, "audio_rate": None, "scope": False, "scope_points": None,
            "updated": 0.0,
            # Kenwood's own. `band` the one driven (0 A, 1 B); `busy` its
            # squelch (BY), None until read and while not watching;
            # `power_level` H/M/L (PC); `squelch_level` 0-31 (SQ, read only);
            # `band_mode` VFO/memory/call/WX (VM); `data_band` and
            # `data_speed` the DATA jack's menu items (MU); `version` E or A
            # (TY's M or K), None when it does not say.
            "band": band, "busy": None, "power_level": None, "squelch_level": None, "band_mode": None,
            "data_band": None, "data_speed": None, "version": None,
        }
        self._listeners: list[Callable[[dict], None]] = []
        self._audio_listeners: list[Callable[[bytes], None]] = []
        self._lock = threading.RLock()
        self._ask_lock = threading.Lock()
        self._answers: queue.Queue = queue.Queue()
        self._port = None
        self._ptt_port = None
        self._buf = ""
        self._last_heard = 0.0
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lease: TxLease | None = None
        self._unkey_owed = False
        self._watch_squelch = False
        self._input = None
        self._tx = None
        self.refused = 0

    # -- lifetime -----------------------------------------------------------------

    def open(self) -> None:
        """Start the supervisor and the poll. Returns at once; `state`
        says when the radio answers."""
        if self._threads:
            return
        self._stop.clear()
        for target, name in ((self._supervise, "pc-supervisor"), (self._poll, "pc-poll")):
            t = threading.Thread(target=target, name=f"{name} {self.device}", daemon=True)
            t.start()
            self._threads.append(t)

    def close(self) -> None:
        """Unkey, and let go of every port and device."""
        self._stop.set()
        self._wake.set()
        self._unkey_now()
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=2)
        self._threads = []
        self._teardown()

    async def start(self) -> None:
        self.open()

    async def stop(self) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self.close)

    async def connect(self) -> None:
        self._set(wanted=True, error=None)
        self._wake.set()

    async def disconnect(self) -> None:
        self._set(wanted=False, error=None)
        self._unkey_now()
        self._teardown()
        self._wake.set()

    def where(self) -> str:
        return self.device

    def link_diagnostics(self) -> dict | None:
        return {"device": self.device, "baud": self.baud, "band": self.band, "ptt": self.ptt_route,
                "ptt_device": self.ptt_device or None, "refused": self.refused}

    def _open_ptt(self):
        if self.ptt_route in ("rts", "dtr"):
            if not self.ptt_device:
                raise SerialError(_("PTT by {line} needs the interface's serial port").format(
                    line=self.ptt_route.upper()))
            port = self._open_port(self.ptt_device, 9600, 1)
            port.open()
            return port
        if self.ptt_route == "cm108":
            from ..cm108 import Cm108Ptt
            port = Cm108Ptt(self.ptt_device, self.cm108_gpio, writer=self._open_cm108)
            port.open()
            return port
        return None

    def _connect(self) -> None:
        port = self._open_port(self.device, self.baud, 1)
        port.open()
        try:
            ptt_port = self._open_ptt()
        except Exception:
            port.close()
            raise
        with self._lock:
            self._port, self._ptt_port = port, ptt_port
            self._buf = ""
        self._last_heard = time.monotonic()
        t = threading.Thread(target=self._read, args=(port,), name=f"pc-read {self.device}", daemon=True)
        t.start()
        # A CR first, as CHIRP's get_id does: whatever half-line was in the
        # radio's buffer (a probe, a terminal) is ended and refused, not
        # glued to the front of ID.
        self.send(cat.TERMINATOR)
        time.sleep(0.05)
        got = self.ask(cat.read_id(), timeout=1.0)
        if got is None or cat.refused(got):
            got = self.ask(cat.read_id(), timeout=1.0)
        if got is None:
            self._teardown()
            raise SerialError(_("{device} opened and the radio did not answer; check the PC port cable, the "
                                "radio's PC port speed (menu 519, {baud} here) and that the radio is on").format(
                device=self.device, baud=self.baud))
        try:
            rid = cat.parse_id(got)
        except cat.CatError:
            self._teardown()
            raise SerialError(_("{device} answered {got!r}, not a Kenwood ID").format(device=self.device,
                                                                                        got=got)) from None
        if self.model.pc_id and rid != self.model.pc_id:
            self._teardown()
            raise SerialError(_("the radio on {device} says it is a {got}, not a {name}").format(
                device=self.device, got=rid, name=self.model.name))
        version = None
        ty = self.ask(cat.read_type())
        if ty and ty.startswith("TY "):
            code = cat.fields(ty, "TY")[0]
            version = {"M": "E", "K": "A"}.get(code)
        menu = self.read_menu()
        if menu is not None and not self._band_given:
            # The DATA jack's receive side: what packet hears is what the
            # rig should watch. A crossband setting is undone by the set-up.
            self.band = {0: 0, 1: 1, 2: 1, 3: 0}.get(menu.data_band, 0)
        if self.band is None:
            self.band = cat.BAND_A
        self._set(connected=True, radio=self.model.name, error=None, version=version, band=self.band,
                  vfo="VFOA" if self.band == 0 else "VFOB")
        self.read_vfo()
        log.info("%s on %s at %d baud, band %s, PTT by %s", self.model.name, self.device, self.baud,
                 "AB"[self.band], self.ptt_route)

    def _teardown(self) -> None:
        with self._lock:
            port, self._port = self._port, None
            ptt_port, self._ptt_port = self._ptt_port, None
        for p in (ptt_port, port):
            if p is not None:
                try:
                    p.close()
                except Exception:                     # noqa: BLE001 - closing regardless
                    log.exception("closing %s", getattr(p, "device", "?"))
        inp, self._input = self._input, None
        if inp is not None:
            inp.close()
        tx, self._tx = self._tx, None
        if tx is not None:
            tx.close()
        self._set(connected=False, audio=False)

    def _supervise(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if not self.state["wanted"]:
                self._wake.wait()
                self._wake.clear()
                continue
            if self._port is None:
                try:
                    self._connect()
                    backoff = 1.0
                    if self._audio_listeners:
                        self._start_input()
                except Exception as e:                # noqa: BLE001 - every failure is a sentence
                    self._set(connected=False, error=str(e))
                    self._wake.wait(backoff)
                    self._wake.clear()
                    backoff = min(30.0, backoff * 2)
                    continue
            elif time.monotonic() - self._last_heard > SILENT_S:
                log.warning("%s has said nothing for %.0f s; reconnecting", self.device, SILENT_S)
                self._teardown()
                self._set(error=_("the radio stopped answering"))
                continue
            self._wake.wait(0.25)
            self._wake.clear()

    # -- the wire -------------------------------------------------------------------

    def send(self, command: str) -> bool:
        """Write one command line. False if there is no port or it would not go."""
        port = self._port
        if port is None:
            return False
        return bool(port.write(command.encode("ascii")))

    def ask(self, command: str, name: str | None = None, timeout: float = ANSWER_TIMEOUT_S) -> str | None:
        """Send one command and return the next answer line ("?" and "N"
        included: a refusal names no command), or None on silence. One at a
        time: the poll and a set-up never interleave. `name` is unused; it
        keeps YaesuRig's signature for callers written against both."""
        with self._ask_lock:
            while True:
                try:
                    self._answers.get_nowait()          # anything stale is not this answer
                except queue.Empty:
                    break
            if not self.send(command):
                return None
            try:
                got = self._answers.get(timeout=timeout)
            except queue.Empty:
                return None
            if cat.refused(got):
                self.refused += 1
                log.debug("the radio refused %r: %s", command.strip(), got)
            return got

    def _read(self, port) -> None:
        while not self._stop.is_set() and self._port is port:
            try:
                data = port.read_wait(0.1)
            except (OSError, SerialError) as e:
                if self._port is port:
                    log.warning("%s went away: %s", self.device, e)
                    self._set(error=_("{device} went away").format(device=self.device))
                    self._last_heard = 0.0
                    self._wake.set()
                return
            if not data:
                continue
            self._last_heard = time.monotonic()
            self._buf += data.decode("ascii", "replace")
            answers, self._buf = cat.split_answers(self._buf)
            for a in answers:
                self._answers.put(a)

    # -- reading the radio ----------------------------------------------------------------

    def read_vfo(self, band: int | None = None) -> cat.Vfo | None:
        """FO for `band` (the rig's own by default), into the state."""
        b = self.band if band is None else band
        if b is None:
            return None
        got = self.ask(cat.read_vfo(b))
        if not got or cat.refused(got):
            return None
        try:
            vfo = cat.parse_vfo(got)
        except cat.CatError as e:
            log.debug("%s", e)
            return None
        if vfo.band == self.band:
            mode = cat.MODES.get(vfo.mode)
            base = "FM" if mode == "NFM" else mode
            self._set(freq=vfo.freq, mode=base, data_mode=False, mode_label=mode_label(base),
                      filter="NAR" if mode == "NFM" else None)
        return vfo

    def write_vfo(self, vfo: cat.Vfo) -> cat.Vfo | None:
        """Send a whole FO and return what the radio echoed, None if refused."""
        got = self.ask(vfo.command())
        if not got or cat.refused(got):
            return None
        try:
            echoed = cat.parse_vfo(got)
        except cat.CatError:
            return None
        if echoed.band == self.band:
            self._set(freq=echoed.freq)
        return echoed

    def read_menu(self) -> cat.Menu | None:
        got = self.ask(cat.read_menu())
        if not got or cat.refused(got):
            return None
        try:
            menu = cat.parse_menu(got)
        except cat.CatError:
            return None
        self._set(data_band=menu.data_band, data_speed=cat.DATA_SPEEDS.get(menu.data_speed))
        return menu

    def write_menu(self, menu: cat.Menu) -> cat.Menu | None:
        """Write a whole menu (cat.Menu: the radio's own with one field
        changed). The echo must match field for field, or it is not believed."""
        got = self.ask(menu.command())
        if not got or cat.refused(got):
            return None
        try:
            echoed = cat.parse_menu(got)
        except cat.CatError:
            return None
        if echoed.raw != menu.raw:
            log.warning("the radio's menu answer differs from what was written: %s", got)
            return None
        self._set(data_band=echoed.data_band, data_speed=cat.DATA_SPEEDS.get(echoed.data_speed))
        return echoed

    def band_for(self, hz: int) -> int | None:
        """The band that can be tuned to `hz` by CAT now: the rig's own if
        its segment covers it, else the other one's, else None (the
        operator presses BAND on one side - see models.py)."""
        want = segment(hz)
        if want is None:
            return None
        order = [self.band, 1 - self.band] if self.band is not None else list(cat.BANDS)
        for b in order:
            if want not in self.model.band_segments.get(b, ()):
                continue
            vfo = self.read_vfo(b)
            if vfo is not None and segment(vfo.freq) == want:
                return b
        return None

    def use_band(self, band: int) -> None:
        """Drive `band` from now on (the data set-up's choice)."""
        if band not in cat.BANDS:
            raise ValueError(_("no band {band}").format(band=repr(band)))
        self.band = band
        self._set(band=band, vfo="VFOA" if band == 0 else "VFOB")
        self.read_vfo()

    # -- the poll ---------------------------------------------------------------------

    def _poll(self) -> None:
        tick = 0
        while not self._stop.is_set():
            time.sleep(self.poll_interval)
            if self._port is None or not self.state["connected"] or self.band is None:
                continue
            try:
                self._watch_lease()
                if self._watch_squelch:
                    got = self.ask(cat.read_busy(self.band))
                    if got and got.startswith("BY "):
                        b, busy = cat.parse_busy(got)
                        if b == self.band:
                            self._set(busy=busy)
                tick += 1
                if tick % 2 == 0:
                    self.read_vfo()
                if tick % 8 == 1:
                    self._read_levels()
            except Exception:                         # noqa: BLE001 - a poll must not die
                log.exception("poll")

    def _read_levels(self) -> None:
        got = self.ask(cat.read_power(self.band))
        if got and got.startswith("PC "):
            _b, level = cat.parse_power(got)
            self._set(power_level=cat.POWER_LEVELS[level])
        got = self.ask(cat.read_squelch(self.band))
        if got and got.startswith("SQ "):
            try:
                self._set(squelch_level=cat.parse_squelch(got))
            except cat.CatError as e:
                log.debug("%s", e)
        got = self.ask(cat.read_band_mode(self.band))
        if got and got.startswith("VM "):
            _b, m = cat.parse_band_mode(got)
            self._set(band_mode=cat.BAND_MODES.get(m))

    def _set(self, **kw) -> None:
        changed = False
        with self._lock:
            for k, v in kw.items():
                if self.state.get(k) != v:
                    self.state[k] = v
                    changed = True
            word = ("disconnected" if not self.state["wanted"] else "connected" if self.state["connected"]
                    else "failed" if self.state["error"] else "connecting")
            if self.state["link_state"] != word:
                self.state["link_state"] = word
                changed = True
            if changed:
                self.state["updated"] = time.time()
        if changed:
            for fn in list(self._listeners):
                try:
                    fn(self.state)
                except Exception:                     # noqa: BLE001
                    log.exception("state listener")

    def listen(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    # -- control ------------------------------------------------------------------------

    def set_freq(self, hz: int) -> bool:
        """Tune the rig's band, inside its segment only (models.py)."""
        if self.band is None or not self.model.covers(hz) or segment(hz) != segment(self.state.get("freq")):
            return False
        vfo = self.read_vfo()
        if vfo is None:
            return False
        echoed = self.write_vfo(vfo.replace(freq=int(hz)))
        return echoed is not None and echoed.freq == int(hz)

    def set_mode(self, mode: str, filt: int | None = None, data: bool | None = None,
                 vfo: int | None = None) -> bool:
        """FM (NFM with `filt` 2) or AM, on the rig's band. There is no data
        flag: data goes through the DATA jack whatever the mode."""
        from ..model import split_mode_label
        mode, suffix = split_mode_label(mode)
        if suffix or data or not self.model.has_mode(mode):
            return False
        code = {"FM": 1 if filt == 2 else 0, "AM": 2}[mode]
        cur = self.read_vfo()
        if cur is None:
            return False
        echoed = self.write_vfo(cur.replace(mode=code))
        if echoed is None:
            return False
        self.read_vfo()
        return True

    def set_data(self, on: bool) -> bool:
        return False

    def set_vfo(self, vfo: str) -> bool:
        return False

    def set_split(self, on: bool) -> bool:
        return False

    def set_scope(self, on: bool) -> bool:
        return False

    def set_preamp(self, level: int) -> bool:
        return False

    def set_preamp_mode(self, mode: int) -> bool:
        return False

    def set_att(self, db: int) -> bool:
        return False

    def spectrum(self) -> dict | None:
        return None

    def listen_scope(self, fn):
        return lambda: None

    def scope_stats(self) -> dict:
        return {"enabled": False}

    def read_power(self) -> int | None:
        """PC on the rig's band: 0 H, 1 M, 2 L; None if not answered."""
        got = self.ask(cat.read_power(self.band)) if self.band is not None else None
        if not got or not got.startswith("PC "):
            return None
        _b, level = cat.parse_power(got)
        self._set(power_level=cat.POWER_LEVELS[level])
        return level

    def set_power(self, level: int) -> bool:
        """PC: 0 H, 1 M, 2 L, on the rig's band; True when the echo agrees."""
        if self.band is None or level not in cat.POWER_LEVELS:
            return False
        got = self.ask(cat.set_power(self.band, int(level)))
        if not got or not got.startswith("PC "):
            return False
        _b, now = cat.parse_power(got)
        self._set(power_level=cat.POWER_LEVELS[now])
        return now == int(level)

    def read_squelch(self) -> int | None:
        got = self.ask(cat.read_squelch(self.band)) if self.band is not None else None
        if not got or not got.startswith("SQ "):
            return None
        try:
            level = cat.parse_squelch(got)
        except cat.CatError:
            return None
        self._set(squelch_level=level)
        return level

    def watch_squelch(self, on: bool = True) -> None:
        """Read BY (the squelch) on every poll while on, into `state["busy"]`."""
        self._watch_squelch = bool(on)
        if not self._watch_squelch:
            self._set(busy=None)

    # -- keying ---------------------------------------------------------------------------

    def set_ptt(self, on: bool, lease: TxLease | None = None) -> bool:
        """Key under `lease`, or unkey. See the module docstring."""
        if not on:
            self._lease = None
            return self._unkey_now()
        txlease.check(lease)
        if not self.allow_ptt:
            raise PttRefused(_("transmitting is switched off for this rig"))
        if self.ptt_route == "none":
            raise PttRefused(_("no PTT is set up for this radio, so it receives only"))
        freq = self.state.get("freq")
        if freq is None:
            raise PttRefused(_("the rig's frequency is not known yet, so it is not keyed"))
        if not self.model.can_transmit(freq):
            raise PttRefused(_("{mhz} MHz is outside the {radio}'s transmit bands ({bands})").format(
                mhz=f"{freq / 1e6:.6f}", radio=self.model.name, bands=self.model.transmit_bands_text()))
        if self.ptt_route == "vox":
            # The interface keys itself on the audio (a SignaLink): nothing to
            # raise, and send_audio is what the lease now permits.
            sent = True
        else:
            port = self._ptt_port
            sent = port is not None and bool(port.set_line(self.ptt_route, True))
        if not sent:
            self._unkey_now()
            return False
        self._lease = lease
        self._set(ptt=True)
        return True

    def _unkey_now(self) -> bool:
        """The PTT line low, and the transmit audio stopped."""
        ok = True
        port = self._ptt_port
        if port is not None and self.ptt_route in ("rts", "dtr", "cm108"):
            ok = bool(port.set_line(self.ptt_route, False))
        out = self._tx
        if out is not None:
            out.close()
        self._unkey_owed = not ok
        if ok:
            self._set(ptt=False)
        return ok

    def _watch_lease(self) -> None:
        if self._unkey_owed and self._unkey_now():
            log.info("sent the unkey that was owed")
        lease = self._lease
        if lease is None:
            return
        try:
            why = lease.refusal()
        except Exception as e:                        # noqa: BLE001 - a broken lease refuses
            why = f"the lease could not be checked: {e}"
        if why is None:
            return
        log.warning("unkeying %s: %s", self.device, why)
        self._lease = None
        self._unkey_now()

    @property
    def lease_holder(self) -> str | None:
        lease = self._lease
        return getattr(lease, "holder", None) if lease is not None else None

    def _keyed_under_lease(self) -> bool:
        lease = self._lease
        if lease is None:
            return False
        try:
            return lease.refusal() is None
        except Exception:                             # noqa: BLE001
            return False

    def ptt_line_up(self) -> bool | None:
        """Whether this rig's PTT line is up now, as the port last set it;
        None for VOX and none (no line to read)."""
        port = self._ptt_port
        if port is None or self.ptt_route not in ("rts", "dtr", "cm108"):
            return None
        state = getattr(port, "line_state", {})
        return bool(state.get("ptt" if self.ptt_route == "cm108" else self.ptt_route))

    # -- audio (YaesuRig's, through the interface's sound card) ---------------------------

    def _start_input(self) -> None:
        if not self.audio_in or self._input is not None:
            return
        from .. import sound
        from ..usbaudio import AudioError
        try:
            self._input = sound.Input(self.audio_in, self._on_audio, backend=self._sound_backend)
        except AudioError as e:
            log.warning("receive audio: %s", e)
            self._set(error=str(e))
            return
        self._set(audio=True, audio_rate=self._input.rate)

    def _on_audio(self, pcm: bytes) -> None:
        for fn in list(self._audio_listeners):
            try:
                fn(pcm)
            except Exception:                         # noqa: BLE001
                log.exception("audio listener")

    def listen_audio(self, fn, receiver: str | None = None):
        """Subscribe to the receive audio; returns the unsubscribe. `fn` runs
        on the sound card's capture thread and must only take the block."""
        self._audio_listeners.append(fn)
        if self.state["connected"]:
            self._start_input()
        return lambda: self._audio_listeners.remove(fn) if fn in self._audio_listeners else None

    def receivers(self) -> tuple:
        return ()

    def primary_receiver(self) -> str:
        return ""

    def audio_stats(self) -> dict:
        inp = self._input
        return {"enabled": bool(self.audio_in), "rate": inp.rate if inp else None, "running": inp is not None,
                "packets": inp.blocks if inp else 0, "dropped": inp.dropped if inp else 0}

    def _sink(self):
        if self._tx is None and self.audio_out:
            from ..txaudio import SoundOut
            rate = self.state.get("audio_rate") or 0
            self._tx = SoundOut(self.audio_out, backend=self._sound_backend, rate=rate)
        return self._tx

    @property
    def tx_rate(self) -> int:
        if not self.audio_out:
            return 0
        return int(self.state.get("audio_rate") or 0)

    @property
    def tx_audio_ready(self) -> bool:
        return bool(self.audio_out) and bool(self.tx_rate)

    @property
    def tx_drain_s(self) -> float:
        out = self._tx
        return out.queued_s() if out is not None else 0.0

    def send_audio(self, pcm: bytes) -> bool:
        """Transmit audio at `tx_rate`, refused unless keyed under a live
        lease (lease.py: an interface's VOX would otherwise key on audio
        alone)."""
        if not self._keyed_under_lease() or not self.tx_rate:
            return False
        sink = self._sink()
        return bool(sink is not None and sink.send_audio(pcm))

    def tx_end(self) -> None:
        out = self._tx
        if out is not None:
            out.tx_end()
