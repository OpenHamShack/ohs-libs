"""Transmit audio to a radio's USB codec: 12 kHz in, the codec's own rate out.

Stage three of the Icom app's docs/TRANSPORTS.md (§4.5, "USB is the first
path that could actually work"), and the other half of `usbaudio.py`. The
sink contract is the one `digi/transmit.py` wrote down and the LAN audio
stream already satisfies: `tx_rate`, `tx_audio_ready`, `send_audio(pcm)`,
`tx_end()` and `tx_drain_s`. What reaches here is 16-bit mono at
`TX_RATE`, 12 kHz, because that is what everything that transmits through
a rig already makes (the Icom app's modulator, the packet modem through
IcomLink), and what leaves is the codec's own rate.

**The trap from USB-AUDIO.md §1 applies in this direction too.** Asking a
sound system for 12 kHz playback on a 48 kHz-only codec makes it resample,
and nobody here has measured how well; so the codec is opened at its own
rate (48 kHz on every radio codec this family has met) and the audio is
raised by the exact integer factor here (`Interpolate`, 4 for 48 kHz). A
codec with no whole multiple of 12 kHz is refused with a sentence, as the
receive side refuses one.

**Two routes, one contract**, because the family runs on two kinds of host:

- `AplayOut` - the appliance's: `aplay` on the `hw:` device the receive
  side found (never `plughw:`, for the reason above), as TRANSPORTS.md §7.1
  designed it - a process that can die and be restarted rather than a C
  binding in a month-long run. Linux only.
- `SoundOut` - a desktop's, Mac, Windows or Linux: PortAudio through
  `shack_rig.sound`, the device named by the operator.

Both are opened for a transmission and let go after it (openhamshack-nb's
TxOut rule): an output left open between overs would put whatever reached
it on the modulator the next time anyone keyed the radio.

**`Interpolate` is linear interpolation**, stdlib only. Its response is
sinc^2: -1 dB at 2.2 kHz (the AFSK space tone), and the first image of that
tone, at 9.8 kHz, 26 dB down - before the radio's own transmit audio filter,
which passes about 3 kHz. A packet or FT8 signal is well inside that.
Measured cost on shack (Ryzen 9 6900HX, 18/10/2026): 5.7 ms of CPU per
second of transmitted audio at 4x, in 20 ms pieces; nothing runs between
overs.
"""

from __future__ import annotations

import array
import logging
import shutil
import subprocess
import threading
import time

from .i18n import _
from .usbaudio import AudioError, find_card, parse_hw_params

log = logging.getLogger("shack_rig.txaudio")

#: What callers hand in. The same as usbaudio.DECODE_RATE, on purpose.
TX_RATE = 12000

#: The highest playback rate worth using; see usbaudio.MAX_RATE.
MAX_RATE = 48000

#: aplay's period and buffer, as arecord's on the receive side: 50 ms
#: periods, four of them. The buffer is what tx_drain_s adds after the last
#: sample.
PERIOD_S = 0.05
BUFFER_PERIODS = 4


class Interpolate:
    """Raise 16-bit mono PCM by an exact integer factor, by straight lines.

    Phase-continuous across calls: the last sample of one block is the
    start of the first line of the next, so a transmission handed over in
    20 ms pieces comes out exactly as it would in one piece (tested). The
    cost of that is one input sample of delay, 83 us at 12 kHz.
    """

    def __init__(self, factor: int):
        if factor < 1:
            raise AudioError(_("an up-sampling factor must be a whole number of at least 1"))
        self.factor = factor
        self._last = 0

    def feed(self, pcm: bytes) -> bytes:
        if self.factor == 1:
            return bytes(pcm)
        a = array.array("h")
        a.frombytes(pcm[:len(pcm) // 2 * 2])
        if not a:
            return b""
        f = self.factor
        prev = array.array("h", [self._last])
        prev.extend(a[:-1])
        out = array.array("h", bytes(2 * len(a) * f))
        for k in range(f):
            w = (k + 1) / f
            out[k::f] = array.array("h", [int(round(p + (c - p) * w)) for p, c in zip(prev, a)])
        self._last = a[-1]
        return out.tobytes()

    def reset(self) -> None:
        self._last = 0


def choose_playback(params: dict[str, tuple[int, int]]) -> tuple[int, int]:
    """The rate and channel count to open a codec's playback at, from
    `aplay --dump-hw-params`: the highest exact multiple of 12 kHz (as
    usbaudio.choose, and for its reasons), and the fewest channels it takes
    (a radio codec's playback is often stereo-only; the mono audio goes to
    both)."""
    lo_r, hi_r = params.get("RATE", (0, 0))
    rate = min(hi_r, MAX_RATE)
    rate -= rate % TX_RATE
    if rate < lo_r or rate <= 0:
        raise AudioError(_(
            "this sound card plays {lo}-{hi} Hz and none of it is a whole multiple of {rate}; transmit audio is "
            "refused rather than resampled by the sound system").format(lo=lo_r, hi=hi_r, rate=TX_RATE))
    lo_c, _hi_c = params.get("CHANNELS", (1, 1))
    return rate, max(1, min(lo_c, 2))


class _Sink:
    """What both routes share: the contract, the up-sampling, and the
    estimate of what has not yet played."""

    tx_rate = TX_RATE

    def __init__(self):
        self._lock = threading.Lock()
        self._interp: Interpolate | None = None
        self.sent_s = 0.0
        self.overs = 0

    @property
    def tx_audio_ready(self) -> bool:
        return True

    @property
    def tx_drain_s(self) -> float:
        return self.queued_s()

    def queued_s(self) -> float:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class AplayOut(_Sink):
    """`aplay` on the radio's codec, one process per transmission.

    `device` is the profile's audio device (a /dev/snd/by-id path, an ALSA
    name, or "" for the only USB card), resolved exactly as the receive
    side resolves it (usbaudio.find_card). `aplay` is a parameter for the
    tests, which hand in a stand-in that writes what it is given to a file.
    """

    def __init__(self, device: str = "", aplay: str = "aplay"):
        super().__init__()
        self.want = device
        self.aplay = aplay
        self.alsa: str | None = None
        self.rate = 0
        self.channels = 1
        self.how = ""
        self._proc: subprocess.Popen | None = None
        self._started_at = 0.0
        self._written_s = 0.0
        self.error: str | None = None

    @property
    def tx_audio_ready(self) -> bool:
        return shutil.which(self.aplay) is not None and self.error is None

    def prepare(self) -> None:
        """Find the card and what it plays. Raises AudioError with the
        reason; a link calls this once, on connecting."""
        if shutil.which(self.aplay) is None:
            raise AudioError(_("aplay (alsa-utils) is not installed, so transmit audio cannot reach the radio"))
        self.alsa, self.how = find_card(self.want)
        out = subprocess.run([self.aplay, "-D", self.alsa, "--dump-hw-params", "-t", "raw", "-f", "S16_LE",
                              "/dev/null"], capture_output=True, text=True, timeout=10)
        self.rate, self.channels = choose_playback(parse_hw_params(out.stderr + out.stdout))
        log.info("transmit audio: %s at %d Hz, %d channel(s) (%s)", self.alsa, self.rate, self.channels, self.how)

    def _argv(self) -> list[str]:
        period = int(self.rate * PERIOD_S)
        return [self.aplay, "-D", self.alsa, "-t", "raw", "-f", "S16_LE", "-c", str(self.channels),
                "-r", str(self.rate), f"--period-size={period}", f"--buffer-size={period * BUFFER_PERIODS}",
                "-q", "-"]

    def send_audio(self, pcm: bytes) -> bool:
        if self.rate == 0:
            try:
                self.prepare()
            except AudioError as e:
                self.error = str(e)
                log.warning("transmit audio: %s", e)
                return False
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._interp = Interpolate(self.rate // TX_RATE)
                try:
                    self._proc = subprocess.Popen(self._argv(), stdin=subprocess.PIPE,
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError as e:
                    self.error = str(e)
                    return False
                self._started_at = time.monotonic()
                self._written_s = 0.0
                self.overs += 1
            data = self._interp.feed(pcm)
            if self.channels == 2:
                data = _stereo(data)
            try:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as e:
                log.warning("transmit audio: aplay went away: %s", e)
                self._proc = None
                return False
            self._written_s += len(pcm) / 2 / TX_RATE
            self.sent_s += len(pcm) / 2 / TX_RATE
            return True

    def tx_end(self) -> None:
        """The last sample is in: close aplay's stdin, and it plays what it
        has and exits. A stopped transmission calls close() instead."""
        with self._lock:
            proc = self._proc
            if proc is not None and proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

    def queued_s(self) -> float:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return 0.0
            played = time.monotonic() - self._started_at
            return max(0.0, self._written_s - played) + PERIOD_S * BUFFER_PERIODS

    def close(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()


class SoundOut(_Sink):
    """PortAudio on a named device (shack_rig.sound), for a desktop.

    The stream opens at the first sample of a transmission and closes once
    `tx_end` has been called and what was written has played.

    **No call into the device is allowed to hang its caller.** `send_audio`
    is called on a rig's own loop (IcomRig), the one that also polls the
    radio and re-asks the transmit lease; a PortAudio open that never
    returns - a Windows USB codec that has stalled - would stop all of it
    with the radio keyed. On 18/10/2026 an IC-9700 on USB stayed keyed for
    two minutes after an "audio out" open with nothing logged at all, and a
    hang there is the suspect. So the open runs in a thread of its own and
    is given OPEN_TIMEOUT_S: past that the over's audio is refused (the
    link then aborts the over and unkeys), the open is abandoned, and a
    stream it opens late is closed by that thread. Closing - PortAudio's
    stop waits for the device - always happens in a thread of its own.
    Writing was never the risk: sound.Output.write only fills a ring.
    """

    #: The longest an open may take before the over is refused. A codec
    #: opens in tens of milliseconds; a second is a device that has gone.
    OPEN_TIMEOUT_S = 1.0

    def __init__(self, device: str, backend=None, rate: int = 0):
        super().__init__()
        self.device = device
        self.backend = backend
        self._out = None
        self._closer: threading.Timer | None = None
        self.error: str | None = None
        #: With `rate`, audio arrives at that rate and is played at it - no
        #: up-sampling - for a rig whose own audio runs at the codec's rate
        #: (YaesuRig). Without, 12 kHz in and the codec's rate out.
        self.in_rate = rate
        self.rate = 0
        #: An open that outran OPEN_TIMEOUT_S and has not finished yet: no
        #: second one is started behind it.
        self._opening: threading.Thread | None = None

    @property
    def tx_rate(self) -> int:
        return self.in_rate or TX_RATE

    def _open_bounded(self):
        """sound.Output, or None (said in the log) if it failed or took too
        long. Called under the lock."""
        from . import sound
        if self._opening is not None and self._opening.is_alive():
            log.warning("transmit audio: the sound device is still opening from an earlier over; "
                        "this over's audio is refused")
            return None
        box: dict = {}
        done = threading.Event()
        lock = threading.Lock()

        def run() -> None:
            try:
                if self.in_rate:
                    out = sound.Output(self.device, rate=self.in_rate, backend=self.backend)
                else:
                    out = sound.Output(self.device, multiple_of=TX_RATE, backend=self.backend)
            except AudioError as e:
                box["error"] = e
                out = None
            except Exception as e:                  # noqa: BLE001 - PortAudio fails in many ways
                box["error"] = AudioError(str(e))
                out = None
            with lock:
                abandoned = box.get("abandoned", False)
                box["out"] = out
                done.set()
            if abandoned and out is not None:
                log.warning("transmit audio: the sound device opened %s after it was given up on; closed",
                            self.device)
                out.close()

        t = threading.Thread(target=run, name=f"audio-out open {self.device}", daemon=True)
        t.start()
        done.wait(self.OPEN_TIMEOUT_S)
        with lock:
            if not done.is_set():
                box["abandoned"] = True
                self._opening = t
                log.error("transmit audio: the sound device %s did not open within %.1f s; this over's audio is "
                          "refused", self.device, self.OPEN_TIMEOUT_S)
                return None
        self._opening = None
        if "error" in box:
            self.error = str(box["error"])
            log.warning("transmit audio: %s", box["error"])
            return None
        return box["out"]

    @staticmethod
    def _close_apart(out, drop: bool) -> None:
        """Close `out` in a thread of its own: see the class docstring."""
        def run() -> None:
            try:
                if drop:
                    out.clear()
                out.close()
            except Exception:                       # noqa: BLE001 - closing regardless
                log.exception("closing the transmit sound device")
        threading.Thread(target=run, name="audio-out close", daemon=True).start()

    @property
    def tx_audio_ready(self) -> bool:
        return bool(self.device) and self.error is None

    def send_audio(self, pcm: bytes) -> bool:
        with self._lock:
            if self._closer is not None:
                self._closer.cancel()
                self._closer = None
            if self._out is None:
                self._out = self._open_bounded()
                if self._out is None:
                    return False
                self.rate = self._out.rate
                self._interp = Interpolate(self.rate // self.tx_rate)
                self.overs += 1
            ok = self._out.write(self._interp.feed(pcm))
            if ok:
                self.sent_s += len(pcm) / 2 / TX_RATE
            return ok

    def tx_end(self) -> None:
        with self._lock:
            out = self._out
            if out is None:
                return
            out.finish()
            wait = out.queued_s() + 0.1
            self._closer = threading.Timer(wait, self._close_if_idle, args=(out,))
            self._closer.daemon = True
            self._closer.start()

    def _close_if_idle(self, out) -> None:
        with self._lock:
            if self._out is out:
                self._out = None
                self._closer = None
        self._close_apart(out, drop=False)

    def queued_s(self) -> float:
        out = self._out
        return out.queued_s() if out is not None else 0.0

    def close(self) -> None:
        with self._lock:
            out, self._out = self._out, None
            if self._closer is not None:
                self._closer.cancel()
                self._closer = None
        if out is not None:
            self._close_apart(out, drop=True)


def _stereo(mono: bytes) -> bytes:
    a = array.array("h")
    a.frombytes(mono)
    out = array.array("h", bytes(4 * len(a)))
    out[0::2] = a
    out[1::2] = a
    return out.tobytes()
