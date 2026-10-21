"""A Kenwood TM-V71 as a packet radio: its PC port for control, its DATA
jack through a sound-card interface for audio and PTT.

Through shack_rig's KenwoodRig, called directly as YaesuRig is (both are
thread-safe). Two cables where the Icom and the FTX-1 need one, because
that is the radio: the PC port (8-pin mini-DIN, menu 519's speed) tunes
and reads it; the 6-pin mini-DIN DATA jack carries the packet - PKD in,
PR9 (discriminator, flat: 9600) or PR1 (1200) out, PKS to key, SQC - to an
interface such as a Digirig or a SignaLink.

**What opening does to the radio** is shack_rig.kenwood.datasetup's list,
read, saved, set and put back on close by shack_rig.datasetup (rig.py says
how). First the band: the one whose VHF/UHF segment already covers the
channel (the TM-V71 cannot be moved between them by command), refused with
a sentence when neither does. Then that band into VFO mode, the frequency,
FM, simplex, no tone, and the DATA jack's menu items EXT. DATA BAND (517)
to that band and EXT. DATA SPEED (518) to the modem's speed - which also
sets PKD's input sensitivity (40 mVp-p at 1200, 2 Vp-p at 9600).

**The modem** is the link's `speed`: 1200 is AFSK, 9600 is G3RUH FSK
(modem.py) through the interface's sound card at its own rate (48 kHz on
nearly every USB interface: five samples a bit). `baud` tells the engine,
which refuses a channel that disagrees.

**Keying** is the interface's, through the rig under this link's lease
(rig.py): RTS or DTR on its serial port, a CM108 GPIO pin, or its VOX.
Never a CAT transmit - shack_rig.kenwood.cat says why - so an unkey is
confirmed by the PTT line's own state (`_radio_ptt_readback`), there being
no command that reads the radio's transmitter. With VOX there is no line
and no confirmation; the radio unkeys when the audio stops.

**Turnaround** is not in any source and nobody here has measured a TM-V71:
0.15 s is assumed (a line through an interface keys in milliseconds; the
rest is the radio's synthesiser and PA), until turnaround.py measures it.
"""

from __future__ import annotations

import logging

from .base import LinkRefused
from .rig import RigLink, sdr_hub_warnings, wait_for

log = logging.getLogger(__name__)

PTT_MODES = ("rts", "dtr", "cm108", "vox", "none")
SPEEDS = (1200, 9600)


class KenwoodLink(RigLink):
    """`device` is the radio's PC port; `audio` the interface's sound
    device (both ways, or `audio_out` for a separate output); `ptt` one of
    PTT_MODES, on `ptt_device` (the interface's serial port, or its CM108
    HID device); `speed` 1200 or 9600. `pc_baud` the PC port's speed (0:
    the model's factory 9600). `sound_backend`, `open_port`, `open_cm108`
    and `rig` are for tests."""

    DEFAULT_TURNAROUND_S = 0.15
    VOX_TURNAROUND_S = 0.2
    CONNECT_S = 10.0

    def __init__(self, device: str, *, model: str = "tm-v71", audio: str = "", audio_out: str = "",
                 ptt: str = "rts", ptt_device: str = "", cm108_gpio: int = 3, speed: int = 1200,
                 pc_baud: int = 0, sound_backend=None, open_port=None, open_cm108=None, rig=None,
                 connect_s: float | None = None, turnaround_s: float | None = None, **kw):
        if ptt not in PTT_MODES:
            raise LinkRefused("setup", f"PTT is one of {', '.join(PTT_MODES)}, not {ptt!r}")
        if ptt in ("rts", "dtr") and not ptt_device:
            raise LinkRefused("setup", f"PTT by {ptt.upper()} needs the interface's serial port")
        if speed not in SPEEDS:
            raise LinkRefused("setup", f"the TM-V71's DATA jack runs at 1200 or 9600, not {speed!r}")
        super().__init__(turnaround_s=turnaround_s, **kw)
        if ptt == "vox" and not self.turnaround_measured:
            self.turnaround_s = self.VOX_TURNAROUND_S
        if ptt == "vox":
            self.CONFIRMS_UNKEY = False             # no line to read back
        from shack_rig import sound
        from shack_rig.usbaudio import AudioError
        self.device = device
        self.model = model
        self.audio_in = audio
        self.audio_out = audio_out or audio
        self.ptt = ptt
        self.ptt_device = ptt_device
        self.cm108_gpio = cm108_gpio
        self.speed = speed
        #: The engine's modem (channel.ChannelConfig takes the link's).
        self.baud = speed
        self.pc_baud = pc_baud
        self._backend = sound_backend
        self._open_port = open_port
        self._open_cm108 = open_cm108
        self._given_rig = rig
        self.connect_s = self.CONNECT_S if connect_s is None else connect_s
        self._controls = None
        if not audio:
            raise LinkRefused("setup", "the interface's sound device is needed for packet")
        try:
            self.rate = sound.native_rate(sound.find(audio, False, sound_backend), False, 0, sound_backend)
        except AudioError as e:
            raise LinkRefused("setup", str(e)) from None

    def _call(self, fn, *args, timeout: float = 2.0):
        return fn(*args)

    def _radio_ptt_readback(self) -> bool | None:
        rig = self.rig
        return None if rig is None else rig.ptt_line_up()

    def _open_radio(self) -> None:
        from shack_rig.kenwood import KenwoodRig
        rig = self._given_rig or KenwoodRig(
            self.device, self.model, baud=self.pc_baud, ptt=self.ptt if self.transmit else "none",
            ptt_device=self.ptt_device, cm108_gpio=self.cm108_gpio, allow_ptt=self.transmit,
            poll_interval=0.1, audio_in=self.audio_in, audio_out=self.audio_out if self.transmit else "",
            sound_backend=self._backend, open_port=self._open_port, open_cm108=self._open_cm108)
        self.rig = rig
        rig.open()
        try:
            if not wait_for(lambda: rig.state.get("connected") and rig.state.get("freq"), self.connect_s):
                raise LinkRefused("radio-error", str(rig.state.get("error") or "the radio did not answer"))
            self._choose_band()
            self._prepare()
            self._rig_ready()
            if self.ptt == "vox" and self.transmit:
                self._event("radio", "vox", {"turnaround_s": self.turnaround_s})
            if not wait_for(lambda: rig.state.get("audio_rate"), 2.0) or rig.state["audio_rate"] != self.rate:
                raise LinkRefused("setup", f"the interface's sound device runs at {rig.state.get('audio_rate')} "
                                           f"Hz, not {self.rate}")
        except Exception:
            self._shutdown_rig()
            raise

    def _choose_band(self) -> None:
        from shack_rig.kenwood import datasetup
        if not self.frequency:
            return
        band = datasetup.choose_band(self.rig, int(self.frequency))
        if band is None:
            mhz = f"{int(self.frequency) / 1e6:.4f}"
            raise LinkRefused("out-of-range", f"neither band of the TM-V71 is on {mhz} MHz's band, and it cannot be "
                                      f"moved there over the PC port: press BAND on band A or B until it shows "
                                      f"that band, then open the radio again")
        self.rig.use_band(band)
        self.setup["band"] = "AB"[band]

    def _prepare(self) -> None:
        from shack_rig.kenwood import datasetup
        settings = datasetup.settings(self.rig, frequency=int(self.frequency) if self.frequency else None,
                                      speed=self.speed)
        if not self.simplex:
            settings = [s for s in settings if s.key not in ("shift", "reverse", "tones")]
        self.setup_warnings = sdr_hub_warnings(self.audio_in)
        self._apply_data_setup(settings, "kenwood", self.device)

    # -- the control strip --------------------------------------------------------------------

    def _control_settings(self) -> dict:
        """RF power, H/M/L (PC), turned through the data set-up's journal;
        the squelch is read only - the TM-V71's is the operator's knob, and
        the app reads busy from it."""
        if self._controls is None:
            from shack_rig.datasetup import Setting
            from shack_rig.kenwood import datasetup
            self._controls = {"power_level": datasetup.power_setting(self.rig),
                              "squelch": Setting("squelch", None, self.rig.read_squelch, None)}
        return self._controls

    def control_choices(self, key: str):
        """A control set by name rather than level: power's H, M and L, as
        (raw, name) in the radio's order."""
        if key == "power_level":
            from shack_rig.kenwood import cat
            return list(cat.POWER_LEVELS.items())
        return None

    def control_range(self, key: str) -> int:
        """The top of a level control's raw scale: the squelch is 00-1F."""
        return 31 if key == "squelch" else super().control_range(key)

    def control_writable(self, key: str) -> bool:
        return key != "squelch"

    def _read_meters(self, transmitting: bool) -> dict:
        """No S-meter command exists (cat.py): the squelch is the reading."""
        rig = self.rig
        if transmitting:
            return {"keyed": True}
        return {"busy": rig.state.get("busy")} if rig is not None else {}

    def _close_radio(self) -> None:
        self._controls = None
        self._shutdown_rig()

    def _shutdown_rig(self) -> None:
        rig = self.rig
        if rig is None:
            return
        self._rig_release()
        try:
            rig.set_ptt(False)
        except Exception:                           # noqa: BLE001 - closing regardless
            log.exception("unkeying the radio")
        try:
            self._restore_data_setup()
        except Exception:                           # noqa: BLE001
            log.exception("putting the radio's settings back")
        try:
            rig.watch_squelch(False)
        except Exception:                           # noqa: BLE001
            log.exception("putting the radio back")
        rig.close()
