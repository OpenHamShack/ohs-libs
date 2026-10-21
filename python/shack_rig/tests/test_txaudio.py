"""Transmit audio to a USB codec (txaudio.py): 12 kHz in, the codec's own
rate out by an exact integer factor, through aplay (a stand-in that writes
what it is given to a file) or PortAudio (FakeBackend). No sound device and
no radio is touched."""

from __future__ import annotations

import array
import os
import stat
import sys
import time

import pytest

from shack_rig import sound, txaudio
from shack_rig.icom.build import make_rig
from shack_rig.usbaudio import AudioError


def samples(pcm: bytes) -> list[int]:
    a = array.array("h")
    a.frombytes(pcm)
    return list(a)


def pcm(values) -> bytes:
    return array.array("h", values).tobytes()


def test_interpolation_is_exact_straight_lines_and_continuous_across_blocks():
    ramp = [i * 100 for i in range(240)]
    whole = txaudio.Interpolate(4).feed(pcm(ramp))
    up = samples(whole)
    assert len(up) == 4 * len(ramp)
    # Every fourth output sample is an input sample (one sample late), and
    # the three between lie on the line.
    assert up[3::4] == ramp
    assert up[4:8] == [25, 50, 75, 100]
    pieces = txaudio.Interpolate(4)
    cut = b"".join(pieces.feed(pcm(ramp[a:b])) for a, b in ((0, 7), (7, 8), (8, 131), (131, 240)))
    assert cut == whole, "the same samples however the transmission is cut up"
    assert txaudio.Interpolate(1).feed(pcm(ramp)) == pcm(ramp)
    with pytest.raises(AudioError):
        txaudio.Interpolate(0)


def test_the_playback_rate_is_the_highest_exact_multiple_and_44k1_is_refused():
    assert txaudio.choose_playback({"RATE": (8000, 48000), "CHANNELS": (2, 2)}) == (48000, 2)
    assert txaudio.choose_playback({"RATE": (8000, 96000), "CHANNELS": (1, 2)}) == (48000, 1)
    with pytest.raises(AudioError, match="refused"):
        txaudio.choose_playback({"RATE": (44100, 44100), "CHANNELS": (2, 2)})


@pytest.fixture
def aplay(tmp_path):
    """A stand-in aplay: answers --dump-hw-params as a stereo-only 48 kHz
    codec does, and otherwise writes its argv and its stdin to files."""
    script = tmp_path / "aplay"
    script.write_text(f"""#!{sys.executable}
import sys
args = sys.argv[1:]
if "--dump-hw-params" in args:
    sys.stderr.write("HW Params of device:\\nACCESS: RW_INTERLEAVED\\nCHANNELS: 2\\nRATE: [8000 48000]\\n")
    sys.exit(0)
open({str(tmp_path / 'argv')!r}, "w").write(" ".join(args))
with open({str(tmp_path / 'played')!r}, "ab") as f:
    f.write(sys.stdin.buffer.read())
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return tmp_path, str(script)


def test_aplay_plays_the_codec_at_its_own_rate_one_process_an_over(aplay):
    where, path = aplay
    out = txaudio.AplayOut("hw:9,0", aplay=path)
    assert out.tx_rate == 12000 and out.tx_audio_ready
    tone = [((i * 37) % 2000) - 1000 for i in range(1200)]          # 0.1 s at 12 kHz
    for at in range(0, 1200, 240):                                   # 20 ms pieces
        assert out.send_audio(pcm(tone[at:at + 240]))
    assert out.tx_drain_s > 0.1
    out.tx_end()
    end = time.monotonic() + 5
    while out.queued_s() > 0 and time.monotonic() < end:
        time.sleep(0.01)
    assert out.queued_s() == 0.0, "aplay exits once its stdin is closed"
    argv = (where / "argv").read_text().split()
    assert argv[argv.index("-D") + 1] == "hw:9,0" and "plughw" not in " ".join(argv)
    assert argv[argv.index("-r") + 1] == "48000" and argv[argv.index("-c") + 1] == "2"
    played = samples((where / "played").read_bytes())
    assert len(played) == 1200 * 4 * 2
    left, right = played[0::2], played[1::2]
    assert left == right, "mono on both channels of a stereo-only codec"
    assert left[3::4] == tone, "the same audio, four times the rate"
    assert out.overs == 1
    out.close()


def test_a_portaudio_device_plays_at_48k_and_lets_go_after_the_over():
    be = sound.FakeBackend([("IC-705 Speakers", 1, 1, (48000,))], realtime=False)
    out = txaudio.SoundOut("IC-705 Speakers", backend=be)
    tone = [((i * 53) % 4000) - 2000 for i in range(480)]
    assert out.send_audio(pcm(tone))
    assert out.rate == 48000
    be.pump(0.05)
    out.tx_end()
    stream = be.stream("IC-705 Speakers", True)
    assert samples(bytes(stream.played))[3:4 * 480:4] == tone
    time.sleep(out.queued_s() + 0.3)
    assert stream.closed, "the device is let go once the over has played"
    out.close()


def test_transmit_audio_is_offered_only_to_a_rig_that_may_transmit():
    profile = {"model": "ic-705", "transport": "usb", "civ_device": "/dev/null", "tx_audio": "alsa"}
    off = make_rig(profile, tx_enabled=False)._make_link()
    on = make_rig(profile, tx_enabled=True)._make_link()
    assert off.tx_audio == "" and on.tx_audio == "alsa"
    assert off.tx_rate == 0 and not off.tx_audio_ready
    assert off.send_audio(b"\0\0") is False


def test_a_sound_device_that_will_not_open_refuses_the_over_instead_of_hanging_it():
    # A Windows USB codec that has stalled: PortAudio's open never returns.
    # send_audio runs on a rig's loop, so it must give up, not wait.
    import threading
    release = threading.Event()

    class Stalled(sound.FakeBackend):
        def open(self, *a, **kw):
            release.wait()
            return super().open(*a, **kw)

    be = Stalled([("IC9700 Speakers", 1, 1, (48000,))], realtime=False)
    out = txaudio.SoundOut("IC9700 Speakers", backend=be)
    out.OPEN_TIMEOUT_S = 0.2
    t = time.monotonic()
    assert out.send_audio(pcm([0] * 120)) is False
    assert time.monotonic() - t < 0.5
    t = time.monotonic()
    assert out.send_audio(pcm([0] * 120)) is False, "no second open queued behind the stuck one"
    assert time.monotonic() - t < 0.05
    release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not (be.streams and be.streams[0].closed):
        time.sleep(0.01)
    assert be.streams and be.streams[0].closed, "the late stream is closed, never left open on the modulator"
    assert out.tx_audio_ready, "the device may work again for the next over"
    assert out.send_audio(pcm([0] * 120)) is True
    out.close()


def test_closing_never_waits_for_the_sound_device():
    import threading
    release = threading.Event()
    be = sound.FakeBackend([("IC9700 Speakers", 1, 1, (48000,))], realtime=False)
    out = txaudio.SoundOut("IC9700 Speakers", backend=be)
    assert out.send_audio(pcm([0] * 120))
    stream = be.stream("IC9700 Speakers", True)
    real_close = stream.close
    stream.close = lambda: (release.wait(), real_close())
    t = time.monotonic()
    out.close()
    assert time.monotonic() - t < 0.1
    release.set()
