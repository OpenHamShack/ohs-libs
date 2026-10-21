"""A Yaesu on its CAT port: the second `Rig` backend.

The same surface as IcomRig (shack_rig/rig.py's contract: a state cache one
poll loop keeps current, `listen`, `set_*` returning False when nothing was
sent, keying only under a lease), over the FTX-1's Enhanced COM port
(CAT-1) instead of CI-V. Threads rather than asyncio: a CAT port on Windows
is a pyserial object no event loop can watch (serialio.py), and a desktop
app - the packet app first - has no loop of its own to lend. `start()` and
`stop()` are still coroutines, so an asyncio app drives it as it drives an
IcomRig.

**Keying.** `set_ptt(True, lease)` is the only way, with the same checks in
the same order as IcomRig.set_ptt: the lease, `allow_ptt`, a known
frequency inside the model's transmit bands. Then either CAT (`TX1;`, the
default: one cable, nothing to configure on the radio) or a modem line on
the radio's *Standard* COM port (`ptt="rts"`/`"dtr"`, for an operator whose
radio has RPTT SELECT set to that line). While keyed the poll asks the
lease again every cycle and unkeys on the first refusal. Unkeying never
needs a lease, is always attempted on both routes (`TX0;` and the line
low), and one that could not be sent is owed and retried every cycle.

**The FTX-1 manual's p. 2 trap.** "When using the USB cable for TX control,
the transceiver may switch to the transmit mode when the computer is
started": a port opened with RTS or DTR up keys a radio whose RPTT SELECT
names that line. Every port here is opened by serialio, which lowers both
lines on open (and opens them low on Windows), and the Standard port is
opened at all only when a line is the configured PTT. Nothing but
`set_ptt` raises a line.

**Busy** is RI's P8, the squelch (p. 22), read on every poll while
`watch_squelch` is on: a packet station's carrier sense, as IcomRig's 15 01.

**Audio** is the radio's USB codec through shack_rig.sound, at the codec's
own rate both ways and with no resampling at all (`audio_rate` and
`tx_rate` are the same number): the codec's rate is not in the CAT manual,
and whatever it is, the packet modem runs at it.

**Menu writes** are MOD SOURCE only, at the model table's items, read first
and put back by `restore_mod_source` - as IcomRig writes only DATA MOD.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable

from .. import lease as txlease
from ..i18n import _
from ..lease import PttRefused, TxLease
from ..model import mode_label
from ..serialio import SerialError, SerialPort
from . import cat
from .models import DEFAULT_MODEL, YaesuModel, model as find_model

log = logging.getLogger("shack_rig.yaesu")

PTT_ROUTES = ("cat", "rts", "dtr")

#: How long to wait for an answer to a read. The radio answers in a few
#: milliseconds at 38400; this is paid only by a question it ignores.
ANSWER_TIMEOUT_S = 0.5

#: With nothing at all heard for this long the link is called down and
#: rebuilt by the supervisor - a radio switched off, a cable pulled.
SILENT_S = 3.0


class YaesuRig:
    """One Yaesu. `device` is the Enhanced COM (CAT-1) port; `ptt_device`
    the Standard COM port, used only for a line PTT.

    `open_port(device, baud, stop_bits)` makes a port; serialio.SerialPort
    unless a test hands in its own (a PTT line needs real modem lines,
    which a pty has not got).
    """

    def __init__(self, device: str, model: YaesuModel | str | None = None, *, baud: int = 0,
                 stop_bits: int = 0, ptt: str = "cat", ptt_device: str = "", allow_ptt: bool = False,
                 poll_interval: float = 0.25, audio_in: str = "", audio_out: str = "",
                 sound_backend=None, open_port: Callable[..., Any] | None = None):
        if isinstance(model, str) or model is None:
            model = find_model(model or DEFAULT_MODEL)
        if ptt not in PTT_ROUTES:
            raise ValueError(_("PTT is one of {routes}, not {ptt}").format(routes=", ".join(PTT_ROUTES),
                                                                              ptt=repr(ptt)))
        self.device = device
        self.model = model
        self.baud = baud or model.cat_baud
        self.stop_bits = stop_bits or model.cat_stop_bits
        self.ptt_route = ptt
        self.ptt_device = ptt_device
        self.allow_ptt = allow_ptt
        self.poll_interval = poll_interval
        self.audio_in = audio_in
        self.audio_out = audio_out
        self._sound_backend = sound_backend
        self._open_port = open_port or (lambda dev, baud, bits: SerialPort(dev, baud, stop_bits=bits))

        self.state: dict[str, Any] = {
            "connected": False, "wanted": True, "link_state": "connecting", "error": None,
            "radio": None, "freq": None, "mode": None, "data_mode": False, "mode_label": None,
            "filter": None, "ptt": False, "vfo": "VFOA", "split": False, "smeter": None,
            "smeter_db": None, "preamp": None, "preamp_mode": None, "att": None, "power": None,
            "swr": None, "audio": False, "audio_rate": None, "scope": False, "scope_points": None,
            "updated": 0.0,
            # Yaesu's own: the squelch as carrier sense (RI P8), None until
            # read and while not watching; TX's answer (0, 1 CAT, 2 the radio
            # itself); MOD SOURCE per block as read; the last IF, parsed.
            "busy": None, "tx_state": None, "mod_source": {}, "info": None,
        }
        self._listeners: list[Callable[[dict], None]] = []
        self._audio_listeners: list[Callable[[bytes], None]] = []
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._port = None
        self._ptt_port = None
        self._buf = ""
        self._waiters: dict[str, list] = {}
        self._last_heard = 0.0
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lease: TxLease | None = None
        self._unkey_owed = False
        self._watch_squelch = False
        self._mod_saved: dict[str, int] = {}
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
        for target, name in ((self._supervise, "cat-supervisor"), (self._poll, "cat-poll")):
            t = threading.Thread(target=target, name=f"{name} {self.device}", daemon=True)
            t.start()
            self._threads.append(t)

    def close(self) -> None:
        """Unkey, put the menu back, and let go of every port and device."""
        self._stop.set()
        self._wake.set()
        self._unkey_now()
        self.restore_mod_source()
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
        return {"device": self.device, "baud": self.baud, "stop_bits": self.stop_bits, "ptt": self.ptt_route,
                "ptt_device": self.ptt_device or None, "refused": self.refused}

    def _connect(self) -> None:
        port = self._open_port(self.device, self.baud, self.stop_bits)
        port.open()
        ptt_port = None
        if self.ptt_route != "cat":
            # The Standard COM port, opened only because a line on it is the
            # PTT, and opened by serialio with both lines low (module docstring).
            try:
                ptt_port = self._open_port(self.ptt_device, self.model.cat_baud, 1)
                ptt_port.open()
            except Exception:
                port.close()
                raise
        with self._lock:
            self._port, self._ptt_port = port, ptt_port
            self._buf = ""
        self._last_heard = time.monotonic()
        t = threading.Thread(target=self._read, args=(port,), name=f"cat-read {self.device}", daemon=True)
        t.start()
        got = self.ask(cat.read_id(), "ID", timeout=1.0)
        if got is None:
            self._teardown()
            raise SerialError(_("{device} opened and the radio did not answer; check the CAT port, its speed "
                                "({baud}) and that the radio is on").format(device=self.device, baud=self.baud))
        rid = got[2:-1]
        if self.model.cat_id and rid != self.model.cat_id:
            self._teardown()
            raise SerialError(_("the radio on {device} says it is ID {got}, not an {name} ({want})").format(
                device=self.device, got=rid, name=self.model.name, want=self.model.cat_id))
        self._set(connected=True, radio=self.model.name, error=None)
        log.info("%s on %s at %d baud, PTT by %s", self.model.name, self.device, self.baud, self.ptt_route)

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
        """Write one command. False if there is no port or it would not go."""
        port = self._port
        if port is None:
            return False
        with self._write_lock:
            return bool(port.write(command.encode("ascii")))

    def ask(self, command: str, prefix: str, timeout: float = ANSWER_TIMEOUT_S) -> str | None:
        """Send a read and wait for the answer that starts with `prefix`."""
        box: list[str] = []
        done = threading.Event()
        entry = (box, done)
        with self._lock:
            self._waiters.setdefault(prefix, []).append(entry)
        try:
            if not self.send(command):
                return None
            done.wait(timeout)
            return box[0] if box else None
        finally:
            with self._lock:
                waiting = self._waiters.get(prefix, [])
                if entry in waiting:
                    waiting.remove(entry)

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
                self._answer(a.strip())

    def _answer(self, a: str) -> None:
        if a == cat.REFUSED:
            self.refused += 1
            log.debug("the radio refused a command")
            return
        try:
            if a.startswith("FA"):
                self._set(freq=cat.parse_freq(a))
            elif a.startswith("MD"):
                side, code = cat.parse_mode(a)
                if side == 0:
                    mode, data, narrow = cat.vocabulary(code)
                    self._set(mode=mode, data_mode=data, mode_label=mode_label(mode, data) if mode else
                              cat.MODE_CODES.get(code), filter="NAR" if narrow else None)
            elif a.startswith("TX"):
                tx = cat.parse_tx(a)
                self._set(tx_state=tx, ptt=tx != 0)
            elif a.startswith("RI"):
                ri = cat.parse_radio_info(a)
                self._set(busy=ri.busy if self._watch_squelch else None, ptt=ri.tx == 1)
            elif a.startswith("SM"):
                raw = cat.parse_smeter(a)
                self._set(smeter=raw)
            elif a.startswith("IF") and self.model.if_layout == "ftx1":
                info = cat.parse_if(a)
                self._set(info=info.__dict__ | {"mode": info.mode, "data": info.data}, freq=info.freq)
            elif a.startswith("EX"):
                where, value = cat.parse_menu(a)
                for block, item in self.model.mod_source.items():
                    if (item.p1, item.p2, item.p3) == where and value.isdigit():
                        self._mod_saved.setdefault(block, int(value))
                        mod = dict(self.state["mod_source"])
                        mod[block] = item.name(int(value)) or value
                        self._set(mod_source=mod)
        except cat.CatError as e:
            log.debug("%s", e)
        with self._lock:
            for prefix in list(self._waiters):
                if a.startswith(prefix):
                    for box, done in self._waiters.pop(prefix):
                        box.append(a)
                        done.set()

    # -- the poll ---------------------------------------------------------------------

    def _poll(self) -> None:
        tick = 0
        while not self._stop.is_set():
            time.sleep(self.poll_interval)
            if self._port is None or not self.state["connected"]:
                continue
            try:
                self._watch_lease()
                self.send(cat.read_freq())
                self.send(cat.read_mode())
                self.send(cat.read_tx())
                if self._watch_squelch and self.model.has_radio_info:
                    self.send(cat.read_radio_info())
                tick += 1
                if tick % 2 == 0 and not self.state["ptt"]:
                    self.send(cat.read_smeter())
            except Exception:                         # noqa: BLE001 - a poll must not die
                log.exception("poll")

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
        if not self.model.covers(hz) or not self.send(cat.set_freq(hz)):
            return False
        self._set(freq=hz)
        return True

    def set_mode(self, mode: str, filt: int | None = None, data: bool | None = None,
                 vfo: int | None = None) -> bool:
        from ..model import split_mode_label
        mode, suffix = split_mode_label(mode)
        if suffix:
            data = True
        if data is None:
            data = bool(self.state.get("data_mode"))
        narrow = filt == 2
        code = cat.code_for(mode, data, narrow)
        if code is None or (not data and not self.model.has_mode(mode)):
            return False
        if not self.send(cat.set_mode(code)):
            return False
        self._set(mode=mode, data_mode=data, mode_label=mode_label(mode, data), filter="NAR" if narrow else None)
        return True

    def set_data(self, on: bool) -> bool:
        mode = self.state.get("mode")
        return bool(mode) and self.set_mode(mode, data=on)

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

    def watch_squelch(self, on: bool = True) -> None:
        """Read RI (the squelch) on every poll while on, into `state["busy"]`."""
        self._watch_squelch = bool(on) and self.model.has_radio_info
        if not self._watch_squelch:
            self._set(busy=None)

    def set_simplex(self) -> bool:
        """CTCSS/DCS off (CT00;) and the repeater shift off (OS00;, FM only)."""
        ok = self.send(cat.tones_off())
        return self.send(cat.simplex()) and ok

    def mod_source_refusal(self) -> str | None:
        if not self.model.mod_source:
            return _("this library does not know where the {radio} keeps its DATA modulation source; set it "
                     "to USB on the radio").format(radio=self.model.name)
        return None

    def read_mod_source(self, timeout: float = ANSWER_TIMEOUT_S) -> dict:
        """Read every MOD SOURCE row the model names; {block: name}."""
        for item in self.model.mod_source.values():
            self.ask(cat.read_menu(item.p1, item.p2, item.p3), f"EX{item.p1:02d}{item.p2:02d}{item.p3:02d}",
                     timeout)
        return dict(self.state["mod_source"])

    def set_mod_source(self, source: str = "usb") -> bool:
        """Every MOD SOURCE row to `source`, after each has been read (and
        kept, for restore_mod_source). False if any was not read or written."""
        if self.mod_source_refusal() is not None:
            return False
        ok = True
        for block, item in self.model.mod_source.items():
            value = item.value(source)
            if value is None or block not in self._mod_saved or not item.confirmed:
                ok = False
                continue
            if self.send(cat.set_menu(item.p1, item.p2, item.p3, value, item.digits)):
                mod = dict(self.state["mod_source"])
                mod[block] = source
                self._set(mod_source=mod)
            else:
                ok = False
        return ok

    def restore_mod_source(self) -> bool:
        ok = True
        for block, saved in list(self._mod_saved.items()):
            item = self.model.mod_source.get(block)
            if item is None or self.state["mod_source"].get(block) == item.name(saved):
                continue
            if self.send(cat.set_menu(item.p1, item.p2, item.p3, saved, item.digits)):
                mod = dict(self.state["mod_source"])
                mod[block] = item.name(saved)
                self._set(mod_source=mod)
            else:
                ok = False
        return ok

    # -- keying ---------------------------------------------------------------------------

    def set_ptt(self, on: bool, lease: TxLease | None = None) -> bool:
        """Key under `lease`, or unkey. See the module docstring."""
        if not on:
            self._lease = None
            return self._unkey_now()
        txlease.check(lease)
        if not self.allow_ptt:
            raise PttRefused(_("transmitting is switched off for this rig"))
        freq = self.state.get("freq")
        if freq is None:
            raise PttRefused(_("the rig's frequency is not known yet, so it is not keyed"))
        if not self.model.can_transmit(freq):
            raise PttRefused(_("{mhz} MHz is outside the {radio}'s transmit bands ({bands})").format(
                mhz=f"{freq / 1e6:.6f}", radio=self.model.name, bands=self.model.transmit_bands_text()))
        if self.ptt_route == "cat":
            sent = self.send(cat.set_tx(True))
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
        """Both routes, always: TX0; and the PTT line low."""
        ok = self.send(cat.set_tx(False))
        port = self._ptt_port
        if port is not None:
            ok = bool(port.set_line(self.ptt_route, False)) and ok
        elif self.ptt_route != "cat":
            # No Standard port open means no line can be up from us.
            pass
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

    # -- audio ------------------------------------------------------------------------------

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
        on the sound card's capture thread and must only take the block and
        return: work done there is audio the card overruns (IcomRig's
        listen_audio says the same for its loop, where it cost CI-V)."""
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
        lease (lease.py: VOX with USB modulation would otherwise key on
        audio alone)."""
        if not self._keyed_under_lease() or not self.tx_rate:
            return False
        sink = self._sink()
        return bool(sink is not None and sink.send_audio(pcm))

    def tx_end(self) -> None:
        out = self._tx
        if out is not None:
            out.tx_end()
