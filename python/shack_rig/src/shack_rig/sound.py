"""Sound devices on any desktop: PortAudio, the way the family's Qt apps do it.

`usbaudio.py` is the appliance's receive path - `arecord` on a Linux box with
one radio in it, chosen for a month-long run on a Pi. A desktop app on macOS
or Windows has no `arecord`, and its operator picks a sound device by name
from a list. So this module is the second route to the same codecs, and it
is the route openhamshack-nb's `ohscore/audio.py` already takes for NB and Radio:
**PortAudio through pyaudio**, an optional dependency (`shack-rig[audio]`),
the device found by name, never the system default for a radio.

What it keeps from both of them, because each was paid for:

- **The device's own rate, never 12 kHz.** USB-AUDIO.md §1: ALSA's plug
  layer resampling 48 kHz to 12 put every decoded frequency 2.1 % out and
  looked healthy. A desktop sound server or Windows' MME will resample to
  any rate asked of it just as quietly, so `native_rate` asks for 48 kHz -
  the one rate every radio codec has - and otherwise the device's own
  default, and refuses a rate that is not a whole multiple of what the
  caller divides down to. What the caller gets is the device's rate; any
  division is the caller's, by an exact integer (usbaudio.Split,
  usbaudio.Interpolate), or none at all (the packet modem runs at any rate).
- **A radio's device is named, never defaulted.** openhamshack-nb's rule since
  NB played an FT8 over through the speakers while the radio transmitted
  silence: "" is refused with a sentence. And the name match is
  openhamshack-nb's too (`same_device`): Windows' MME cuts a name at 31
  characters, so a saved full name matches its cut listing. GUIDE §5's
  other trap - every Icom's codec is called "USB Audio CODEC" - is the
  caller's to solve (pair by USB place, or ask); this module only refuses
  to guess between two devices with the same name.
- **The callbacks only copy.** An output callback takes bytes from a ring;
  an input callback puts bytes on a queue, and a thread of this module's
  hands them to the caller - so a slow listener can never stall the
  sound card's own thread, which is a click on the air.

Stdlib only apart from pyaudio itself: 16-bit samples as bytes throughout,
which is what PortAudio's paInt16 takes and gives.

`FakeBackend` stands in for PortAudio in tests: named devices with the rates
they have, output streams that keep what was played, input streams fed by
the test, and `route()` - a cable from one fake device's output to another's
input, gated by a callable (a pretend radio's PTT). No test opens a real
sound device: one of shack's is an IC-7610's codec.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .i18n import _
from .usbaudio import AudioError

log = logging.getLogger("shack_rig.sound")

#: The rate asked for first: every USB codec in a radio has it, and the
#: integer factors to 12 kHz (4) and 9.6 kHz are exact.
PREFERRED_RATE = 48000

#: Windows' MME (PortAudio's default host API there, and the one offered:
#: it takes any format from any card) cuts device names at 31 characters
#: (MAXPNAMELEN). openhamshack-nb's Windows test, 17/10/2026.
MME_NAME_MAX = 31

#: One callback's worth of audio. 20 ms, as openhamshack-nb's streams and the
#: Icom LAN stream's packets.
BLOCK_S = 0.02

WINDOWS = sys.platform.startswith("win")


def same_device(want: str, name: str) -> bool:
    """Whether a device listed as `name` is the one saved as `want`: the same
    name ignoring case, or - where MME cut the listing at 31 characters -
    `want` being the whole of it (openhamshack-nb ohscore/audio.py)."""
    w, n = want.strip().lower(), name.strip().lower()
    if not w or not n:
        return False
    return w == n or (len(name) == MME_NAME_MAX and w.startswith(n))


@dataclass(frozen=True)
class Device:
    """One sound device as the backend lists it."""

    index: int
    name: str
    inputs: int
    outputs: int
    default_rate: int
    host_api: str = ""

    def public(self) -> dict:
        return {"index": self.index, "name": self.name, "inputs": self.inputs,
                "outputs": self.outputs, "rate": self.default_rate, "api": self.host_api}


# -- backends -----------------------------------------------------------------


class Backend:
    """What this module needs of PortAudio. `PortAudioBackend` is the real
    one, `FakeBackend` the tests'."""

    def devices(self) -> list[Device]:
        raise NotImplementedError

    def supports(self, device: Device, rate: int, channels: int, output: bool) -> bool:
        raise NotImplementedError

    def open(self, device: Device, rate: int, channels: int, output: bool, frames: int,
             callback: Callable[[bytes | None, int], bytes | None]):
        """A started stream. `callback(data, frames)`: for an input, `data`
        is what arrived and the return is ignored; for an output, `data` is
        None and the return is `frames` frames of audio. The stream has
        `close()` and `latency` (seconds)."""
        raise NotImplementedError


class PortAudioBackend(Backend):
    """PortAudio through pyaudio (`shack-rig[audio]`).

    `pyaudio` may be handed in: the tests give it a stand-in module, which is
    how the device matching and the callbacks are exercised without a card.
    On Windows only the default host API's devices are offered (MME), as
    openhamshack-nb does: PortAudio lists every card once per host API, and a
    match by name would otherwise land on whichever came first.
    """

    def __init__(self, pyaudio=None):
        if pyaudio is None:
            try:
                import pyaudio                       # noqa: PLC0415 - optional
            except ImportError:
                raise AudioError(_("PortAudio (pyaudio) is not installed, so no sound device can be "
                                   "opened")) from None
        self.pa_mod = pyaudio
        self.pa = pyaudio.PyAudio()

    def close(self) -> None:
        pa, self.pa = self.pa, None
        if pa is not None:
            pa.terminate()

    def _default_api(self) -> int | None:
        if not WINDOWS:
            return None
        try:
            return int(self.pa.get_default_host_api_info()["index"])
        except (OSError, KeyError, TypeError):
            return None

    def devices(self) -> list[Device]:
        api = self._default_api()
        out = []
        for i in range(self.pa.get_device_count()):
            d = self.pa.get_device_info_by_index(i)
            if api is not None and d.get("hostApi") != api:
                continue
            try:
                host = str(self.pa.get_host_api_info_by_index(d["hostApi"])["name"])
            except (OSError, KeyError, TypeError, ValueError):
                host = ""
            out.append(Device(i, str(d["name"]), int(d["maxInputChannels"]), int(d["maxOutputChannels"]),
                              int(d["defaultSampleRate"]), host))
        return out

    def supports(self, device: Device, rate: int, channels: int, output: bool) -> bool:
        kw = ({"output_device": device.index, "output_channels": channels, "output_format": self.pa_mod.paInt16}
              if output else
              {"input_device": device.index, "input_channels": channels, "input_format": self.pa_mod.paInt16})
        try:
            return bool(self.pa.is_format_supported(rate, **kw))
        except (ValueError, OSError):
            return False

    def open(self, device, rate, channels, output, frames, callback):
        mod = self.pa_mod

        def cb(in_data, frame_count, _time_info, _status):
            if output:
                return callback(None, frame_count), mod.paContinue
            callback(in_data, frame_count)
            return None, mod.paContinue

        kw = {"output_device_index": device.index} if output else {"input_device_index": device.index}
        stream = self.pa.open(format=mod.paInt16, channels=channels, rate=rate, frames_per_buffer=frames,
                              output=output, input=not output, stream_callback=cb, **kw)
        stream.start_stream()
        return _PaStream(stream, output)


class _PaStream:
    def __init__(self, stream, output: bool):
        self._stream = stream
        try:
            self.latency = float(stream.get_output_latency() if output else stream.get_input_latency())
        except (OSError, AttributeError):
            self.latency = 0.05

    def close(self) -> None:
        st, self._stream = self._stream, None
        if st is None:
            return
        try:
            st.stop_stream()
            st.close()
        except OSError:
            pass


class FakeBackend(Backend):
    """Sound devices for tests: see the module docstring.

    `devices` is a list of (name, inputs, outputs, rates) - the first rate is
    the device's default. With `realtime` a thread runs every open stream's
    callback every 20 ms of wall time, as a card would; without it a test
    calls `pump(seconds)` to move every stream on by that much audio.
    """

    def __init__(self, devices, realtime: bool = True):
        self._devices = [Device(i, name, ins, outs, rates[0], "fake")
                         for i, (name, ins, outs, rates) in enumerate(devices)]
        self._rates = {name: tuple(rates) for name, _i, _o, rates in devices}
        self.streams: list[FakeStream] = []
        self.routes: list[tuple[str, str, Callable[[], bool] | None]] = []
        self.opened: list[tuple[str, int, int, bool]] = []
        self.realtime = realtime
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def devices(self) -> list[Device]:
        return list(self._devices)

    def supports(self, device, rate, channels, output):
        return rate in self._rates.get(device.name, ()) and channels <= (device.outputs if output else device.inputs)

    def open(self, device, rate, channels, output, frames, callback):
        s = FakeStream(self, device.name, rate, channels, output, frames, callback)
        with self._lock:
            self.streams.append(s)
            self.opened.append((device.name, rate, channels, output))
        if self.realtime and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="fake-sound", daemon=True)
            self._thread.start()
        return s

    def route(self, output: str, input: str, gate: Callable[[], bool] | None = None) -> None:
        """Whatever plays on `output` arrives at `input` while `gate()` is
        true (a pretend transmitter keyed) - silence otherwise."""
        self.routes.append((output, input, gate))

    def stream(self, name: str, output: bool) -> FakeStream | None:
        with self._lock:
            for s in reversed(self.streams):
                if s.name == name and s.output == output and not s.closed:
                    return s
        return None

    def feed(self, name: str, pcm: bytes) -> None:
        """Audio arriving at input device `name` (mono 16-bit at its rate)."""
        s = self.stream(name, output=False)
        if s is not None:
            s.pending += pcm

    def pump(self, seconds: float) -> None:
        with self._lock:
            streams = [s for s in self.streams if not s.closed]
        for s in streams:
            if s.output:
                s.step(int(s.rate * seconds))
        for s in streams:
            if not s.output:
                s.step(int(s.rate * seconds))

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        tick = BLOCK_S
        nxt = time.monotonic()
        while not self._stop.is_set():
            self.pump(tick)
            nxt += tick
            time.sleep(max(0.0, nxt - time.monotonic()))


class FakeStream:
    """One open fake stream. An output keeps everything played in `played`
    (mono 16-bit), with `played_at` the time line of it."""

    def __init__(self, backend, name, rate, channels, output, frames, callback):
        self.backend = backend
        self.name = name
        self.rate = rate
        self.channels = channels
        self.output = output
        self.frames = frames
        self.callback = callback
        self.latency = 0.03
        self.closed = False
        self.played = bytearray()
        self.played_at: list[tuple[float, int]] = []
        #: Mono audio waiting to arrive at an input (copied to every channel),
        #: and interleaved frames that arrive as they are.
        self.pending = bytearray()
        self.raw = bytearray()

    def step(self, n: int) -> None:
        if self.closed or n <= 0:
            return
        width = 2 * self.channels
        if self.output:
            data = self.callback(None, n) or b""
            data = bytes(data[:n * width]).ljust(n * width, b"\0")
            mono = data if self.channels == 1 else bytes(
                b for i in range(0, len(data), width) for b in data[i:i + 2])
            self.played += mono
            self.played_at.append((time.monotonic(), len(mono)))
            for out, into, gate in self.backend.routes:
                if out == self.name:
                    self.backend.feed(into, mono if gate is None or gate() else b"\0" * len(mono))
        elif self.raw:
            width = 2 * self.channels
            take = bytes(self.raw[:n * width]).ljust(n * width, b"\0")
            del self.raw[:n * width]
            self.callback(take, n)
        else:
            take = bytes(self.pending[:2 * n])
            del self.pending[:2 * n]
            take = take.ljust(2 * n, b"\0")
            if self.channels > 1:
                take = b"".join(take[i:i + 2] * self.channels for i in range(0, len(take), 2))
            self.callback(take, n)

    def close(self) -> None:
        self.closed = True


_shared: Backend | None = None
_shared_lock = threading.Lock()


def default_backend() -> Backend:
    """One PortAudio for the process (PortAudio's own advice: initialise
    once). Raises AudioError when pyaudio is not installed."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = PortAudioBackend()
        return _shared


# -- finding a device and its rate ----------------------------------------------


def find(name: str, output: bool, backend: Backend | None = None) -> Device:
    """The device saved as `name`, with at least one channel the right way.
    Raises AudioError with a sentence: "" is refused rather than defaulted,
    a missing device is named, and two devices answering to one name are
    refused rather than guessed between (GUIDE §5: every Icom's codec has
    the same name)."""
    if not name.strip():
        raise AudioError(_("choose the radio's sound device; the system default is never used for a radio"))
    be = backend or default_backend()
    usable = [d for d in be.devices() if (d.outputs if output else d.inputs) > 0]
    exact = [d for d in usable if same_device(name, d.name)]
    if len(exact) > 1:
        raise AudioError(_("{n} sound devices are called {name}, and nothing says which is the radio; "
                           "choose one by its place on the computer").format(n=len(exact), name=name))
    if exact:
        return exact[0]
    loose = [d for d in usable if name.strip().lower() in d.name.lower()]
    if len(loose) == 1:
        return loose[0]
    raise AudioError(_("the sound device {name} is not connected").format(name=name))


def native_rate(device: Device, output: bool, multiple_of: int = 0, backend: Backend | None = None) -> int:
    """The rate to open `device` at: 48 kHz if it has it, else its own
    default - never a rate chosen for the caller's convenience (module
    docstring). With `multiple_of`, the rate must divide by it exactly, or
    AudioError says so: resampling quietly is the fault this exists to avoid."""
    be = backend or default_backend()
    for rate in (PREFERRED_RATE, device.default_rate):
        if multiple_of and rate % multiple_of:
            continue
        if be.supports(device, rate, 1, output) or be.supports(device, rate, 2, output):
            return rate
    if multiple_of and device.default_rate % multiple_of:
        raise AudioError(_("the sound device {name} runs at {rate} Hz, which is not a whole multiple of {want} Hz; "
                           "set it to 48000 Hz in the computer's sound settings").format(
            name=device.name, rate=device.default_rate, want=multiple_of))
    raise AudioError(_("the sound device {name} will not open at {rate} Hz").format(
        name=device.name, rate=device.default_rate))


# -- streams --------------------------------------------------------------------


class Output:
    """Mono 16-bit audio to a device at its native rate.

    `write(pcm)` puts audio in a ring the device's callback copies from, and
    returns False rather than blocking when the ring is full; `queued_s()`
    is what has not yet left the computer, the device's own buffer included
    (openhamshack-nb TxOut.tx_queued: until that has played, the radio is still
    modulating). Opened for a transmission and closed after, never left
    open between overs: an output left open would put whatever reached it on
    the radio's modulator the next time anyone keyed it.
    """

    #: The most audio the ring holds, seconds.
    RING_S = 2.0

    def __init__(self, name: str, *, rate: int = 0, multiple_of: int = 0, backend: Backend | None = None):
        self.backend = backend or default_backend()
        self.device = find(name, True, self.backend)
        self.rate = rate or native_rate(self.device, True, multiple_of, self.backend)
        self._ring = bytearray()
        self._lock = threading.Lock()
        self._empty_at: float | None = None
        self.underruns = 0
        self.started = False
        self.finishing = False
        self._channels = 1 if self.backend.supports(self.device, self.rate, 1, True) else 2
        try:
            self._stream = self.backend.open(self.device, self.rate, self._channels, True,
                                             int(self.rate * BLOCK_S), self._callback)
        except Exception as e:                       # noqa: BLE001 - PortAudio fails in many ways
            raise AudioError(_("the sound device {name} would not open ({error})").format(
                name=self.device.name, error=e)) from None
        self.latency = float(getattr(self._stream, "latency", 0.05) or 0.0)
        log.info("audio out: %s at %d Hz, %d channel(s), latency %.0f ms", self.device.name, self.rate,
                 self._channels, 1000 * self.latency)

    def _callback(self, _data, frames: int) -> bytes:
        want = 2 * frames
        with self._lock:
            out = bytes(self._ring[:want])
            del self._ring[:want]
        if len(out) < want:
            if self.started and not self.finishing and len(out) < want:
                self.underruns += 1
            out += b"\0" * (want - len(out))
        if self._channels == 2:
            out = b"".join(out[i:i + 2] * 2 for i in range(0, len(out), 2))
        return out

    def write(self, pcm: bytes) -> bool:
        if len(pcm) % 2:
            return False
        with self._lock:
            if len(self._ring) + len(pcm) > self.RING_S * self.rate * 2:
                return False
            self._ring += pcm
        self.started = True
        self.finishing = False
        return True

    def finish(self) -> None:
        """The last sample has been written: an empty ring is no longer an
        underrun."""
        self.finishing = True

    def queued_s(self) -> float:
        with self._lock:
            left = len(self._ring) / 2 / self.rate
        if left > 0:
            self._empty_at = None
            return left + self.latency
        if self._empty_at is None:
            self._empty_at = time.monotonic()
        return max(0.0, self._empty_at + self.latency - time.monotonic())

    def clear(self) -> None:
        """Drop what has not played: a transmission that was stopped."""
        with self._lock:
            self._ring.clear()

    def close(self) -> None:
        st, self._stream = self._stream, None
        if st is not None:
            st.close()


class Input:
    """Mono 16-bit audio from one channel of a device at its native rate,
    handed to `on_pcm(bytes)` from a thread of this module's in blocks of
    about 20 ms (module docstring: the card's callback only queues)."""

    def __init__(self, name: str, on_pcm: Callable[[bytes], None], *, rate: int = 0, multiple_of: int = 0,
                 channel: int = 0, backend: Backend | None = None):
        self.backend = backend or default_backend()
        self.device = find(name, False, self.backend)
        self.rate = rate or native_rate(self.device, False, multiple_of, self.backend)
        self.on_pcm = on_pcm
        self.channel = channel
        self._channels = max(1, min(self.device.inputs, channel + 1))
        if not self.backend.supports(self.device, self.rate, self._channels, False):
            self._channels = max(self._channels, 2)
        self._q: queue.Queue[bytes | None] = queue.Queue(maxsize=200)
        self.dropped = 0
        self.blocks = 0
        self._thread = threading.Thread(target=self._deliver, name=f"audio-in {name}", daemon=True)
        self._thread.start()
        try:
            self._stream = self.backend.open(self.device, self.rate, self._channels, False,
                                             int(self.rate * BLOCK_S), self._callback)
        except Exception as e:                       # noqa: BLE001
            self._q.put(None)
            raise AudioError(_("the sound device {name} would not open ({error})").format(
                name=self.device.name, error=e)) from None
        log.info("audio in: %s at %d Hz, channel %d of %d", self.device.name, self.rate, channel, self._channels)

    def _callback(self, data, _frames) -> None:
        if self._channels > 1 and data:
            width = 2 * self._channels
            off = 2 * min(self.channel, self._channels - 1)
            data = b"".join(data[i + off:i + off + 2] for i in range(0, len(data), width))
        try:
            self._q.put_nowait(bytes(data or b""))
        except queue.Full:
            self.dropped += 1

    def _deliver(self) -> None:
        while True:
            block = self._q.get()
            if block is None:
                return
            self.blocks += 1
            try:
                self.on_pcm(block)
            except Exception:                        # noqa: BLE001 - a listener must not end the capture
                log.exception("audio listener")

    def close(self) -> None:
        st, self._stream = self._stream, None
        if st is not None:
            st.close()
        self._q.put(None)
        self._thread.join(timeout=2)


def list_devices(backend: Backend | None = None) -> list[dict]:
    """Every device the backend offers, for a settings page; [] without
    PortAudio rather than an exception."""
    try:
        be = backend or default_backend()
    except AudioError:
        return []
    return [d.public() for d in be.devices()]
