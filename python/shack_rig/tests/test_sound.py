"""shack_rig.sound: devices by name, the native rate, and streams whose
callbacks only copy - against FakeBackend and a stand-in pyaudio, never a
real sound device (one of shack's is an IC-7610's codec)."""

from __future__ import annotations

import array
import threading
import types

import pytest

from shack_rig import sound
from shack_rig.usbaudio import AudioError

CODEC = "USB Audio CODEC"
LONG = "Speakers (USB Audio CODEC Analog Stereo)"


def ramp(n: int, start: int = 0) -> bytes:
    return array.array("h", [(start + i) % 30000 for i in range(n)]).tobytes()


def fake(*extra, realtime=False):
    return sound.FakeBackend([(CODEC, 1, 1, (48000, 44100)), ("Built-in", 2, 2, (44100,)),
                              (LONG[:31], 0, 2, (48000,)), *extra], realtime=realtime)


def test_a_radio_device_is_named_never_defaulted():
    be = fake()
    with pytest.raises(AudioError, match="choose"):
        sound.find("", True, be)
    assert sound.find("usb audio codec", True, be).name == CODEC
    # Windows MME's 31-character cut matches the whole saved name.
    assert sound.find(LONG, True, be).name == LONG[:31]
    with pytest.raises(AudioError, match="not connected"):
        sound.find("IC-9700", True, be)
    # An output-only device is not an input.
    with pytest.raises(AudioError):
        sound.find(LONG, False, be)


def test_two_devices_with_one_name_are_refused_not_guessed():
    be = fake((CODEC, 1, 1, (48000,)))
    with pytest.raises(AudioError, match="2 sound devices"):
        sound.find(CODEC, True, be)


def test_the_native_rate_and_never_a_convenient_one():
    be = fake()
    codec = sound.find(CODEC, True, be)
    assert sound.native_rate(codec, True, 12000, be) == 48000
    builtin = sound.find("Built-in", True, be)
    assert sound.native_rate(builtin, True, 0, be) == 44100       # the packet modem takes any rate
    with pytest.raises(AudioError, match="whole multiple"):
        sound.native_rate(builtin, True, 12000, be)               # a 12 kHz path would resample


def test_an_output_plays_what_was_written_and_says_what_is_left():
    be = fake()
    out = sound.Output(CODEC, backend=be)
    assert out.rate == 48000
    pcm = ramp(4800)                                               # 0.1 s
    assert out.write(pcm)
    assert out.queued_s() == pytest.approx(0.1 + out.latency, abs=1e-6)
    be.pump(0.06)
    assert out.queued_s() == pytest.approx(0.04 + out.latency, abs=1e-3)
    be.pump(0.04)
    stream = be.stream(CODEC, output=True)
    assert bytes(stream.played[:len(pcm)]) == pcm
    out.finish()
    be.pump(0.02)
    assert out.underruns == 0, "an empty ring after the last sample is not an underrun"
    assert out.write(b"\0") is False, "half a sample"
    assert out.write(b"\0" * int(2 * 48000 * 2.5)) is False, "more than the ring holds"
    out.close()
    assert stream.closed


def test_a_stereo_only_output_gets_the_audio_on_both_channels():
    be = sound.FakeBackend([("Stereo", 0, 2, (48000,))], realtime=False)
    be.supports = lambda d, rate, ch, output: ch == 2 and rate == 48000
    out = sound.Output("Stereo", backend=be)
    out.write(ramp(10))
    be.pump(10 / 48000)
    assert be.opened[-1] == ("Stereo", 48000, 2, True)
    # FakeStream keeps channel one of what it played: the ramp, intact.
    assert bytes(be.stream("Stereo", True).played[:20]) == ramp(10)


def test_an_input_hands_over_one_channel_from_its_own_thread():
    be = fake()
    got, threads = [], set()
    done = threading.Event()

    def on_pcm(pcm):
        got.append(pcm)
        threads.add(threading.current_thread().name)
        if sum(len(g) for g in got) >= 2 * 882:
            done.set()

    inp = sound.Input("Built-in", on_pcm, channel=1, backend=be)
    assert inp.rate == 44100
    assert be.opened[-1] == ("Built-in", 44100, 2, False)
    # Two channels interleaved: left all 1s, right the ramp.
    left_right = array.array("h")
    for v in array.array("h", ramp(882)):
        left_right.extend((1, v))
    be.stream("Built-in", output=False).raw += left_right.tobytes()
    be.pump(0.02)
    assert done.wait(2)
    inp.close()
    assert b"".join(got)[:2 * 882] == ramp(882)
    assert threading.main_thread().name not in threads


def test_a_routed_cable_carries_audio_only_while_the_gate_is_open():
    be = sound.FakeBackend([("Radio out", 0, 1, (48000,)), ("Radio in", 1, 0, (48000,))], realtime=False)
    keyed = [False]
    be.route("Radio out", "Radio in", gate=lambda: keyed[0])
    heard = []
    inp = sound.Input("Radio in", heard.append, backend=be)
    out = sound.Output("Radio out", backend=be)
    out.write(ramp(480))
    be.pump(0.01)
    keyed[0] = True
    out.write(ramp(480, 7))
    be.pump(0.01)
    be.pump(0.01)
    out.close()
    inp.close()
    joined = b"".join(heard)
    assert ramp(480, 7) in joined and ramp(480) not in joined


class _FakePyAudio:
    """Enough of pyaudio to drive PortAudioBackend: two devices, formats by
    a table, streams whose callback the test calls."""
    paInt16, paContinue = 8, 0

    def __init__(self):
        self.opened = []

    def PyAudio(self):
        mod = self

        class PA:
            def get_device_count(self):
                return 2

            def get_device_info_by_index(self, i):
                return [{"name": "Mic", "maxInputChannels": 1, "maxOutputChannels": 0,
                         "defaultSampleRate": 44100.0, "hostApi": 0},
                        {"name": CODEC, "maxInputChannels": 1, "maxOutputChannels": 2,
                         "defaultSampleRate": 48000.0, "hostApi": 0}][i]

            def get_host_api_info_by_index(self, i):
                return {"name": "ALSA"}

            def get_default_host_api_info(self):
                return {"index": 0}

            def is_format_supported(self, rate, **kw):
                assert kw.get("output_format", kw.get("input_format")) == mod.paInt16
                device = kw.get("output_device", kw.get("input_device"))
                if rate != (48000 if device == 1 else 44100):
                    raise ValueError("Invalid sample rate")
                return True

            def open(self, **kw):
                mod.opened.append(kw)
                return types.SimpleNamespace(start_stream=lambda: None, stop_stream=lambda: None,
                                             close=lambda: None, get_output_latency=lambda: 0.02,
                                             get_input_latency=lambda: 0.02, kw=kw)

            def terminate(self):
                pass

        return PA()


def test_portaudio_is_asked_for_16_bit_at_the_native_rate():
    pa = _FakePyAudio()
    be = sound.PortAudioBackend(pyaudio=pa)
    assert [d.name for d in be.devices()] == ["Mic", CODEC]
    out = sound.Output(CODEC, backend=be)
    kw = pa.opened[-1]
    assert kw["rate"] == 48000 and kw["format"] == pa.paInt16 and kw["output"] is True
    assert kw["output_device_index"] == 1 and kw["channels"] == 1
    out.write(ramp(5))
    data, flag = kw["stream_callback"](None, 5, {}, 0)
    assert data == ramp(5) and flag == pa.paContinue
    with pytest.raises(AudioError, match="whole multiple"):
        sound.Input("Mic", lambda pcm: None, multiple_of=12000, backend=be)


def test_without_pyaudio_the_listing_is_empty_and_opening_says_why(monkeypatch):
    import builtins
    real = builtins.__import__

    def no_pyaudio(name, *a, **kw):
        if name == "pyaudio":
            raise ImportError(name)
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_pyaudio)
    monkeypatch.setattr(sound, "_shared", None)
    assert sound.list_devices() == []
    with pytest.raises(AudioError, match="not installed"):
        sound.Output(CODEC)
