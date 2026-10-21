"""A Yaesu as a packet radio: the FTX-1 over its USB cable.

Through shack_rig's YaesuRig: CAT on the Enhanced COM port (CAT-1), PTT by
`TX1;` unless the operator has chosen a line on the Standard COM port (and
set RPTT SELECT on the radio to match), the radio's USB codec through
shack_rig.sound. YaesuRig is thread-safe and called directly.

**What opening does to the radio** is shack_rig.yaesu.datasetup's list,
read, saved, set and put back on close by shack_rig.datasetup (rig.py says
how): the frequency, DATA-FM (`MD0A;`), simplex and tones off (`OS00;`,
`CT00;`, both read first and put back, as the FTX-1 reports them), DNF,
DNR, NB, VOX, monitor and the processor off, MOD SOURCE to USB in both the
FM and the DATA menu blocks (the manual does not say which one DATA-FM
reads; models.py), and USB MOD GAIN read (set only when a level is given).
Then the squelch (RI P8) is read as busy and receive audio goes to the
engine.

**Audio** runs at the codec's own rate both ways - the CAT manual does not
give it, and nothing here needs to know it in advance: the link reads the
device's rate when it is made (shack_rig.sound.native_rate, the same rule
YaesuRig opens the device by), and the modem runs at whatever that is.

**Turnaround** is not in the manual and nobody here has measured an FTX-1:
0.15 s is assumed (a CAT key at 38400 baud is under 2 ms on the wire;
the rest is the radio's), until turnaround.py measures it.
"""

from __future__ import annotations

import logging

from .base import LinkRefused
from .rig import RigLink, sdr_hub_warnings, wait_for

log = logging.getLogger(__name__)


class YaesuLink(RigLink):
    """`device` is the Enhanced COM port; `audio` the codec's name (both ways,
    or `audio_out` for a separate output); `ptt` "cat", "rts" or "dtr", the
    line on `ptt_device`, the Standard COM port. `sound_backend`,
    `open_port` and `rig` are for tests."""

    DEFAULT_TURNAROUND_S = 0.15
    CONNECT_S = 10.0

    def __init__(self, device: str, *, model: str = "ftx-1", audio: str = "", audio_out: str = "",
                 ptt: str = "cat", ptt_device: str = "", baud: int = 0, sound_backend=None, open_port=None,
                 rig=None, connect_s: float | None = None, **kw):
        super().__init__(**kw)
        from shack_rig import sound
        from shack_rig.usbaudio import AudioError
        self.device = device
        self.model = model
        self.audio_in = audio
        self.audio_out = audio_out or audio
        self.ptt = ptt
        self.ptt_device = ptt_device
        self.baud = baud
        self._backend = sound_backend
        self._open_port = open_port
        self._given_rig = rig
        self.connect_s = self.CONNECT_S if connect_s is None else connect_s
        if not audio:
            raise LinkRefused("setup", "the radio's sound device is needed for packet")
        try:
            self.rate = sound.native_rate(sound.find(audio, False, sound_backend), False, 0, sound_backend)
        except AudioError as e:
            raise LinkRefused("setup", str(e)) from None

    def _call(self, fn, *args, timeout: float = 2.0):
        return fn(*args)

    def _radio_ptt_readback(self) -> bool | None:
        from shack_rig.yaesu import cat
        rig = self.rig
        if rig is None:
            return None
        got = rig.ask(cat.read_tx(), "TX", 0.5)
        if got is None:
            return None
        return cat.parse_tx(got) != 0

    def _open_radio(self) -> None:
        from shack_rig.yaesu import YaesuRig
        rig = self._given_rig or YaesuRig(
            self.device, self.model, baud=self.baud, ptt=self.ptt, ptt_device=self.ptt_device,
            allow_ptt=self.transmit, poll_interval=0.1, audio_in=self.audio_in,
            audio_out=self.audio_out if self.transmit else "", sound_backend=self._backend,
            open_port=self._open_port)
        self.rig = rig
        rig.open()
        try:
            if not wait_for(lambda: rig.state.get("connected") and rig.state.get("freq"), self.connect_s):
                raise LinkRefused("radio-error", str(rig.state.get("error") or "the radio did not answer"))
            self._prepare()
            self._rig_ready()
            if not wait_for(lambda: rig.state.get("audio_rate"), 2.0) or rig.state["audio_rate"] != self.rate:
                raise LinkRefused("setup", f"the radio's sound device runs at {rig.state.get('audio_rate')} Hz, "
                                           f"not {self.rate}")
        except Exception:
            self._shutdown_rig()
            raise

    def _prepare(self) -> None:
        from shack_rig.yaesu import datasetup
        gain = None if self.mod_level_percent is None else max(0, min(100, round(self.mod_level_percent)))
        settings = datasetup.settings(self.rig, frequency=int(self.frequency) if self.frequency else None,
                                      mod_gain=gain)
        if not self.simplex:
            settings = [s for s in settings if s.key not in ("shift", "tones")]
        self.setup_warnings = sdr_hub_warnings(self.audio_in)
        self._apply_data_setup(settings, "yaesu", self.device)

    def _close_radio(self) -> None:
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
