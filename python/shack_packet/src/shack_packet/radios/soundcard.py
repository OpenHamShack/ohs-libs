"""Any radio on a sound card: audio in and out, and PTT by a serial line,
by VOX, or not at all.

The generic link, for a radio this app has no CAT for - a handheld on a
SignaLink, a mobile on a home-made interface, an old TNC-less 2 m rig. The
operator sets the frequency and mode on the radio; this only moves audio
and keys.

**Audio** goes through shack_rig.sound (PortAudio, the family's desktop
route on Mac, Windows and Linux; openhamshack-nb ohscore/audio.py), both devices
named by the operator and never the system default. The link runs at the
input device's own rate - 48 kHz on nearly every USB interface - and the
output is opened at the same rate or refused: the packet modem runs at any
rate, so nothing here resamples at all. The output is opened when a
transmission starts and closed once it has played (openhamshack-nb's TxOut rule:
an output left open would put whatever reached it on the radio's
modulator).

**PTT**:

- `rts` / `dtr`: a line on a serial port (`ptt_device`) - the commonest
  interface wiring. The port is opened by shack_rig.serialio with both lines
  low (on Windows, opened with them low), and nothing but `_radio_key`
  raises the configured one. Yaesu's FTX-1 manual warns that a port opened
  with the line up keys the radio (p. 2), and so does every interface whose
  PTT is that line.
- `vox`: no PTT at all; the radio keys itself on the audio. Allowed, with a
  warning (REASONS "vox") reported when the link opens: the radio keys
  after its VOX attack time, which eats the start of the preamble unless
  `turnaround_s` says how long it is, and it holds the carrier for its own
  hang time after the audio stops, which this station cannot shorten - so a
  station answering at once may be talking into a transmitter still keyed.
  Set the radio's VOX delay as short as it goes.
- `none`: receive-only. key() refuses ("receive-only").
"""

from __future__ import annotations

import logging

from .base import AudioRadioLink, LinkRefused

log = logging.getLogger(__name__)

PTT_MODES = ("rts", "dtr", "vox", "none")


class SoundcardLink(AudioRadioLink):
    """`input` and `output` are sound device names (shack_rig.sound.find);
    `ptt` one of PTT_MODES; `ptt_device` the serial port for rts/dtr.

    `sound_backend` and `open_port(device)` are for tests: a FakeBackend,
    and a stand-in port (a pty has no modem lines)."""

    #: A line through an interface keys in a few milliseconds and a radio's
    #: transmitter comes up in tens; assumed until measured (turnaround.py).
    DEFAULT_TURNAROUND_S = 0.1
    #: VOX: most radios' attack is shorter than this; measure it.
    VOX_TURNAROUND_S = 0.15

    def __init__(self, input: str, output: str = "", *, ptt: str = "rts", ptt_device: str = "",
                 rate: int = 0, sound_backend=None, open_port=None, turnaround_s: float | None = None,
                 **kw):
        if ptt not in PTT_MODES:
            raise LinkRefused("setup", f"PTT is one of {', '.join(PTT_MODES)}, not {ptt!r}")
        if ptt in ("rts", "dtr") and not ptt_device:
            raise LinkRefused("setup", f"PTT by {ptt.upper()} needs the serial port it is on")
        super().__init__(turnaround_s=turnaround_s, **kw)
        if ptt == "vox" and not self.turnaround_measured:
            self.turnaround_s = self.VOX_TURNAROUND_S
        from shack_rig import sound
        from shack_rig.usbaudio import AudioError
        self.input_name = input
        self.output_name = output or input
        self.ptt = ptt
        self.ptt_device = ptt_device
        self._sound = sound
        self._backend = sound_backend
        self._open_port = open_port
        self._port = None
        self._input = None
        self._output = None
        try:
            dev = sound.find(input, False, sound_backend)
            self.rate = rate or sound.native_rate(dev, False, 0, sound_backend)
            out = sound.find(self.output_name, True, sound_backend) if ptt != "none" else None
            be = sound_backend or sound.default_backend()
        except AudioError as e:
            raise LinkRefused("setup", str(e)) from None
        if out is not None and not (be.supports(out, self.rate, 1, True) or be.supports(out, self.rate, 2, True)):
            # One rate both ways, because the engine has one modem rate.
            raise LinkRefused("setup", f"{out.name} will not play at {self.rate} Hz, the input's rate")

    # -- lifetime -------------------------------------------------------------------

    def _open_radio(self) -> None:
        from shack_rig.usbaudio import AudioError
        if self.ptt in ("rts", "dtr"):
            if self._open_port is not None:
                port = self._open_port(self.ptt_device)
            else:
                from shack_rig.serialio import SerialPort
                port = SerialPort(self.ptt_device, 9600)
            try:
                port.open()
            except Exception as e:                  # noqa: BLE001 - SerialError's sentence
                raise LinkRefused("setup", str(e)) from None
            self._port = port
        try:
            self._input = self._sound.Input(self.input_name, self._rx, rate=self.rate, backend=self._backend)
        except AudioError as e:
            if self._port is not None:
                self._port.close()
                self._port = None
            raise LinkRefused("setup", str(e)) from None
        if self.ptt == "vox":
            self._event("radio", "vox", {"turnaround_s": self.turnaround_s})

    def _close_radio(self) -> None:
        for thing in (self._output, self._input, self._port):
            if thing is not None:
                try:
                    thing.close()
                except Exception:                   # noqa: BLE001
                    log.exception("closing")
        self._output = self._input = self._port = None

    # -- keying ---------------------------------------------------------------------

    def _radio_key(self) -> None:
        from shack_rig.usbaudio import AudioError
        if self.ptt == "none":
            raise LinkRefused("receive-only")
        if self._output is None:
            try:
                self._output = self._sound.Output(self.output_name, rate=self.rate, backend=self._backend)
            except AudioError as e:
                raise LinkRefused("radio-refused", str(e)) from None
        if self.ptt in ("rts", "dtr"):
            if not self._port.set_line(self.ptt, True):
                raise LinkRefused("radio-refused", f"{self.ptt.upper()} could not be raised on {self.ptt_device}")

    def _radio_unkey(self) -> None:
        try:
            if self._port is not None and self.ptt in ("rts", "dtr"):
                if not self._port.set_line(self.ptt, False) and self._port.line_state.get(self.ptt):
                    raise OSError(f"{self.ptt.upper()} could not be lowered on {self.ptt_device}")
        finally:
            out, self._output = self._output, None
            if out is not None:
                out.clear()
                out.close()

    def _radio_audio(self, pcm: bytes) -> bool:
        out = self._output
        return out is not None and out.write(pcm)

    def _radio_audio_end(self) -> None:
        if self._output is not None:
            self._output.finish()

    def _radio_audio_abort(self) -> None:
        if self._output is not None:
            self._output.clear()

    def _radio_drain_s(self) -> float:
        return self._output.queued_s() if self._output is not None else 0.0
